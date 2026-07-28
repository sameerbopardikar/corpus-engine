#!/usr/bin/env python3
"""Replay-safe deterministic query rotation for the X corpus radar.

The LLM/tool layer executes X search, but it does not own query selection or
cursor advancement. A query is leased without advancement; only a durable,
content-addressed result receipt advances the index. Failed searches retain the
same index for the next cycle.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
MAX_HISTORY = 256


class XRadarStateError(ValueError):
    """The X-radar command or durable state is invalid or conflicting."""


def _text(name: str, value: Any, maximum: int = 20_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise XRadarStateError(f"{name} must be non-blank text")
    value = value.strip()
    if len(value) > maximum:
        raise XRadarStateError(f"{name} exceeds {maximum} characters")
    return value


def _timestamp(name: str, value: Any) -> str:
    value = _text(name, value, 64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise XRadarStateError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise XRadarStateError(f"{name} must include a timezone")
    return value


def _sha256(name: str, value: Any) -> str:
    value = _text(name, value, 64)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise XRadarStateError(f"{name} must be a lowercase SHA-256 digest")
    return value


def normalize_queries(queries: Iterable[str]) -> tuple[str, ...]:
    if isinstance(queries, (str, bytes)):
        raise XRadarStateError("queries must be an iterable of query strings")
    normalized = tuple(_text("query", item) for item in queries)
    if not 1 <= len(normalized) <= 100:
        raise XRadarStateError("queries must contain 1 through 100 items")
    if len(set(normalized)) != len(normalized):
        raise XRadarStateError("queries must be unique")
    return normalized


def queries_sha256(queries: Iterable[str]) -> str:
    normalized = normalize_queries(queries)
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class XRadarLease:
    cycle_id: str
    lease_id: str
    index: int
    query: str
    queries_sha256: str
    leased_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "cycle_id", _text("cycle_id", self.cycle_id, 512))
        lease_id = _text("lease_id", self.lease_id, 128)
        if not lease_id.startswith("lease_"):
            raise XRadarStateError("lease_id must begin with lease_")
        object.__setattr__(self, "lease_id", lease_id)
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise XRadarStateError("index must be a non-negative integer")
        object.__setattr__(self, "query", _text("query", self.query))
        object.__setattr__(self, "queries_sha256", _sha256("queries_sha256", self.queries_sha256))
        object.__setattr__(self, "leased_at", _timestamp("leased_at", self.leased_at))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "XRadarLease":
        return cls(**value)


def _lease_id(query_digest: str, cycle_id: str, index: int) -> str:
    material = json.dumps([query_digest, cycle_id, index], separators=(",", ":")).encode("utf-8")
    return "lease_" + hashlib.sha256(material).hexdigest()[:24]


def _empty_state(query_digest: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "queries_sha256": query_digest,
        "next_index": 0,
        "active_lease": None,
        "history": [],
    }


def _read_state(path: Path, query_digest: str, query_count: int) -> dict[str, Any]:
    if not path.exists():
        return _empty_state(query_digest)
    try:
        state = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(f"non-finite value: {item}")),
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise XRadarStateError(f"invalid X-radar state: {exc}") from exc
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise XRadarStateError("invalid X-radar state schema")
    if state.get("queries_sha256") != query_digest:
        raise XRadarStateError("query configuration changed; explicit migration is required")
    index = state.get("next_index")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < query_count:
        raise XRadarStateError("invalid next_index")
    if state.get("active_lease") is not None and not isinstance(state["active_lease"], dict):
        raise XRadarStateError("invalid active_lease")
    if not isinstance(state.get("history"), list):
        raise XRadarStateError("invalid history")
    for entry in state["history"]:
        if not isinstance(entry, dict) or entry.get("status") not in {"leased", "succeeded", "failed"}:
            raise XRadarStateError("invalid history entry")
        XRadarLease.from_dict(entry["lease"])
    return state


def _atomic_write(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "r+")


def _find_entry(state: dict[str, Any], lease_id: str) -> dict[str, Any] | None:
    for entry in state["history"]:
        if entry["lease"].get("lease_id") == lease_id:
            return entry
    return None


def _require_matching_lease(state: dict[str, Any], lease: XRadarLease) -> dict[str, Any]:
    entry = _find_entry(state, lease.lease_id)
    if entry is None or entry.get("lease") != lease.to_dict():
        raise XRadarStateError("lease does not match durable state")
    return entry


def lease_next(
    queries: Iterable[str],
    state_path: Path,
    *,
    cycle_id: str,
    leased_at: str,
) -> XRadarLease:
    normalized = normalize_queries(queries)
    digest = queries_sha256(normalized)
    cycle_id = _text("cycle_id", cycle_id, 512)
    leased_at = _timestamp("leased_at", leased_at)
    state_path = Path(state_path)
    with _locked(state_path) as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        state = _read_state(state_path, digest, len(normalized))
        active = state["active_lease"]
        if active is not None:
            lease = XRadarLease.from_dict(active)
            if lease.cycle_id == cycle_id:
                return lease
            raise XRadarStateError(f"active lease belongs to another cycle: {lease.cycle_id}")
        for entry in state["history"]:
            prior = XRadarLease.from_dict(entry["lease"])
            if prior.cycle_id == cycle_id:
                return prior
        index = state["next_index"]
        lease = XRadarLease(
            cycle_id=cycle_id,
            lease_id=_lease_id(digest, cycle_id, index),
            index=index,
            query=normalized[index],
            queries_sha256=digest,
            leased_at=leased_at,
        )
        state["active_lease"] = lease.to_dict()
        state["history"].append({"lease": lease.to_dict(), "status": "leased"})
        state["history"] = state["history"][-MAX_HISTORY:]
        _atomic_write(state_path, state)
        return lease


def commit_success(
    queries: Iterable[str],
    state_path: Path,
    *,
    lease: XRadarLease,
    result_sha256: str,
    committed_at: str,
) -> dict[str, Any]:
    normalized = normalize_queries(queries)
    digest = queries_sha256(normalized)
    result_sha256 = _sha256("result_sha256", result_sha256)
    committed_at = _timestamp("committed_at", committed_at)
    state_path = Path(state_path)
    with _locked(state_path) as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        state = _read_state(state_path, digest, len(normalized))
        entry = _require_matching_lease(state, lease)
        if entry["status"] == "succeeded":
            if entry.get("result_sha256") != result_sha256:
                raise XRadarStateError("conflicting result for completed lease")
            return state
        if entry["status"] == "failed":
            raise XRadarStateError("failed lease cannot be converted to success; use a new cycle")
        if state.get("active_lease") != lease.to_dict():
            raise XRadarStateError("lease does not match active lease")
        entry.update(
            {
                "status": "succeeded",
                "result_sha256": result_sha256,
                "committed_at": committed_at,
            }
        )
        state["next_index"] = (lease.index + 1) % len(normalized)
        state["active_lease"] = None
        _atomic_write(state_path, state)
        return state


def commit_failure(
    queries: Iterable[str],
    state_path: Path,
    *,
    lease: XRadarLease,
    error: str,
    committed_at: str,
) -> dict[str, Any]:
    normalized = normalize_queries(queries)
    digest = queries_sha256(normalized)
    error = _text("error", error, 4000)
    committed_at = _timestamp("committed_at", committed_at)
    state_path = Path(state_path)
    with _locked(state_path) as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        state = _read_state(state_path, digest, len(normalized))
        entry = _require_matching_lease(state, lease)
        if entry["status"] == "failed":
            if entry.get("error") != error:
                raise XRadarStateError("conflicting error for failed lease")
            return state
        if entry["status"] == "succeeded":
            raise XRadarStateError("succeeded lease cannot be failed")
        if state.get("active_lease") != lease.to_dict():
            raise XRadarStateError("lease does not match active lease")
        entry.update({"status": "failed", "error": error, "committed_at": committed_at})
        state["active_lease"] = None
        _atomic_write(state_path, state)
        return state
