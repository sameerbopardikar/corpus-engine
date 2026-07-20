#!/usr/bin/env python3
"""Strict loader for evidence-ranked candidate seed bundles.

Seed bundles are metadata inputs only. Loading never promotes candidates, and
``dry_run=True`` never writes the discovery ledger.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from corpus_discovery import DiscoveryEngine
from corpus_engine_models import CandidateObservation, CandidateRecord, ENTITY_TYPES, parse_iso

SEED_SCHEMA_VERSION = 1
SEED_STATUS = "candidate-seed-not-promoted"
_REFRESH_CLASSES = {"fast", "daily", "weekly", "monthly", "frozen", "incident"}
_TOP_LEVEL_FIELDS = {"schema_version", "domain", "status", "generated_at", "policy", "candidates"}
_CANDIDATE_FIELDS = {
    "id",
    "entity_type",
    "canonical_url",
    "source_type",
    "evidence_lane",
    "authority_tier",
    "refresh_class",
    "topics",
    "related_locators",
}
_CANDIDATE_REQUIRED = _CANDIDATE_FIELDS - {"related_locators"}


class CandidateSeedError(ValueError):
    """Raised when candidate seed data is malformed or unsafe to ingest."""


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CandidateSeedError(f"{field_name} must be a non-empty string")
    return value.strip()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CandidateSeedError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise CandidateSeedError(f"non-finite JSON number: {value}")


def _strict_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except CandidateSeedError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateSeedError(f"cannot read strict candidate seed {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CandidateSeedError("candidate seed root must be an object")
    return data


def _validate_locator(value: str, field_name: str) -> str:
    locator = _text(value, field_name)
    parsed = urlsplit(locator)
    if not parsed.scheme or not parsed.hostname:
        raise CandidateSeedError(f"{field_name} must be an absolute URL")
    if parsed.username is not None or parsed.password is not None:
        raise CandidateSeedError(f"{field_name} must not contain credentials")
    return locator


@dataclass(frozen=True)
class SeedCandidate:
    seed_id: str
    entity_type: str
    canonical_url: str
    source_type: str
    evidence_lane: str
    authority_tier: str
    refresh_class: str
    topics: tuple[str, ...]
    related_locators: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SeedCandidate":
        unknown = set(data) - _CANDIDATE_FIELDS
        missing = _CANDIDATE_REQUIRED - set(data)
        if unknown or missing:
            raise CandidateSeedError(f"candidate fields mismatch: missing={missing} unknown={unknown}")
        entity_type = _text(data["entity_type"], "entity_type")
        if entity_type not in ENTITY_TYPES:
            raise CandidateSeedError(f"unknown entity_type: {entity_type!r}")
        refresh_class = _text(data["refresh_class"], "refresh_class")
        if refresh_class not in _REFRESH_CLASSES:
            raise CandidateSeedError(f"unknown refresh_class: {refresh_class!r}")
        topics = data["topics"]
        if not isinstance(topics, list) or not topics:
            raise CandidateSeedError("topics must be a non-empty list")
        normalized_topics = tuple(_text(topic, "topic") for topic in topics)
        if len(normalized_topics) != len(set(normalized_topics)):
            raise CandidateSeedError("topics must be unique")
        related = data.get("related_locators", [])
        if not isinstance(related, list):
            raise CandidateSeedError("related_locators must be a list")
        normalized_related = tuple(
            _validate_locator(locator, "related_locator") for locator in related
        )
        if len(normalized_related) != len(set(normalized_related)):
            raise CandidateSeedError("related_locators must be unique")
        return cls(
            seed_id=_text(data["id"], "candidate id"),
            entity_type=entity_type,
            canonical_url=_validate_locator(data["canonical_url"], "canonical_url"),
            source_type=_text(data["source_type"], "source_type"),
            evidence_lane=_text(data["evidence_lane"], "evidence_lane"),
            authority_tier=_text(data["authority_tier"], "authority_tier"),
            refresh_class=refresh_class,
            topics=normalized_topics,
            related_locators=normalized_related,
        )

    def observation(self, *, domain: str, generated_at: str, source_ref: str) -> CandidateObservation:
        return CandidateObservation.create(
            domain=domain,
            entity_type=self.entity_type,
            canonical_url=self.canonical_url,
            discovery_source=f"candidate_seed:{self.seed_id}",
            evidence_pointer=f"{_text(source_ref, 'source_ref')}#{self.seed_id}",
            evidence_lane=self.evidence_lane,
            topics=self.topics,
            observed_at=generated_at,
        )


@dataclass(frozen=True)
class CandidateSeedBundle:
    schema_version: int
    domain: str
    status: str
    generated_at: str
    policy: str
    candidates: tuple[SeedCandidate, ...]

    def observations(self, *, source_ref: str) -> tuple[CandidateObservation, ...]:
        return tuple(
            candidate.observation(
                domain=self.domain,
                generated_at=self.generated_at,
                source_ref=source_ref,
            )
            for candidate in self.candidates
        )


def load_candidate_seed(path: Path | str) -> CandidateSeedBundle:
    seed_path = Path(path)
    data = _strict_json(seed_path)
    unknown = set(data) - _TOP_LEVEL_FIELDS
    missing = _TOP_LEVEL_FIELDS - set(data)
    if unknown or missing:
        raise CandidateSeedError(f"seed fields mismatch: missing={missing} unknown={unknown}")
    if data["schema_version"] != SEED_SCHEMA_VERSION:
        raise CandidateSeedError(f"unsupported schema_version: {data['schema_version']!r}")
    status = _text(data["status"], "status")
    if status != SEED_STATUS:
        raise CandidateSeedError(f"seed status must remain {SEED_STATUS!r}")
    generated_at = _text(data["generated_at"], "generated_at")
    try:
        parse_iso(generated_at)
    except (TypeError, ValueError) as exc:
        raise CandidateSeedError("generated_at must be an aware ISO timestamp") from exc
    raw_candidates = data["candidates"]
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise CandidateSeedError("candidates must be a non-empty list")
    candidates = tuple(SeedCandidate.from_dict(candidate) for candidate in raw_candidates)
    seed_ids = [candidate.seed_id for candidate in candidates]
    if len(seed_ids) != len(set(seed_ids)):
        raise CandidateSeedError("candidate IDs must be unique")
    bundle = CandidateSeedBundle(
        schema_version=SEED_SCHEMA_VERSION,
        domain=_text(data["domain"], "domain"),
        status=status,
        generated_at=generated_at,
        policy=_text(data["policy"], "policy"),
        candidates=candidates,
    )
    keys = [observation.candidate_key for observation in bundle.observations(source_ref=str(seed_path))]
    if len(keys) != len(set(keys)):
        raise CandidateSeedError("candidate seed contains canonically equivalent locators")
    return bundle


def ingest_candidate_seed(
    engine: DiscoveryEngine,
    bundle: CandidateSeedBundle,
    *,
    source_ref: str,
    dry_run: bool = True,
) -> tuple[CandidateObservation, ...] | tuple[CandidateRecord, ...]:
    observations = bundle.observations(source_ref=source_ref)
    if dry_run:
        return observations
    return tuple(engine.observe(observation) for observation in observations)
