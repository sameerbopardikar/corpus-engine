#!/usr/bin/env python3
"""Deterministic candidate disposition and shadow watch projection.

This policy operates on the shared discovery ledger and source graph. It may
change candidate lifecycle state, but it never changes rights and writes only a
caller-selected watch projection. Production registry mutation remains a
separate reviewed operation.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from corpus_discovery import DiscoveryEngine
from corpus_source_graph import SourceRelationship


@dataclass(frozen=True, slots=True)
class CandidatePolicy:
    probation_quality: float = 0.45
    promotion_quality: float = 0.58
    rejection_quality: float = 0.20
    promotion_occurrences: int = 2
    promotion_source_families: int = 2
    promotion_evidence_pointers: int = 2

    def __post_init__(self) -> None:
        for name in ("probation_quality", "promotion_quality", "rejection_quality"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0, 1]")
        if not self.rejection_quality < self.probation_quality < self.promotion_quality:
            raise ValueError("quality thresholds must increase from rejection to promotion")
        for name in (
            "promotion_occurrences", "promotion_source_families", "promotion_evidence_pointers"
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


_LANE_AUTHORITY = {
    "canonical": 0.90,
    "foundational": 0.90,
    "scientific": 0.85,
    "scientific-evaluation": 0.85,
    "security-evaluation": 0.85,
    "production-reliability": 0.80,
    "production": 0.75,
    "production-architecture": 0.75,
    "production-deployment": 0.75,
    "implementation-research": 0.75,
    "practitioner-implementation": 0.65,
    "practitioner": 0.55,
    "zeitgeist": 0.35,
    "unverified-discovery-signal": 0.30,
}


def _read_relationships(path: Path) -> list[SourceRelationship]:
    if not path.exists():
        return []
    relationships: list[SourceRelationship] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid source graph JSON at line {line_number}: {exc}") from exc
        if value.get("schema_version") != 1:
            raise ValueError(f"invalid source graph schema at line {line_number}")
        payload = {
            key: item for key, item in value.items()
            if key not in {"schema_version", "relation_id"}
        }
        relationship = SourceRelationship.from_dict(payload)
        if value.get("relation_id") != relationship.relation_id:
            raise ValueError(f"source graph identity mismatch at line {line_number}")
        relationships.append(relationship)
    return relationships


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
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


def _quality(candidate, relationships: list[SourceRelationship]) -> tuple[float, dict[str, Any]]:
    families = sorted({relationship.source_family for relationship in relationships})
    pointers = sorted({relationship.evidence_pointer for relationship in relationships})
    lanes = sorted({relationship.evidence_lane for relationship in relationships})
    scores = dict(candidate.score_components)
    authority = max(
        [scores["authority"]]
        + [_LANE_AUTHORITY.get(lane.strip().lower(), 0.40) for lane in lanes]
    )
    independent_corroboration = min(
        0.2 + 0.2 * max(0, len(families) - 1) + 0.05 * max(0, len(pointers) - 1),
        1.0,
    )
    corroboration = max(scores["corroboration"], independent_corroboration)
    quality = (
        0.22 * authority
        + 0.18 * scores["demonstrated_practice"]
        + 0.14 * scores["novelty"]
        + 0.20 * scores["relevance"]
        + 0.12 * corroboration
        + 0.20 * scores["production_or_scientific_value"]
        - 0.06 * scores["cost"]
    )
    quality = round(max(0.0, min(1.0, quality)), 6)
    return quality, {
        "independent_source_families": len(families),
        "source_families": families,
        "independent_evidence_pointers": len(pointers),
        "evidence_lanes": lanes,
        "authority_used": authority,
        "corroboration_used": corroboration,
    }


def evaluate_candidates(
    *,
    ledger_path: Path,
    graph_path: Path,
    watch_projection_path: Path,
    evaluated_at: str,
    policy: CandidatePolicy | None = None,
) -> dict[str, Any]:
    policy = policy or CandidatePolicy()
    engine = DiscoveryEngine(Path(ledger_path))
    by_url: dict[str, list[SourceRelationship]] = {}
    for relationship in _read_relationships(Path(graph_path)):
        by_url.setdefault(relationship.canonical_url, []).append(relationship)

    candidate_ids = sorted(engine.candidates)
    for candidate_id in candidate_ids:
        candidate = engine.candidates[candidate_id]
        quality, evidence = _quality(candidate, by_url.get(candidate.canonical_url, []))
        if candidate.status == "discovered":
            if candidate.occurrences >= 2 and quality <= policy.rejection_quality:
                engine.transition_candidate(
                    candidate_id,
                    "rejected",
                    rejection_reason=(
                        f"quality {quality:.3f} is at or below rejection threshold "
                        f"{policy.rejection_quality:.3f} after {candidate.occurrences} observations"
                    ),
                )
            elif quality >= policy.probation_quality:
                engine.transition_candidate(candidate_id, "probationary")
        candidate = engine.candidates[candidate_id]
        if (
            candidate.status == "probationary"
            and quality >= policy.promotion_quality
            and candidate.occurrences >= policy.promotion_occurrences
            and evidence["independent_source_families"] >= policy.promotion_source_families
            and evidence["independent_evidence_pointers"] >= policy.promotion_evidence_pointers
        ):
            engine.transition_candidate(candidate_id, "promoted")

    engine = DiscoveryEngine(Path(ledger_path))
    dispositions: list[dict[str, Any]] = []
    watch_entries: list[dict[str, Any]] = []
    counts = {status: 0 for status in ("discovered", "probationary", "promoted", "rejected", "blocked")}
    for candidate_id in sorted(engine.candidates):
        candidate = engine.candidates[candidate_id]
        relationships = by_url.get(candidate.canonical_url, [])
        quality, evidence = _quality(candidate, relationships)
        counts[candidate.status] += 1
        disposition = {
            "candidate_id": candidate.candidate_id,
            "canonical_url": candidate.canonical_url,
            "entity_type": candidate.entity_type,
            "status_after": candidate.status,
            "quality": quality,
            "occurrences": candidate.occurrences,
            "rights_state": candidate.rights_state,
            "rejection_reason": candidate.rejection_reason,
            **evidence,
        }
        dispositions.append(disposition)
        if candidate.status == "promoted":
            watch_entries.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "canonical_url": candidate.canonical_url,
                    "entity_type": candidate.entity_type,
                    "rights_state": candidate.rights_state,
                    "promoted_by": "candidate-policy-v1",
                    "evaluated_at": evaluated_at,
                    "quality": quality,
                    "source_families": evidence["source_families"],
                }
            )
    result = {
        "schema_version": 1,
        "policy": {
            "probation_quality": policy.probation_quality,
            "promotion_quality": policy.promotion_quality,
            "rejection_quality": policy.rejection_quality,
            "promotion_occurrences": policy.promotion_occurrences,
            "promotion_source_families": policy.promotion_source_families,
            "promotion_evidence_pointers": policy.promotion_evidence_pointers,
        },
        "evaluated_at": evaluated_at,
        "counts": counts,
        "dispositions": dispositions,
        "watch_entries": watch_entries,
    }
    _atomic_json(
        Path(watch_projection_path),
        {
            "schema_version": 1,
            "domain": sorted({candidate.domain for candidate in engine.candidates.values()}),
            "evaluated_at": evaluated_at,
            "entries": watch_entries,
        },
    )
    return result
