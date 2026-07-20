#!/usr/bin/env python3
"""Versioned, applied-feedback profiles for the generalized Corpus Engine.

A :class:`FeedbackProfile` is a strict, versioned contract that governs how a
domain's candidates and outcomes are *scored* — nothing more. It carries a
profile id/version, the decisions it targets, weighted score dimensions,
applicability constraints, outcome metrics (each scoped shared or private), a
time horizon, known confounders, an explicit privacy/export policy, minimum
evidence rules, and regression thresholds. Every ranking or decision made under
a profile binds the profile id, version, and content digest, so a historical
receipt stays attributable to the exact profile revision that produced it.

Feedback is applied belief about *ranking*, never authority over rights or
doctrine: a feedback event may not carry a rights classification or a doctrine
adoption flag, and private-scope outcomes never enter a shared projection.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# A feedback event must never smuggle a rights classification or a doctrine
# adoption decision; those are governed by their own audited subsystems.
_FORBIDDEN_EVENT_KEYS = {
    "rights_state", "sameer_adopted", "adoption_state", "holder_id",
    "doctrine_promoted", "promotion",
}

_TOP_FIELDS = {
    "schema_version", "profile_id", "version", "domain", "title",
    "target_decisions", "score_dimensions", "applicability", "outcome_metrics",
    "time_horizon_days", "confounders", "privacy", "min_evidence",
    "regression_thresholds",
}
_APPLICABILITY_FIELDS = {"evidence_lanes_allow", "evidence_lanes_deny", "rights_allow"}
_METRIC_FIELDS = {"key", "direction", "scope"}
_PRIVACY_FIELDS = {"export_scope", "forbid_private_outcome_export"}
_MIN_EVIDENCE_FIELDS = {"min_corroborations", "min_distinct_sources"}
_REGRESSION_FIELDS = {"max_outcome_regression"}
_METRIC_DIRECTIONS = {"maximize", "minimize"}
_METRIC_SCOPES = {"shared", "private"}
_EXPORT_SCOPES = {"shared", "private"}


class FeedbackProfileError(ValueError):
    """Raised when a feedback profile or event is malformed or unsafe."""


class FeedbackConflictError(FeedbackProfileError):
    """Raised when a feedback event id is reused with different content."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FeedbackProfileError(f"{field} must be a non-empty string")
    return value.strip()


def _kebab(value: Any, field: str) -> str:
    text = _text(value, field)
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-"
    if text != text.lower() or any(ch not in allowed for ch in text):
        raise FeedbackProfileError(f"{field} must be lowercase kebab-case")
    if text.startswith("-") or text.endswith("-") or "--" in text:
        raise FeedbackProfileError(f"{field} must be lowercase kebab-case")
    return text


