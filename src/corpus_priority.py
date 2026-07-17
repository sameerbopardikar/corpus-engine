#!/usr/bin/env python3
"""Deterministic, rights-aware priority and budget policy for corpus work."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from corpus_engine_models import CandidateRecord, SCORE_COMPONENTS

_SCHEMA_VERSION = 1
_BUDGET_SCHEMA_VERSION = 1
_ALLOWED_RIGHTS = {
    "public_rights_clear",
    "public_metadata_only",
    "private_authorized",
    "rights_unclear",
    "unknown",
}
_AUTO_ACQUIRE_RIGHTS = {"public_rights_clear"}
_METADATA_RIGHTS = {"public_metadata_only"}
_PRIVATE_RIGHTS = {"private_authorized"}
_POLICY_EVIDENCE_LANES = {
    "scientific-evaluation",
    "security-evaluation",
    "production-reliability",
    "production-architecture",
    "implementation-research",
    "practitioner-implementation",
    "unverified-discovery-signal",
    "default",
}
_POLICY_SCORE_KEYS = set(SCORE_COMPONENTS) - {"cost"}


def _strict_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_strict_json(text: str) -> Any:
    return json.loads(
        text,
        object_pairs_hook=_strict_json_pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON constant: {value}")
        ),
    )


def _finite_number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return result


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _aware(value: datetime, name: str = "now") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_iso(value: str, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    return _aware(parsed, name)


def _iso_seconds(value: datetime) -> str:
    return _aware(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _strict_mapping(value: Any, name: str) -> Mapping[str, float]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{name} must be a non-empty object")
    normalized: dict[str, float] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{name} keys must be non-blank strings")
        normalized[key] = _finite_number(item, f"{name}.{key}")
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class PriorityPolicy:
    schema_version: int
    evidence_lane_weights: Mapping[str, float]
    score_weights: Mapping[str, float]
    max_items_per_cycle: int
    max_deep_acquisitions_per_utc_day: int
    max_llm_tasks_per_cycle: int
    max_llm_tasks_per_utc_day: int
    max_llm_tokens_per_utc_day: int
    max_cost_usd_per_utc_day: float
    retry_base_seconds: int
    retry_max_seconds: int
    retry_max_attempts: int
    starvation_age_boost_per_day: float
    starvation_max_boost: float
    probationary_threshold: float
    promotion_threshold: float
    rejection_threshold: float
    automatic_promotion_enabled: bool

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version!r}")
        object.__setattr__(self, "evidence_lane_weights", _strict_mapping(dict(self.evidence_lane_weights), "evidence_lane_weights"))
        object.__setattr__(self, "score_weights", _strict_mapping(dict(self.score_weights), "score_weights"))
        unknown_lanes = set(self.evidence_lane_weights) - _POLICY_EVIDENCE_LANES
        missing_lanes = _POLICY_EVIDENCE_LANES - set(self.evidence_lane_weights)
        if unknown_lanes:
            raise ValueError(f"unknown evidence_lane_weights: {sorted(unknown_lanes)}")
        if missing_lanes:
            raise ValueError(f"missing evidence_lane_weights: {sorted(missing_lanes)}")
        unknown_scores = set(self.score_weights) - _POLICY_SCORE_KEYS
        missing_scores = _POLICY_SCORE_KEYS - set(self.score_weights)
        if unknown_scores:
            raise ValueError(f"unknown score_weights: {sorted(unknown_scores)}")
        if missing_scores:
            raise ValueError(f"missing score_weights: {sorted(missing_scores)}")
        if "default" not in self.evidence_lane_weights:
            raise ValueError("evidence_lane_weights must include default")
        if self.evidence_lane_weights["default"] <= 0:
            raise ValueError("evidence_lane_weights.default must be positive")
        if sum(self.score_weights.values()) <= 0:
            raise ValueError("score_weights must have a positive total")
        for name in (
            "max_items_per_cycle", "max_deep_acquisitions_per_utc_day",
            "max_llm_tasks_per_cycle", "max_llm_tasks_per_utc_day",
            "max_llm_tokens_per_utc_day", "retry_base_seconds",
            "retry_max_seconds", "retry_max_attempts",
        ):
            object.__setattr__(self, name, _positive_int(getattr(self, name), name))
        object.__setattr__(self, "max_cost_usd_per_utc_day", _finite_number(self.max_cost_usd_per_utc_day, "max_cost_usd_per_utc_day"))
        object.__setattr__(self, "starvation_age_boost_per_day", _finite_number(self.starvation_age_boost_per_day, "starvation_age_boost_per_day"))
        object.__setattr__(self, "starvation_max_boost", _finite_number(self.starvation_max_boost, "starvation_max_boost"))
        for name in ("probationary_threshold", "promotion_threshold", "rejection_threshold"):
            value = _finite_number(getattr(self, name), name)
            if value > 1:
                raise ValueError(f"{name} must be <= 1")
            object.__setattr__(self, name, value)
        if not self.rejection_threshold <= self.probationary_threshold <= self.promotion_threshold:
            raise ValueError("thresholds must be ordered rejection <= probationary <= promotion")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("retry_max_seconds must be >= retry_base_seconds")
        if not isinstance(self.automatic_promotion_enabled, bool):
            raise ValueError("automatic_promotion_enabled must be boolean")

    @classmethod
    def default(cls) -> "PriorityPolicy":
        return cls(
            schema_version=1,
            evidence_lane_weights={
                "scientific-evaluation": 1.15,
                "security-evaluation": 1.12,
                "production-reliability": 1.10,
                "production-architecture": 1.05,
                "implementation-research": 1.00,
                "practitioner-implementation": 0.95,
                "unverified-discovery-signal": 0.55,
                "default": 0.85,
            },
            score_weights={
                "authority": 1.0,
                "demonstrated_practice": 1.1,
                "novelty": 1.0,
                "relevance": 1.3,
                "corroboration": 1.1,
                "production_or_scientific_value": 1.3,
            },
            max_items_per_cycle=3,
            max_deep_acquisitions_per_utc_day=3,
            max_llm_tasks_per_cycle=1,
            max_llm_tasks_per_utc_day=1,
            max_llm_tokens_per_utc_day=50_000,
            max_cost_usd_per_utc_day=1.0,
            retry_base_seconds=300,
            retry_max_seconds=86_400,
            retry_max_attempts=5,
            starvation_age_boost_per_day=0.03,
            starvation_max_boost=0.30,
            probationary_threshold=0.35,
            promotion_threshold=0.65,
            rejection_threshold=0.10,
            automatic_promotion_enabled=False,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence_lane_weights": dict(self.evidence_lane_weights),
            "score_weights": dict(self.score_weights),
            "max_items_per_cycle": self.max_items_per_cycle,
            "max_deep_acquisitions_per_utc_day": self.max_deep_acquisitions_per_utc_day,
            "max_llm_tasks_per_cycle": self.max_llm_tasks_per_cycle,
            "max_llm_tasks_per_utc_day": self.max_llm_tasks_per_utc_day,
            "max_llm_tokens_per_utc_day": self.max_llm_tokens_per_utc_day,
            "max_cost_usd_per_utc_day": self.max_cost_usd_per_utc_day,
            "retry_base_seconds": self.retry_base_seconds,
            "retry_max_seconds": self.retry_max_seconds,
            "retry_max_attempts": self.retry_max_attempts,
            "starvation_age_boost_per_day": self.starvation_age_boost_per_day,
            "starvation_max_boost": self.starvation_max_boost,
            "probationary_threshold": self.probationary_threshold,
            "promotion_threshold": self.promotion_threshold,
            "rejection_threshold": self.rejection_threshold,
            "automatic_promotion_enabled": self.automatic_promotion_enabled,
        }

    @classmethod
    def from_file(cls, path: Path) -> "PriorityPolicy":
        document = _load_strict_json(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("policy must be a JSON object")
        expected = set(cls.default().to_dict())
        unknown = set(document) - expected
        missing = expected - set(document)
        if unknown:
            raise ValueError(f"unknown policy fields: {sorted(unknown)}")
        if missing:
            raise ValueError(f"missing policy fields: {sorted(missing)}")
        return cls(**document)


@dataclass(frozen=True)
class UsageSnapshot:
    deep_acquisitions: int = 0
    llm_tasks: int = 0
    llm_tokens: int = 0
    estimated_cost_usd: float = 0.0

    def __post_init__(self) -> None:
        for name in ("deep_acquisitions", "llm_tasks", "llm_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        object.__setattr__(self, "estimated_cost_usd", _finite_number(self.estimated_cost_usd, "estimated_cost_usd"))


@dataclass(frozen=True)
class CandidateTask:
    candidate: CandidateRecord
    material_delta: bool = True
    requires_llm: bool = False
    estimated_llm_tokens: int = 0
    estimated_cost_usd: float = 0.0
    failure_count: int = 0
    last_attempted_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, CandidateRecord):
            raise ValueError("candidate must be a CandidateRecord")
        if not isinstance(self.material_delta, bool) or not isinstance(self.requires_llm, bool):
            raise ValueError("material_delta and requires_llm must be booleans")
        if isinstance(self.estimated_llm_tokens, bool) or not isinstance(self.estimated_llm_tokens, int) or self.estimated_llm_tokens < 0:
            raise ValueError("estimated_llm_tokens must be a non-negative integer")
        object.__setattr__(self, "estimated_cost_usd", _finite_number(self.estimated_cost_usd, "estimated_cost_usd"))
        if isinstance(self.failure_count, bool) or not isinstance(self.failure_count, int) or self.failure_count < 0:
            raise ValueError("failure_count must be a non-negative integer")
        if self.failure_count and self.last_attempted_at is None:
            raise ValueError("failed work requires last_attempted_at")
        if self.last_attempted_at is not None:
            _parse_iso(self.last_attempted_at, "last_attempted_at")
        if self.requires_llm and self.estimated_llm_tokens <= 0:
            raise ValueError("LLM-bearing work requires a positive token estimate")
        if not self.requires_llm and self.estimated_llm_tokens:
            raise ValueError("estimated_llm_tokens requires requires_llm=true")


@dataclass(frozen=True)
class PriorityDecision:
    candidate_id: str
    canonical_url: str
    action: str
    evidence_score: float
    priority_score: float
    starvation_boost: float
    scheduled: bool
    auto_acquire_eligible: bool
    auto_promote: bool
    suggested_disposition: str
    requires_llm: bool
    estimated_llm_tokens: int
    estimated_cost_usd: float
    retry_after: str | None
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "canonical_url": self.canonical_url,
            "action": self.action,
            "evidence_score": self.evidence_score,
            "priority_score": self.priority_score,
            "starvation_boost": self.starvation_boost,
            "scheduled": self.scheduled,
            "auto_acquire_eligible": self.auto_acquire_eligible,
            "auto_promote": self.auto_promote,
            "suggested_disposition": self.suggested_disposition,
            "requires_llm": self.requires_llm,
            "estimated_llm_tokens": self.estimated_llm_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "retry_after": self.retry_after,
            "reason_codes": list(self.reason_codes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PriorityDecision":
        expected = {
            "candidate_id", "canonical_url", "action", "evidence_score",
            "priority_score", "starvation_boost", "scheduled",
            "auto_acquire_eligible", "auto_promote", "suggested_disposition",
            "requires_llm", "estimated_llm_tokens", "estimated_cost_usd",
            "retry_after", "reason_codes",
        }
        if set(data) != expected or not isinstance(data.get("reason_codes"), list):
            raise ValueError("invalid stored priority decision")
        values = dict(data)
        values["reason_codes"] = tuple(values["reason_codes"])
        return cls(**values)


@dataclass(frozen=True)
class CyclePlan:
    decisions: tuple[PriorityDecision, ...]
    selected: tuple[PriorityDecision, ...]
    deep_acquisitions_scheduled: int
    llm_tasks_scheduled: int
    llm_tokens_scheduled: int
    estimated_cost_usd: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "decisions": [item.to_dict() for item in self.decisions],
            "selected_candidate_ids": [item.candidate_id for item in self.selected],
            "deep_acquisitions_scheduled": self.deep_acquisitions_scheduled,
            "llm_tasks_scheduled": self.llm_tasks_scheduled,
            "llm_tokens_scheduled": self.llm_tokens_scheduled,
            "estimated_cost_usd": self.estimated_cost_usd,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CyclePlan":
        expected = {
            "decisions", "selected_candidate_ids", "deep_acquisitions_scheduled",
            "llm_tasks_scheduled", "llm_tokens_scheduled", "estimated_cost_usd",
        }
        if set(data) != expected or not isinstance(data.get("decisions"), list):
            raise ValueError("invalid stored cycle plan")
        decisions = tuple(PriorityDecision.from_dict(item) for item in data["decisions"])
        selected_ids = data.get("selected_candidate_ids")
        if not isinstance(selected_ids, list):
            raise ValueError("invalid stored selected_candidate_ids")
        by_id = {item.candidate_id: item for item in decisions}
        if len(by_id) != len(decisions) or any(item not in by_id for item in selected_ids):
            raise ValueError("stored cycle plan has invalid candidate identities")
        selected = tuple(by_id[item] for item in selected_ids)
        expected_selected = tuple(item for item in decisions if item.scheduled)
        expected_ids = [item.candidate_id for item in expected_selected]
        for item in decisions:
            if (
                type(item.scheduled) is not bool
                or type(item.requires_llm) is not bool
                or item.action not in {"acquire", "inspect"}
                or isinstance(item.estimated_llm_tokens, bool)
                or not isinstance(item.estimated_llm_tokens, int)
                or item.estimated_llm_tokens < 0
                or isinstance(item.estimated_cost_usd, bool)
                or not isinstance(item.estimated_cost_usd, (int, float))
                or not math.isfinite(float(item.estimated_cost_usd))
                or item.estimated_cost_usd < 0
            ):
                raise ValueError("invalid stored priority decision accounting")
        expected_deep = sum(item.action == "acquire" for item in expected_selected)
        expected_llm_tasks = sum(item.requires_llm for item in expected_selected)
        expected_tokens = sum(
            item.estimated_llm_tokens for item in expected_selected if item.requires_llm
        )
        expected_cost = round(sum(item.estimated_cost_usd for item in expected_selected), 8)
        if (
            selected_ids != expected_ids
            or data["deep_acquisitions_scheduled"] != expected_deep
            or data["llm_tasks_scheduled"] != expected_llm_tasks
            or data["llm_tokens_scheduled"] != expected_tokens
            or data["estimated_cost_usd"] != expected_cost
        ):
            raise ValueError("inconsistent stored cycle plan accounting")
        return cls(
            decisions=decisions,
            selected=selected,
            deep_acquisitions_scheduled=data["deep_acquisitions_scheduled"],
            llm_tasks_scheduled=data["llm_tasks_scheduled"],
            llm_tokens_scheduled=data["llm_tokens_scheduled"],
            estimated_cost_usd=data["estimated_cost_usd"],
        )


def _score(task: CandidateTask, policy: PriorityPolicy, now: datetime) -> tuple[float, float, float]:
    record = task.candidate
    weighted = 0.0
    weight_total = 0.0
    for component, weight in policy.score_weights.items():
        weighted += record.score_components.get(component, 0.0) * weight
        weight_total += weight
    quality = weighted / weight_total
    lane_weight = policy.evidence_lane_weights.get(record.evidence_lane, policy.evidence_lane_weights["default"])
    evidence_score = quality * lane_weight
    age_days = max((_aware(now) - _parse_iso(record.first_seen_at, "first_seen_at")).total_seconds() / 86_400, 0.0)
    starvation = min(age_days * policy.starvation_age_boost_per_day, policy.starvation_max_boost)
    cost_factor = 1.0 + task.estimated_cost_usd
    return (evidence_score + starvation) / cost_factor, evidence_score, starvation


def _disposition(score: float, policy: PriorityPolicy) -> str:
    if score < policy.rejection_threshold:
        return "reject"
    if score >= policy.promotion_threshold:
        return "promotion_eligible"
    if score >= policy.probationary_threshold:
        return "probationary"
    return "inspect_only"


def _retry_after(task: CandidateTask, policy: PriorityPolicy) -> datetime | None:
    if task.failure_count == 0:
        return None
    if task.failure_count >= policy.retry_max_attempts:
        return datetime.max.replace(tzinfo=timezone.utc)
    delay = min(policy.retry_base_seconds * (2 ** (task.failure_count - 1)), policy.retry_max_seconds)
    assert task.last_attempted_at is not None
    return _parse_iso(task.last_attempted_at, "last_attempted_at") + timedelta(seconds=delay)


def _retry_ready(task: CandidateTask, policy: PriorityPolicy, now: datetime) -> bool:
    retry_at = _retry_after(task, policy)
    return retry_at is None or now >= retry_at


def plan_cycle(
    tasks: Iterable[CandidateTask],
    policy: PriorityPolicy,
    usage: UsageSnapshot,
    *,
    now: datetime,
) -> CyclePlan:
    current = _aware(now)
    ranked: list[tuple[float, float, float, CandidateTask]] = []
    seen: set[str] = set()
    for task in tasks:
        candidate_id = task.candidate.candidate_id
        if candidate_id in seen:
            raise ValueError(f"duplicate candidate task: {candidate_id}")
        seen.add(candidate_id)
        priority_score, evidence_score, starvation = _score(task, policy, current)
        ranked.append((priority_score, evidence_score, starvation, task))
    forced_candidates = [
        row for row in ranked
        if row[2] >= policy.starvation_max_boost
        and row[3].material_delta
        and row[3].candidate.rights_state in (_AUTO_ACQUIRE_RIGHTS | _METADATA_RIGHTS)
        and _retry_ready(row[3], policy, current)
    ]
    forced_id = None
    if forced_candidates:
        forced_id = min(
            forced_candidates,
            key=lambda row: (
                _parse_iso(row[3].candidate.first_seen_at, "first_seen_at"),
                row[3].candidate.candidate_id,
            ),
        )[3].candidate.candidate_id
    ranked.sort(
        key=lambda row: (
            0 if row[3].candidate.candidate_id == forced_id else 1,
            -row[0],
            row[3].candidate.candidate_id,
        )
    )

    decisions: list[PriorityDecision] = []
    selected_count = 0
    deep_count = 0
    llm_count = 0
    token_count = 0
    cost_total = 0.0

    for raw_score, evidence_score, starvation, task in ranked:
        record = task.candidate
        rights = record.rights_state
        if rights not in _ALLOWED_RIGHTS:
            rights = "unknown"
        auto_acquire = rights in _AUTO_ACQUIRE_RIGHTS
        action = "acquire" if auto_acquire else "inspect"
        reasons: list[str] = []
        retry_at = _retry_after(task, policy)

        if not task.material_delta:
            reasons.append("no_material_delta")
        if rights in _PRIVATE_RIGHTS:
            reasons.extend(("private_source_human_gate", "rights_not_publicly_acquirable"))
        elif rights not in _AUTO_ACQUIRE_RIGHTS and rights not in _METADATA_RIGHTS:
            reasons.append("rights_not_publicly_acquirable")
        if retry_at == datetime.max.replace(tzinfo=timezone.utc):
            reasons.append("retry_attempts_exhausted")
        elif retry_at is not None and current < retry_at:
            reasons.append("retry_backoff_active")

        hard_block = bool(reasons)
        if not hard_block and selected_count >= policy.max_items_per_cycle:
            reasons.append("per_cycle_item_cap")
        if not hard_block and action == "acquire" and usage.deep_acquisitions + deep_count >= policy.max_deep_acquisitions_per_utc_day:
            reasons.append("daily_deep_acquisition_cap")
        if not hard_block and task.requires_llm:
            if llm_count >= policy.max_llm_tasks_per_cycle:
                reasons.append("per_cycle_llm_task_cap")
            elif usage.llm_tasks + llm_count >= policy.max_llm_tasks_per_utc_day:
                reasons.append("daily_llm_task_cap")
            elif usage.llm_tokens + token_count + task.estimated_llm_tokens > policy.max_llm_tokens_per_utc_day:
                reasons.append("daily_llm_token_cap")
        if not hard_block and usage.estimated_cost_usd + cost_total + task.estimated_cost_usd > policy.max_cost_usd_per_utc_day:
            reasons.append("daily_cost_cap")

        scheduled = not reasons
        if scheduled and record.candidate_id == forced_id:
            reasons.append("reserved_aging_slot")
        disposition = _disposition(evidence_score, policy)
        auto_promote = bool(
            scheduled
            and policy.automatic_promotion_enabled
            and auto_acquire
            and disposition == "promotion_eligible"
        )
        if disposition == "promotion_eligible" and not policy.automatic_promotion_enabled:
            reasons.append("automatic_promotion_disabled")
        if scheduled:
            selected_count += 1
            if action == "acquire":
                deep_count += 1
            if task.requires_llm:
                llm_count += 1
                token_count += task.estimated_llm_tokens
            cost_total += task.estimated_cost_usd
        decision = PriorityDecision(
            candidate_id=record.candidate_id,
            canonical_url=record.canonical_url,
            action=action,
            evidence_score=round(evidence_score, 8),
            priority_score=round(raw_score, 8),
            starvation_boost=round(starvation, 8),
            scheduled=scheduled,
            auto_acquire_eligible=auto_acquire,
            auto_promote=auto_promote,
            suggested_disposition=disposition,
            requires_llm=task.requires_llm,
            estimated_llm_tokens=task.estimated_llm_tokens,
            estimated_cost_usd=task.estimated_cost_usd,
            retry_after=None if retry_at is None or retry_at.year == datetime.max.year else _iso_seconds(retry_at),
            reason_codes=tuple(reasons),
        )
        decisions.append(decision)

    selected = tuple(item for item in decisions if item.scheduled)
    return CyclePlan(
        decisions=tuple(decisions),
        selected=selected,
        deep_acquisitions_scheduled=deep_count,
        llm_tasks_scheduled=llm_count,
        llm_tokens_scheduled=token_count,
        estimated_cost_usd=round(cost_total, 8),
    )


def _task_payload(task: CandidateTask) -> dict[str, Any]:
    return {
        "candidate": task.candidate.to_dict(),
        "material_delta": task.material_delta,
        "requires_llm": task.requires_llm,
        "estimated_llm_tokens": task.estimated_llm_tokens,
        "estimated_cost_usd": task.estimated_cost_usd,
        "failure_count": task.failure_count,
        "last_attempted_at": task.last_attempted_at,
    }


def _task_from_payload(data: Any) -> CandidateTask:
    expected = {
        "candidate", "material_delta", "requires_llm", "estimated_llm_tokens",
        "estimated_cost_usd", "failure_count", "last_attempted_at",
    }
    if not isinstance(data, dict) or set(data) != expected or not isinstance(data.get("candidate"), dict):
        raise ValueError("invalid stored candidate task")
    return CandidateTask(
        candidate=CandidateRecord.from_dict(data["candidate"]),
        material_delta=data["material_delta"],
        requires_llm=data["requires_llm"],
        estimated_llm_tokens=data["estimated_llm_tokens"],
        estimated_cost_usd=data["estimated_cost_usd"],
        failure_count=data["failure_count"],
        last_attempted_at=data["last_attempted_at"],
    )


def _open_private_regular(path: Path, flags: int) -> int:
    descriptor = os.open(
        path,
        flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        descriptor_stat = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_stat.st_mode):
            raise ValueError(f"path is not a regular file: {path}")
        if stat.S_IMODE(descriptor_stat.st_mode) != 0o600:
            os.fchmod(descriptor, 0o600)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


class BudgetLedger:
    """Atomic daily reservation authority for bounded corpus work."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_fd = _open_private_regular(self.lock_path, os.O_CREAT | os.O_RDWR)
        os.close(lock_fd)
        if self.path.is_symlink():
            raise ValueError(f"budget ledger path may not be a symlink: {self.path}")

    def _read_state(self) -> dict[str, Any]:
        if self.path.is_symlink():
            raise ValueError(f"budget ledger path may not be a symlink: {self.path}")
        if not self.path.exists():
            return {"schema_version": _BUDGET_SCHEMA_VERSION, "reservations": {}}
        descriptor = _open_private_regular(self.path, os.O_RDONLY)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            document = _load_strict_json(handle.read())
        if (
            not isinstance(document, dict)
            or set(document) != {"schema_version", "reservations"}
            or document.get("schema_version") != _BUDGET_SCHEMA_VERSION
            or not isinstance(document.get("reservations"), dict)
        ):
            raise ValueError("invalid budget ledger state")
        ordered_reservations: list[tuple[int, str, dict[str, Any]]] = []
        for reservation_id, reservation in document["reservations"].items():
            if (
                not isinstance(reservation_id, str)
                or not reservation_id.strip()
                or not isinstance(reservation, dict)
                or set(reservation) != {"request_sha256", "request", "utc_day", "sequence", "plan"}
                or not isinstance(reservation["request_sha256"], str)
                or not isinstance(reservation["request"], dict)
                or not isinstance(reservation["utc_day"], str)
                or isinstance(reservation["sequence"], bool)
                or not isinstance(reservation["sequence"], int)
                or reservation["sequence"] < 0
                or not isinstance(reservation["plan"], dict)
            ):
                raise ValueError("invalid budget reservation")
            CyclePlan.from_dict(reservation["plan"])
            ordered_reservations.append((reservation["sequence"], reservation_id, reservation))
        ordered_reservations.sort()
        if [item[0] for item in ordered_reservations] != list(range(len(ordered_reservations))):
            raise ValueError("invalid budget reservation sequence")
        replay_usage: dict[str, UsageSnapshot] = {}
        for _, reservation_id, reservation in ordered_reservations:
            request = reservation["request"]
            request_bytes = json.dumps(
                request, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            if hashlib.sha256(request_bytes).hexdigest() != reservation["request_sha256"]:
                raise ValueError("budget reservation request digest mismatch")
            if set(request) != {"reservation_id", "now", "policy", "tasks"}:
                raise ValueError("invalid stored budget request")
            if request["reservation_id"] != reservation_id:
                raise ValueError("stored reservation identity mismatch")
            try:
                policy = PriorityPolicy(**request["policy"])
                tasks = tuple(_task_from_payload(item) for item in request["tasks"])
                requested_at = _parse_iso(request["now"], "stored request now")
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("invalid stored budget request") from exc
            utc_day = requested_at.date().isoformat()
            if reservation["utc_day"] != utc_day:
                raise ValueError("stored reservation UTC day mismatch")
            prior_usage = replay_usage.get(utc_day, UsageSnapshot())
            expected_plan = plan_cycle(tasks, policy, prior_usage, now=requested_at)
            stored_plan = CyclePlan.from_dict(reservation["plan"])
            if stored_plan.to_dict() != expected_plan.to_dict():
                raise ValueError("stored reservation plan does not match request")
            replay_usage[utc_day] = UsageSnapshot(
                deep_acquisitions=prior_usage.deep_acquisitions + expected_plan.deep_acquisitions_scheduled,
                llm_tasks=prior_usage.llm_tasks + expected_plan.llm_tasks_scheduled,
                llm_tokens=prior_usage.llm_tokens + expected_plan.llm_tokens_scheduled,
                estimated_cost_usd=round(prior_usage.estimated_cost_usd + expected_plan.estimated_cost_usd, 8),
            )
        return document

    def _write_state(self, state: Mapping[str, Any]) -> None:
        if self.path.is_symlink():
            raise ValueError(f"budget ledger path may not be a symlink: {self.path}")
        encoded = (
            json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        ).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            readback_fd = _open_private_regular(self.path, os.O_RDONLY)
            with os.fdopen(readback_fd, "rb") as handle:
                if handle.read() != encoded:
                    raise OSError("budget ledger readback mismatch")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _usage(state: Mapping[str, Any], utc_day: str) -> UsageSnapshot:
        deep = llm_tasks = llm_tokens = 0
        cost = 0.0
        for reservation in state["reservations"].values():
            if reservation["utc_day"] != utc_day:
                continue
            plan = CyclePlan.from_dict(reservation["plan"])
            deep += plan.deep_acquisitions_scheduled
            llm_tasks += plan.llm_tasks_scheduled
            llm_tokens += plan.llm_tokens_scheduled
            cost += plan.estimated_cost_usd
        return UsageSnapshot(
            deep_acquisitions=deep,
            llm_tasks=llm_tasks,
            llm_tokens=llm_tokens,
            estimated_cost_usd=round(cost, 8),
        )

    def usage_for_day(self, day: datetime) -> UsageSnapshot:
        current = _aware(day)
        lock_fd = _open_private_regular(self.lock_path, os.O_RDWR)
        with os.fdopen(lock_fd, "r+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
            try:
                return self._usage(self._read_state(), current.date().isoformat())
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def reserve_cycle(
        self,
        reservation_id: str,
        tasks: Iterable[CandidateTask],
        policy: PriorityPolicy,
        *,
        now: datetime,
    ) -> CyclePlan:
        if not isinstance(reservation_id, str) or not reservation_id.strip():
            raise ValueError("reservation_id must be a non-blank string")
        current = _aware(now)
        task_list = tuple(tasks)
        request = {
            "reservation_id": reservation_id,
            "now": _iso_seconds(current),
            "policy": policy.to_dict(),
            "tasks": sorted(
                (_task_payload(task) for task in task_list),
                key=lambda item: item["candidate"]["candidate_id"],
            ),
        }
        request_bytes = json.dumps(
            request, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        request_sha256 = hashlib.sha256(request_bytes).hexdigest()
        lock_fd = _open_private_regular(self.lock_path, os.O_RDWR)
        with os.fdopen(lock_fd, "r+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                state = self._read_state()
                prior = state["reservations"].get(reservation_id)
                if prior is not None:
                    if prior["request_sha256"] != request_sha256:
                        raise ValueError(
                            f"conflicting reservation_id reuse: {reservation_id}"
                        )
                    return CyclePlan.from_dict(prior["plan"])
                utc_day = current.date().isoformat()
                usage = self._usage(state, utc_day)
                plan = plan_cycle(task_list, policy, usage, now=current)
                state["reservations"][reservation_id] = {
                    "request_sha256": request_sha256,
                    "request": request,
                    "utc_day": utc_day,
                    "sequence": len(state["reservations"]),
                    "plan": plan.to_dict(),
                }
                self._write_state(state)
                return plan
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
