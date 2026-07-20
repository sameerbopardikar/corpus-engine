#!/usr/bin/env python3
"""The ``corpus-start`` orchestrator: one resumable state machine that turns
``start a corpus on <topic>`` into a verified corpus + Atlas.

This module wires the *existing* generic engine components — domain spec,
``bootstrap_domain``, ``run_domain_cycle`` (field map), acquisition, GBrain
sync, Atlas instantiation, and scheduler attachment — behind the
:mod:`corpus_start_state` phase ledger so a run can never report success until
every required receipt exists.

The acquisition, GBrain, Atlas, and scheduler steps are *injected boundaries*
(:class:`StartBoundaries`) so the orchestration is fully deterministic and
network/service-free under test. :func:`default_boundaries` supplies the real,
live-service adapters for production runs (Tasks 7/8), constructed lazily.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from corpus_bootstrap_packet import BootstrapPacketError, compile_domain_inputs, validate_bootstrap_packet
from corpus_domain_spec import DomainSpecError, bootstrap_domain, load_domain_spec
from corpus_domain_runner import run_domain_cycle
from corpus_start_state import (
    REQUIRED_RECEIPT_PHASES,
    StartRunError,
    StartRunState,
    receipt_sha256,
)

STATE_DIRNAME = ".corpus-start"


class StartOrchestrationError(RuntimeError):
    """Raised when a start-run step is malformed, unsafe, or cannot complete."""


@dataclass(frozen=True)
class PhaseContext:
    """Everything a boundary needs, resolved from the run's domain spec."""

    run_root: Path
    domain: str
    title: str
    topic_input: str
    clean_root: bool
    config_dir: Path
    spec_path: Path
    seed_path: Path
    corpora_base: Path
    corpus_root: Path
    state_root: Path
    output_root: Path
    archive_root: Path
    now: str


@dataclass(frozen=True)
class StartBoundaries:
    """Injected side-effecting steps. Each returns a receipt dict."""

    acquire: Callable[[PhaseContext], dict[str, Any]]
    verify_gbrain: Callable[[PhaseContext], dict[str, Any]]
    verify_atlas: Callable[[PhaseContext], dict[str, Any]]
    verify_scheduler: Callable[[PhaseContext], dict[str, Any]]


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n"
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(encoded, encoding="utf-8")
    temp.replace(path)