def _int(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FeedbackProfileError(f"{field} must be an integer")
    if value < minimum:
        raise FeedbackProfileError(f"{field} must be >= {minimum}")
    return value


def _number(value: Any, field: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FeedbackProfileError(f"{field} must be a finite number")
    numeric = float(value)
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        raise FeedbackProfileError(f"{field} must be a finite number")
    if minimum is not None and numeric < minimum:
        raise FeedbackProfileError(f"{field} must be >= {minimum}")
    return numeric


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise FeedbackProfileError(f"{field} must be a boolean")
    return value


def _exact(data: Any, allowed: set[str], name: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise FeedbackProfileError(f"{name} must be an object")
    unknown = set(data) - allowed
    missing = allowed - set(data)
    if unknown or missing:
        raise FeedbackProfileError(f"{name} fields mismatch: missing={sorted(missing)} unknown={sorted(unknown)}")
    return data


def _str_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise FeedbackProfileError(f"{field} must be a list")
    return [_text(item, f"{field} item") for item in value]


@dataclass(frozen=True)
class FeedbackProfile:
    schema_version: int
    profile_id: str
    version: int
    domain: str
    title: str
    target_decisions: tuple[str, ...]
    score_dimensions: dict[str, float]
    applicability: dict[str, tuple[str, ...]]
    outcome_metrics: tuple[dict[str, str], ...]
    time_horizon_days: int
    confounders: tuple[str, ...]
    privacy: dict[str, Any]
    min_evidence: dict[str, int]
    regression_thresholds: dict[str, float]

    @property
    def shared_metric_keys(self) -> tuple[str, ...]:
        return tuple(m["key"] for m in self.outcome_metrics if m["scope"] == "shared")

    @property
    def private_metric_keys(self) -> tuple[str, ...]:
        return tuple(m["key"] for m in self.outcome_metrics if m["scope"] == "private")


def _identity(profile: FeedbackProfile) -> dict[str, Any]:
    return {
        "schema_version": profile.schema_version,
        "profile_id": profile.profile_id,
        "version": profile.version,
        "domain": profile.domain,
        "title": profile.title,
        "target_decisions": list(profile.target_decisions),
        "score_dimensions": profile.score_dimensions,
        "applicability": {k: list(v) for k, v in profile.applicability.items()},
        "outcome_metrics": [dict(m) for m in profile.outcome_metrics],
        "time_horizon_days": profile.time_horizon_days,
        "confounders": list(profile.confounders),
        "privacy": profile.privacy,
        "min_evidence": profile.min_evidence,
        "regression_thresholds": profile.regression_thresholds,
    }


def profile_digest(profile: FeedbackProfile) -> str:
    blob = json.dumps(_identity(profile), sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def profile_binding(profile: FeedbackProfile) -> dict[str, Any]:
    """The (id, version, digest) triple every decision/receipt must carry."""
    return {
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "profile_digest": profile_digest(profile),
    }


def parse_feedback_profile(data: Any) -> FeedbackProfile:
    fields = _exact(data, _TOP_FIELDS, "feedback profile")
    if fields["schema_version"] != SCHEMA_VERSION:
        raise FeedbackProfileError(f"unsupported schema_version: {fields['schema_version']!r}")

    raw_dimensions = fields["score_dimensions"]
    if not isinstance(raw_dimensions, dict) or not raw_dimensions:
        raise FeedbackProfileError("score_dimensions must be a non-empty object")
    dimensions = {_text(dim, "score dimension"): _number(weight, f"score_dimensions[{dim}]", minimum=0.0)
                  for dim, weight in raw_dimensions.items()}

    applic = _exact(fields["applicability"], _APPLICABILITY_FIELDS, "applicability")
    applicability = {name: tuple(_str_list(applic[name], f"applicability.{name}")) for name in sorted(_APPLICABILITY_FIELDS)}

    raw_metrics = fields["outcome_metrics"]
    if not isinstance(raw_metrics, list) or not raw_metrics:
        raise FeedbackProfileError("outcome_metrics must be a non-empty list")
    metrics: list[dict[str, str]] = []
    for entry in raw_metrics:
        metric = _exact(entry, _METRIC_FIELDS, "outcome metric")
        direction = _text(metric["direction"], "outcome metric direction")
        scope = _text(metric["scope"], "outcome metric scope")
        if direction not in _METRIC_DIRECTIONS:
            raise FeedbackProfileError(f"unknown outcome metric direction: {direction!r}")
        if scope not in _METRIC_SCOPES:
            raise FeedbackProfileError(f"unknown outcome metric scope: {scope!r}")
        metrics.append({"key": _text(metric["key"], "outcome metric key"), "direction": direction, "scope": scope})
    if len({m["key"] for m in metrics}) != len(metrics):
        raise FeedbackProfileError("outcome metric keys must be unique")

    privacy_raw = _exact(fields["privacy"], _PRIVACY_FIELDS, "privacy")
    export_scope = _text(privacy_raw["export_scope"], "privacy.export_scope")
    if export_scope not in _EXPORT_SCOPES:
        raise FeedbackProfileError(f"unknown privacy.export_scope: {export_scope!r}")
    forbid_private_export = _bool(
        privacy_raw["forbid_private_outcome_export"],
        "privacy.forbid_private_outcome_export",
    )
    if export_scope == "shared" and not forbid_private_export:
        raise FeedbackProfileError(
            "shared export_scope requires forbid_private_outcome_export=true"
        )
    privacy = {
        "export_scope": export_scope,
        "forbid_private_outcome_export": forbid_private_export,
    }

    min_evidence_raw = _exact(fields["min_evidence"], _MIN_EVIDENCE_FIELDS, "min_evidence")
    min_evidence = {name: _int(min_evidence_raw[name], f"min_evidence.{name}", minimum=0) for name in sorted(_MIN_EVIDENCE_FIELDS)}

    regression_raw = _exact(fields["regression_thresholds"], _REGRESSION_FIELDS, "regression_thresholds")
    regression = {"max_outcome_regression": _number(regression_raw["max_outcome_regression"], "regression_thresholds.max_outcome_regression", minimum=0.0)}

    target_decisions = _str_list(fields["target_decisions"], "target_decisions")
    if not target_decisions:
        raise FeedbackProfileError("target_decisions must be non-empty")

    return FeedbackProfile(
        schema_version=SCHEMA_VERSION,
        profile_id=_kebab(fields["profile_id"], "profile_id"),
        version=_int(fields["version"], "version", minimum=1),
        domain=_kebab(fields["domain"], "domain"),
        title=_text(fields["title"], "title"),
        target_decisions=tuple(target_decisions),
        score_dimensions=dimensions,
        applicability=applicability,
        outcome_metrics=tuple(metrics),
        time_horizon_days=_int(fields["time_horizon_days"], "time_horizon_days", minimum=1),
        confounders=tuple(_str_list(fields["confounders"], "confounders")),
        privacy=privacy,
        min_evidence=min_evidence,
        regression_thresholds=regression,
    )


def load_feedback_profile(path: Path | str) -> FeedbackProfile:
    profile_path = Path(path)
    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FeedbackProfileError(f"cannot read feedback profile {profile_path}: {exc}") from exc
    return parse_feedback_profile(data)


def rank_candidates(profile: FeedbackProfile, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank candidates by the profile's weighted score dimensions.

    Returns a new list (never mutating inputs) whose order can differ between
    profiles. Each item binds the profile so the decision is attributable.
    """
    binding = profile_binding(profile)
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = _text(candidate.get("candidate_id"), "candidate_id")
        dimensions = candidate.get("dimensions", {})
        if not isinstance(dimensions, dict):
            raise FeedbackProfileError("candidate dimensions must be an object")
        score = 0.0
        for dim, weight in profile.score_dimensions.items():
            value = dimensions.get(dim, 0.0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FeedbackProfileError(f"candidate dimension {dim} must be numeric")
            score += weight * float(value)
        ranked.append({"candidate_id": candidate_id, "score": round(score, 8), **binding})
    ranked.sort(key=lambda item: (-item["score"], item["candidate_id"]))
    return ranked


def shared_outcome_projection(profile: FeedbackProfile, outcomes: dict[str, Any]) -> dict[str, Any]:
    """Project only shared-scope outcome metrics; private outcomes never leak."""
    private = set(profile.private_metric_keys)
    return {key: value for key, value in outcomes.items() if key not in private}


def _canonical_event(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


class FeedbackLedger:
    """Append-only, deduplicating feedback event ledger with provenance."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            self.path.touch(mode=0o600)
        self.events: list[dict[str, Any]] = []
        self._by_id: dict[str, str] = {}  # event_id -> content digest
        self._replay()

    def _replay(self) -> None:
        self.events = []
        self._by_id = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            self.events.append(event)
            self._by_id[event["event_id"]] = event["content_digest"]

    def record(self, profile: FeedbackProfile, event: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(event, dict):
            raise FeedbackProfileError("feedback event must be an object")
        forbidden = _FORBIDDEN_EVENT_KEYS & set(event)
        if forbidden:
            raise FeedbackProfileError(f"feedback event may not carry rights/doctrine keys: {sorted(forbidden)}")
        event_id = _text(event.get("event_id"), "event_id")
        target = _text(event.get("target_decision"), "target_decision")
        if target not in profile.target_decisions:
            raise FeedbackProfileError(f"target_decision {target!r} not in profile targets")
        provenance = event.get("provenance")
        if not isinstance(provenance, dict) or not provenance.get("source"):
            raise FeedbackProfileError("feedback event requires provenance.source")

        binding = profile_binding(profile)
        stored = {**event, **binding}
        content_digest = hashlib.sha256(_canonical_event(stored)).hexdigest()
        stored["content_digest"] = content_digest

        if event_id in self._by_id:
            if self._by_id[event_id] != content_digest:
                raise FeedbackConflictError(f"feedback event {event_id!r} reused with different content")
            return next(e for e in self.events if e["event_id"] == event_id)  # converge

        encoded = json.dumps(stored, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        with open(self.path, "a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self.events.append(stored)
        self._by_id[event_id] = content_digest
        return stored
