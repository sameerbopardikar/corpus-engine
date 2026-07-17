from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from urllib.parse import urlsplit


class RightsState(str, Enum):
    PUBLIC_RIGHTS_CLEAR = "public_rights_clear"
    PUBLIC_METADATA_ONLY = "public_metadata_only"
    PRIVATE_AUTHORIZED = "private_authorized"
    RIGHTS_UNCLEAR = "rights_unclear"


class FailureKind(str, Enum):
    TRANSPORT = "transport"
    MALFORMED_RESPONSE = "malformed_response"
    RAW_INTEGRITY = "raw_integrity"
    CURSOR_CONFLICT = "cursor_conflict"
    CONTRACT = "contract"


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-blank")


def _require_http_url(name: str, value: str) -> None:
    _require_text(name, value)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{name} must not contain credentials")


def _require_aware_iso(name: str, value: str) -> None:
    _require_text(name, value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


def _require_sha256(name: str, value: str) -> None:
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class SourceSpec:
    source_id: str
    domain: str
    source_family: str
    canonical_locator: str
    evidence_lane: str
    rights_state: RightsState

    def __post_init__(self) -> None:
        for field_name in ("source_id", "domain", "source_family", "evidence_lane"):
            _require_text(field_name, getattr(self, field_name))
        _require_http_url("canonical_locator", self.canonical_locator)
        if not isinstance(self.rights_state, RightsState):
            raise ValueError("rights_state must be a RightsState")


@dataclass(frozen=True, slots=True)
class InventoryRequest:
    max_items: int
    cursor: str | None = None
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if isinstance(self.max_items, bool) or not isinstance(self.max_items, int) or not 1 <= self.max_items <= 100:
            raise ValueError("max_items must be an integer from 1 through 100")
        if self.cursor is not None:
            _require_text("cursor", self.cursor)
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be numeric")
        if not math.isfinite(float(self.timeout_seconds)) or not 0 < float(self.timeout_seconds) <= 300:
            raise ValueError("timeout_seconds must be finite and in (0, 300]")


@dataclass(frozen=True, slots=True)
class TransportPayload:
    body: bytes
    final_url: str
    fetched_at: str
    source_revision: str
    raw_pointer: str

    def __post_init__(self) -> None:
        if not isinstance(self.body, bytes):
            raise ValueError("body must be bytes")
        _require_http_url("final_url", self.final_url)
        _require_aware_iso("fetched_at", self.fetched_at)
        _require_text("source_revision", self.source_revision)
        _require_text("raw_pointer", self.raw_pointer)

    @property
    def raw_sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


@dataclass(frozen=True, slots=True)
class NormalizedObservation:
    canonical_locator: str
    title: str
    evidence_pointer: str
    raw_pointer: str
    raw_sha256: str
    normalized_pointer: str
    normalized_sha256: str
    content_kind: str
    fetched_at: str
    source_revision: str
    rights_state: RightsState

    def __post_init__(self) -> None:
        _require_http_url("canonical_locator", self.canonical_locator)
        _require_text("title", self.title)
        _require_http_url("evidence_pointer", self.evidence_pointer)
        _require_text("raw_pointer", self.raw_pointer)
        _require_sha256("raw_sha256", self.raw_sha256)
        _require_text("normalized_pointer", self.normalized_pointer)
        _require_sha256("normalized_sha256", self.normalized_sha256)
        _require_text("content_kind", self.content_kind)
        _require_aware_iso("fetched_at", self.fetched_at)
        _require_text("source_revision", self.source_revision)
        if not isinstance(self.rights_state, RightsState):
            raise ValueError("rights_state must be a RightsState")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["rights_state"] = self.rights_state.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NormalizedObservation":
        return cls(**{**value, "rights_state": RightsState(value["rights_state"])})


@dataclass(frozen=True, slots=True)
class ObservationBatch:
    batch_id: str
    source_id: str
    cursor_before: str | None
    cursor_after: str | None
    transport_url: str
    transport_sha256: str
    fetched_at: str
    source_revision: str
    observations: tuple[NormalizedObservation, ...]

    def __post_init__(self) -> None:
        _require_sha256("batch_id", self.batch_id)
        _require_text("source_id", self.source_id)
        if self.cursor_before is not None:
            _require_text("cursor_before", self.cursor_before)
        if self.cursor_after is not None:
            _require_text("cursor_after", self.cursor_after)
        _require_http_url("transport_url", self.transport_url)
        _require_sha256("transport_sha256", self.transport_sha256)
        _require_aware_iso("fetched_at", self.fetched_at)
        _require_text("source_revision", self.source_revision)
        if not isinstance(self.observations, tuple):
            raise ValueError("observations must be a tuple")

    @classmethod
    def create(
        cls,
        *,
        source_id: str,
        cursor_before: str | None,
        cursor_after: str | None,
        payload: TransportPayload,
        observations: tuple[NormalizedObservation, ...],
    ) -> "ObservationBatch":
        semantic_observations = []
        for item in observations:
            value = item.to_dict()
            # Retrieval time and local preservation paths are provenance, not
            # source identity. Identical source bytes/revisions must converge
            # even when fetched in a later run or staged under another root.
            for volatile_key in ("fetched_at", "raw_pointer", "raw_sha256", "normalized_pointer"):
                value.pop(volatile_key)
            semantic_observations.append(value)
        identity = {
            "source_id": source_id,
            # cursor_before is a commit precondition, not material source
            # identity. Omitting it lets a later no-delta check converge on the
            # original batch while _commit still fences real cursor conflicts.
            "cursor_after": cursor_after,
            "transport_url": payload.final_url,
            "source_revision": payload.source_revision,
            "observations": semantic_observations,
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        return cls(
            batch_id=hashlib.sha256(encoded).hexdigest(),
            source_id=source_id,
            cursor_before=cursor_before,
            cursor_after=cursor_after,
            transport_url=payload.final_url,
            transport_sha256=payload.raw_sha256,
            fetched_at=payload.fetched_at,
            source_revision=payload.source_revision,
            observations=observations,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "source_id": self.source_id,
            "cursor_before": self.cursor_before,
            "cursor_after": self.cursor_after,
            "transport_url": self.transport_url,
            "transport_sha256": self.transport_sha256,
            "fetched_at": self.fetched_at,
            "source_revision": self.source_revision,
            "observations": [item.to_dict() for item in self.observations],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ObservationBatch":
        return cls(
            **{key: val for key, val in value.items() if key != "observations"},
            observations=tuple(NormalizedObservation.from_dict(item) for item in value["observations"]),
        )
