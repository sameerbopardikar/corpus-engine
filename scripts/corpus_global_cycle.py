#!/usr/bin/env python3
"""One global, manifest-driven corpus cycle across all enabled domains.

The single scheduler enumerates every checked-in domain spec, loads each
domain's seed candidates, and ranks them in ONE plan reserved against ONE
shared budget authority. No domain is special-cased here — adding a domain is
adding a spec file. Seed candidates enter as rights-unclear (unverified): the
plan ranks them, but nothing is auto-acquired until the discovery pipeline
independently classifies rights.

Two modes, one scheduler and one global budget in both:

* **Planning dry run** (default CLI, or ``CORPUS_ACQUISITION_EXECUTE=0``): ranks
  candidates and performs no network acquisition and no corpus mutation.
* **Bounded execute** (``--execute --corpora-root <root>``): additionally
  discovers unseeded candidates, resolves rights fail-closed, and acquires only
  the budget-selected rights-clear content, admitting eval-passed evidence into
  per-domain isolated roots under ``<corpora-root>/<domain>``. All network I/O
  is stdlib-only (no ``requests`` dependency) and SSRF-guarded.

It replaces the Agentic-only body of the legacy cycle script while preserving
the one-scheduler / one-global-budget invariant.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
# The script directory is sys.path[0] when this file is executed directly and
# contains this entrypoint under the same basename as src/corpus_global_cycle.py.
# Force src to the front even when PYTHONPATH already contains it, otherwise
# Python imports this partially initialized script as its own library module.
while str(_SRC) in sys.path:
    sys.path.remove(str(_SRC))
sys.path.insert(0, str(_SRC))

import corpus_priority
from corpus_acquisition_executor import (
    DEFAULT_MAX_BYTES,
    AcquisitionCandidate,
    AcquisitionExecutor,
)
from corpus_discovery import score_observation
from corpus_live_fetch import live_fetch
from corpus_domain_spec import load_domain_spec
from corpus_global_cycle import (
    DomainAcquisitionInputs,
    run_global_acquisition_cycle,
    run_global_cycle,
)
from corpus_priority import CandidateTask, PriorityPolicy

# Build candidate records with the exact class the budget ledger validates and
# replays against. Under the test suite a sibling reloads corpus_engine_models
# into sys.modules, so a direct import here could bind a different class object.
CandidateRecord = corpus_priority.CandidateRecord
from corpus_rights_resolver import resolve_rights
from corpus_scholarly_discovery import discover_europepmc, discover_scholarly
from corpus_seed_loader import load_candidate_seed

DEFAULT_CONFIG_DIR = _REPO_ROOT / "config" / "domains"
# One shared budget authority for the whole engine — never per-domain.
DEFAULT_BUDGET_PATH = Path("/root/exports/thinker-corpora/_engine/budget.json")


def default_reservation_id(
    config_dir: Path, day: str, *, planner_root: Path | None = None
) -> str:
    """Bind a daily reservation to exact planner, domain-spec, and seed bytes.

    The planner binding is required when immutable engine releases change during
    a UTC day. Without it, a corrected release can reuse the prior release's
    reservation ID with a different command preimage and fail every retry.
    """
    digest = hashlib.sha256()
    root = Path(planner_root) if planner_root is not None else _REPO_ROOT
    planner_paths = [root / "scripts" / "corpus_global_cycle.py"]
    planner_paths.extend(sorted((root / "src").glob("*.py")))
    for planner_path in planner_paths:
        if not planner_path.is_file():
            continue
        digest.update(str(planner_path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(planner_path.read_bytes())
    for spec_path in sorted(Path(config_dir).glob("*.json")):
        spec_bytes = spec_path.read_bytes()
        digest.update(spec_path.name.encode())
        digest.update(b"\0")
        digest.update(spec_bytes)
        raw = json.loads(spec_bytes)
        seed_ref = raw.get("seed_ref")
        if not isinstance(seed_ref, str) or not seed_ref:
            continue
        seed_path = _REPO_ROOT / seed_ref
        digest.update(seed_ref.encode())
        digest.update(b"\0")
        digest.update(seed_path.read_bytes())
    return f"global-{day}-{digest.hexdigest()[:16]}"


def enumerate_domain_specs(config_dir: Path):
    """Yield (spec, spec_path) for every checked-in domain spec, sorted."""
    for spec_path in sorted(Path(config_dir).glob("*.json")):
        yield load_domain_spec(spec_path), spec_path


def _domain_loader(spec, seed_path: Path):
    def _load() -> list[CandidateTask]:
        bundle = load_candidate_seed(seed_path)
        if bundle.domain != spec.domain:
            raise ValueError(f"seed domain {bundle.domain!r} != spec domain {spec.domain!r}")
        tasks: list[CandidateTask] = []
        for observation in bundle.observations(source_ref=spec.seed_ref):
            scores, rationale = score_observation(observation)
            record = CandidateRecord.from_observation(
                observation, scores, rationale=rationale, rights_state="rights_unclear"
            )
            tasks.append(CandidateTask(record, material_delta=True))
        return tasks

    return _load


def build_domain_loaders(config_dir: Path):
    loaders = []
    for spec, spec_path in enumerate_domain_specs(config_dir):
        seed_path = _REPO_ROOT / spec.seed_ref
        loaders.append((spec.domain, _domain_loader(spec, seed_path)))
    return loaders


def run(*, config_dir: Path, budget_ledger_path: Path, reservation_id: str, now: datetime):
    loaders = build_domain_loaders(config_dir)
    return run_global_cycle(
        reservation_id=reservation_id,
        domain_loaders=loaders,
        policy=PriorityPolicy.default(),
        budget_ledger_path=budget_ledger_path,
        now=now,
    )


def _source_revision(metadata: dict, content_locator, canonical_locator) -> str:
    """A stable digest of the source's revision identity.

    Bound to the source's own version markers (OpenAlex ``updated_date`` and
    ``doi``) plus the concrete content locator — never the cycle reservation. An
    unchanged source yields the same revision on any day; a genuinely updated
    source (new updated_date/DOI/locator) yields a new one.
    """
    material = "|".join(
        str(part or "")
        for part in (
            metadata.get("doi"),
            metadata.get("updated_date"),
            content_locator or canonical_locator,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _acquisition_idempotency_key(domain: str, candidate_id: str, source_revision: str) -> str:
    """Idempotency bound to domain + stable candidate identity + source revision.

    Deliberately independent of the daily reservation id: re-running unchanged
    discovery on a later day maps to the same key, so the executor short-circuits
    on the stored receipt and never refetches or rewrites canonical bytes.
    """
    return f"acq:{domain}:{candidate_id}:{source_revision}"


def _acquisition_domain_loader(spec, *, discover, reservation_id: str):
    """Build one domain's acquisition inputs from live scholarly discovery.

    Discovery yields candidates that are NOT in checked-in seeds; each gets its
    rights resolved (fail-closed) and is bound to an executable candidate keyed
    by its own stable source identity (not the cycle reservation).
    """
    def load() -> DomainAcquisitionInputs:
        topics = list(spec.axis_topics)
        discovered = discover(spec.domain, topics)
        tasks: list[CandidateTask] = []
        candidates: dict[str, AcquisitionCandidate] = {}
        for scholarly in discovered:
            observation = scholarly.observation
            scores, rationale = score_observation(observation)
            resolution = resolve_rights(scholarly.rights_evidence)
            record = CandidateRecord.from_observation(
                observation, scores, rationale=rationale,
                rights_state=resolution.rights_state.value,
            )
            candidate_id = record.candidate_id
            if candidate_id in candidates:
                continue
            revision = _source_revision(
                scholarly.metadata, scholarly.content_locator, observation.canonical_url
            )
            metadata = dict(scholarly.metadata)
            metadata["source_revision"] = revision
            tasks.append(CandidateTask(record, material_delta=True))
            candidates[candidate_id] = AcquisitionCandidate(
                candidate_id=candidate_id, domain=spec.domain,
                canonical_locator=observation.canonical_url,
                content_locator=scholarly.content_locator,
                evidence_lane=observation.evidence_lane,
                source_family="scholarly-openalex", title=scholarly.title,
                topics=observation.topics, rights_evidence=scholarly.rights_evidence,
                metadata=metadata,
                idempotency_key=_acquisition_idempotency_key(spec.domain, candidate_id, revision),
            )
        return DomainAcquisitionInputs(domain=spec.domain, tasks=tasks, acquisition_candidates=candidates)

    return (spec.domain, load)


def build_acquisition_loaders(config_dir: Path, *, discover, reservation_id: str):
    return [
        _acquisition_domain_loader(spec, discover=discover, reservation_id=reservation_id)
        for spec, _ in enumerate_domain_specs(config_dir)
    ]


def _atomic_write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(encoded, encoding="utf-8")
    temp.replace(path)


def _domain_executor_factory(corpora_root: Path, *, http_fetch, fetched_at):
    """One isolated executor per domain, rooted under ``<corpora_root>/<domain>``.

    Each domain gets its own archive/staging/receipts/quarantine under its own
    ``acquisition`` root; canonical evidence pages are admitted into
    ``<corpora_root>/<domain>/sources/acquired`` (the executor derives the
    per-domain namespace from the candidate domain). Domain names are validated
    against traversal before any root is derived.
    """
    corpora_root = Path(corpora_root)
    root_resolved = corpora_root.resolve()

    def make_executor(domain: str) -> AcquisitionExecutor:
        domain_root = corpora_root / domain
        resolved = domain_root.resolve()
        if resolved != root_resolved and root_resolved not in resolved.parents:
            raise ValueError(f"domain {domain!r} escapes corpora_root {corpora_root}")
        acquisition = domain_root / "acquisition"
        return AcquisitionExecutor(
            raw_root=acquisition / "archive", staging_root=acquisition / "staging",
            receipts_root=acquisition / "receipts", quarantine_root=acquisition / "quarantine",
            canonical_root=corpora_root, http_fetch=http_fetch, now=fetched_at,
        )

    return make_executor


def run_execute(
    *,
    config_dir: Path,
    budget_ledger_path: Path,
    reservation_id: str,
    now: datetime,
    corpora_root: Path,
    discover,
    http_fetch,
    fetched_at,
    corpus_revision: str,
    cycle_time_seconds: float = 0.0,
):
    """Bounded per-domain execution mode for the deployed daily wrapper.

    Plans across all domains under ONE shared budget (single scheduler), then
    acquires only the budget-selected rights-clear content, writing each domain
    into its own isolated roots under ``<corpora_root>/<domain>``. A redacted
    projection is emitted per domain to ``<corpora_root>/<domain>/acquisition/
    latest.json`` and returned keyed by domain. All network I/O is injected so
    this is testable without hitting the network.
    """
    corpora_root = Path(corpora_root)
    factory = _domain_executor_factory(corpora_root, http_fetch=http_fetch, fetched_at=fetched_at)
    loaders = build_acquisition_loaders(Path(config_dir), discover=discover, reservation_id=reservation_id)
    result = run_global_acquisition_cycle(
        reservation_id=reservation_id, domain_loaders=loaders,
        policy=PriorityPolicy.default(), budget_ledger_path=budget_ledger_path,
        now=now, executor_factory=factory, corpus_revision=corpus_revision,
        cycle_time_seconds=cycle_time_seconds,
    )
    for domain, projection in result.get("projections", {}).items():
        _atomic_write_json(corpora_root / domain / "acquisition" / "latest.json", projection)
    return result


def _default_content_fetch(url, *, timeout, max_bytes=DEFAULT_MAX_BYTES):
    # Stdlib-only, SSRF-pinned, streaming transport (no `requests` dependency).
    return live_fetch(url, timeout=timeout, max_bytes=max_bytes)


class _DiscoveryResponse:
    """Minimal response shim for discover_scholarly (needs .content and .url)."""

    def __init__(self, content: bytes, url: str):
        self.content = content
        self.url = url


def _default_discover(fetched_at, *, max_bytes=DEFAULT_MAX_BYTES):
    def http_get(url, timeout):
        fetched = live_fetch(url, timeout=timeout, max_bytes=max_bytes)
        return _DiscoveryResponse(content=fetched.body, url=fetched.final_url)

    def discover(domain, topics):
        openalex_candidates = []
        try:
            openalex_candidates = discover_scholarly(
                domain=domain, topics=topics, http_get=http_get, fetched_at=fetched_at
            )
            # OpenAlex can return a healthy-looking candidate set whose only
            # rights-cleared content locators are PDFs. The executor deliberately
            # gates PDFs until a packaged extractor exists, so that set cannot
            # satisfy the positive-acquisition contract. Continue through the
            # independent Europe PMC/NCBI OA lane unless OpenAlex already yielded
            # directly normalizable non-PDF content.
            if any(
                candidate.content_locator
                and not urlsplit(candidate.content_locator).path.lower().endswith(".pdf")
                for candidate in openalex_candidates
            ):
                return openalex_candidates
        except Exception:
            # Independent public fallback: Europe PMC search plus NCBI's OA
            # license service. This keeps a topic start moving when OpenAlex is
            # throttled without weakening the rights gate.
            openalex_candidates = []

        try:
            fallback_candidates = discover_europepmc(
                domain=domain, topics=topics, http_get=http_get, fetched_at=fetched_at
            )
        except Exception:
            # Preserve truthful OpenAlex metadata/PDF candidates when the
            # independent fallback itself is unavailable.
            return openalex_candidates

        seen = {candidate.observation.canonical_url for candidate in fallback_candidates}
        return fallback_candidates + [
            candidate for candidate in openalex_candidates
            if candidate.observation.canonical_url not in seen
        ]

    return discover


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one global corpus cycle across all enabled domains.")
    parser.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR))
    parser.add_argument("--budget-path", default=str(DEFAULT_BUDGET_PATH))
    parser.add_argument("--reservation-id")
    parser.add_argument(
        "--execute", action="store_true",
        help="Bounded execution mode: discover, resolve rights, and acquire budget-selected content.",
    )
    parser.add_argument(
        "--corpora-root",
        help="Corpora root; each domain gets isolated roots under <corpora-root>/<domain> (execute mode only).",
    )
    parser.add_argument("--corpus-revision", default="unknown")
    args = parser.parse_args(argv)
    clock_now = datetime.now(timezone.utc)
    # Reservation requests and discovered candidate timestamps must share one
    # replay-stable cycle clock. Wall-clock seconds/microseconds would make an
    # otherwise identical scheduler retry conflict before idempotency can reuse
    # the acquisition receipt.
    now = clock_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if args.reservation_id:
        reservation_id = args.reservation_id
    else:
        reservation_id = default_reservation_id(
            Path(args.config_dir), now.date().isoformat()
        )

    if args.execute:
        if not args.corpora_root:
            parser.error("--execute requires --corpora-root")
        fetched_at = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        result = run_execute(
            config_dir=Path(args.config_dir),
            budget_ledger_path=Path(args.budget_path),
            reservation_id=reservation_id,
            now=now,
            corpora_root=Path(args.corpora_root),
            discover=_default_discover(
                lambda: now.isoformat(timespec="seconds").replace("+00:00", "Z")
            ),
            http_fetch=_default_content_fetch,
            fetched_at=fetched_at,
            corpus_revision=args.corpus_revision,
        )
        summary = {
            "status": result["status"],
            "reservation_id": reservation_id,
            "domains_planned": result["domains_planned"],
            "failures": result["failures"],
            "report": result["report"],
        }
        print(json.dumps(summary, indent=2))
        return 0

    result = run(
        config_dir=Path(args.config_dir),
        budget_ledger_path=Path(args.budget_path),
        reservation_id=reservation_id,
        now=now,
    )
    summary = {
        "status": result["status"],
        "reservation_id": reservation_id,
        "domains_planned": result["domains_planned"],
        "failures": result["failures"],
        "selected_candidate_ids": result["selected_candidate_ids"],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
