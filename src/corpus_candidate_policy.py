#!/usr/bin/env python3
"""Deterministic candidate disposition and recurring watch admission.

This policy operates on the shared discovery ledger and source graph. It may
change candidate lifecycle state, but it never changes rights. Promoted
candidates receive one idempotent ``inspect`` work item per UTC day in the
existing durable discovery queue and are materialized into a readable watch
projection. Material acquisition remains rights-gated elsewhere.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from corpus_discovery import DiscoveryEngine
from corpus_entity_identity import evidence_document_identity, publisher_identity
from corpus_source_graph import SourceRelationship

INSPECTION_ACTION = "inspect"
INSPECTION_BUDGET = 1.0
PROMOTED_BY = "candidate-policy-v1"


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


def _quality(
    candidate,
    relationships: list[SourceRelationship],
    *,
    evaluation_moment: datetime,
) -> tuple[float, dict[str, Any]]:
    """Score a candidate from evidence that already existed at evaluation time.

    Independence is measured on normalized evidence-document and publisher
    identities, never on caller-supplied ``source_family`` labels alone. Query
    aliases of one page are one document, and two pages from one owner are one
    publisher, so relabelling or re-querying a single source cannot manufacture
    corroboration.
    """
    admitted: list[SourceRelationship] = []
    deferred = 0
    for relationship in relationships:
        observed = datetime.fromisoformat(relationship.observed_at.replace("Z", "+00:00"))
        if observed.astimezone(timezone.utc) > evaluation_moment:
            deferred += 1
            continue
        admitted.append(relationship)
    relationships = admitted
    families = sorted({relationship.source_family for relationship in relationships})
    pointers = sorted({relationship.evidence_pointer for relationship in relationships})
    documents = sorted(
        {evidence_document_identity(relationship.evidence_pointer) for relationship in relationships}
    )
    publishers = sorted(
        {publisher_identity(relationship.evidence_pointer) for relationship in relationships}
    )
    lanes = sorted({relationship.evidence_lane for relationship in relationships})
    scores = dict(candidate.score_components)
    authority = max(
        [scores["authority"]]
        + [_LANE_AUTHORITY.get(lane.strip().lower(), 0.40) for lane in lanes]
    )
    independent_corroboration = min(
        0.2 + 0.2 * max(0, len(publishers) - 1) + 0.05 * max(0, len(documents) - 1),
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
        "independent_evidence_documents": len(documents),
        "evidence_documents": documents,
        "independent_publishers": len(publishers),
        "publishers": publishers,
        "deferred_future_observations": deferred,
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
    try:
        evaluation_moment = datetime.fromisoformat(evaluated_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("evaluated_at must be an ISO-8601 timestamp") from exc
    if evaluation_moment.tzinfo is None:
        raise ValueError("evaluated_at must include a timezone")
    evaluation_moment = evaluation_moment.astimezone(timezone.utc)
    watch_day = evaluation_moment.date().isoformat()
    engine = DiscoveryEngine(Path(ledger_path))
    by_url: dict[str, list[SourceRelationship]] = {}
    for relationship in _read_relationships(Path(graph_path)):
        by_url.setdefault(relationship.canonical_url, []).append(relationship)

    candidate_ids = sorted(engine.candidates)
    for candidate_id in candidate_ids:
        candidate = engine.candidates[candidate_id]
        quality, evidence = _quality(
            candidate,
            by_url.get(candidate.canonical_url, []),
            evaluation_moment=evaluation_moment,
        )
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
            and evidence["independent_publishers"] >= policy.promotion_source_families
            and evidence["independent_evidence_documents"] >= policy.promotion_evidence_pointers
        ):
            engine.transition_candidate(candidate_id, "promoted")

    engine = DiscoveryEngine(Path(ledger_path))
    dispositions: list[dict[str, Any]] = []
    watch_entries: list[dict[str, Any]] = []
    watch_work_ids: list[str] = []
    counts = {status: 0 for status in ("discovered", "probationary", "promoted", "rejected", "blocked")}
    for candidate_id in sorted(engine.candidates):
        candidate = engine.candidates[candidate_id]
        relationships = by_url.get(candidate.canonical_url, [])
        quality, evidence = _quality(
            candidate, relationships, evaluation_moment=evaluation_moment
        )
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
            # One idempotent daily inspection per promoted candidate in the one
            # existing durable queue. Inspection is metadata-only work: it never
            # implies acquisition rights, and replaying the same UTC day returns
            # the identical work item instead of enqueueing a second.
            work = engine.enqueue_work(
                domain=candidate.domain,
                candidate_id=candidate.candidate_id,
                action=INSPECTION_ACTION,
                score_components={"priority_score": quality},
                budget_estimate=INSPECTION_BUDGET,
                idempotency_key=f"daily-{INSPECTION_ACTION}:{watch_day}",
                now=evaluation_moment,
            )
            watch_work_ids.append(work.work_id)
            watch_entries.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "canonical_url": candidate.canonical_url,
                    "entity_type": candidate.entity_type,
                    "status": "promoted",
                    "rights_state": candidate.rights_state,
                    "promoted_by": PROMOTED_BY,
                    "evaluated_at": evaluated_at,
                    "quality": quality,
                    "source_families": evidence["source_families"],
                    "work_id": work.work_id,
                    "work_action": work.action,
                    "work_day": watch_day,
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
        "watch_work_ids": watch_work_ids,
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