class CorpusStartRun:
    """A single start run bound to one run root and one set of boundaries."""

    def __init__(self, state: StartRunState, *, boundaries: StartBoundaries):
        self._state = state
        self._boundaries = boundaries

    # ---- construction -------------------------------------------------

    @classmethod
    def begin(
        cls,
        run_root: Path | str,
        *,
        topic: str,
        now: str,
        clean_root: bool = True,
        corpora_base: Path | str | None = None,
        boundaries: StartBoundaries | None = None,
    ) -> "CorpusStartRun":
        state = StartRunState.begin(
            run_root, topic=topic, now=now, clean_root=clean_root,
            corpora_base=corpora_base,
        )
        return cls(state, boundaries=boundaries or default_boundaries())

    @classmethod
    def load(cls, run_root: Path | str, *, boundaries: StartBoundaries | None = None) -> "CorpusStartRun":
        state = StartRunState.load(run_root)
        return cls(state, boundaries=boundaries or default_boundaries())

    # ---- paths --------------------------------------------------------

    @property
    def run_root(self) -> Path:
        return self._state.run_root

    def _receipts_dir(self) -> Path:
        return self.run_root / STATE_DIRNAME / "receipts"

    def _receipt_path(self, phase: str) -> Path:
        return self._receipts_dir() / f"{phase}.json"

    def _write_receipt(self, phase: str, receipt: dict[str, Any]) -> None:
        _atomic_write_json(self._receipt_path(phase), receipt)

    def _read_receipt(self, phase: str) -> dict[str, Any] | None:
        path = self._receipt_path(phase)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _config_dir(self) -> Path:
        return self.run_root / "config" / "domains"

    def _seed_path(self, domain: str) -> Path:
        return self.run_root / "config" / "seeds" / f"{domain}-seed-candidates.json"

    def _spec_path(self, domain: str) -> Path:
        return self._config_dir() / f"{domain}.json"

    # ---- verbs --------------------------------------------------------

    def next_action(self) -> str:
        phase = self._state.phase
        if phase == "initialized":
            return "produce_bootstrap_packet"
        if phase == "complete":
            return "done"
        return "continue"

    def apply_packet(self, packet_path: Path | str, *, now: str) -> dict[str, Any]:
        packet_path = Path(packet_path)
        try:
            raw = json.loads(packet_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StartOrchestrationError(f"unreadable packet {packet_path}: {exc}") from exc
        try:
            packet = validate_bootstrap_packet(raw, clean=self._state.clean_root)
            inputs = compile_domain_inputs(packet, now=now, clean=self._state.clean_root)
        except BootstrapPacketError as exc:
            raise StartOrchestrationError(f"invalid bootstrap packet: {exc}") from exc

        domain = packet["domain"]
        packet_sha = receipt_sha256(packet)
        spec_path = self._spec_path(domain)
        seed_path = self._seed_path(domain)
        _atomic_write_json(spec_path, inputs["domain_spec"])
        _atomic_write_json(seed_path, inputs["seed_bundle"])
        _atomic_write_json(self.run_root / STATE_DIRNAME / "packet.json", packet)

        # Fail closed if the compiled spec does not load cleanly.
        try:
            load_domain_spec(spec_path)
        except DomainSpecError as exc:
            raise StartOrchestrationError(f"compiled domain spec is invalid: {exc}") from exc

        receipt = {
            "packet_sha256": packet_sha,
            "domain": domain,
            "clean_root": self._state.clean_root,
            "spec_path": str(spec_path.relative_to(self.run_root)),
            "seed_path": str(seed_path.relative_to(self.run_root)),
        }
        self._write_receipt("bootstrap_packet_validated", receipt)
        self._state.record_transition(
            "bootstrap_packet_validated", receipt=receipt, now=now,
            domain=domain, packet_sha256=packet_sha,
        )
        return receipt

    def continue_run(self, *, now: str) -> dict[str, Any]:
        if self._state.phase == "initialized":
            raise StartOrchestrationError("apply a bootstrap packet before continuing")
        ctx = self._context(now=now)
        steps: list[tuple[str, Callable[[PhaseContext], dict[str, Any]]]] = [
            ("domain_bootstrapped", self._do_bootstrap),
            ("initial_acquisition_complete", self._do_acquire),
            ("gbrain_verified", self._do_gbrain),
            ("atlas_verified", self._do_atlas),
            ("scheduler_verified", self._do_scheduler),
            ("complete", self._do_complete),
        ]
        for phase, fn in steps:
            if self._phase_reached(phase):
                continue  # resumable, physical no-op
            receipt = fn(ctx)
            self._write_receipt(phase, receipt)
            self._state.record_transition(
                phase, receipt=receipt, now=now,
                domain=self._state.domain, packet_sha256=self._state.packet_sha256,
            )
        return self.status()

    # ---- phase implementations ---------------------------------------

    def _context(self, *, now: str) -> PhaseContext:
        domain = self._state.domain
        if not domain:
            raise StartOrchestrationError("run has no bound domain; apply a packet first")
        spec = load_domain_spec(self._spec_path(domain))
        corpora_base = self._state.corpora_base or (self.run_root / "corpora")
        corpus_root = corpora_base / spec.roots["corpus_root"]
        state_root = corpora_base / spec.roots["state_root"]
        output_root = corpora_base / spec.roots["output_root"]
        archive_root = corpora_base / spec.roots["archive_root"]
        return PhaseContext(
            run_root=self.run_root,
            domain=domain,
            title=spec.title,
            topic_input=self._state.topic_input,
            clean_root=self._state.clean_root,
            config_dir=self._config_dir(),
            spec_path=self._spec_path(domain),
            seed_path=self._seed_path(domain),
            corpora_base=corpora_base,
            corpus_root=corpus_root,
            state_root=state_root,
            output_root=output_root,
            archive_root=archive_root,
            now=now,
        )

    def _do_bootstrap(self, ctx: PhaseContext) -> dict[str, Any]:
        spec = load_domain_spec(ctx.spec_path)
        bootstrap_receipt = bootstrap_domain(spec, ctx.corpora_base, now=ctx.now)
        cycle = run_domain_cycle(
            spec=spec, seed_path=ctx.seed_path,
            corpus_root=ctx.corpus_root, state_root=ctx.state_root,
            output_root=ctx.output_root, cycle_id=f"start-{ctx.domain}", now=ctx.now,
        )
        field_map = cycle["project"]["field_map"]
        axis_keys = {axis.key for axis in spec.ontology_axes}
        field_map_ready = bool(field_map) and set(field_map) == axis_keys
        if not field_map_ready:
            raise StartOrchestrationError("domain cycle did not produce a complete field map")
        return {
            "bootstrap": bootstrap_receipt,
            "field_map": field_map,
            "field_map_ready": True,
            "next_gap": cycle.get("next_gap"),
            "cycle_receipt_sha256": cycle.get("receipt_sha256"),
        }

    def _do_acquire(self, ctx: PhaseContext) -> dict[str, Any]:
        receipt = self._boundaries.acquire(ctx)
        sources_discovered = int(receipt.get("sources_discovered", 0))
        source_families = receipt.get("source_families_discovered")
        if sources_discovered < 5:
            raise StartOrchestrationError(
                "clean proof requires at least five discovered candidates"
            )
        if not isinstance(source_families, list) or len(set(source_families)) < 3:
            raise StartOrchestrationError(
                "clean proof requires at least three discovered source families"
            )
        sources_acquired = int(receipt.get("sources_acquired", 0))
        if sources_acquired < 1:
            raise StartOrchestrationError(
                "acquisition produced zero sources; a corpus with no acquired evidence is not complete"
            )
        owned = receipt.get("owned_sources")
        if not isinstance(owned, list) or len(owned) < sources_acquired:
            raise StartOrchestrationError("acquisition receipt must own every acquired source path")
        return {
            "sources_discovered": sources_discovered,
            "source_families_discovered": sorted(set(str(family) for family in source_families)),
            "sources_acquired": sources_acquired,
            "owned_sources": [str(p) for p in owned],
        }

    def _do_gbrain(self, ctx: PhaseContext) -> dict[str, Any]:
        receipt = self._boundaries.verify_gbrain(ctx)
        if not receipt.get("verified"):
            raise StartOrchestrationError("GBrain adapter did not verify corpus retrieval")
        return dict(receipt)

    def _do_atlas(self, ctx: PhaseContext) -> dict[str, Any]:
        receipt = self._boundaries.verify_atlas(ctx)
        if not receipt.get("atlas_ready"):
            raise StartOrchestrationError("Atlas is not health/ready")
        return dict(receipt)

    def _do_scheduler(self, ctx: PhaseContext) -> dict[str, Any]:
        receipt = self._boundaries.verify_scheduler(ctx)
        owner_count = int(receipt.get("owner_count", 0))
        if owner_count != 1:
            raise StartOrchestrationError(
                f"exactly one scheduler owner is required, found {owner_count}"
            )
        return {"owner_count": owner_count, **{k: v for k, v in receipt.items() if k != "owner_count"}}

    def _do_complete(self, ctx: PhaseContext) -> dict[str, Any]:
        self._state.assert_completable()
        # A clean proof must prove that every source page was admitted by the
        # orchestrator.  Resume-live runs intentionally start with authored
        # pages, so the same check would reject the corpus they are resuming.
        if ctx.clean_root and self._detect_manual_substitution(ctx.corpus_root):
            raise StartOrchestrationError(
                "manual source pages exist outside the orchestrator receipt set"
            )
        return {"completed_at": ctx.now, "manual_seed_substitution": False}

    # ---- helpers ------------------------------------------------------

    def _phase_reached(self, phase: str) -> bool:
        from corpus_start_state import _PHASE_INDEX  # local import to avoid re-export

        return _PHASE_INDEX[self._state.phase] >= _PHASE_INDEX[phase]

    def _owned_sources(self) -> set[str]:
        receipt = self._read_receipt("initial_acquisition_complete")
        if not receipt:
            return set()
        return set(receipt.get("owned_sources", []))

    def _detect_manual_substitution(self, corpus_root: Path) -> bool:
        sources_dir = corpus_root / "sources"
        if not sources_dir.exists():
            return False
        owned = self._owned_sources()
        for path in sorted(sources_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = str(path.relative_to(corpus_root))
            if rel not in owned:
                return True
        return False

    # ---- status -------------------------------------------------------

    def status(self) -> dict[str, Any]:
        bootstrap = self._read_receipt("domain_bootstrapped") or {}
        acquire = self._read_receipt("initial_acquisition_complete") or {}
        gbrain = self._read_receipt("gbrain_verified") or {}
        atlas = self._read_receipt("atlas_verified") or {}
        scheduler = self._read_receipt("scheduler_verified") or {}

        manual = False
        domain = self._state.domain
        if domain:
            spec = load_domain_spec(self._spec_path(domain))
            corpora_base = self._state.corpora_base or (self.run_root / "corpora")
            corpus_root = corpora_base / spec.roots["corpus_root"]
            manual = self._detect_manual_substitution(corpus_root) if self._state.clean_root else False

        return {
            "status": self._state.phase,
            "topic_input": self._state.topic_input,
            "clean_root": self._state.clean_root,
            "field_map_ready": bool(bootstrap.get("field_map_ready", False)),
            "sources_discovered": int(acquire.get("sources_discovered", 0)),
            "source_family_count": len(set(acquire.get("source_families_discovered", []))),
            "sources_acquired": int(acquire.get("sources_acquired", 0)),
            "gbrain_sync_verified": bool(gbrain.get("verified", False)),
            "atlas_ready": bool(atlas.get("atlas_ready", False)),
            "scheduler_owner_count": int(scheduler.get("owner_count", 0)),
            "manual_seed_substitution": manual,
        }


# --------------------------------------------------------------------------
# Default (live) boundaries — constructed lazily, exercised only in real runs.
# --------------------------------------------------------------------------

def default_boundaries() -> StartBoundaries:
    """Return the real runtime adapters for acquisition, GBrain, Atlas, and scheduler."""
    # Lazy import keeps the deterministic core side-effect free in unit tests
    # and avoids the live module's intentional import of PhaseContext.
    from corpus_start_live import make_live_boundaries
    return make_live_boundaries()
