#!/usr/bin/env python3
"""Generic eval-before-doctrine transaction for the generalized Corpus Engine.

The transaction enforces the ordering the old Agentic shadow cycle lacked:

    1. stage/acquire       — produce a package and stage its corpus page
    2. sync/retrieval       — sync the staged page and verify retrieval
    3. integrity + behavior — deterministic integrity and behavioral evaluation
    4. doctrine commit      — ONLY if every prior gate passed

Doctrine bytes never change before evaluation succeeds. A retrieval, integrity,
or behavior regression quarantines the staged corpus write with exact-byte
(SHA-256) evidence and leaves the doctrine ledger untouched. State is written
atomically after each phase, so a crash after evaluation but before the doctrine
commit resumes idempotently (the commit callback must be command-id idempotent).
The committed doctrine is hash-bound to the behavior-eval report digest.

All external effects are injected, so fixtures, copied-live, and deployed runs
exercise one domain-agnostic state machine.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class EvalGateError(RuntimeError):
    """The eval-gated transaction cannot safely commit doctrine."""


@dataclass(frozen=True)
class GatePaths:
    state_root: Path
    corpus_root: Path
    quarantine_root: Path

    def __post_init__(self) -> None:
        for name in ("state_root", "corpus_root", "quarantine_root"):
            value = getattr(self, name)
            if not isinstance(value, Path):
                object.__setattr__(self, name, Path(value))


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _quarantine_staged(paths: GatePaths, cycle_id: str, package: dict[str, Any], reason: str) -> dict[str, Any]:
    """Move the staged corpus page into quarantine, recording exact-byte proof."""
    items: list[dict[str, Any]] = []
    staged_path = Path(package["staged_path"])
    if staged_path.is_file():
        sha = hashlib.sha256(staged_path.read_bytes()).hexdigest()
        destination_dir = paths.quarantine_root / cycle_id
        destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = destination_dir / staged_path.name
        shutil.move(str(staged_path), str(destination))
        items.append({
            "original_path": str(staged_path),
            "quarantined_path": str(destination),
            "sha256": sha,
            "expected_sha256": package.get("staged_sha256"),
        })
    return {"reason": reason, "items": items}


def run_eval_gated_cycle(
    *,
    cycle_id: str,
    domain: str,
    paths: GatePaths,
    acquire: Callable[[], dict[str, Any]],
    sync_corpus: Callable[[dict[str, Any]], dict[str, Any]],
    retrieve: Callable[[dict[str, Any]], list[dict[str, Any]]],
    integrity_eval: Callable[[dict[str, Any]], dict[str, Any]],
    behavior_eval: Callable[[dict[str, Any]], dict[str, Any]],
    commit_doctrine: Callable[[dict[str, Any], str], dict[str, Any]],
    now: str,
    interrupt_after: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if not isinstance(cycle_id, str) or not _SAFE_ID.fullmatch(cycle_id):
        raise ValueError("cycle_id must be a safe 1-128 character identifier")
    if not isinstance(domain, str) or not domain.strip():
        raise ValueError("domain is required")

    state_path = paths.state_root / "gate" / f"{cycle_id}.json"
    if state_path.exists():
        state = _load(state_path)
        if state.get("domain") != domain:
            raise EvalGateError(f"cycle_id conflict for domain {domain!r}")
    else:
        state = {"schema_version": 1, "cycle_id": cycle_id, "domain": domain, "started_at": now, "phases": {}}
        _atomic_write(state_path, state)

    phases = state["phases"]

    # 1. stage/acquire
    if "acquired" not in phases:
        package = acquire()
        staged = Path(package["staged_path"])
        if not staged.is_file():
            raise EvalGateError("acquire did not stage a corpus page file")
        actual = hashlib.sha256(staged.read_bytes()).hexdigest()
        if actual != package.get("staged_sha256"):
            raise EvalGateError("staged page sha256 does not match package")
        phases["acquired"] = package
        _atomic_write(state_path, state)
    package = phases["acquired"]
    if interrupt_after:
        interrupt_after("acquired")

    # 2. sync + retrieval verification
    if "synced" not in phases:
        sync_receipt = sync_corpus(package)
        if sync_receipt.get("source_id") != "corpora" or package["page_slug"] not in sync_receipt.get("changed_pages", []):
            phases["quarantine"] = _quarantine_staged(paths, cycle_id, package, "sync_receipt_invalid")
            _atomic_write(state_path, state)
            raise EvalGateError("sync receipt invalid or missing the staged page")
        phases["synced"] = sync_receipt
        _atomic_write(state_path, state)
    if interrupt_after:
        interrupt_after("synced")

    if "retrieval_verified" not in phases:
        results = retrieve(package)
        hit = any(item.get("page_slug") == package["page_slug"] for item in results)
        if not hit:
            phases["quarantine"] = _quarantine_staged(paths, cycle_id, package, "retrieval_regression")
            _atomic_write(state_path, state)
            raise EvalGateError("retrieval verification failed: staged page not retrievable")
        phases["retrieval_verified"] = {"results": results}
        _atomic_write(state_path, state)
    if interrupt_after:
        interrupt_after("retrieval_verified")

    # 3. integrity + behavioral evaluation (BEFORE any doctrine mutation)
    if "evaluated" not in phases:
        integrity = integrity_eval(package)
        if not integrity.get("passed"):
            phases["quarantine"] = _quarantine_staged(paths, cycle_id, package, "integrity_failed")
            phases["integrity_failures"] = integrity.get("failures", [])
            _atomic_write(state_path, state)
            raise EvalGateError("integrity evaluation failed")
        behavior = behavior_eval(package)
        digest = behavior.get("report_digest")
        if not isinstance(digest, str) or not digest:
            raise EvalGateError("behavior evaluation must return a report_digest")
        if behavior.get("blocks_doctrine"):
            phases["quarantine"] = _quarantine_staged(paths, cycle_id, package, "behavior_regression")
            phases["behavior_report_digest"] = digest
            _atomic_write(state_path, state)
            raise EvalGateError("behavioral evaluation blocks doctrine (regression)")
        phases["evaluated"] = {"integrity": integrity, "behavior": behavior, "behavior_report_digest": digest}
        _atomic_write(state_path, state)
    if interrupt_after:
        interrupt_after("evaluated")

    # 4. doctrine commit (only reached when every gate passed)
    behavior_report_digest = phases["evaluated"]["behavior_report_digest"]
    if "committed" not in phases:
        doctrine_event = commit_doctrine(package, behavior_report_digest)
        if doctrine_event.get("behavior_report_digest") != behavior_report_digest:
            raise EvalGateError("doctrine event is not hash-bound to the behavior report")
        phases["committed"] = doctrine_event
        _atomic_write(state_path, state)
    doctrine_event = phases["committed"]
    if interrupt_after:
        interrupt_after("committed")

    receipt = {
        "schema_version": 1,
        "cycle_id": cycle_id,
        "domain": domain,
        "status": "committed",
        "started_at": state["started_at"],
        "sync": phases["synced"],
        "retrieval": phases["retrieval_verified"],
        "evaluation": phases["evaluated"],
        "doctrine": doctrine_event,
        "behavior_bound": doctrine_event.get("behavior_report_digest") == behavior_report_digest,
        "automatic_promotion_enabled": False,
        "production_mutation": False,
    }
    return receipt
