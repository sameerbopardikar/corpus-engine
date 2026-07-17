#!/usr/bin/env python3
"""Deterministic, no-LLM corpus and doctrine evaluations.

The evaluator binds filesystem bytes, corpus citations, source-scoped retrieval,
epistemic attribution, contradiction surfacing, doctrine lineage and bounded cost
into one strict, content-addressed report. Reports retain a compact immutable
inventory so a later corpus revision can be compared with its prior snapshot.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 1
_RECORD_FIELDS = {
    "record_id", "source_id", "page_slug", "locator", "claim_sha256",
    "raw_pointer", "raw_sha256", "normalized_pointer", "normalized_sha256",
    "canonical_url", "source_revision", "retrieved_at", "evidence_lane",
    "evidence_class", "epistemic_layer", "rights_state", "candidate_status",
    "contradiction_group", "contradiction_stance",
}
_CITATION_FIELDS = {"source_id", "page_slug", "locator", "claim_sha256", "evidence_class"}
_USAGE_FIELDS = {"deep_acquisitions", "llm_tasks", "llm_tokens", "estimated_cost_usd", "actual_cost_usd"}
_POLICY_FIELDS = {
    "max_deep_acquisitions_per_utc_day", "max_llm_tasks_per_utc_day",
    "max_llm_tokens_per_utc_day", "max_cost_usd_per_utc_day",
    "max_freshness_days", "min_evidence_lanes",
}
_RETRIEVAL_FIELDS = {"case_id", "query", "expected_page_slugs", "results"}
_RESULT_FIELDS = {"source_id", "page_slug"}
_PROMOTABLE_RIGHTS = {"public_rights_clear"}
_DOCTRINE_FORBIDDEN_LANES = {"unverified-discovery-signal"}


class EvaluationError(ValueError):
    """Evaluation input or a prior report is malformed or unverifiable."""


def _strict_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvaluationError(f"value is not strict JSON: {exc}") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_strict_bytes(value)).hexdigest()


def _mapping(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise EvaluationError(f"{name} fields must be exactly {sorted(fields)}")
    return dict(value)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationError(f"{name} must be a non-empty string")
    return value.strip()


def _sha(value: Any, name: str) -> str:
    result = _text(value, name)
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise EvaluationError(f"{name} must be a lowercase SHA-256 digest")
    return result


def _aware(value: Any, name: str) -> datetime:
    text = _text(value, name)
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationError(f"{name} must be ISO-8601") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise EvaluationError(f"{name} must be timezone-aware")
    return result.astimezone(timezone.utc)


def _nonnegative_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise EvaluationError(f"{name} must be finite and non-negative")
    return result


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvaluationError(f"{name} must be a non-negative integer")
    return value


def _positive_int(value: Any, name: str) -> int:
    result = _nonnegative_int(value, name)
    if result < 1:
        raise EvaluationError(f"{name} must be a positive integer")
    return result


def _path_digest(pointer: Any) -> tuple[Path, str | None]:
    path = Path(_text(pointer, "artifact pointer"))
    if path.is_symlink() or not path.is_file():
        return path, None
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _normalize_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(records, (str, bytes, Mapping)):
        raise EvaluationError("records must be an iterable of record objects")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for value in records:
        row = _mapping(value, _RECORD_FIELDS, "record")
        record_id = _text(row["record_id"], "record_id")
        if record_id in seen_ids:
            raise EvaluationError(f"duplicate record_id: {record_id}")
        seen_ids.add(record_id)
        for name in (
            "source_id", "page_slug", "locator", "canonical_url", "source_revision",
            "evidence_lane", "evidence_class", "epistemic_layer", "rights_state",
            "candidate_status", "contradiction_group", "contradiction_stance",
        ):
            row[name] = _text(row[name], name)
        for name in ("claim_sha256", "raw_sha256", "normalized_sha256"):
            row[name] = _sha(row[name], name)
        _aware(row["retrieved_at"], "retrieved_at")
        row["raw_pointer"] = _text(row["raw_pointer"], "raw_pointer")
        row["normalized_pointer"] = _text(row["normalized_pointer"], "normalized_pointer")
        normalized.append(row)
    return sorted(normalized, key=lambda item: item["record_id"])


def _normalize_usage(value: Mapping[str, Any]) -> dict[str, Any]:
    usage = _mapping(value, _USAGE_FIELDS, "usage")
    for name in ("deep_acquisitions", "llm_tasks", "llm_tokens"):
        usage[name] = _nonnegative_int(usage[name], f"usage.{name}")
    for name in ("estimated_cost_usd", "actual_cost_usd"):
        usage[name] = _nonnegative_number(usage[name], f"usage.{name}")
    return usage


def _normalize_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    policy = _mapping(value, _POLICY_FIELDS, "policy")
    for name in (
        "max_deep_acquisitions_per_utc_day", "max_llm_tasks_per_utc_day",
        "max_llm_tokens_per_utc_day", "max_freshness_days", "min_evidence_lanes",
    ):
        policy[name] = _positive_int(policy[name], f"policy.{name}")
    policy["max_cost_usd_per_utc_day"] = _nonnegative_number(
        policy["max_cost_usd_per_utc_day"], "policy.max_cost_usd_per_utc_day"
    )
    return policy


def _citation_key(value: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    citation = _mapping(value, _CITATION_FIELDS, "citation")
    return (
        _text(citation["source_id"], "citation.source_id"),
        _text(citation["page_slug"], "citation.page_slug"),
        _text(citation["locator"], "citation.locator"),
        _text(citation["claim_sha256"], "citation.claim_sha256"),
        _text(citation["evidence_class"], "citation.evidence_class"),
    )


def _report_digest(report: Mapping[str, Any]) -> str:
    return _digest({key: value for key, value in report.items() if key != "report_sha256"})


def _validate_previous(report: Any) -> dict[str, Any]:
    if not isinstance(report, Mapping) or "report_sha256" not in report or "inventory" not in report:
        raise EvaluationError("previous report is missing digest or inventory")
    result = dict(report)
    if _sha(result["report_sha256"], "previous report digest") != _report_digest(result):
        raise EvaluationError("previous report digest mismatch")
    inventory = result["inventory"]
    if not isinstance(inventory, list):
        raise EvaluationError("previous report inventory must be a list")
    for item in inventory:
        _mapping(item, {"record_id", "normalized_sha256"}, "previous inventory item")
        _text(item["record_id"], "previous inventory record_id")
        _sha(item["normalized_sha256"], "previous inventory normalized_sha256")
    return result


def evaluate_corpus(
    *,
    records: Iterable[Mapping[str, Any]],
    doctrine_snapshot: Mapping[str, Any],
    doctrine_lineages: Mapping[str, Any],
    retrieval_cases: Iterable[Mapping[str, Any]],
    usage: Mapping[str, Any],
    policy: Mapping[str, Any],
    evaluated_at: str,
    previous_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate one immutable corpus revision and optionally compare its predecessor."""
    rows = _normalize_records(records)
    current_time = _aware(evaluated_at, "evaluated_at")
    usage_value = _normalize_usage(usage)
    policy_value = _normalize_policy(policy)
    failures: set[str] = set()

    valid_integrity = 0
    for row in rows:
        raw_path, raw_digest = _path_digest(row["raw_pointer"])
        normalized_path, normalized_digest = _path_digest(row["normalized_pointer"])
        record_failures = 0
        if raw_digest is None:
            failures.add(f"raw_pointer_missing:{row['record_id']}")
            record_failures += 1
        elif raw_digest != row["raw_sha256"]:
            failures.add(f"raw_hash_mismatch:{row['record_id']}")
            record_failures += 1
        if normalized_digest is None:
            failures.add(f"normalized_pointer_missing:{row['record_id']}")
            record_failures += 1
        elif normalized_digest != row["normalized_sha256"]:
            failures.add(f"normalized_hash_mismatch:{row['record_id']}")
            record_failures += 1
        del raw_path, normalized_path
        if not record_failures:
            valid_integrity += 1
        if row["source_id"] != "corpora":
            failures.add(f"record_not_corpora:{row['record_id']}")
        if row["epistemic_layer"] != "primary_source":
            failures.add(f"invalid_evidence_attribution:{row['record_id']}")
        age_days = (current_time - _aware(row["retrieved_at"], "retrieved_at")).total_seconds() / 86400
        if age_days < 0:
            failures.add(f"future_retrieval:{row['record_id']}")
        elif age_days > policy_value["max_freshness_days"]:
            failures.add(f"stale_record:{row['record_id']}")
        if row["candidate_status"] == "promoted" and (
            row["rights_state"] not in _PROMOTABLE_RIGHTS
            or row["evidence_lane"] in _DOCTRINE_FORBIDDEN_LANES
        ):
            failures.add(f"false_promotion:{row['record_id']}")

    duplicates = 0
    material_owner: dict[str, str] = {}
    for row in rows:
        prior_id = material_owner.setdefault(row["normalized_sha256"], row["record_id"])
        if prior_id != row["record_id"]:
            duplicates += 1
            failures.add(f"duplicate_material:{row['record_id']}")

    lanes = sorted({row["evidence_lane"] for row in rows})
    if len(lanes) < policy_value["min_evidence_lanes"]:
        failures.add("insufficient_lane_diversity")

    snapshot = dict(doctrine_snapshot)
    if set(snapshot) != {"schema_version", "concepts", "aliases", "lineage_edges"}:
        raise EvaluationError("doctrine snapshot fields are invalid")
    if snapshot["schema_version"] != SCHEMA_VERSION:
        raise EvaluationError("unsupported doctrine snapshot schema_version")
    if not isinstance(snapshot["concepts"], list) or not isinstance(snapshot["aliases"], Mapping) or not isinstance(snapshot["lineage_edges"], list):
        raise EvaluationError("doctrine snapshot collections are invalid")

    citation_index = {
        (row["source_id"], row["page_slug"], row["locator"], row["claim_sha256"], row["evidence_class"]): row["record_id"]
        for row in rows
    }
    concepts_by_key: dict[str, dict[str, Any]] = {}
    concept_cited_ids: dict[str, set[str]] = {}
    valid_citations = 0
    total_citations = 0
    for raw_concept in snapshot["concepts"]:
        if not isinstance(raw_concept, Mapping):
            raise EvaluationError("doctrine concept must be an object")
        concept = dict(raw_concept)
        key = _text(concept.get("concept_key"), "concept_key")
        if key in concepts_by_key:
            raise EvaluationError(f"duplicate doctrine concept: {key}")
        concepts_by_key[key] = concept
        if concept.get("epistemic_layer") != "external_corpus_synthesis":
            failures.add(f"invalid_doctrine_attribution:{key}")
        if concept.get("sameer_adopted") is not False:
            failures.add(f"sameer_adoption_forbidden:{key}")
        citations = concept.get("citations")
        if not isinstance(citations, list) or not citations:
            failures.add(f"citation_missing:{key}")
            concept_cited_ids[key] = set()
            continue
        cited_ids: set[str] = set()
        for citation in citations:
            total_citations += 1
            identity = _citation_key(citation)
            if identity[0] != "corpora":
                failures.add(f"citation_not_corpora:{key}")
            record_id = citation_index.get(identity)
            if record_id is None:
                failures.add(f"citation_unresolved:{key}")
            else:
                valid_citations += 1
                cited_ids.add(record_id)
        concept_cited_ids[key] = cited_ids

    for alias, target in sorted(snapshot["aliases"].items()):
        alias_name = _text(alias, "alias")
        target_name = _text(target, "alias target")
        if target_name not in concepts_by_key:
            failures.add(f"dangling_alias:{alias_name}")

    if not isinstance(doctrine_lineages, Mapping):
        raise EvaluationError("doctrine_lineages must be an object")
    lineage_current_keys: set[str] = set()
    for requested_key, raw_lineage in sorted(doctrine_lineages.items()):
        key = _text(requested_key, "lineage key")
        if not isinstance(raw_lineage, Mapping):
            raise EvaluationError("lineage must be an object")
        lineage = dict(raw_lineage)
        expected_fields = {"requested_key", "current_key", "versions", "descendants", "edges"}
        if set(lineage) != expected_fields:
            raise EvaluationError(f"lineage fields invalid: {key}")
        if lineage["requested_key"] != key:
            failures.add(f"lineage_requested_key_mismatch:{key}")
        current_key = lineage["current_key"]
        if current_key not in concepts_by_key:
            failures.add(f"lineage_current_missing:{key}")
        else:
            lineage_current_keys.add(current_key)
        if not isinstance(lineage["versions"], list) or not lineage["versions"]:
            failures.add(f"lineage_versions_missing:{key}")
        if not isinstance(lineage["edges"], list):
            failures.add(f"lineage_edges_invalid:{key}")
        elif lineage["edges"]:
            snapshot_edge_keys = {
                (edge.get("from"), edge.get("to"), edge.get("relation"), edge.get("recorded_at"))
                for edge in snapshot["lineage_edges"] if isinstance(edge, Mapping)
            }
            for edge in lineage["edges"]:
                if not isinstance(edge, Mapping) or (
                    edge.get("from"), edge.get("to"), edge.get("relation"), edge.get("recorded_at")
                ) not in snapshot_edge_keys:
                    failures.add(f"lineage_edge_unresolved:{key}")
                    break
    for concept_key in sorted(set(concepts_by_key) - lineage_current_keys):
        failures.add(f"lineage_missing:{concept_key}")

    contradiction_groups: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        group = contradiction_groups.setdefault(row["contradiction_group"], {})
        group.setdefault(row["contradiction_stance"], set()).add(row["record_id"])
    contradiction_count = surfaced_count = 0
    for group_name, stances in sorted(contradiction_groups.items()):
        if len(stances) < 2:
            continue
        contradiction_count += 1
        required_ids = set().union(*stances.values())
        surfaced = any(
            concept.get("status") == "bounded" and required_ids <= concept_cited_ids.get(key, set())
            for key, concept in concepts_by_key.items()
        )
        if surfaced:
            surfaced_count += 1
        else:
            failures.add(f"contradiction_not_surfaced:{group_name}")

    retrieval_values: list[dict[str, Any]] = []
    if isinstance(retrieval_cases, (str, bytes, Mapping)):
        raise EvaluationError("retrieval_cases must be an iterable")
    seen_cases: set[str] = set()
    hits = 0
    for raw_case in retrieval_cases:
        case = _mapping(raw_case, _RETRIEVAL_FIELDS, "retrieval case")
        case_id = _text(case["case_id"], "case_id")
        if case_id in seen_cases:
            raise EvaluationError(f"duplicate retrieval case: {case_id}")
        seen_cases.add(case_id)
        _text(case["query"], "retrieval query")
        expected = case["expected_page_slugs"]
        results = case["results"]
        if not isinstance(expected, list) or not expected or not isinstance(results, list):
            raise EvaluationError("retrieval expected_page_slugs/results are invalid")
        expected_set = {_text(item, "expected page slug") for item in expected}
        returned: set[str] = set()
        scope_valid = True
        for raw_result in results:
            result = _mapping(raw_result, _RESULT_FIELDS, "retrieval result")
            source_id = _text(result["source_id"], "retrieval source_id")
            if source_id != "corpora":
                scope_valid = False
            else:
                returned.add(_text(result["page_slug"], "retrieval page_slug"))
        hit = bool(expected_set & returned)
        if not scope_valid:
            failures.add(f"retrieval_source_scope:{case_id}")
        if not hit:
            failures.add(f"retrieval_miss:{case_id}")
        if hit:
            hits += 1
        retrieval_values.append({"case_id": case_id, "hit": hit, "source_scoped": scope_valid})
    retrieval_values.sort(key=lambda item: item["case_id"])

    cap_pairs = (
        ("deep_acquisitions", "max_deep_acquisitions_per_utc_day", "deep_acquisition_cap_exceeded"),
        ("llm_tasks", "max_llm_tasks_per_utc_day", "llm_task_cap_exceeded"),
        ("llm_tokens", "max_llm_tokens_per_utc_day", "llm_token_cap_exceeded"),
    )
    for usage_key, policy_key, failure in cap_pairs:
        if usage_value[usage_key] > policy_value[policy_key]:
            failures.add(failure)
    if max(usage_value["estimated_cost_usd"], usage_value["actual_cost_usd"]) > policy_value["max_cost_usd_per_utc_day"]:
        failures.add("cost_cap_exceeded")

    inventory = [
        {"record_id": row["record_id"], "normalized_sha256": row["normalized_sha256"]}
        for row in rows
    ]
    previous = _validate_previous(previous_report) if previous_report is not None else None
    if previous is None:
        prior_ids: set[str] = set()
        prior_hashes: set[str] = set()
    else:
        prior_ids = {item["record_id"] for item in previous["inventory"]}
        prior_hashes = {item["normalized_sha256"] for item in previous["inventory"]}
    new_ids = sorted(item["record_id"] for item in inventory if item["record_id"] not in prior_ids)
    new_hashes = sorted({item["normalized_sha256"] for item in inventory if item["normalized_sha256"] not in prior_hashes})

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "evaluated_at": current_time.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "passed": not failures,
        "failures": sorted(failures),
        "llm_calls": 0,
        "inventory": inventory,
        "metrics": {
            "integrity": {"total_records": len(rows), "valid_records": valid_integrity},
            "citations": {"total": total_citations, "valid": valid_citations},
            "retrieval": {
                "cases": retrieval_values,
                "hits": hits,
                "recall": round(hits / len(retrieval_values), 8) if retrieval_values else 0.0,
            },
            "lane_diversity": {"count": len(lanes), "lanes": lanes},
            "duplicates": {"count": duplicates, "rate": round(duplicates / len(rows), 8) if rows else 0.0},
            "contradictions": {"detected": contradiction_count, "surfaced": surfaced_count},
            "lineage": {"concepts": len(concepts_by_key), "lineages_checked": len(doctrine_lineages)},
            "cost": usage_value,
            "material_yield": {
                "new_material_records": len(new_hashes),
                "yield_per_deep_acquisition": round(len(new_hashes) / usage_value["deep_acquisitions"], 8)
                if usage_value["deep_acquisitions"] else float(len(new_hashes)),
            },
        },
        "comparison": {
            "previous_report_sha256": previous["report_sha256"] if previous is not None else None,
            "new_record_ids": new_ids,
            "new_normalized_sha256": new_hashes,
            "removed_record_ids": sorted(prior_ids - {item["record_id"] for item in inventory}),
        },
    }
    report["report_sha256"] = _report_digest(report)
    return report
