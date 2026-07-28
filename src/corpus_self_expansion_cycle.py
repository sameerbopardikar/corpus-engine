#!/usr/bin/env python3
"""Bounded self-expansion phase for the normal global corpus cycle.

The phase reuses the source graph, discovery ledger, candidate policy, durable
queue, and watch consumer.  It owns no scheduler and grants no rights: source
bodies and watch candidates must already carry explicit body-inspection rights.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from corpus_candidate_policy import evaluate_candidates
from corpus_discovery import DiscoveryEngine
from corpus_entity_identity import canonical_entity_identity
from corpus_source_graph import ingest_relationships, relationship_to_observation
from corpus_watch_inspection import (
    project_preserved_source_artifact,
    run_watch_inspection,
)

SCHEMA_VERSION = 1
_BODY_RIGHTS = frozenset({"public_rights_clear", "private_authorized"})
_DOMAIN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class SelfExpansionCycleError(ValueError):
    """The self-expansion configuration or state cannot be safely processed."""


def _positive_int(config: dict[str, Any], name: str, *, maximum: int) -> int:
    value = config.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise SelfExpansionCycleError(f"{name} must be an integer from 1 through {maximum}")
    return value


def _positive_number(config: dict[str, Any], name: str, *, maximum: float) -> float:
    value = config.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= maximum:
        raise SelfExpansionCycleError(f"{name} must be greater than zero and at most {maximum}")
    return float(value)


def _moment(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SelfExpansionCycleError("cycle clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_config(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise SelfExpansionCycleError("unsupported self-expansion schema_version")
    if not isinstance(value.get("enabled"), bool):
        raise SelfExpansionCycleError("enabled must be boolean")
    if not value["enabled"]:
        return {"schema_version": 1, "enabled": False}
    required = {
        "state_root",
        "max_inspections_per_cycle",
        "max_relationships_per_artifact",
        "max_candidates_per_cycle",
        "max_artifact_bytes",
        "max_wall_seconds",
        "promotion_mode",
        "domains",
    }
    missing = required - set(value)
    if missing:
        raise SelfExpansionCycleError(f"missing self-expansion fields: {sorted(missing)}")
    state_root = value["state_root"]
    if not isinstance(state_root, str) or not state_root.strip():
        raise SelfExpansionCycleError("state_root must be a non-blank path")
    mode = value["promotion_mode"]
    if mode not in {"shadow", "live"}:
        raise SelfExpansionCycleError("promotion_mode must be shadow or live")
    domains = value["domains"]
    if not isinstance(domains, list):
        raise SelfExpansionCycleError("domains must be a list")
    normalized = dict(value)
    normalized["max_inspections_per_cycle"] = _positive_int(
        value, "max_inspections_per_cycle", maximum=100
    )
    normalized["max_relationships_per_artifact"] = _positive_int(
        value, "max_relationships_per_artifact", maximum=1000
    )
    normalized["max_candidates_per_cycle"] = _positive_int(
        value, "max_candidates_per_cycle", maximum=10000
    )
    normalized["max_artifact_bytes"] = _positive_int(
        value, "max_artifact_bytes", maximum=8 * 1024 * 1024
    )
    normalized["max_wall_seconds"] = _positive_number(
        value, "max_wall_seconds", maximum=3600
    )
    return normalized


def _paths(state_root: Path, domain: str) -> dict[str, Path]:
    root = state_root / domain
    return {
        "root": root,
        "ledger": root / "discovery-ledger.jsonl",
        "graph": root / "source-graph.jsonl",
        "watch": root / "watch-projection.json",
        "artifacts": root / "artifacts",
        "receipts": root / "receipts",
    }


def _candidate_guard(ledger: Path, maximum: int) -> int:
    count = len(DiscoveryEngine(ledger).candidates)
    if count > maximum:
        raise SelfExpansionCycleError(
            f"candidate count {count} exceeds max_candidates_per_cycle {maximum}"
        )
    return count


def _due_work_ids(ledger: Path, now: datetime) -> set[str]:
    engine = DiscoveryEngine(ledger)
    return {
        item.work_id
        for item in engine.select_work(action="inspect", now=now)
    }


def _inspection_work_id(
    ledger: Path, watch_path: Path, inspection: dict[str, Any]
) -> str | None:
    if not watch_path.is_file():
        return None
    projection = json.loads(watch_path.read_text(encoding="utf-8"))
    wanted = canonical_entity_identity(inspection.get("watch_source_url", ""))
    for entry in projection.get("entries", []):
        if canonical_entity_identity(entry.get("canonical_url", "")) == wanted:
            work_id = entry.get("work_id")
            if isinstance(work_id, str) and work_id in DiscoveryEngine(ledger).work_items:
                return work_id
    return None


def _run_domain(
    domain_config: dict[str, Any],
    *,
    config: dict[str, Any],
    now: datetime,
    started: float,
    monotonic,
) -> dict[str, Any]:
    domain = domain_config.get("domain")
    if not isinstance(domain, str) or not _DOMAIN.fullmatch(domain):
        raise SelfExpansionCycleError("domain must be a lowercase slug")
    source_artifacts = domain_config.get("source_artifacts", [])
    rights_assertions = domain_config.get("rights_assertions", [])
    inspections = domain_config.get("inspections", [])
    if not all(isinstance(value, list) for value in (source_artifacts, rights_assertions, inspections)):
        raise SelfExpansionCycleError("source_artifacts, rights_assertions, and inspections must be lists")
    if any("relationships" in item for item in inspections if isinstance(item, dict)):
        raise SelfExpansionCycleError("inspection relationships cannot be supplied")

    paths = _paths(Path(config["state_root"]), domain)
    pre_due_engine = DiscoveryEngine(paths["ledger"])
    leased_at_start = {
        item.work_id for item in pre_due_engine.work_items.values() if item.state == "leased"
    }
    # Only work that existed before this global-cycle phase is eligible.  This
    # forces a newly persisted Cycle-A work_id to be consumed by a later cycle.
    due_at_start = _due_work_ids(paths["ledger"], now)
    failures: list[dict[str, str]] = []
    source_receipts: list[dict[str, Any]] = []
    source_relationships = 0
    appended = 0
    produced = []

    for index, artifact in enumerate(source_artifacts):
        if monotonic() - started > config["max_wall_seconds"]:
            failures.append({"stage": "source_artifact", "error": "max_wall_seconds exceeded"})
            break
        try:
            receipt, relationships = project_preserved_source_artifact(
                domain=domain,
                request=artifact,
                preserve_root=paths["artifacts"],
                max_artifact_bytes=config["max_artifact_bytes"],
                max_relationships=config["max_relationships_per_artifact"],
            )
            source_receipts.append(receipt)
            source_relationships += len(relationships)
            produced.extend(relationships)
        except Exception as exc:
            failures.append({"stage": f"source_artifact[{index}]", "error": f"{type(exc).__name__}: {exc}"})

    by_identity = {}
    existing_identities = {
        canonical_entity_identity(item.canonical_url)
        for item in DiscoveryEngine(paths["ledger"]).candidates.values()
    }
    admitted_produced = []
    projected_identities = set(existing_identities)
    for relationship in produced:
        identity = canonical_entity_identity(relationship.canonical_url)
        if identity not in projected_identities and len(projected_identities) >= config["max_candidates_per_cycle"]:
            failures.append({
                "stage": "candidate_cap",
                "error": "relationship projection would exceed max_candidates_per_cycle",
            })
            continue
        projected_identities.add(identity)
        admitted_produced.append(relationship)
        by_identity.setdefault(identity, relationship)
    produced = admitted_produced
    rights_engine = DiscoveryEngine(paths["ledger"])
    for index, assertion in enumerate(rights_assertions):
        try:
            if not isinstance(assertion, dict) or set(assertion) != {"canonical_url", "rights_state"}:
                raise SelfExpansionCycleError("rights assertion fields must be canonical_url and rights_state")
            identity = canonical_entity_identity(assertion["canonical_url"])
            rights = assertion["rights_state"]
            if rights not in _BODY_RIGHTS:
                raise SelfExpansionCycleError("rights assertion is not body-authorized")
            relationship = by_identity.get(identity)
            if relationship is None:
                # Replay may assert an already durable candidate, but never a new,
                # unrelated URL.  It must already exist with the same explicit rights.
                existing = next(
                    (item for item in rights_engine.candidates.values()
                     if canonical_entity_identity(item.canonical_url) == identity),
                    None,
                )
                if existing is None or existing.rights_state != rights:
                    raise SelfExpansionCycleError("rights assertion does not bind a produced candidate")
            else:
                rights_engine.observe(relationship_to_observation(relationship), rights_state=rights)
        except Exception as exc:
            failures.append({"stage": f"rights_assertion[{index}]", "error": f"{type(exc).__name__}: {exc}"})

    for relationship in produced:
        result = ingest_relationships(
            [relationship],
            graph_path=paths["graph"],
            discovery_ledger_path=paths["ledger"],
            max_items=config["max_relationships_per_artifact"],
        )
        appended += result["relationships_appended"]

    candidate_count = _candidate_guard(paths["ledger"], config["max_candidates_per_cycle"])
    policy = None
    if config["promotion_mode"] == "live":
        policy = evaluate_candidates(
            ledger_path=paths["ledger"],
            graph_path=paths["graph"],
            watch_projection_path=paths["watch"],
            evaluated_at=_iso(now),
        )

    inspection_results: list[dict[str, Any]] = []
    consumed = 0
    recovered = 0
    for index, inspection in enumerate(inspections):
        if consumed >= config["max_inspections_per_cycle"]:
            break
        if monotonic() - started > config["max_wall_seconds"]:
            failures.append({"stage": "inspection", "error": "max_wall_seconds exceeded"})
            break
        try:
            work_id = _inspection_work_id(paths["ledger"], paths["watch"], inspection)
            if work_id is None or work_id not in due_at_start:
                continue
            if work_id in leased_at_start:
                recovered += 1
            result = run_watch_inspection(
                domain=domain,
                watch_projection_path=paths["watch"],
                ledger_path=paths["ledger"],
                graph_path=paths["graph"],
                inspections=[inspection],
                preserve_root=paths["artifacts"],
                receipts_root=paths["receipts"],
                now=now,
                max_artifact_bytes=config["max_artifact_bytes"],
                max_relationships_per_artifact=config["max_relationships_per_artifact"],
                max_candidate_count=config["max_candidates_per_cycle"],
            )
            inspection_results.append(result)
            consumed += result["processed_inspections"]
        except Exception as exc:
            failures.append({"stage": f"inspection[{index}]", "error": f"{type(exc).__name__}: {exc}"})

    candidate_count_after = _candidate_guard(paths["ledger"], config["max_candidates_per_cycle"])
    post_policy = None
    if config["promotion_mode"] == "live" and consumed:
        post_policy = evaluate_candidates(
            ledger_path=paths["ledger"],
            graph_path=paths["graph"],
            watch_projection_path=paths["watch"],
            evaluated_at=_iso(now),
        )
    return {
        "domain": domain,
        "status": "partial_failure" if failures else "completed",
        "failures": failures,
        "source_artifacts_processed": len(source_receipts),
        "source_artifact_receipts": source_receipts,
        "source_relationships_derived": source_relationships,
        "relationships_appended": appended + sum(
            item["relationships_appended"] for item in inspection_results
        ),
        "candidate_count_before_inspection": candidate_count,
        "candidate_count_after": candidate_count_after,
        "policy": policy,
        "post_inspection_policy": post_policy,
        "due_work_ids_at_cycle_start": sorted(due_at_start),
        "inspections_consumed": consumed,
        "expired_leases_recovered": recovered,
        "consumed_work_ids": [
            work_id for item in inspection_results for work_id in item["consumed_work_ids"]
        ],
        "inspection_results": inspection_results,
        "supplied_inspection_relationships": 0,
    }


def run_self_expansion_cycle(
    raw_config: dict[str, Any], *, now: datetime, monotonic=time.monotonic
) -> dict[str, Any]:
    """Run one optional self-expansion phase and return its receipt projection."""
    config = _load_config(raw_config)
    if not config["enabled"]:
        return {"schema_version": 1, "enabled": False, "status": "disabled", "domains": []}
    now = _moment(now)
    started = monotonic()
    domains: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for index, domain_config in enumerate(config["domains"]):
        try:
            if not isinstance(domain_config, dict):
                raise SelfExpansionCycleError("domain configuration must be an object")
            domains.append(
                _run_domain(
                    domain_config,
                    config=config,
                    now=now,
                    started=started,
                    monotonic=monotonic,
                )
            )
        except Exception as exc:
            failures.append({"domain_index": str(index), "error": f"{type(exc).__name__}: {exc}"})
    return {
        "schema_version": 1,
        "enabled": True,
        "status": "partial_failure" if failures or any(item["failures"] for item in domains) else "completed",
        "evaluated_at": _iso(now),
        "promotion_mode": config["promotion_mode"],
        "limits": {
            key: config[key]
            for key in (
                "max_inspections_per_cycle",
                "max_relationships_per_artifact",
                "max_candidates_per_cycle",
                "max_artifact_bytes",
                "max_wall_seconds",
            )
        },
        "failures": failures,
        "domains": domains,
    }
