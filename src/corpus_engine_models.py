#!/usr/bin/env python3
"""Versioned, serialization-safe contracts for corpus candidates and work.

The contracts are immutable value objects. Every public constructor enforces the
same invariants as the convenience factories, nested mappings are defensively
copied, and every record has an explicit strict-JSON codec.
"""
from __future__ import annotations

import hashlib
import json
import math
import posixpath
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

SCHEMA_VERSION = 1

ENTITY_TYPES = {
    "creator", "repository", "paper", "channel", "community",
    "benchmark", "document", "other",
}

SCORE_COMPONENTS = (
    "authority",
    "demonstrated_practice",
    "novelty",
    "relevance",
    "corroboration",
    "production_or_scientific_value",
    "cost",
)

CANDIDATE_STATUSES = {"discovered", "probationary", "promoted", "rejected", "blocked"}
CANDIDATE_TRANSITIONS = {
    "discovered": {"probationary", "rejected", "blocked"},
    "probationary": {"promoted", "rejected", "blocked"},
    "promoted": {"blocked", "rejected"},
    "rejected": {"discovered"},
    "blocked": {"discovered"},
}

WORK_ACTIONS = {"inspect", "acquire", "normalize", "verify", "synthesize", "evaluate", "repair"}
WORK_STATES = {"pending", "leased", "done", "dead_letter"}
MAX_ATTEMPTS = 5


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime, field_name: str = "datetime") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    value = utcnow() if dt is None else _aware_utc(dt)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = _require_text(value, "timestamp")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return _aware_utc(parsed, "timestamp")


def _parse_required_iso(value: str, field_name: str) -> datetime:
    parsed = parse_iso(_require_text(value, field_name))
    assert parsed is not None
    return parsed


def _canonical_timestamp(value: str, field_name: str) -> str:
    return iso(_parse_required_iso(value, field_name))


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _normalize_url(url: str) -> str:
    raw = _require_text(url, "canonical_url")
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("canonical_url must be an absolute URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("canonical_url must not contain credentials")
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    assert host is not None
    if ":" in host:
        normalized_host = f"[{host.lower()}]"
    else:
        normalized_host = host.encode("idna").decode("ascii").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("canonical_url has an invalid port") from exc
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        normalized_host = f"{normalized_host}:{port}"
    path = parsed.path or "/"
    normalized_path = posixpath.normpath(path)
    if path.startswith("/") and not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    if normalized_path in {"/", "."}:
        normalized_path = ""
    else:
        normalized_path = normalized_path.rstrip("/")
    return urlunsplit((scheme, normalized_host, normalized_path, parsed.query, ""))


def _frozen_scores(scores: Mapping[str, float], *, exact_candidate_scores: bool) -> Mapping[str, float]:
    if not isinstance(scores, Mapping):
        raise ValueError("score_components must be a mapping")
    if exact_candidate_scores and set(scores) != set(SCORE_COMPONENTS):
        missing = set(SCORE_COMPONENTS) - set(scores)
        extra = set(scores) - set(SCORE_COMPONENTS)
        raise ValueError(f"score_components mismatch: missing={missing} extra={extra}")
    normalized: dict[str, float] = {}
    for key, value in scores.items():
        name = _require_text(key, "score component name")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"score component {name!r} must be numeric")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError(f"score component {name!r} must be finite")
        if exact_candidate_scores and not 0.0 <= numeric <= 1.0:
            raise ValueError(f"score component {name!r} out of range [0, 1]: {value}")
        if not exact_candidate_scores and numeric < 0:
            raise ValueError(f"score component {name!r} must be non-negative")
        normalized[name] = numeric
    return MappingProxyType(normalized)


