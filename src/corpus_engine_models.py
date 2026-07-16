#!/usr/bin/env python3
"""Versioned data contracts for discovered candidates and bounded work items.

Pure stdlib, no I/O. Every record is immutable (dataclass(frozen=True));
state changes return a new instance rather than mutating in place, so
callers can persist append-only observations and transitions without
losing prior history.
"""
from __future__ import annotations

import hashlib
import math
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
WORK_STATES = {"pending", "leased", "done", "failed", "dead_letter"}
MAX_ATTEMPTS = 5


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _normalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("canonical_url must be an absolute URL")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    port = parsed.port
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        host = f"{host}:{port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((scheme, host, path, parsed.query, ""))


def _validate_work_scores(scores: Mapping[str, float]) -> Mapping[str, float]:
    if not isinstance(scores, Mapping):
        raise ValueError("score_components must be a mapping")
    normalized: dict[str, float] = {}
    for key, value in scores.items():
        name = _require_text(key, "score component name")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"score component {name!r} must be numeric")
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0:
            raise ValueError(f"score component {name!r} must be finite and non-negative")
        normalized[name] = numeric
    return MappingProxyType(normalized)


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


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
        domain = _require_text(domain, "domain")
        entity_type = _require_text(entity_type, "entity_type")
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"unknown entity_type: {entity_type!r}")
        canonical_url = _require_text(canonical_url, "canonical_url")
        _normalize_url(canonical_url)
        discovery_source = _require_text(discovery_source, "discovery_source")
        evidence_pointer = _require_text(evidence_pointer, "evidence_pointer")
        evidence_lane = _require_text(evidence_lane, "evidence_lane")
        if not isinstance(topics, (tuple, list)):
            raise ValueError("topics must be a tuple/list of strings")
        normalized_topics = tuple(_require_text(topic, "topic") for topic in topics)
        timestamp = observed_at or iso()
        parse_iso(timestamp)
        return cls(
            schema_version=SCHEMA_VERSION,
            domain=domain,
            entity_type=entity_type,
            canonical_url=canonical_url,
            discovery_source=discovery_source,
            evidence_pointer=evidence_pointer,
            evidence_lane=evidence_lane,
            topics=normalized_topics,
            observed_at=timestamp,
        )

    @property
    def candidate_key(self) -> str:
        return stable_id("cand", self.domain, self.entity_type, _normalize_url(self.canonical_url))


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

    @staticmethod
    def _validate_scores(scores: Mapping[str, float]) -> Mapping[str, float]:
        if set(scores) != set(SCORE_COMPONENTS):
            missing = set(SCORE_COMPONENTS) - set(scores)
            extra = set(scores) - set(SCORE_COMPONENTS)
            raise ValueError(f"score_components mismatch: missing={missing} extra={extra}")
        normalized: dict[str, float] = {}
        for key in SCORE_COMPONENTS:
            value = scores[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"score component {key!r} must be numeric")
            if not (0.0 <= float(value) <= 1.0):
                raise ValueError(f"score component {key!r} out of range [0, 1]: {value}")
            normalized[key] = float(value)
        return MappingProxyType(normalized)

    @classmethod
    def from_observation(
        cls,
        observation: CandidateObservation,
        score_components: Mapping[str, float],
        *,
        rationale: str = "",
        rights_state: str = "unknown",
    ) -> "CandidateRecord":
        scores = cls._validate_scores(score_components)
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
            score_components=scores,
            status="discovered",
            first_seen_at=observation.observed_at,
            last_seen_at=observation.observed_at,
            occurrences=1,
            dedup_keys=(observation.candidate_key,),
            rationale=rationale,
            rejection_reason=None,
            rights_state=rights_state,
        )

    def compute_score(self) -> dict[str, float]:
        s = self.score_components
        cost = max(s["cost"], 0.01)
        total = (
            s["relevance"] * s["authority"] * max(s["novelty"], 0.01)
            * max(s["corroboration"], 0.01) * s["production_or_scientific_value"]
        ) / cost
        return {**s, "total": total}

    def observe(self, observation: CandidateObservation) -> "CandidateRecord":
        if observation.candidate_key != self.candidate_id:
            raise ValueError("observation does not converge onto this candidate")
        bumped_corroboration = min(self.score_components["corroboration"] + 0.1, 1.0)
        new_scores = MappingProxyType({**self.score_components, "corroboration": bumped_corroboration})
        return replace(
            self,
            occurrences=self.occurrences + 1,
            last_seen_at=observation.observed_at,
            score_components=new_scores,
            dedup_keys=tuple(dict.fromkeys((*self.dedup_keys, observation.candidate_key))),
        )

    def transition(self, new_status: str, *, rejection_reason: str | None = None) -> "CandidateRecord":
        if new_status not in CANDIDATE_STATUSES:
            raise ValueError(f"unknown status: {new_status!r}")
        allowed = CANDIDATE_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(f"illegal transition {self.status!r} -> {new_status!r}")
        if new_status == "rejected" and not (rejection_reason and rejection_reason.strip()):
            raise ValueError("rejected transition requires a non-empty rejection_reason")
        return replace(
            self,
            status=new_status,
            rejection_reason=rejection_reason.strip() if rejection_reason else (
                None if new_status != "rejected" else self.rejection_reason
            ),
        )


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
    lease_expires_at: str | None
    retry_after: str | None
    proof_receipts: tuple[str, ...]
    created_at: str
    updated_at: str

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
        domain = _require_text(domain, "domain")
        candidate_id = _require_text(candidate_id, "candidate_id")
        action = _require_text(action, "action")
        if action not in WORK_ACTIONS:
            raise ValueError(f"unknown action: {action!r}")
        if not isinstance(budget_estimate, (int, float)) or isinstance(budget_estimate, bool) or budget_estimate < 0:
            raise ValueError("budget_estimate must be a non-negative number")
        work_id = stable_id("work", domain, candidate_id, action)
        created = iso(now)
        return cls(
            schema_version=SCHEMA_VERSION,
            work_id=work_id,
            idempotency_key=idempotency_key or work_id,
            domain=domain,
            candidate_id=candidate_id,
            action=action,
            score_components=_validate_work_scores(score_components),
            budget_estimate=float(budget_estimate),
            state="pending",
            attempts=0,
            lease_owner=None,
            lease_expires_at=None,
            retry_after=None,
            proof_receipts=(),
            created_at=created,
            updated_at=created,
        )

    def is_lease_expired(self, *, now: datetime | None = None) -> bool:
        if self.state != "leased" or not self.lease_expires_at:
            return False
        expiry = parse_iso(self.lease_expires_at)
        assert expiry is not None
        return (now or utcnow()) >= expiry

    def lease(self, *, owner: str, ttl_seconds: int, now: datetime | None = None) -> "WorkItem":
        owner = _require_text(owner, "owner")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if self.state not in {"pending", "failed"}:
            raise ValueError(f"cannot lease work item in state {self.state!r}")
        moment = now or utcnow()
        retry_after = parse_iso(self.retry_after)
        if retry_after and moment < retry_after:
            raise ValueError("work item is not eligible until retry_after")
        expiry = moment + timedelta(seconds=ttl_seconds)
        return replace(
            self,
            state="leased",
            lease_owner=owner,
            lease_expires_at=iso(expiry),
            updated_at=iso(moment),
        )

    def release_if_expired(self, *, now: datetime | None = None) -> "WorkItem":
        if not self.is_lease_expired(now=now):
            return self
        moment = now or utcnow()
        attempts = self.attempts + 1
        next_state = "dead_letter" if attempts >= MAX_ATTEMPTS else "pending"
        return replace(
            self,
            state=next_state,
            lease_owner=None,
            lease_expires_at=None,
            attempts=attempts,
            retry_after=None,
            updated_at=iso(moment),
        )

    def complete(self, *, proof_receipt: str, now: datetime | None = None) -> "WorkItem":
        proof_receipt = _require_text(proof_receipt, "proof_receipt")
        if self.state != "leased":
            raise ValueError(f"cannot complete work item in state {self.state!r}")
        moment = now or utcnow()
        return replace(
            self,
            state="done",
            proof_receipts=(*self.proof_receipts, proof_receipt),
            lease_owner=None,
            lease_expires_at=None,
            updated_at=iso(moment),
        )

    def fail(self, *, retry_after_seconds: int = 60, now: datetime | None = None) -> "WorkItem":
        if self.state != "leased":
            raise ValueError(f"cannot fail work item in state {self.state!r}")
        if retry_after_seconds <= 0:
            raise ValueError("retry_after_seconds must be positive")
        moment = now or utcnow()
        attempts = self.attempts + 1
        next_state = "dead_letter" if attempts >= MAX_ATTEMPTS else "pending"
        return replace(
            self,
            state=next_state,
            attempts=attempts,
            lease_owner=None,
            lease_expires_at=None,
            retry_after=iso(moment + timedelta(seconds=retry_after_seconds)) if next_state == "pending" else None,
            updated_at=iso(moment),
        )
