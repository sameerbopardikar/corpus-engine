#!/usr/bin/env python3
"""Accelerated two-cycle proof for recursive corpus source expansion.

The harness enforces causal visibility, not merely temporal ordering: Cycle B
is produced by the real durable watch consumer, which leases the ``inspect``
work item that Cycle A's promotion enqueued, fetches artifacts bound to that
promoted source, and derives relationships with the production extractors.
Cycle-B relationship objects can never be supplied by the packet.

Every worker-visible byte - the seed packet, both cycle configurations, and the
preserved artifact bytes themselves - is frozen, hashed, and scanned for the
hidden target label before any claim is scored, and every concept claim must
bind to preserved bytes by digest plus an exact validated span. All writes
remain inside an explicitly marked shadow run root.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from corpus_candidate_policy import evaluate_candidates
from corpus_discovery import DiscoveryEngine
from corpus_entity_identity import EntityIdentityError, canonical_entity_identity
from corpus_source_graph import (
    SourceRelationship,
    ingest_relationships,
    relationship_to_observation,
)
from corpus_watch_inspection import WatchInspectionError, run_watch_inspection

SCHEMA_VERSION = 2
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


def _moment(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecursiveProofError("evaluated_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RecursiveProofError("evaluated_at must include a timezone")
    return parsed.astimezone(timezone.utc)


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


def _haystack(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).casefold()


def _identity(name: str, value: Any) -> str:
    try:
        return canonical_entity_identity(_text(name, value, 4096))
    except EntityIdentityError as exc:
        raise RecursiveProofError(f"{name} is not a resolvable URL: {exc}") from exc


def _scan_leakage(haystack: str, terms: Iterable[str]) -> list[str]:
    return sorted({term for term in terms if term.strip() and term.casefold() in haystack})


def _tokens(text: str) -> list[str]:
    """Fold text to comparable tokens, tolerating only simple plural forms."""
    words = []
    for raw in text.casefold().split():
        word = "".join(character for character in raw if character.isalnum())
        if not word:
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        words.append(word)
    return words


def _phrase_present(haystack: list[str], phrase: list[str]) -> bool:
    if not phrase or len(phrase) > len(haystack):
        return False
    return any(
        haystack[index : index + len(phrase)] == phrase
        for index in range(len(haystack) - len(phrase) + 1)
    )


def _ground_concepts(
    worker_output: dict[str, Any],
    *,
    preserved: dict[str, dict[str, Any]],
    target_label: str,
    required_mechanisms: list[str],
) -> dict[str, Any]:
    """Admit a concept claim only when preserved bytes still carry its evidence.

    ``preserved`` maps a processed artifact identity to its digest and exact
    decoded bytes. A claim is grounded when every evidence item names a
    processed artifact, restates that artifact's current digest, and points at a
    span that still holds the quoted bytes. Naming a URL is not evidence.
    """
    concept_candidates = _list(
        "worker_output.concept_candidates", worker_output.get("concept_candidates")
    )
    grounded: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, raw_candidate in enumerate(concept_candidates):
        candidate = _object(f"concept candidate {index}", raw_candidate)
        statement = _text(f"concept candidate {index} statement", candidate.get("statement"))
        evidence = _list(f"concept candidate {index} evidence", candidate.get("evidence"))
        pointers: list[str] = []
        quotes: list[str] = []
        failure: str | None = None
        if not evidence:
            failure = "no evidence supplied"
        for item_index, raw_item in enumerate(evidence):
            item = _object(f"concept candidate {index} evidence {item_index}", raw_item)
            identity = _identity(
                f"concept candidate {index} evidence {item_index} artifact_url",
                item.get("artifact_url"),
            )
            artifact = preserved.get(identity)
            if artifact is None:
                failure = f"artifact was never fetched in this run: {identity}"
                break
            declared = _text(
                f"concept candidate {index} evidence {item_index} artifact_sha256",
                item.get("artifact_sha256"),
                64,
            ).lower()
            if declared != artifact["sha256"]:
                failure = f"artifact digest does not match preserved bytes: {identity}"
                break
            quote = _text(f"concept candidate {index} evidence {item_index} quote", item.get("quote"))
            span = item.get("span")
            if (
                not isinstance(span, (list, tuple))
                or len(span) != 2
                or any(isinstance(part, bool) or not isinstance(part, int) for part in span)
            ):
                failure = "evidence span must be a [start, end] integer pair"
                break
            start, end = int(span[0]), int(span[1])
            text = artifact["text"]
            if not 0 <= start < end <= len(text) or text[start:end] != quote:
                failure = f"evidence span does not hold the quoted bytes: {identity}"
                break
            pointers.append(f"{identity}?artifact_sha256={declared}&evidence_span={start}-{end}")
            quotes.append(quote)
        # A mechanism only counts when the cited span states it, so a keyword-rich
        # sentence cannot borrow authority from an unrelated but valid quote.
        span_tokens = _tokens(" ".join(quotes))
        supported = [
            mechanism
            for mechanism in required_mechanisms
            if _phrase_present(span_tokens, _tokens(mechanism))
        ]
        record = {
            "statement": statement,
            "evidence_pointers": sorted(set(pointers)),
            "mechanisms_supported_by_span": supported,
        }
        if failure is None:
            grounded.append(record)
        else:
            rejected.append({**record, "reason": failure})
    target_folded = target_label.casefold()
    exact_target_label_guessed = any(
        target_folded in candidate["statement"].casefold() for candidate in grounded
    )
    matching_candidates = [
        candidate
        for candidate in grounded
        if all(
            mechanism.casefold() in candidate["statement"].casefold()
            for mechanism in required_mechanisms
        )
        and all(
            mechanism in candidate["mechanisms_supported_by_span"]
            for mechanism in required_mechanisms
        )
    ]
    return {
        "evidence_grounded": bool(grounded),
        "grounded_candidates": grounded,
        "rejected_candidates": rejected,
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
    hidden_terms = [term for term in {target_label, *forbidden_terms} if term.strip()]
    seed_leaked = _scan_leakage(_haystack(seed_packet), hidden_terms)
    if seed_leaked:
        raise RecursiveProofError(f"target leakage in seed packet: {seed_leaked}")

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
    rights_assertions = _list(
        "cycle_a.rights_assertions", cycle_a.get("rights_assertions", [])
    )
    asserted_rights: dict[str, str] = {}
    by_identity = {
        canonical_entity_identity(relationship.canonical_url): relationship
        for relationship in cycle_a_relationships
    }
    rights_engine = DiscoveryEngine(ledger_path)
    for index, raw_assertion in enumerate(rights_assertions):
        assertion = _object(f"cycle_a rights assertion {index}", raw_assertion)
        if set(assertion) != {"canonical_url", "rights_state"}:
            raise RecursiveProofError(
                "Cycle A rights assertions require exactly canonical_url and rights_state"
            )
        identity = canonical_entity_identity(
            _text("rights assertion canonical_url", assertion.get("canonical_url"), 4096)
        )
        rights_state = _text("rights assertion rights_state", assertion.get("rights_state"), 64)
        if rights_state not in {"public_rights_clear", "private_authorized"}:
            raise RecursiveProofError(
                "proof body inspection requires public_rights_clear or private_authorized"
            )
        relationship = by_identity.get(identity)
        if relationship is None:
            raise RecursiveProofError("rights assertion does not bind a Cycle A candidate")
        rights_engine.observe(
            relationship_to_observation(relationship), rights_state=rights_state
        )
        asserted_rights[identity] = rights_state
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
    # Seed registries mix prose descriptions with URLs; only resolvable URLs can
    # claim identity, and they must be compared as identities so a tracking
    # alias of a seeded source cannot be reported as an unseeded discovery.
    seed_identities: set[str] = set()
    for value in seed_packet.get("registry", []):
        if not isinstance(value, str):
            continue
        try:
            seed_identities.add(canonical_entity_identity(value))
        except EntityIdentityError:
            continue
    unseeded_promoted = sorted(
        url for url in promoted_watch_urls if canonical_entity_identity(url) not in seed_identities
    )
    engine_after_a = DiscoveryEngine(ledger_path)
    cycle_a_candidate_identities = {
        canonical_entity_identity(candidate.canonical_url)
        for candidate in engine_after_a.candidates.values()
    }

    # Cycle B is produced by the durable watch consumer. The packet supplies
    # artifacts to fetch, never relationships; the consumer reads the persisted
    # watch projection, leases the enqueued inspection, and derives every edge
    # from preserved bytes through the production extractors.
    cycle_b = _object("cycle_b", config.get("cycle_b"))
    unsupported = set(cycle_b) - {"inspections"}
    if unsupported:
        raise RecursiveProofError(
            f"cycle_b accepts only fetched artifact inspections, not {sorted(unsupported)}"
        )
    inspections = _list("cycle_b.inspections", cycle_b.get("inspections"))
    for index, raw_inspection in enumerate(inspections):
        inspection = _object(f"cycle_b inspection {index}", raw_inspection)
        if "relationships" in inspection:
            raise RecursiveProofError(
                "Cycle B relationships cannot be supplied; they must be derived from fetched artifacts"
            )
    try:
        inspection_result = run_watch_inspection(
            domain=domain,
            watch_projection_path=watch_path,
            ledger_path=ledger_path,
            graph_path=graph_path,
            inspections=inspections,
            preserve_root=Path(run_root) / "artifacts",
            receipts_root=Path(run_root) / "receipts",
            now=_moment(evaluated_at),
        )
    except WatchInspectionError as exc:
        raise RecursiveProofError(f"Cycle B inspection failed: {exc}") from exc

    preserved: dict[str, dict[str, Any]] = {}
    for receipt_item in inspection_result["artifact_receipts"]:
        raw = Path(receipt_item["preserved_path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt_item["sha256"]:
            raise RecursiveProofError("preserved Cycle B artifact no longer matches its receipt")
        preserved[receipt_item["artifact_identity"]] = {
            "sha256": receipt_item["sha256"],
            "text": raw.decode("utf-8", errors="replace"),
        }

    cycle_b_policy = evaluate_candidates(
        ledger_path=ledger_path,
        graph_path=graph_path,
        watch_projection_path=watch_path,
        evaluated_at=evaluated_at,
    )
    engine_after_b = DiscoveryEngine(ledger_path)
    cycle_b_candidate_identities = {
        canonical_entity_identity(candidate.canonical_url)
        for candidate in engine_after_b.candidates.values()
    }
    second_order_candidates = sorted(cycle_b_candidate_identities - cycle_a_candidate_identities)
    rights_preserved = all(
        candidate.rights_state
        == asserted_rights.get(
            canonical_entity_identity(candidate.canonical_url), "rights_unclear"
        )
        for candidate in engine_after_b.candidates.values()
    ) and all(item.action == "inspect" for item in engine_after_b.work_items.values())

    # Freeze and scan every byte the worker could see: both cycle configurations
    # and the preserved artifact bytes, not just the seed packet.
    worker_visible = {
        "seed_packet": seed_packet,
        "cycle_a": cycle_a,
        "cycle_b": cycle_b,
        "preserved_artifacts": {
            identity: value["text"] for identity, value in sorted(preserved.items())
        },
    }
    worker_visible_bytes = json.dumps(
        worker_visible, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    leaked_terms = _scan_leakage(worker_visible_bytes.decode("utf-8").casefold(), hidden_terms)

    worker_output = _object("worker_output", config.get("worker_output"))
    concept = _ground_concepts(
        worker_output,
        preserved=preserved,
        target_label=target_label,
        required_mechanisms=required_mechanisms,
    )
    gates = {
        "leakage_absent": not leaked_terms,
        "candidate_promotion": bool(unseeded_promoted),
        "durable_inspection": bool(
            inspection_result["consumed_work_ids"]
            and set(inspection_result["consumed_work_ids"]).issubset(
                set(cycle_a_policy["watch_work_ids"])
            )
        ),
        "recursive_expansion": bool(
            inspection_result["processed_inspections"] and second_order_candidates
        ),
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
            # Cycle A is the seeded first-order layer and is declared by the
            # packet. The recursion claim rests entirely on Cycle B, which the
            # packet cannot declare.
            "relationship_source": "packet_declared",
            "rights_assertions": [
                {"canonical_url": identity, "rights_state": rights}
                for identity, rights in sorted(asserted_rights.items())
            ],
            "candidate_count": len(engine_after_a.candidates),
            "promoted_watch_urls": promoted_watch_urls,
            "unseeded_promoted_watch_urls": unseeded_promoted,
            "watch_work_ids": cycle_a_policy["watch_work_ids"],
            "policy": cycle_a_policy,
        },
        "cycle_b": {
            "declared_inspections": len(inspections),
            "inspection": inspection_result,
            "processed_artifact_urls": sorted(preserved),
            "second_order_candidate_urls": second_order_candidates,
            "supplied_relationships": 0,
            "policy": cycle_b_policy,
        },
        "unknown_concept": concept,
        "target_leakage": {
            "leaked_terms": leaked_terms,
            "seed_packet_frozen": True,
            "worker_visible_frozen": True,
            "worker_visible_sha256": hashlib.sha256(worker_visible_bytes).hexdigest(),
            "worker_visible_byte_length": len(worker_visible_bytes),
            "scanned_term_count": len(hidden_terms),
        },
        "overall_status": "passed" if all(gates.values()) else "failed",
    }
    _atomic_json(Path(run_root) / "receipt.json", receipt)
    return receipt
