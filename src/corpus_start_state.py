#!/usr/bin/env python3
"""Deterministic phase ledger for the ``corpus-start`` orchestrator.

One :class:`StartRunState` is the sole authority for a start run's phase,
retries, and completion. State lives at ``<run-root>/.corpus-start/run.json``
and every transition is bound to the run's topic, domain slug, packet SHA-256,
prior phase, receipt SHA-256, and timestamp.

Invariants enforced here (not by the caller):

* Phases advance one step at a time along :data:`PHASES`; skips and backwards
  moves are rejected.
* A repeat transition to the *current* phase with the *same* receipt is a
  byte-level physical no-op; the same phase with a different receipt conflicts.
* ``complete`` is impossible unless every phase in
  :data:`REQUIRED_RECEIPT_PHASES` already carries a receipt.
* Topic, domain, and packet SHA are write-once; a conflicting value fails
  closed rather than overwriting.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
STATE_DIRNAME = ".corpus-start"
STATE_FILENAME = "run.json"

PHASES: tuple[str, ...] = (
    "initialized",
    "bootstrap_packet_validated",
    "domain_bootstrapped",
    "initial_acquisition_complete",
    "gbrain_verified",
    "atlas_verified",
    "scheduler_verified",
    "complete",
)
# Every phase between the base ("initialized") and the terminal ("complete")
# must carry a receipt before completion is permitted.
REQUIRED_RECEIPT_PHASES: tuple[str, ...] = PHASES[1:-1]

_PHASE_INDEX = {phase: index for index, phase in enumerate(PHASES)}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class StartRunError(ValueError):
    """Raised when a start-run ledger operation is malformed or out of order."""


class StartRunConflictError(StartRunError):
    """Raised when a transition contradicts already-committed run identity."""


def derive_slug(value: Any) -> str:
    """Normalize free text into a safe kebab slug or fail closed.

    Whitespace collapses to single hyphens; the result must match a strict
    safe-identifier pattern (no path separators, traversal, or empties).
    """
    if not isinstance(value, str):
        raise StartRunError("slug source must be a string")
    collapsed = re.sub(r"\s+", "-", value.strip().lower())
    if not _SLUG_RE.match(collapsed):
        raise StartRunError(f"unsafe slug: {value!r}")
    return collapsed


def validate_slug(value: Any) -> str:
    """Validate an already-kebab slug without transforming it."""
    if not isinstance(value, str) or not _SLUG_RE.match(value):
        raise StartRunError(f"unsafe slug: {value!r}")
    return value


def receipt_sha256(receipt: Any) -> str:
    """SHA-256 over a canonicalized receipt object."""
    blob = json.dumps(
        receipt, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _state_path(run_root: Path) -> Path:
    return run_root / STATE_DIRNAME / STATE_FILENAME


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n"
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(encoded, encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


class StartRunState:
    """Mutable in-memory view over the on-disk phase ledger."""

    def __init__(self, run_root: Path, data: dict[str, Any]):
        self.run_root = Path(run_root)
        self._data = data

    # ---- construction -------------------------------------------------

    @classmethod
    def begin(
        cls,
        run_root: Path | str,
        *,
        topic: str,
        now: str,
        clean_root: bool = True,
        run_id: str | None = None,
        corpora_base: Path | str | None = None,
    ) -> "StartRunState":
        run_root = Path(run_root)
        if not isinstance(topic, str) or not topic.strip():
            raise StartRunError("topic must be a non-empty string")
        topic = topic.strip()
        derived_id = validate_slug(run_id) if run_id is not None else derive_slug(topic)
        resolved_corpora_base = str(Path(corpora_base).resolve()) if corpora_base is not None else None

        path = _state_path(run_root)
        if path.exists():
            existing = cls.load(run_root)
            if existing.topic_input != topic:
                raise StartRunConflictError(
                    f"run already initialized for topic {existing.topic_input!r}, not {topic!r}"
                )
            if bool(existing._data.get("clean_root", True)) != bool(clean_root):
                raise StartRunConflictError("run already initialized with a different clean_root")
            if existing._data.get("corpora_base") != resolved_corpora_base:
                raise StartRunConflictError("run already initialized with a different corpora_base")
            return existing

        data = {
            "schema_version": SCHEMA_VERSION,
            "run_id": derived_id,
            "topic_input": topic,
            "clean_root": bool(clean_root),
            "corpora_base": resolved_corpora_base,
            "domain": None,
            "packet_sha256": None,
            "phase": "initialized",
            "history": [
                {
                    "phase": "initialized",
                    "prior_phase": None,
                    "receipt_sha256": None,
                    "at": now,
                }
            ],
            "receipts": {},
        }
        _atomic_write_json(path, data)
        return cls(run_root, data)

    @classmethod
    def load(cls, run_root: Path | str) -> "StartRunState":
        run_root = Path(run_root)
        path = _state_path(run_root)
        if not path.exists():
            raise StartRunError(f"no start-run ledger at {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StartRunError(f"unreadable start-run ledger: {exc}") from exc
        if not isinstance(data, dict) or data.get("phase") not in _PHASE_INDEX:
            raise StartRunError("corrupt start-run ledger")
        return cls(run_root, data)

    # ---- read views ---------------------------------------------------

    @property
    def phase(self) -> str:
        return self._data["phase"]

    @property
    def topic_input(self) -> str:
        return self._data["topic_input"]

    @property
    def domain(self) -> str | None:
        return self._data.get("domain")

    @property
    def packet_sha256(self) -> str | None:
        return self._data.get("packet_sha256")

    @property
    def clean_root(self) -> bool:
        return bool(self._data.get("clean_root", True))

    @property
    def corpora_base(self) -> Path | None:
        value = self._data.get("corpora_base")
        return Path(value) if value else None

    @property
    def run_id(self) -> str:
        return self._data["run_id"]

    @property
    def receipts(self) -> dict[str, Any]:
        return dict(self._data.get("receipts", {}))

    def receipt(self, phase: str) -> dict[str, Any] | None:
        return self._data.get("receipts", {}).get(phase)

    def as_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._data))

    # ---- identity binding --------------------------------------------

    def _bind_identity(self, *, domain: str | None, packet_sha256: str | None) -> None:
        if domain is not None:
            domain = validate_slug(domain)
            current = self._data.get("domain")
            if current is None:
                self._data["domain"] = domain
            elif current != domain:
                raise StartRunConflictError(
                    f"run already bound to domain {current!r}, not {domain!r}"
                )
        if packet_sha256 is not None:
            if not isinstance(packet_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", packet_sha256):
                raise StartRunError("packet_sha256 must be a 64-char hex digest")
            current = self._data.get("packet_sha256")
            if current is None:
                self._data["packet_sha256"] = packet_sha256
            elif current != packet_sha256:
                raise StartRunConflictError(
                    "run already bound to a different bootstrap packet"
                )

    # ---- transitions --------------------------------------------------

    def record_transition(
        self,
        target_phase: str,
        *,
        receipt: Any,
        now: str,
        domain: str | None = None,
        packet_sha256: str | None = None,
    ) -> dict[str, Any]:
        if target_phase not in _PHASE_INDEX:
            raise StartRunError(f"unknown phase: {target_phase!r}")
        current = self._data["phase"]
        current_idx = _PHASE_INDEX[current]
        target_idx = _PHASE_INDEX[target_phase]
        digest = receipt_sha256(receipt)

        # Retry of the current phase: must be a physical no-op or a conflict.
        if target_idx == current_idx:
            existing = self._data.get("receipts", {}).get(target_phase)
            if existing is None:
                # Only the base phase ("initialized") has no receipt; a retry of
                # it with any receipt is meaningless.
                raise StartRunError(f"phase {target_phase!r} cannot be re-recorded")
            if existing.get("receipt_sha256") != digest:
                raise StartRunConflictError(
                    f"phase {target_phase!r} already recorded with a different receipt"
                )
            # Identity may still be (re)asserted, but only consistently.
            self._bind_identity(domain=domain, packet_sha256=packet_sha256)
            return existing  # byte-level no-op: no write

        if target_idx != current_idx + 1:
            raise StartRunError(
                f"cannot transition {current!r} -> {target_phase!r}; phases advance one step"
            )

        if target_phase == "complete":
            self.assert_completable()

        # Bind identity before committing so a conflict fails closed.
        self._bind_identity(domain=domain, packet_sha256=packet_sha256)

        entry = {
            "phase": target_phase,
            "prior_phase": current,
            "receipt_sha256": digest,
            "recorded_at": now,
        }
        self._data.setdefault("receipts", {})[target_phase] = entry
        self._data["history"].append(
            {
                "phase": target_phase,
                "prior_phase": current,
                "receipt_sha256": digest,
                "at": now,
            }
        )
        self._data["phase"] = target_phase
        _atomic_write_json(_state_path(self.run_root), self._data)
        return entry

    def assert_completable(self) -> None:
        missing = [p for p in REQUIRED_RECEIPT_PHASES if p not in self._data.get("receipts", {})]
        if missing:
            raise StartRunError(f"cannot complete: missing receipts for {missing}")
