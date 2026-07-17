from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .types import (
    FailureKind,
    InventoryRequest,
    NormalizedObservation,
    ObservationBatch,
    SourceSpec,
    TransportPayload,
)


class AdapterFailure(RuntimeError):
    def __init__(self, kind: FailureKind, detail: str, *, retryable: bool = False):
        super().__init__(detail)
        self.kind = kind
        self.retryable = retryable


class SourceAdapter(ABC):
    family: str

    @abstractmethod
    def fetch(self, spec: SourceSpec, request: InventoryRequest) -> TransportPayload:
        """Fetch bytes and preserve them before returning the transport payload."""

    @abstractmethod
    def parse(
        self,
        spec: SourceSpec,
        request: InventoryRequest,
        payload: TransportPayload,
    ) -> tuple[tuple[NormalizedObservation, ...], str | None]:
        """Deterministically parse a bounded observation tuple and next cursor."""


class AdapterRunner:
    """Execute an adapter and atomically commit its observations with its cursor.

    The state file is the adapter ledger. A successful batch and its resulting
    cursor are replaced in one fsync-backed rename. There is deliberately no
    registry or doctrine writer in this contract layer.
    """

    SCHEMA_VERSION = 1

    def __init__(self, state_path: Path):
        self.state_path = Path(state_path)
        self.lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")

    def run(self, adapter: SourceAdapter, spec: SourceSpec, request: InventoryRequest) -> ObservationBatch:
        if adapter.family != spec.source_family:
            raise AdapterFailure(FailureKind.CONTRACT, "adapter family does not match SourceSpec")
        try:
            payload = adapter.fetch(spec, request)
        except AdapterFailure:
            raise
        except Exception as exc:
            raise AdapterFailure(FailureKind.TRANSPORT, f"{type(exc).__name__}: {exc}", retryable=True) from exc
        try:
            parsed = adapter.parse(spec, request, payload)
            if not isinstance(parsed, tuple) or len(parsed) != 2:
                raise ValueError("parse must return (observations, cursor)")
            observations, cursor_after = parsed
            if not isinstance(observations, tuple):
                raise ValueError("observations must be a tuple")
            if len(observations) > request.max_items:
                raise ValueError("adapter returned more observations than max_items")
            if cursor_after is not None and (not isinstance(cursor_after, str) or not cursor_after.strip()):
                raise ValueError("next cursor must be non-blank or None")
        except AdapterFailure:
            raise
        except Exception as exc:
            raise AdapterFailure(FailureKind.MALFORMED_RESPONSE, f"{type(exc).__name__}: {exc}") from exc

        self._verify_raw(spec, payload, observations)
        batch = ObservationBatch.create(
            source_id=spec.source_id,
            cursor_before=request.cursor,
            cursor_after=cursor_after,
            payload=payload,
            observations=observations,
        )
        return self._commit(batch)

    def _verify_raw(
        self,
        spec: SourceSpec,
        payload: TransportPayload,
        observations: tuple[NormalizedObservation, ...],
    ) -> None:
        raw_path = Path(payload.raw_pointer)
        try:
            raw_bytes = raw_path.read_bytes()
        except OSError as exc:
            raise AdapterFailure(FailureKind.RAW_INTEGRITY, f"raw pointer unreadable: {exc}") from exc
        actual = hashlib.sha256(raw_bytes).hexdigest()
        if actual != payload.raw_sha256:
            raise AdapterFailure(FailureKind.RAW_INTEGRITY, "transport raw pointer hash mismatch")
        for observation in observations:
            if observation.rights_state != spec.rights_state:
                raise AdapterFailure(FailureKind.CONTRACT, "adapter cannot change the source rights state")
            if observation.raw_pointer != payload.raw_pointer or observation.raw_sha256 != payload.raw_sha256:
                raise AdapterFailure(FailureKind.RAW_INTEGRITY, "observation raw provenance differs from transport receipt")
            if observation.fetched_at != payload.fetched_at or observation.source_revision != payload.source_revision:
                raise AdapterFailure(FailureKind.RAW_INTEGRITY, "observation transport metadata mismatch")

    def _empty_state(self) -> dict[str, Any]:
        return {"schema_version": self.SCHEMA_VERSION, "sources": {}}

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._empty_state()
        try:
            state = json.loads(
                self.state_path.read_text(encoding="utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite value: {value}")),
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise AdapterFailure(FailureKind.CONTRACT, f"invalid adapter state: {exc}") from exc
        if state.get("schema_version") != self.SCHEMA_VERSION or not isinstance(state.get("sources"), dict):
            raise AdapterFailure(FailureKind.CONTRACT, "invalid adapter state schema")
        return state

    def _commit(self, batch: ObservationBatch) -> ObservationBatch:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(lock_fd, 0o600)
            with os.fdopen(lock_fd, "r+") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                state = self._read_state()
                source_state = state["sources"].get(batch.source_id, {"cursor": None, "batches": []})
                for prior in source_state.get("batches", []):
                    if prior.get("batch_id") == batch.batch_id:
                        return ObservationBatch.from_dict(prior)
                if source_state.get("cursor") != batch.cursor_before:
                    raise AdapterFailure(
                        FailureKind.CURSOR_CONFLICT,
                        f"cursor changed: expected {batch.cursor_before!r}, current {source_state.get('cursor')!r}",
                        retryable=True,
                    )
                source_state = {
                    "cursor": batch.cursor_after,
                    "batches": [*source_state.get("batches", []), batch.to_dict()],
                }
                state["sources"][batch.source_id] = source_state
                self._atomic_write(state)
                return batch
        except Exception:
            # fdopen owns lock_fd after successful construction.
            raise

    def _atomic_write(self, state: dict[str, Any]) -> None:
        encoded = (json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.state_path.name}.", dir=self.state_path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.state_path)
            os.chmod(self.state_path, 0o600)
            directory_fd = os.open(self.state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
