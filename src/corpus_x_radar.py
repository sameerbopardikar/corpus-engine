#!/usr/bin/env python3
"""Replay-safe deterministic query rotation for an X discovery radar.

The state machine owns query selection and cursor advancement. A caller leases
one query, persists a lease-bound JSON result receipt, then commits success or
failure. Leasing never advances. Only a verified success receipt advances once.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA_VERSION = 1
ALLOWED_RESULTS = {"material_signal_ingested", "no_material_delta"}


class XRadarStateError(RuntimeError):
    """The deterministic rotation state cannot safely transition."""


class XRadarCycleFinalized(XRadarStateError):
    """The requested cycle already reached a terminal state and must not rerun."""


@dataclass(frozen=True, slots=True)
class XRadarLease:
    cycle_id: str
    lease_id: str
    index: int
    query: str
    query_sha256: str
    queries_sha256: str
    leased_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "XRadarLease":
        if not isinstance(value, dict):
            raise XRadarStateError("lease must be a JSON object")
        required = {
            "cycle_id", "lease_id", "index", "query", "query_sha256",
            "queries_sha256", "leased_at",
        }
        if set(value) != required:
            raise XRadarStateError("lease has unexpected or missing fields")
        try:
            return cls(
                cycle_id=str(value["cycle_id"]),
                lease_id=str(value["lease_id"]),
                index=int(value["index"]),
                query=str(value["query"]),
                query_sha256=str(value["query_sha256"]),
                queries_sha256=str(value["queries_sha256"]),
                leased_at=str(value["leased_at"]),
            )
        except (TypeError, ValueError) as exc:
            raise XRadarStateError("lease fields are malformed") from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def normalize_queries(queries: Sequence[str]) -> tuple[str, ...]:
    if isinstance(queries, (str, bytes)):
        raise XRadarStateError("queries must be a sequence of strings")
    normalized: list[str] = []
    for raw in queries:
        if not isinstance(raw, str):
            raise XRadarStateError("each X-radar query must be a string")
        query = " ".join(raw.split())
        if not query:
            raise XRadarStateError("X-radar queries cannot be blank")
        normalized.append(query)
    if not normalized:
        raise XRadarStateError("at least one X-radar query is required")
    if len(set(normalized)) != len(normalized):
        raise XRadarStateError("X-radar queries must be unique")
    return tuple(normalized)


def _queries_digest(queries: Sequence[str]) -> str:
    payload = json.dumps(list(queries), ensure_ascii=False, separators=(",", ":"))
    return _sha256_text(payload)


def _absolute_path(path: Path) -> str:
    return os.path.abspath(os.fspath(path))


def _strict_json_loads(raw: bytes, *, label: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite number {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise XRadarStateError(f"{label} is not strict JSON: {exc}") from exc


def _safe_read_regular(path: Path, *, label: str, missing_ok: bool = False) -> bytes | None:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise XRadarStateError(f"{label} does not exist: {path}") from None
    except OSError as exc:
        raise XRadarStateError(f"{label} cannot be opened safely (symlink or invalid file): {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise XRadarStateError(f"{label} must be a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        metadata = os.lstat(path)
        if not stat.S_ISREG(metadata.st_mode):
            raise XRadarStateError(f"state path must be a regular file: {path}")
    payload = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextlib.contextmanager
def _locked(state_path: Path) -> Iterator[None]:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise XRadarStateError(f"lock cannot be opened safely (symlink or invalid file): {lock_path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise XRadarStateError(f"lock must be a regular file: {lock_path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        visible = os.stat(lock_path, follow_symlinks=False)
        if (visible.st_dev, visible.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise XRadarStateError("lock path inode changed while acquiring authority")
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _initial_state(queries: Sequence[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "queries_sha256": _queries_digest(queries),
        "query_count": len(queries),
        "next_index": 0,
        "active_lease_id": None,
        "history": [],
    }


def _validate_state(value: Any, queries: Sequence[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise XRadarStateError("X-radar state must be a JSON object")
    required = {
        "schema_version", "queries_sha256", "query_count", "next_index",
        "active_lease_id", "history",
    }
    if set(value) != required:
        raise XRadarStateError("X-radar state has unexpected or missing fields")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise XRadarStateError("unsupported X-radar state schema")
    expected_digest = _queries_digest(queries)
    if value.get("queries_sha256") != expected_digest or value.get("query_count") != len(queries):
        raise XRadarStateError("query configuration changed; explicit migration is required")
    next_index = value.get("next_index")
    if not isinstance(next_index, int) or isinstance(next_index, bool) or not 0 <= next_index < len(queries):
        raise XRadarStateError("X-radar state has invalid next_index")
    history = value.get("history")
    if not isinstance(history, list):
        raise XRadarStateError("X-radar history must be a list")

    cycle_ids: set[str] = set()
    lease_ids: set[str] = set()
    leased_entries: list[dict[str, Any]] = []
    cursor = 0
    for position, entry in enumerate(history):
        if not isinstance(entry, dict):
            raise XRadarStateError("X-radar history entry must be an object")
        required_entry = {
            "cycle_id", "lease_id", "index", "query_sha256", "status",
            "leased_at", "committed_at", "error", "result_receipt_path",
            "result_receipt_sha256",
        }
        if set(entry) != required_entry:
            raise XRadarStateError("X-radar history entry has unexpected or missing fields")
        cycle_id = entry.get("cycle_id")
        lease_id = entry.get("lease_id")
        index = entry.get("index")
        status_value = entry.get("status")
        if not isinstance(cycle_id, str) or not cycle_id.strip() or cycle_id in cycle_ids:
            raise XRadarStateError("X-radar history has invalid or duplicate cycle_id")
        if not isinstance(lease_id, str) or not lease_id.startswith("lease_") or lease_id in lease_ids:
            raise XRadarStateError("X-radar history has invalid or duplicate lease_id")
        if not isinstance(index, int) or isinstance(index, bool) or index != cursor:
            raise XRadarStateError("X-radar history violates deterministic cursor order")
        if entry.get("query_sha256") != _sha256_text(queries[index]):
            raise XRadarStateError("X-radar history query binding is invalid")
        if not isinstance(entry.get("leased_at"), str) or not entry["leased_at"]:
            raise XRadarStateError("X-radar history leased_at is invalid")
        if status_value == "leased":
            if position != len(history) - 1:
                raise XRadarStateError("active lease must be the final history entry")
            if any(entry.get(key) is not None for key in (
                "committed_at", "error", "result_receipt_path", "result_receipt_sha256"
            )):
                raise XRadarStateError("leased entry contains terminal fields")
            leased_entries.append(entry)
        elif status_value == "succeeded":
            if not isinstance(entry.get("committed_at"), str) or not entry["committed_at"]:
                raise XRadarStateError("succeeded entry committed_at is invalid")
            if entry.get("error") is not None:
                raise XRadarStateError("succeeded entry cannot contain an error")
            if not isinstance(entry.get("result_receipt_path"), str) or not entry["result_receipt_path"]:
                raise XRadarStateError("succeeded entry receipt path is invalid")
            if not _is_sha256(entry.get("result_receipt_sha256")):
                raise XRadarStateError("succeeded entry receipt digest is invalid")
            cursor = (cursor + 1) % len(queries)
        elif status_value == "failed":
            if not isinstance(entry.get("committed_at"), str) or not entry["committed_at"]:
                raise XRadarStateError("failed entry committed_at is invalid")
            if not isinstance(entry.get("error"), str) or not entry["error"]:
                raise XRadarStateError("failed entry error is invalid")
            if entry.get("result_receipt_path") is not None or entry.get("result_receipt_sha256") is not None:
                raise XRadarStateError("failed entry cannot contain a result receipt")
        else:
            raise XRadarStateError("X-radar history has invalid status")
        cycle_ids.add(cycle_id)
        lease_ids.add(lease_id)

    if cursor != next_index:
        raise XRadarStateError("X-radar next_index does not match successful transitions")
    active = value.get("active_lease_id")
    if len(leased_entries) > 1:
        raise XRadarStateError("X-radar state has multiple active leases")
    expected_active = leased_entries[0]["lease_id"] if leased_entries else None
    if active != expected_active:
        raise XRadarStateError("X-radar active lease projection is inconsistent")
    return value


def _load_state(state_path: Path, queries: Sequence[str]) -> dict[str, Any]:
    raw = _safe_read_regular(state_path, label="state", missing_ok=True)
    if raw is None:
        return _initial_state(queries)
    return _validate_state(_strict_json_loads(raw, label="state"), queries)


def _lease_id(state_path: Path, cycle_id: str, index: int, query: str, queries_sha256: str) -> str:
    preimage = json.dumps(
        {
            "state_path": _absolute_path(state_path),
            "cycle_id": cycle_id,
            "index": index,
            "query_sha256": _sha256_text(query),
            "queries_sha256": queries_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "lease_" + _sha256_text(preimage)


def _entry_to_lease(entry: dict[str, Any], queries: Sequence[str]) -> XRadarLease:
    index = int(entry["index"])
    return XRadarLease(
        cycle_id=str(entry["cycle_id"]),
        lease_id=str(entry["lease_id"]),
        index=index,
        query=queries[index],
        query_sha256=str(entry["query_sha256"]),
        queries_sha256=_queries_digest(queries),
        leased_at=str(entry["leased_at"]),
    )


def _find_cycle(state: dict[str, Any], cycle_id: str) -> dict[str, Any] | None:
    return next((entry for entry in state["history"] if entry["cycle_id"] == cycle_id), None)


def _assert_lease_matches(entry: dict[str, Any], lease: XRadarLease, queries: Sequence[str]) -> None:
    expected = _entry_to_lease(entry, queries)
    if lease != expected:
        raise XRadarStateError("lease does not match durable state")


def lease_next(
    queries: Sequence[str],
    state_path: Path,
    *,
    cycle_id: str,
    leased_at: str,
) -> XRadarLease:
    normalized = normalize_queries(queries)
    if not isinstance(cycle_id, str) or not cycle_id.strip():
        raise XRadarStateError("cycle_id is required")
    if not isinstance(leased_at, str) or not leased_at:
        raise XRadarStateError("leased_at is required")
    state_path = Path(state_path)
    with _locked(state_path):
        state = _load_state(state_path, normalized)
        prior = _find_cycle(state, cycle_id)
        if prior is not None:
            if prior["status"] == "leased":
                return _entry_to_lease(prior, normalized)
            raise XRadarCycleFinalized(
                f"cycle {cycle_id!r} already {prior['status']}; do not rerun X search"
            )
        if state["active_lease_id"] is not None:
            raise XRadarStateError("another X-radar cycle has an active lease")
        index = int(state["next_index"])
        query = normalized[index]
        digest = str(state["queries_sha256"])
        lease = XRadarLease(
            cycle_id=cycle_id,
            lease_id=_lease_id(state_path, cycle_id, index, query, digest),
            index=index,
            query=query,
            query_sha256=_sha256_text(query),
            queries_sha256=digest,
            leased_at=leased_at,
        )
        state["active_lease_id"] = lease.lease_id
        state["history"].append(
            {
                "cycle_id": lease.cycle_id,
                "lease_id": lease.lease_id,
                "index": lease.index,
                "query_sha256": lease.query_sha256,
                "status": "leased",
                "leased_at": lease.leased_at,
                "committed_at": None,
                "error": None,
                "result_receipt_path": None,
                "result_receipt_sha256": None,
            }
        )
        _atomic_write_json(state_path, state)
        return lease


def _validate_receipt(
    path: Path,
    lease: XRadarLease,
    *,
    verify_artifacts: bool,
) -> tuple[str, str]:
    absolute_path = _absolute_path(path)
    if not os.path.isabs(os.fspath(path)):
        raise XRadarStateError("result receipt path must be absolute")
    raw = _safe_read_regular(Path(absolute_path), label="result receipt")
    assert raw is not None
    digest = _sha256_bytes(raw)
    value = _strict_json_loads(raw, label="result receipt")
    if not isinstance(value, dict):
        raise XRadarStateError("result receipt must be a JSON object")
    required = {
        "schema_version", "cycle_id", "lease_id", "queries_sha256",
        "query_index", "query_sha256", "result", "checked_at", "artifacts",
    }
    if set(value) != required:
        raise XRadarStateError("result receipt has unexpected or missing fields")
    expected_binding = {
        "schema_version": SCHEMA_VERSION,
        "cycle_id": lease.cycle_id,
        "lease_id": lease.lease_id,
        "queries_sha256": lease.queries_sha256,
        "query_index": lease.index,
        "query_sha256": lease.query_sha256,
    }
    if any(value.get(key) != expected for key, expected in expected_binding.items()):
        raise XRadarStateError("result receipt binding does not match lease")
    if value.get("result") not in ALLOWED_RESULTS:
        raise XRadarStateError("result receipt has unsupported result")
    if not isinstance(value.get("checked_at"), str) or not value["checked_at"]:
        raise XRadarStateError("result receipt checked_at is invalid")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, list):
        raise XRadarStateError("result receipt artifacts must be a list")
    if value["result"] == "material_signal_ingested" and not artifacts:
        raise XRadarStateError("material result receipt must declare at least one artifact")
    if verify_artifacts:
        seen_paths: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
                raise XRadarStateError("result receipt artifact is malformed")
            artifact_path = artifact.get("path")
            artifact_digest = artifact.get("sha256")
            if not isinstance(artifact_path, str) or not os.path.isabs(artifact_path):
                raise XRadarStateError("result receipt artifact path must be absolute")
            absolute_artifact = _absolute_path(Path(artifact_path))
            if absolute_artifact in seen_paths:
                raise XRadarStateError("result receipt contains duplicate artifact paths")
            if not _is_sha256(artifact_digest):
                raise XRadarStateError("result receipt artifact digest is invalid")
            artifact_raw = _safe_read_regular(Path(absolute_artifact), label="result artifact")
            assert artifact_raw is not None
            if _sha256_bytes(artifact_raw) != artifact_digest:
                raise XRadarStateError("result artifact digest does not match durable bytes")
            seen_paths.add(absolute_artifact)
    return absolute_path, digest


def commit_success(
    queries: Sequence[str],
    state_path: Path,
    *,
    lease: XRadarLease,
    result_receipt: Path,
    committed_at: str,
) -> dict[str, Any]:
    normalized = normalize_queries(queries)
    state_path = Path(state_path)
    with _locked(state_path):
        state = _load_state(state_path, normalized)
        entry = _find_cycle(state, lease.cycle_id)
        if entry is None:
            raise XRadarStateError("lease does not match durable state")
        _assert_lease_matches(entry, lease, normalized)
        receipt_path, receipt_digest = _validate_receipt(
            Path(result_receipt), lease, verify_artifacts=entry["status"] == "leased"
        )
        if entry["status"] == "succeeded":
            if (
                entry["result_receipt_path"] == receipt_path
                and entry["result_receipt_sha256"] == receipt_digest
            ):
                return state
            raise XRadarStateError("conflicting result receipt for finalized cycle")
        if entry["status"] == "failed":
            raise XRadarStateError("cannot commit success after failure")
        if state["active_lease_id"] != lease.lease_id:
            raise XRadarStateError("lease is not active")
        entry["status"] = "succeeded"
        entry["committed_at"] = committed_at
        entry["result_receipt_path"] = receipt_path
        entry["result_receipt_sha256"] = receipt_digest
        entry["error"] = None
        state["active_lease_id"] = None
        state["next_index"] = (lease.index + 1) % len(normalized)
        _validate_state(state, normalized)
        _atomic_write_json(state_path, state)
        return state


def commit_failure(
    queries: Sequence[str],
    state_path: Path,
    *,
    lease: XRadarLease,
    error: str,
    committed_at: str,
) -> dict[str, Any]:
    normalized = normalize_queries(queries)
    state_path = Path(state_path)
    normalized_error = " ".join(str(error).split())
    if not normalized_error:
        raise XRadarStateError("failure error is required")
    with _locked(state_path):
        state = _load_state(state_path, normalized)
        entry = _find_cycle(state, lease.cycle_id)
        if entry is None:
            raise XRadarStateError("lease does not match durable state")
        _assert_lease_matches(entry, lease, normalized)
        if entry["status"] == "failed":
            if entry["error"] == normalized_error:
                return state
            raise XRadarStateError("conflicting failure for finalized cycle")
        if entry["status"] == "succeeded":
            raise XRadarStateError("cannot commit failure after success")
        if state["active_lease_id"] != lease.lease_id:
            raise XRadarStateError("lease is not active")
        entry["status"] = "failed"
        entry["committed_at"] = committed_at
        entry["error"] = normalized_error
        entry["result_receipt_path"] = None
        entry["result_receipt_sha256"] = None
        state["active_lease_id"] = None
        _validate_state(state, normalized)
        _atomic_write_json(state_path, state)
        return state
