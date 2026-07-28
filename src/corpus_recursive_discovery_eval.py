#!/usr/bin/env python3
"""Accelerated two-cycle proof for recursive corpus source expansion.

The harness enforces temporal visibility: Cycle B artifacts are eligible only
when their watch source was promoted by Cycle A. A hidden evaluator checks
label leakage and mechanism-level concept emergence after the worker packet is
frozen. All writes remain inside an explicitly marked shadow run root.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable

from corpus_candidate_policy import evaluate_candidates
from corpus_discovery import DiscoveryEngine
from corpus_source_graph import SourceRelationship, ingest_relationships

SCHEMA_VERSION = 1
_MARKER = ".self-expansion-proof-root"


class RecursiveProofError(ValueError):
    """The proof configuration, grounding, or shadow-root contract is invalid."""


def _text(name: str, value: Any, maximum: int = 100_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecursiveProofError(f"{name} must be non-blank text")
    value = value.strip()
    if len(value) > maximum:
        raise RecursiveProofError(f"{name} exceeds {maximum} characters")
    return value


def _list(name: str, value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise RecursiveProofError(f"{name} must be a list")
    return value


def _object(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecursiveProofError(f"{name} must be an object")
    return value


def _relationship(value: Any) -> SourceRelationship:
    value = _object("relationship", value)
    payload = {
        key: item for key, item in value.items()
        if key not in {"schema_version", "relation_id"}
    }
    try:
        relationship = SourceRelationship.from_dict(payload)
    except ValueError as exc:
        raise RecursiveProofError(f"invalid relationship: {exc}") from exc
    supplied_relation_id = value.get("relation_id")
    if supplied_relation_id is not None and supplied_relation_id != relationship.relation_id:
        raise RecursiveProofError("relationship identity mismatch")
    return relationship


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
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _prepare_run_root(run_root: Path) -> None:
    run_root = Path(run_root)
    if run_root.exists():
        marker = run_root / _MARKER
        if not marker.is_file() or marker.read_text(encoding="utf-8") != "self-expansion-proof-v1\n":
            raise RecursiveProofError(
                f"refusing to reset unmarked proof root: {run_root}"
            )
        shutil.rmtree(run_root)
    run_root.mkdir(parents=True, mode=0o700)
    os.chmod(run_root, 0o700)
    (run_root / _MARKER).write_text("self-expansion-proof-v1\n", encoding="utf-8")
    os.chmod(run_root / _MARKER, 0o600)


def _seed_haystack(seed_packet: dict[str, Any]) -> str:
    return json.dumps(seed_packet, ensure_ascii=False, sort_keys=True).casefold()


def _ground_concepts(
    worker_output: dict[str, Any],
    *,
    processed_artifact_urls: set[str],
    target_label: str,
    required_mechanisms: list[str],
) -> dict[str, Any]:
    concept_candidates = _list(
        "worker_output.concept_candidates", worker_output.get("concept_candidates")
    )
    grounded: list[dict[str, Any]] = []
    for index, candidate in enumerate(concept_candidates):
        candidate = _object(f"concept candidate {index}", candidate)
        statement = _text(f"concept candidate {index} statement", candidate.get("statement"))
        pointers = [
            _text(f"concept candidate {index} evidence pointer", pointer, 4096)
            for pointer in _list(
                f"concept candidate {index} evidence_pointers",
                candidate.get("evidence_pointers"),
            )
        ]
        if pointers and set(pointers).issubset(processed_artifact_urls):
            grounded.append({"statement": statement, "evidence_pointers": sorted(set(pointers))})
    target_folded = target_label.casefold()
    exact_target_label_guessed = any(
        target_folded in candidate["statement"].casefold() for candidate in grounded
    )
    matching_candidates = []
    for candidate in grounded:
        statement = candidate["statement"].casefold()
        if all(mechanism.casefold() in statement for mechanism in required_mechanisms):
            matching_candidates.append(candidate)
    return {
        "evidence_grounded": bool(grounded),
        "grounded_candidates": grounded,
        "exact_target_label_guessed": exact_target_label_guessed,
        "mechanism_cluster_matched": bool(matching_candidates),
        "matching_candidates": matching_candidates,
        "required_mechanisms": required_mechanisms,
    }


def run_recursive_proof(config: dict[str, Any], *, run_root: Path) -> dict[str, Any]:
    config = _object("config", config)
    if config.get("schema_version") != SCHEMA_VERSION:
        raise RecursiveProofError("unsupported proof schema_version")
    domain = _text("domain", config.get("domain"), 256)
    evaluated_at = _text("evaluated_at", config.get("evaluated_at"), 64)
    seed_packet = _object("seed_packet", config.get("seed_packet"))
    hidden = _object("hidden_evaluator", config.get("hidden_evaluator"))
    target_label = _text("target_label", hidden.get("target_label"), 1024)
    forbidden_terms = [
        _text("forbidden_seed_term", value, 1024)
        for value in _list("forbidden_seed_terms", hidden.get("forbidden_seed_terms"))
    ]
    required_mechanisms = [
        _text("required_mechanism", value, 1024)
        for value in _list("required_mechanisms", hidden.get("required_mechanisms"))
    ]
    if not required_mechanisms:
        raise RecursiveProofError("hidden evaluator requires at least one mechanism")
    haystack = _seed_haystack(seed_packet)
    leaked = sorted(
        term for term in {target_label, *forbidden_terms}
        if term.casefold() in haystack
    )
    if leaked:
        raise RecursiveProofError(f"target leakage in seed packet: {leaked}")

    _prepare_run_root(Path(run_root))
    graph_path = Path(run_root) / "source-graph.jsonl"
    ledger_path = Path(run_root) / "discovery-ledger.jsonl"
    watch_path = Path(run_root) / "watch-projection.json"

    cycle_a = _object("cycle_a", config.get("cycle_a"))
    cycle_a_relationships = [
        _relationship(value)
        for value in _list("cycle_a.relationships", cycle_a.get("relationships"))
    ]
    if any(relationship.domain != domain for relationship in cycle_a_relationships):
        raise RecursiveProofError("Cycle A relationship domain mismatch")
    ingest_relationships(
        cycle_a_relationships,
        graph_path=graph_path,
        discovery_ledger_path=ledger_path,
    )
    cycle_a_policy = evaluate_candidates(
        ledger_path=ledger_path,
        graph_path=graph_path,
        watch_projection_path=watch_path,
        evaluated_at=evaluated_at,
    )
    promoted_watch_urls = sorted(
        entry["canonical_url"] for entry in cycle_a_policy["watch_entries"]
    )
    seed_urls = {
        value for value in seed_packet.get("registry", []) if isinstance(value, str)
    }
    unseeded_promoted = sorted(set(promoted_watch_urls) - seed_urls)
    engine_after_a = DiscoveryEngine(ledger_path)
    cycle_a_candidate_urls = {
        candidate.canonical_url for candidate in engine_after_a.candidates.values()
    }

    cycle_b = _object("cycle_b", config.get("cycle_b"))
    batches = _list("cycle_b.batches", cycle_b.get("batches"))
    processed_artifact_urls: set[str] = set()
    skipped_sources: list[str] = []
    processed_batches = 0
    for index, raw_batch in enumerate(batches):
        batch = _object(f"cycle_b batch {index}", raw_batch)
        watch_source_url = _text(
            f"cycle_b batch {index} watch_source_url", batch.get("watch_source_url"), 4096
        )
        artifact_url = _text(
            f"cycle_b batch {index} artifact_url", batch.get("artifact_url"), 4096
        )
        if watch_source_url not in promoted_watch_urls:
            skipped_sources.append(watch_source_url)
            continue
        relationships = [
            _relationship(value)
            for value in _list(
                f"cycle_b batch {index} relationships", batch.get("relationships")
            )
        ]
        if any(relationship.domain != domain for relationship in relationships):
            raise RecursiveProofError("Cycle B relationship domain mismatch")
        if any(relationship.discovered_from_url != artifact_url for relationship in relationships):
            raise RecursiveProofError(
                "Cycle B relationships must be grounded in their declared artifact_url"
            )
        ingest_relationships(
            relationships,
            graph_path=graph_path,
            discovery_ledger_path=ledger_path,
        )
        processed_artifact_urls.add(artifact_url)
        processed_batches += 1

    cycle_b_policy = evaluate_candidates(
        ledger_path=ledger_path,
        graph_path=graph_path,
        watch_projection_path=watch_path,
        evaluated_at=evaluated_at,
    )
    engine_after_b = DiscoveryEngine(ledger_path)
    cycle_b_candidate_urls = {
        candidate.canonical_url for candidate in engine_after_b.candidates.values()
    }
    second_order_candidates = sorted(cycle_b_candidate_urls - cycle_a_candidate_urls)
    rights_preserved = all(
        candidate.rights_state == "rights_unclear"
        for candidate in engine_after_b.candidates.values()
    )

    worker_output = _object("worker_output", config.get("worker_output"))
    concept = _ground_concepts(
        worker_output,
        processed_artifact_urls=processed_artifact_urls,
        target_label=target_label,
        required_mechanisms=required_mechanisms,
    )
    gates = {
        "leakage_absent": True,
        "candidate_promotion": bool(unseeded_promoted),
        "recursive_expansion": bool(processed_batches and second_order_candidates),
        "unknown_concept": bool(
            concept["evidence_grounded"] and concept["mechanism_cluster_matched"]
        ),
        "rights_preserved": rights_preserved,
    }
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "domain": domain,
        "evaluated_at": evaluated_at,
        "gates": gates,
        "cycle_a": {
            "relationships_ingested": len(cycle_a_relationships),
            "candidate_count": len(engine_after_a.candidates),
            "promoted_watch_urls": promoted_watch_urls,
            "unseeded_promoted_watch_urls": unseeded_promoted,
            "policy": cycle_a_policy,
        },
        "cycle_b": {
            "declared_batches": len(batches),
            "processed_batches": processed_batches,
            "processed_artifact_urls": sorted(processed_artifact_urls),
            "skipped_unpromoted_sources": sorted(skipped_sources),
            "second_order_candidate_urls": second_order_candidates,
            "policy": cycle_b_policy,
        },
        "unknown_concept": concept,
        "target_leakage": {"leaked_terms": [], "seed_packet_frozen": True},
        "overall_status": "passed" if all(gates.values()) else "failed",
    }
    _atomic_json(Path(run_root) / "receipt.json", receipt)
    return receipt