def _text_tuple(values: Any, field_name: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{field_name} must be a tuple/list of strings")
    result = tuple(_require_text(value, field_name) for value in values)
    if not allow_empty and not result:
        raise ValueError(f"{field_name} must not be empty")
    return result


def stable_id(prefix: str, *parts: str) -> str:
    normalized_prefix = _require_text(prefix, "prefix")
    framed = json.dumps([_require_text(part, "stable ID part") for part in parts], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(framed.encode("utf-8")).hexdigest()[:24]
    return f"{normalized_prefix}_{digest}"


@dataclass(frozen=True)
class CandidateObservation:
    schema_version: int
    domain: str
    entity_type: str
    canonical_url: str
    discovery_source: str
    evidence_pointer: str
    evidence_lane: str
    topics: tuple[str, ...]
    observed_at: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        object.__setattr__(self, "domain", _require_text(self.domain, "domain"))
        entity_type = _require_text(self.entity_type, "entity_type")
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"unknown entity_type: {entity_type!r}")
        object.__setattr__(self, "entity_type", entity_type)
        object.__setattr__(self, "canonical_url", _normalize_url(self.canonical_url))
        object.__setattr__(self, "discovery_source", _require_text(self.discovery_source, "discovery_source"))
        object.__setattr__(self, "evidence_pointer", _require_text(self.evidence_pointer, "evidence_pointer"))
        object.__setattr__(self, "evidence_lane", _require_text(self.evidence_lane, "evidence_lane"))
        object.__setattr__(self, "topics", _text_tuple(self.topics, "topics"))
        object.__setattr__(self, "observed_at", _canonical_timestamp(self.observed_at, "observed_at"))

    @classmethod
    def create(
        cls,
        *,
        domain: str,
        entity_type: str,
        canonical_url: str,
        discovery_source: str,
        evidence_pointer: str,
        evidence_lane: str,
        topics: tuple[str, ...] = (),
        observed_at: str | None = None,
    ) -> "CandidateObservation":
        return cls(
            schema_version=SCHEMA_VERSION,
            domain=domain,
            entity_type=entity_type,
            canonical_url=canonical_url,
            discovery_source=discovery_source,
            evidence_pointer=evidence_pointer,
            evidence_lane=evidence_lane,
            topics=topics,
            observed_at=observed_at or iso(),
        )

    @property
    def candidate_key(self) -> str:
        return stable_id("cand", self.domain, self.entity_type, self.canonical_url)

    @property
    def observation_key(self) -> str:
        return stable_id("obs", self.candidate_key, self.discovery_source, self.evidence_pointer)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "domain": self.domain,
            "entity_type": self.entity_type,
            "canonical_url": self.canonical_url,
            "discovery_source": self.discovery_source,
            "evidence_pointer": self.evidence_pointer,
            "evidence_lane": self.evidence_lane,
            "topics": list(self.topics),
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CandidateObservation":
        return cls(**dict(data))


@dataclass(frozen=True)
class CandidateRecord:
    schema_version: int
    candidate_id: str
    domain: str
    entity_type: str
    canonical_url: str
    discovery_source: str
    evidence_pointer: str
    evidence_lane: str
    topics: tuple[str, ...]
    score_components: Mapping[str, float]
    status: str
    first_seen_at: str
    last_seen_at: str
    occurrences: int
    dedup_keys: tuple[str, ...]
    rationale: str
    rejection_reason: str | None
    rights_state: str = "unknown"

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        domain = _require_text(self.domain, "domain")
        entity_type = _require_text(self.entity_type, "entity_type")
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"unknown entity_type: {entity_type!r}")
        canonical_url = _normalize_url(self.canonical_url)
        expected_id = stable_id("cand", domain, entity_type, canonical_url)
        if _require_text(self.candidate_id, "candidate_id") != expected_id:
            raise ValueError("candidate_id does not match canonical candidate identity")
        status = _require_text(self.status, "status")
        if status not in CANDIDATE_STATUSES:
            raise ValueError(f"unknown status: {status!r}")
        if not isinstance(self.occurrences, int) or isinstance(self.occurrences, bool) or self.occurrences < 1:
            raise ValueError("occurrences must be a positive integer")
        first_seen = _canonical_timestamp(self.first_seen_at, "first_seen_at")
        last_seen = _canonical_timestamp(self.last_seen_at, "last_seen_at")
        if _parse_required_iso(last_seen, "last_seen_at") < _parse_required_iso(first_seen, "first_seen_at"):
            raise ValueError("last_seen_at cannot precede first_seen_at")
        dedup_keys = _text_tuple(self.dedup_keys, "dedup_keys", allow_empty=False)
        if len(dedup_keys) != len(set(dedup_keys)):
            raise ValueError("dedup_keys must be unique")
        rejection_reason = self.rejection_reason
        if status == "rejected":
            rejection_reason = _require_text(rejection_reason, "rejection_reason")
        elif rejection_reason is not None:
            rejection_reason = _require_text(rejection_reason, "rejection_reason")
        object.__setattr__(self, "domain", domain)
        object.__setattr__(self, "entity_type", entity_type)
        object.__setattr__(self, "canonical_url", canonical_url)
        object.__setattr__(self, "discovery_source", _require_text(self.discovery_source, "discovery_source"))
        object.__setattr__(self, "evidence_pointer", _require_text(self.evidence_pointer, "evidence_pointer"))
        object.__setattr__(self, "evidence_lane", _require_text(self.evidence_lane, "evidence_lane"))
        object.__setattr__(self, "topics", _text_tuple(self.topics, "topics"))
        object.__setattr__(self, "score_components", _frozen_scores(self.score_components, exact_candidate_scores=True))
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "first_seen_at", first_seen)
        object.__setattr__(self, "last_seen_at", last_seen)
        object.__setattr__(self, "dedup_keys", dedup_keys)
        object.__setattr__(self, "rationale", str(self.rationale).strip())
        object.__setattr__(self, "rejection_reason", rejection_reason)
        object.__setattr__(self, "rights_state", _require_text(self.rights_state, "rights_state"))

    @classmethod
    def from_observation(
        cls,
        observation: CandidateObservation,
        score_components: Mapping[str, float],
        *,
        rationale: str = "",
        rights_state: str = "unknown",
    ) -> "CandidateRecord":
        return cls(
            schema_version=SCHEMA_VERSION,
            candidate_id=observation.candidate_key,
            domain=observation.domain,
            entity_type=observation.entity_type,
            canonical_url=observation.canonical_url,
            discovery_source=observation.discovery_source,
            evidence_pointer=observation.evidence_pointer,
            evidence_lane=observation.evidence_lane,
            topics=observation.topics,
            score_components=score_components,
            status="discovered",
            first_seen_at=observation.observed_at,
            last_seen_at=observation.observed_at,
            occurrences=1,
            dedup_keys=(observation.observation_key,),
            rationale=rationale,
            rejection_reason=None,
            rights_state=rights_state,
        )

    def compute_score(self) -> dict[str, float]:
        scores = self.score_components
        cost = max(scores["cost"], 0.01)
        total = (
            scores["relevance"]
            * scores["authority"]
            * max(scores["novelty"], 0.01)
            * max(scores["corroboration"], 0.01)
            * scores["production_or_scientific_value"]
        ) / cost
        return {**scores, "total": total}

    def observe(self, observation: CandidateObservation) -> "CandidateRecord":
        if observation.candidate_key != self.candidate_id:
            raise ValueError("observation does not converge onto this candidate")
        if observation.observation_key in self.dedup_keys:
            return self
        corroboration = min(self.score_components["corroboration"] + 0.1, 1.0)
        last_seen = max(
            _parse_required_iso(self.last_seen_at, "last_seen_at"),
            _parse_required_iso(observation.observed_at, "observed_at"),
        )
        return replace(
            self,
            occurrences=self.occurrences + 1,
            last_seen_at=iso(last_seen),
            score_components={**self.score_components, "corroboration": corroboration},
            dedup_keys=(*self.dedup_keys, observation.observation_key),
        )

    def transition(self, new_status: str, *, rejection_reason: str | None = None) -> "CandidateRecord":
        status = _require_text(new_status, "new_status")
        if status not in CANDIDATE_STATUSES:
            raise ValueError(f"unknown status: {status!r}")
        if status not in CANDIDATE_TRANSITIONS.get(self.status, set()):
            raise ValueError(f"illegal transition {self.status!r} -> {status!r}")
        if status == "rejected":
            rejection_reason = _require_text(rejection_reason, "rejection_reason")
        else:
            rejection_reason = None
        return replace(self, status=status, rejection_reason=rejection_reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "domain": self.domain,
            "entity_type": self.entity_type,
            "canonical_url": self.canonical_url,
            "discovery_source": self.discovery_source,
            "evidence_pointer": self.evidence_pointer,
            "evidence_lane": self.evidence_lane,
            "topics": list(self.topics),
            "score_components": dict(self.score_components),
            "status": self.status,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "occurrences": self.occurrences,
            "dedup_keys": list(self.dedup_keys),
            "rationale": self.rationale,
            "rejection_reason": self.rejection_reason,
            "rights_state": self.rights_state,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CandidateRecord":
        return cls(**dict(data))


@dataclass(frozen=True)
class WorkItem:
    schema_version: int
    work_id: str
    idempotency_key: str
    domain: str
    candidate_id: str
    action: str
    score_components: Mapping[str, float]
    budget_estimate: float
    state: str
    attempts: int
    lease_owner: str | None
    lease_token: str | None
    lease_generation: int
    lease_expires_at: str | None
    retry_after: str | None
    proof_receipts: tuple[str, ...]
    last_error: str | None
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        domain = _require_text(self.domain, "domain")
        candidate_id = _require_text(self.candidate_id, "candidate_id")
        action = _require_text(self.action, "action")
        if action not in WORK_ACTIONS:
            raise ValueError(f"unknown action: {action!r}")
        expected_id = stable_id("work", domain, candidate_id, action)
        if _require_text(self.work_id, "work_id") != expected_id:
            raise ValueError("work_id does not match canonical work identity")
        idempotency_key = _require_text(self.idempotency_key, "idempotency_key")
        if not isinstance(self.budget_estimate, (int, float)) or isinstance(self.budget_estimate, bool):
            raise ValueError("budget_estimate must be numeric")
        budget = float(self.budget_estimate)
        if not math.isfinite(budget) or budget < 0:
            raise ValueError("budget_estimate must be finite and non-negative")
        state = _require_text(self.state, "state")
        if state not in WORK_STATES:
            raise ValueError(f"unknown work state: {state!r}")
        if not isinstance(self.attempts, int) or isinstance(self.attempts, bool) or self.attempts < 0:
            raise ValueError("attempts must be a non-negative integer")
        if not isinstance(self.lease_generation, int) or isinstance(self.lease_generation, bool) or self.lease_generation < 0:
            raise ValueError("lease_generation must be a non-negative integer")
        created_at = _canonical_timestamp(self.created_at, "created_at")
        updated_at = _canonical_timestamp(self.updated_at, "updated_at")
        if _parse_required_iso(updated_at, "updated_at") < _parse_required_iso(created_at, "created_at"):
            raise ValueError("updated_at cannot precede created_at")
        lease_owner = self.lease_owner
        lease_token = self.lease_token
        lease_expires_at = self.lease_expires_at
        retry_after = self.retry_after
        proof_receipts = _text_tuple(self.proof_receipts, "proof_receipts")
        last_error = self.last_error
        if state == "leased":
            lease_owner = _require_text(lease_owner, "lease_owner")
            lease_token = _require_text(lease_token, "lease_token")
            lease_expires_at = _canonical_timestamp(
                _require_text(lease_expires_at, "lease_expires_at"),
                "lease_expires_at",
            )
            if retry_after is not None:
                raise ValueError("leased work cannot retain retry_after")
        else:
            if lease_owner is not None or lease_token is not None or lease_expires_at is not None:
                raise ValueError(f"{state} work cannot retain lease metadata")
            if retry_after is not None:
                retry_after = _canonical_timestamp(retry_after, "retry_after")
        if state == "done":
            if not proof_receipts:
                raise ValueError("done work requires at least one proof receipt")
            if retry_after is not None:
                raise ValueError("done work cannot retain retry_after")
        if state == "dead_letter":
            last_error = _require_text(last_error, "last_error")
            if retry_after is not None:
                raise ValueError("dead-letter work cannot retain retry_after")
        elif last_error is not None:
            last_error = _require_text(last_error, "last_error")
        object.__setattr__(self, "work_id", expected_id)
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(self, "domain", domain)
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "score_components", _frozen_scores(self.score_components, exact_candidate_scores=False))
        object.__setattr__(self, "budget_estimate", budget)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "lease_owner", lease_owner)
        object.__setattr__(self, "lease_token", lease_token)
        object.__setattr__(self, "lease_expires_at", lease_expires_at)
        object.__setattr__(self, "retry_after", retry_after)
        object.__setattr__(self, "proof_receipts", proof_receipts)
        object.__setattr__(self, "last_error", last_error)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)

    @classmethod
    def create(
        cls,
        *,
        domain: str,
        candidate_id: str,
        action: str,
        score_components: Mapping[str, float],
        budget_estimate: float,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> "WorkItem":
        moment = utcnow() if now is None else _aware_utc(now, "now")
        work_id = stable_id("work", domain, candidate_id, action)
        return cls(
            schema_version=SCHEMA_VERSION,
            work_id=work_id,
            idempotency_key=work_id if idempotency_key is None else idempotency_key,
            domain=domain,
            candidate_id=candidate_id,
            action=action,
            score_components=score_components,
            budget_estimate=budget_estimate,
            state="pending",
            attempts=0,
            lease_owner=None,
            lease_token=None,
            lease_generation=0,
            lease_expires_at=None,
            retry_after=None,
            proof_receipts=(),
            last_error=None,
            created_at=iso(moment),
            updated_at=iso(moment),
        )

    def is_lease_expired(self, *, now: datetime | None = None) -> bool:
        if self.state != "leased":
            return False
        moment = utcnow() if now is None else _aware_utc(now, "now")
        expiry = parse_iso(self.lease_expires_at)
        assert expiry is not None
        return moment >= expiry

    def lease(
        self,
        *,
        owner: str,
        ttl_seconds: int,
        now: datetime | None = None,
        lease_token: str | None = None,
    ) -> "WorkItem":
        if self.state != "pending":
            raise ValueError(f"cannot lease work item in state {self.state!r}")
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        moment = utcnow() if now is None else _aware_utc(now, "now")
        retry_after = parse_iso(self.retry_after)
        if retry_after is not None and moment < retry_after:
            raise ValueError("work item is not eligible until retry_after")
        token = secrets.token_hex(16) if lease_token is None else _require_text(lease_token, "lease_token")
        return replace(
            self,
            state="leased",
            lease_owner=_require_text(owner, "owner"),
            lease_token=token,
            lease_generation=self.lease_generation + 1,
            lease_expires_at=iso(moment + timedelta(seconds=ttl_seconds)),
            retry_after=None,
            updated_at=iso(moment),
        )

    def _validate_active_lease(self, *, owner: str, lease_token: str, now: datetime | None) -> datetime:
        if self.state != "leased":
            raise ValueError(f"work item is not leased: {self.state!r}")
        if _require_text(owner, "owner") != self.lease_owner:
            raise ValueError("lease owner does not match current lease")
        if _require_text(lease_token, "lease_token") != self.lease_token:
            raise ValueError("lease token does not match current lease")
        moment = utcnow() if now is None else _aware_utc(now, "now")
        if self.is_lease_expired(now=moment):
            raise ValueError("lease has expired")
        return moment

    def release_if_expired(self, *, now: datetime | None = None) -> "WorkItem":
        if not self.is_lease_expired(now=now):
            return self
        moment = utcnow() if now is None else _aware_utc(now, "now")
        attempts = self.attempts + 1
        next_state = "dead_letter" if attempts >= MAX_ATTEMPTS else "pending"
        error = f"lease expired at {self.lease_expires_at}"
        return replace(
            self,
            state=next_state,
            attempts=attempts,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            retry_after=None,
            last_error=error,
            updated_at=iso(moment),
        )

    def complete(
        self,
        *,
        owner: str,
        lease_token: str,
        proof_receipt: str,
        now: datetime | None = None,
    ) -> "WorkItem":
        moment = self._validate_active_lease(owner=owner, lease_token=lease_token, now=now)
        receipt = _require_text(proof_receipt, "proof_receipt")
        return replace(
            self,
            state="done",
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            retry_after=None,
            proof_receipts=(*self.proof_receipts, receipt),
            updated_at=iso(moment),
        )

    def fail(
        self,
        *,
        owner: str,
        lease_token: str,
        error: str,
        retry_after_seconds: int = 60,
        now: datetime | None = None,
    ) -> "WorkItem":
        moment = self._validate_active_lease(owner=owner, lease_token=lease_token, now=now)
        if not isinstance(retry_after_seconds, int) or isinstance(retry_after_seconds, bool) or retry_after_seconds <= 0:
            raise ValueError("retry_after_seconds must be a positive integer")
        attempts = self.attempts + 1
        next_state = "dead_letter" if attempts >= MAX_ATTEMPTS else "pending"
        return replace(
            self,
            state=next_state,
            attempts=attempts,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            retry_after=iso(moment + timedelta(seconds=retry_after_seconds)) if next_state == "pending" else None,
            last_error=_require_text(error, "error"),
            updated_at=iso(moment),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "work_id": self.work_id,
            "idempotency_key": self.idempotency_key,
            "domain": self.domain,
            "candidate_id": self.candidate_id,
            "action": self.action,
            "score_components": dict(self.score_components),
            "budget_estimate": self.budget_estimate,
            "state": self.state,
            "attempts": self.attempts,
            "lease_owner": self.lease_owner,
            "lease_token": self.lease_token,
            "lease_generation": self.lease_generation,
            "lease_expires_at": self.lease_expires_at,
            "retry_after": self.retry_after,
            "proof_receipts": list(self.proof_receipts),
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkItem":
        return cls(**dict(data))
