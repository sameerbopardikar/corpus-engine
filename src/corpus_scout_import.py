#!/usr/bin/env python3
"""Bounded, deterministic import of scout terrain-mapping results.

LLM terrain mapping stays entirely behind this boundary: a scout produces a
bounded result document, and this module deterministically validates it and
projects it into a provisional field map plus ranked, rights-gated acquisition
suggestions. Scout provenance and uncertainty are preserved, never adopted. No
LLM call happens here; tests need no model.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from corpus_domain_spec import RIGHTS_STATES, DomainSpec

SCOUT_SCHEMA_VERSION = 1

_TOP_FIELDS = {
    "schema_version", "domain", "scout_id", "model", "generated_at",
    "bounds", "fields", "source_suggestions",
}
_BOUNDS_FIELDS = {"max_fields", "max_sources"}
_FIELD_FIELDS = {"axis", "field", "confidence", "evidence_hint", "topics"}
_SUGGESTION_FIELDS = {"family", "locator", "title", "confidence", "rationale", "rights_hint"}

# Deterministic down-weighting by declared rights posture. Only public_rights_clear
# suggestions may ever be marked shared-corpus eligible.
_RIGHTS_WEIGHT = {
    "public_rights_clear": 1.0,
    "public_metadata_only": 0.7,
    "private_authorized": 0.5,
    "rights_unclear": 0.3,
}
_SHARED_ELIGIBLE_RIGHTS = {"public_rights_clear"}
_HUMAN_GATE_RIGHTS = {"private_authorized", "rights_unclear"}


class ScoutImportError(ValueError):
    """Raised when a scout result is malformed or violates its declared bounds."""


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScoutImportError(f"{field_name} must be a non-empty string")
    return value.strip()


def _kebab(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-"
    if text != text.lower() or any(char not in allowed for char in text) or text.startswith("-") or text.endswith("-") or "--" in text:
        raise ScoutImportError(f"{field_name} must be lowercase kebab-case")
    return text


def _confidence(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScoutImportError(f"{field_name} must be a number in [0, 1]")
    numeric = float(value)
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        raise ScoutImportError(f"{field_name} must be finite")
    if not 0.0 <= numeric <= 1.0:
        raise ScoutImportError(f"{field_name} must be in [0, 1]")
    return numeric


def _locator(value: Any, field_name: str) -> str:
    locator = _text(value, field_name)
    parsed = urlsplit(locator)
    if not parsed.scheme or not parsed.hostname:
        raise ScoutImportError(f"{field_name} must be an absolute URL")
    if parsed.username is not None or parsed.password is not None:
        raise ScoutImportError(f"{field_name} must not contain credentials")
    return locator


def _exact_fields(data: Any, allowed: set[str], name: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ScoutImportError(f"{name} must be an object")
    unknown = set(data) - allowed
    missing = allowed - set(data)
    if unknown or missing:
        raise ScoutImportError(f"{name} fields mismatch: missing={sorted(missing)} unknown={sorted(unknown)}")
    return data


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ScoutImportError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ScoutImportError(f"non-finite JSON number: {value}")


def _strict_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except ScoutImportError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScoutImportError(f"cannot read strict scout result {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ScoutImportError("scout result root must be an object")
    return data


@dataclass(frozen=True)
class ProvisionalField:
    axis: str
    field: str
    confidence: float
    evidence_hint: str
    topics: tuple[str, ...]
    provenance: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "field": self.field,
            "confidence": self.confidence,
            "evidence_hint": self.evidence_hint,
            "topics": list(self.topics),
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True)
class AcquisitionSuggestion:
    family: str
    locator: str
    title: str
    confidence: float
    rationale: str
    rights_hint: str
    rank_score: float
    requires_human_gate: bool
    shared_corpus_eligible: bool
    provenance: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "locator": self.locator,
            "title": self.title,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "rights_hint": self.rights_hint,
            "rank_score": self.rank_score,
            "requires_human_gate": self.requires_human_gate,
            "shared_corpus_eligible": self.shared_corpus_eligible,
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True)
class ScoutProjection:
    domain: str
    provenance: dict[str, str]
    field_map: dict[str, list[ProvisionalField]]
    unmapped_fields: tuple[ProvisionalField, ...]
    ranked_acquisitions: tuple[AcquisitionSuggestion, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCOUT_SCHEMA_VERSION,
            "domain": self.domain,
            "provenance": dict(self.provenance),
            "field_map": {
                axis: [item.to_dict() for item in items]
                for axis, items in self.field_map.items()
            },
            "unmapped_fields": [item.to_dict() for item in self.unmapped_fields],
            "ranked_acquisitions": [item.to_dict() for item in self.ranked_acquisitions],
        }


def _family_priority_weight(spec: DomainSpec, family: str) -> float:
    priority = spec.acquisition_policy["family_priority"]
    size = len(priority)
    if family in priority:
        return (size - priority.index(family)) / size
    return 1.0 / (size + 1)


def import_scout_result(spec: DomainSpec, scout_path: Path | str) -> ScoutProjection:
    data = _exact_fields(_strict_json(Path(scout_path)), _TOP_FIELDS, "scout result")
    if data["schema_version"] != SCOUT_SCHEMA_VERSION:
        raise ScoutImportError(f"unsupported schema_version: {data['schema_version']!r}")
    if _kebab(data["domain"], "domain") != spec.domain:
        raise ScoutImportError(f"scout domain {data['domain']!r} does not match spec domain {spec.domain!r}")

    provenance = {
        "scout_id": _text(data["scout_id"], "scout_id"),
        "model": _text(data["model"], "model"),
        "generated_at": _text(data["generated_at"], "generated_at"),
    }
    bounds = _exact_fields(data["bounds"], _BOUNDS_FIELDS, "bounds")
    max_fields = bounds["max_fields"]
    max_sources = bounds["max_sources"]
    if not isinstance(max_fields, int) or isinstance(max_fields, bool) or max_fields < 0:
        raise ScoutImportError("bounds.max_fields must be a non-negative integer")
    if not isinstance(max_sources, int) or isinstance(max_sources, bool) or max_sources < 0:
        raise ScoutImportError("bounds.max_sources must be a non-negative integer")

    raw_fields = data["fields"]
    raw_sources = data["source_suggestions"]
    if not isinstance(raw_fields, list) or not isinstance(raw_sources, list):
        raise ScoutImportError("fields and source_suggestions must be lists")
    if len(raw_fields) > max_fields:
        raise ScoutImportError(f"scout emitted {len(raw_fields)} fields over declared bound {max_fields}")
    if len(raw_sources) > max_sources:
        raise ScoutImportError(f"scout emitted {len(raw_sources)} sources over declared bound {max_sources}")

    axis_keys = {axis.key for axis in spec.ontology_axes}
    field_map: dict[str, list[ProvisionalField]] = {}
    unmapped: list[ProvisionalField] = []
    for entry in raw_fields:
        fields = _exact_fields(entry, _FIELD_FIELDS, "scout field")
        topics = fields["topics"]
        if not isinstance(topics, list) or not topics:
            raise ScoutImportError("scout field topics must be a non-empty list")
        provisional = ProvisionalField(
            axis=_kebab(fields["axis"], "field axis"),
            field=_kebab(fields["field"], "field name"),
            confidence=_confidence(fields["confidence"], "field confidence"),
            evidence_hint=_text(fields["evidence_hint"], "evidence_hint"),
            topics=tuple(_text(topic, "field topic") for topic in topics),
            provenance=dict(provenance),
        )
        if provisional.axis in axis_keys:
            field_map.setdefault(provisional.axis, []).append(provisional)
        else:
            unmapped.append(provisional)

    declared_families = {family.family for family in spec.source_families}
    suggestions: list[AcquisitionSuggestion] = []
    for entry in raw_sources:
        fields = _exact_fields(entry, _SUGGESTION_FIELDS, "source suggestion")
        family = _kebab(fields["family"], "suggestion family")
        if family not in declared_families:
            raise ScoutImportError(f"scout suggested undeclared source family: {family!r}")
        rights_hint = _text(fields["rights_hint"], "rights_hint")
        if rights_hint not in RIGHTS_STATES:
            raise ScoutImportError(f"unknown rights_hint: {rights_hint!r}")
        confidence = _confidence(fields["confidence"], "suggestion confidence")
        rank_score = round(confidence * _family_priority_weight(spec, family) * _RIGHTS_WEIGHT[rights_hint], 8)
        suggestions.append(
            AcquisitionSuggestion(
                family=family,
                locator=_locator(fields["locator"], "suggestion locator"),
                title=_text(fields["title"], "suggestion title"),
                confidence=confidence,
                rationale=_text(fields["rationale"], "rationale"),
                rights_hint=rights_hint,
                rank_score=rank_score,
                requires_human_gate=rights_hint in _HUMAN_GATE_RIGHTS,
                shared_corpus_eligible=rights_hint in _SHARED_ELIGIBLE_RIGHTS,
                provenance=dict(provenance),
            )
        )
    suggestions.sort(key=lambda item: (-item.rank_score, item.locator))
    max_suggestions = spec.acquisition_policy["max_suggestions"]
    ranked = tuple(suggestions[:max_suggestions])

    return ScoutProjection(
        domain=spec.domain,
        provenance=provenance,
        field_map=field_map,
        unmapped_fields=tuple(unmapped),
        ranked_acquisitions=ranked,
    )
