#!/usr/bin/env python3
"""Restart-safe, deterministic Agentic Engineering shadow-cycle controller.

The controller owns no discovery or retrieval substrate. It composes the shared
candidate, budget, doctrine and evaluation contracts into one bounded shadow
transaction. External I/O is injected so fixture, copied-live and deployed runs
exercise the same state machine.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from corpus_doctrine import DoctrineEngine
from corpus_engine_models import CandidateRecord
from corpus_eval import evaluate_corpus
from corpus_priority import BudgetLedger, CandidateTask, PriorityPolicy


class ShadowCycleError(RuntimeError):
    """The shadow cycle cannot safely make or verify progress."""


@dataclass(frozen=True)
class ShadowCyclePaths:
    state_root: Path
    corpus_root: Path
    archive_root: Path

    def __post_init__(self) -> None:
        for name in ("state_root", "corpus_root", "archive_root"):
            value = getattr(self, name)
            if not isinstance(value, Path):
                object.__setattr__(self, name, Path(value))


Acquire = Callable[[CandidateRecord, ShadowCyclePaths], Mapping[str, Any]]
SyncCorpus = Callable[[Mapping[str, Any]], Mapping[str, Any]]
Retrieve = Callable[[Mapping[str, Any]], list[Mapping[str, str]]]
Interrupt = Callable[[str], None]

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RECORD_FIELDS = {
    "record_id", "source_id", "page_slug", "locator", "claim_sha256",
    "raw_pointer", "raw_sha256", "normalized_pointer", "normalized_sha256",
    "canonical_url", "source_revision", "retrieved_at", "evidence_lane",
    "evidence_class", "epistemic_layer", "rights_state", "candidate_status",
    "contradiction_group", "contradiction_stance",
}
_PACKAGE_FIELDS = {"record", "doctrine", "page_path"}
_DOCTRINE_FIELDS = {"concept_key", "title", "statement", "rationale"}


def _strict_bytes(value: Any, *, pretty: bool = False) -> bytes:
    try:
        if pretty:
            text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        else:
            text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ShadowCycleError(f"shadow state is not strict JSON: {exc}") from exc
    return (text + "\n").encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_strict_bytes(value)).hexdigest()


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = _strict_bytes(value, pretty=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if path.read_bytes() != encoded:
            raise ShadowCycleError(f"atomic readback mismatch: {path}")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ShadowCycleError(f"state path is not a regular file: {path}")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ShadowCycleError(f"invalid strict JSON state: {path}") from exc
    if not isinstance(value, dict):
        raise ShadowCycleError(f"state must be an object: {path}")
    return value


def _owned_regular_file(path_value: Any, root: Path, field: str) -> Path:
    path = Path(path_value)
    if path.is_symlink() or not path.is_file():
        raise ShadowCycleError(f"{field} must be a regular existing file")
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ShadowCycleError(f"{field} is outside the owned root") from exc
    return resolved


def _validate_package(
    value: Mapping[str, Any], selected: CandidateRecord, paths: ShadowCyclePaths
) -> dict[str, Any]:
    package = dict(value)
    if set(package) != _PACKAGE_FIELDS or not isinstance(package.get("record"), Mapping) or not isinstance(package.get("doctrine"), Mapping):
        raise ShadowCycleError("acquisition package fields are invalid")
    record = dict(package["record"])
    doctrine = dict(package["doctrine"])
    if set(record) != _RECORD_FIELDS or set(doctrine) != _DOCTRINE_FIELDS:
        raise ShadowCycleError("acquisition record/doctrine fields are invalid")
    if record["record_id"] != selected.candidate_id or record["canonical_url"] != selected.canonical_url:
        raise ShadowCycleError("acquisition package does not bind the selected candidate")
    if record["rights_state"] != "public_rights_clear" or record["source_id"] != "corpora":
        raise ShadowCycleError("acquisition package violates rights or source boundary")
    page_path = _owned_regular_file(package["page_path"], paths.corpus_root, "acquisition page_path")
    for pointer_name, digest_name in (("raw_pointer", "raw_sha256"), ("normalized_pointer", "normalized_sha256")):
        pointer = _owned_regular_file(record[pointer_name], paths.archive_root, pointer_name)
        actual = hashlib.sha256(pointer.read_bytes()).hexdigest()
        if actual != record[digest_name]:
            raise ShadowCycleError(f"{pointer_name} digest mismatch")
    return {"record": record, "doctrine": doctrine, "page_path": str(page_path)}


def _citation(record: Mapping[str, Any]) -> dict[str, str]:
    return {
        "source_id": record["source_id"],
        "page_slug": record["page_slug"],
        "locator": record["locator"],
        "claim_sha256": record["claim_sha256"],
        "evidence_class": record["evidence_class"],
    }


def _doctrine_decision(engine: DoctrineEngine, package: Mapping[str, Any], *, cycle_id: str) -> dict[str, Any]:
    proposal = package["doctrine"]
    citation = _citation(package["record"])
    key = proposal["concept_key"]
    current = engine.resolve(key)
    command_id = f"shadow:{cycle_id}:doctrine"
    if current is None:
        concept = engine.propose(
            command_id=command_id,
            concept_key=key,
            title=proposal["title"],
            statement=proposal["statement"],
            citations=[citation],
            rationale=proposal["rationale"],
        )
        return {"decision": "proposed", **concept}
    if citation in current["citations"] and current["statement"] == proposal["statement"]:
        return {
            "decision": "unchanged",
            "reason": "same statement and citation already present",
            **current,
        }
    concept = engine.revise(
        command_id=command_id,
        concept_key=key,
        statement=proposal["statement"],
        citations=[citation],
        rationale=proposal["rationale"],
    )
    return {"decision": "revised", **concept}


def _receipt_digest(receipt: Mapping[str, Any]) -> str:
    return _digest({key: value for key, value in receipt.items() if key != "receipt_sha256"})


def run_shadow_cycle(
    *,
    cycle_id: str,
    candidates: Iterable[CandidateRecord],
    policy: PriorityPolicy,
    paths: ShadowCyclePaths,
    now: datetime,
    acquire: Acquire,
    sync_corpus: SyncCorpus,
    retrieve: Retrieve,
    interrupt_after: Interrupt | None = None,
) -> dict[str, Any]:
    """Run or resume one bounded no-promotion shadow cycle."""
    if not isinstance(cycle_id, str) or not _SAFE_ID.fullmatch(cycle_id):
        raise ValueError("cycle_id must be a safe 1-128 character identifier")
    if not isinstance(policy, PriorityPolicy):
        raise ValueError("policy must be a PriorityPolicy")
    if policy.automatic_promotion_enabled:
        raise ShadowCycleError("shadow cycle requires automatic promotion disabled")
    started_at = _iso(now)
    candidate_values = tuple(candidates)
    if not candidate_values or any(not isinstance(item, CandidateRecord) for item in candidate_values):
        raise ValueError("candidates must contain CandidateRecord values")
    if len({item.candidate_id for item in candidate_values}) != len(candidate_values):
        raise ValueError("candidate identities must be unique")
    input_contract = {
        "schema_version": 1,
        "cycle_id": cycle_id,
        "candidates": sorted((item.to_dict() for item in candidate_values), key=lambda item: item["candidate_id"]),
        "policy": policy.to_dict(),
    }
    input_sha256 = _digest(input_contract)
    state_path = paths.state_root / "cycles" / f"{cycle_id}.json"
    receipt_path = paths.state_root / "receipts" / f"{cycle_id}.json"

    if receipt_path.exists():
        receipt = _load_object(receipt_path)
        if receipt.get("input_sha256") != input_sha256:
            raise ShadowCycleError(f"cycle_id conflict: {cycle_id}")
        if receipt.get("receipt_sha256") != _receipt_digest(receipt):
            raise ShadowCycleError("final receipt digest mismatch")
        return receipt

    if state_path.exists():
        state = _load_object(state_path)
        if state.get("input_sha256") != input_sha256:
            raise ShadowCycleError(f"cycle_id conflict: {cycle_id}")
        started_at = state["started_at"]
    else:
        state = {
            "schema_version": 1,
            "cycle_id": cycle_id,
            "input_sha256": input_sha256,
            "started_at": started_at,
            "phases": {},
        }
        _atomic_write(state_path, state)

    public_candidates = [item for item in candidate_values if item.rights_state == "public_rights_clear"]
    if not public_candidates:
        raise ShadowCycleError("no public rights-clear acquisition candidate")

    shadow_policy = replace(policy, max_items_per_cycle=1)
    tasks = tuple(CandidateTask(candidate=item, material_delta=True) for item in candidate_values)
    budget = BudgetLedger(paths.state_root / "budget.json")
    plan = budget.reserve_cycle(
        f"shadow:{cycle_id}", tasks, shadow_policy,
        now=datetime.fromisoformat(started_at.replace("Z", "+00:00")),
    )
    selected_decision = next(
        (item for item in plan.selected if item.action == "acquire" and item.auto_acquire_eligible),
        None,
    )
    if selected_decision is None:
        raise ShadowCycleError("no public rights-clear acquisition selected under budget")
    selected = next(item for item in candidate_values if item.candidate_id == selected_decision.candidate_id)
    state["phases"].setdefault("ranked", plan.to_dict())
    _atomic_write(state_path, state)
    if interrupt_after:
        interrupt_after("ranked")

    if "acquired" in state["phases"]:
        package = _validate_package(state["phases"]["acquired"], selected, paths)
    else:
        package = _validate_package(acquire(selected, paths), selected, paths)
        state["phases"]["acquired"] = package
        _atomic_write(state_path, state)
    if interrupt_after:
        interrupt_after("acquired")

    if "synced" not in state["phases"]:
        sync_receipt = dict(sync_corpus(package))
        if sync_receipt.get("source_id") != "corpora":
            raise ShadowCycleError("sync receipt is not source-scoped to corpora")
        if package["record"]["page_slug"] not in sync_receipt.get("changed_pages", []):
            raise ShadowCycleError("sync receipt does not include acquired page")
        state["phases"]["synced"] = sync_receipt
        _atomic_write(state_path, state)
    if interrupt_after:
        interrupt_after("synced")

    doctrine = DoctrineEngine(paths.state_root / "doctrine.jsonl")
    doctrine_decision = _doctrine_decision(doctrine, package, cycle_id=cycle_id)
    if interrupt_after:
        interrupt_after("doctrine_applied")
    state["phases"]["doctrine"] = doctrine_decision
    _atomic_write(state_path, state)

    results = [dict(item) for item in retrieve(package)]
    retrieval_cases = [{
        "case_id": f"shadow-{cycle_id}",
        "query": package["doctrine"]["concept_key"].replace("-", " "),
        "expected_page_slugs": [package["record"]["page_slug"]],
        "results": results,
    }]
    usage = budget.usage_for_day(datetime.fromisoformat(started_at.replace("Z", "+00:00")))
    doctrine_snapshot = doctrine.snapshot()
    doctrine_lineages = {
        concept["concept_key"]: doctrine.lineage(concept["concept_key"])
        for concept in doctrine_snapshot["concepts"]
    }
    evaluation = evaluate_corpus(
        records=[package["record"]],
        doctrine_snapshot=doctrine_snapshot,
        doctrine_lineages=doctrine_lineages,
        retrieval_cases=retrieval_cases,
        usage={
            "deep_acquisitions": usage.deep_acquisitions,
            "llm_tasks": usage.llm_tasks,
            "llm_tokens": usage.llm_tokens,
            "estimated_cost_usd": usage.estimated_cost_usd,
            "actual_cost_usd": 0.0,
        },
        policy={
            "max_deep_acquisitions_per_utc_day": policy.max_deep_acquisitions_per_utc_day,
            "max_llm_tasks_per_utc_day": policy.max_llm_tasks_per_utc_day,
            "max_llm_tokens_per_utc_day": policy.max_llm_tokens_per_utc_day,
            "max_cost_usd_per_utc_day": policy.max_cost_usd_per_utc_day,
            "max_freshness_days": 30,
            "min_evidence_lanes": 1,
        },
        evaluated_at=package["record"]["retrieved_at"],
    )
    if not evaluation["passed"]:
        state["phases"]["evaluation_failures"] = evaluation["failures"]
        _atomic_write(state_path, state)
        raise ShadowCycleError("evaluation failed: " + ", ".join(evaluation["failures"]))

    remaining = [item for item in plan.decisions if item.candidate_id != selected.candidate_id]
    next_gap = remaining[0].to_dict() if remaining else {
        "candidate_id": selected.candidate_id,
        "reason_codes": ["refresh_discovery_universe"],
    }
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "cycle_id": cycle_id,
        "input_sha256": input_sha256,
        "started_at": started_at,
        "status": "verified_shadow_complete",
        "selected": selected_decision.to_dict(),
        "acquisition": package,
        "sync": state["phases"]["synced"],
        "doctrine_decision": doctrine_decision,
        "evaluation": evaluation,
        "next_gap": next_gap,
        "automatic_promotion_enabled": False,
        "production_mutation": False,
        "llm_calls": 0,
    }
    receipt["receipt_sha256"] = _receipt_digest(receipt)
    _atomic_write(receipt_path, receipt)
    state["phases"]["complete"] = {
        "receipt_path": str(receipt_path),
        "receipt_sha256": receipt["receipt_sha256"],
    }
    _atomic_write(state_path, state)
    if interrupt_after:
        interrupt_after("complete")
    return receipt
