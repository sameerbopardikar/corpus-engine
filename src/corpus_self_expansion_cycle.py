#!/usr/bin/env python3
"""Bounded self-expansion phase for the normal global corpus cycle.

The phase reuses the source graph, discovery ledger, candidate policy, durable
queue, and watch consumer.  It owns no scheduler and grants no rights: source
bodies and watch candidates must already carry explicit body-inspection rights.
"""
from __future__ import annotations

import json
import os
import re
import signal
import stat
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from corpus_candidate_policy import evaluate_candidates
from corpus_discovery import DiscoveryEngine
from corpus_entity_identity import canonical_entity_identity
from corpus_source_graph import ingest_relationships
from corpus_watch_inspection import (
    project_preserved_source_artifact,
    run_watch_inspection,
)

SCHEMA_VERSION = 1
_BODY_RIGHTS = frozenset({"public_rights_clear", "private_authorized"})
_DOMAIN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class SelfExpansionCycleError(ValueError):
    """The self-expansion configuration or state cannot be safely processed."""


class SelfExpansionDeadlineExceeded(SelfExpansionCycleError):
    """The hard self-expansion wall-clock deadline expired."""


def _reject_symlink_components(path: Path) -> Path:
    """Return an absolute path only when every existing component is non-symlink."""
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            # Once a component does not exist, no descendant can exist yet.
            break
        if stat.S_ISLNK(mode):
            raise SelfExpansionCycleError(
                f"state authority path contains symlink component: {current}"
            )
    return absolute


def _deadline_check(started: float, maximum: float, monotonic) -> None:
    if monotonic() - started >= maximum:
        raise SelfExpansionDeadlineExceeded("max_wall_seconds exceeded")


@contextmanager
def _hard_wall_deadline(seconds: float, *, enabled: bool):
    """Preempt a stalled phase on POSIX while preserving any prior alarm."""
    if not enabled or threading.current_thread() is not threading.main_thread():
        yield
        return
    started = time.monotonic()
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_delay, previous_interval = signal.getitimer(signal.ITIMER_REAL)

    def _expired(_signum, _frame):
        raise SelfExpansionDeadlineExceeded("max_wall_seconds exceeded")

    signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_delay > 0:
            elapsed = time.monotonic() - started
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.000001, previous_delay - elapsed),
                previous_interval,
            )


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
    bounded = dict(value)
    bounded.setdefault("max_source_artifacts_per_cycle", 8)
    bounded.setdefault("max_artifacts_per_inspection", 4)
    bounded.setdefault("max_total_artifact_bytes_per_cycle", normalized["max_artifact_bytes"])
    bounded.setdefault("max_relationships_per_cycle", normalized["max_relationships_per_artifact"])
    normalized["max_source_artifacts_per_cycle"] = _positive_int(
        bounded, "max_source_artifacts_per_cycle", maximum=100
    )
    normalized["max_artifacts_per_inspection"] = _positive_int(
        bounded, "max_artifacts_per_inspection", maximum=100
    )
    normalized["max_total_artifact_bytes_per_cycle"] = _positive_int(
        bounded, "max_total_artifact_bytes_per_cycle", maximum=64 * 1024 * 1024
    )
    normalized["max_relationships_per_cycle"] = _positive_int(
        bounded, "max_relationships_per_cycle", maximum=10000
    )
    normalized["max_wall_seconds"] = _positive_number(
        value, "max_wall_seconds", maximum=3600
    )
    return normalized


def _open_directory_at(parent_fd: int, name: str) -> int:
    if name in {"", ".", ".."} or "/" in name:
        raise SelfExpansionCycleError("unsafe state authority component")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        try:
            return os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise SelfExpansionCycleError(
                f"state authority component is not a no-follow directory: {name}"
            ) from exc
    except OSError as exc:
        raise SelfExpansionCycleError(
            f"state authority component is not a no-follow directory: {name}"
        ) from exc


def _open_absolute_directory(path: Path) -> tuple[int, Path]:
    expanded = path.expanduser()
    if any(part in {".", ".."} for part in expanded.parts):
        raise SelfExpansionCycleError("state_root cannot contain traversal components")
    absolute = Path(os.path.abspath(os.fspath(expanded)))
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            next_fd = _open_directory_at(fd, part)
            os.close(fd)
            fd = next_fd
        return fd, absolute
    except Exception:
        os.close(fd)
        raise


@contextmanager
def _paths(state_root: Path, domain: str, state_path: str | None = None):
    """Hold identity-bound directory authorities for one complete domain cycle."""
    base_fd, base = _open_absolute_directory(state_root)
    held = [base_fd]
    try:
        domain_fd = _open_directory_at(base_fd, domain)
        held.append(domain_fd)
        domain_base = base / domain
        if state_path is None:
            root_fd = os.dup(domain_fd)
            root = domain_base
        else:
            relative = Path(state_path)
            if (
                relative.is_absolute()
                or not relative.parts
                or relative.parts[0] != domain
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise SelfExpansionCycleError(
                    "state_path must be a relative path rooted beneath its domain"
                )
            root_fd = os.dup(domain_fd)
            root = domain_base
            for part in relative.parts[1:]:
                next_fd = _open_directory_at(root_fd, part)
                os.close(root_fd)
                root_fd = next_fd
                root /= part
        held.append(root_fd)
        artifacts_fd = _open_directory_at(root_fd, "artifacts")
        receipts_fd = _open_directory_at(root_fd, "receipts")
        held.extend([artifacts_fd, receipts_fd])
        expected = {
            base: os.fstat(base_fd),
            domain_base: os.fstat(domain_fd),
            root: os.fstat(root_fd),
            root / "artifacts": os.fstat(artifacts_fd),
            root / "receipts": os.fstat(receipts_fd),
        }

        def guard() -> None:
            for authority, fingerprint in expected.items():
                try:
                    current = authority.stat(follow_symlinks=False)
                except (FileNotFoundError, OSError) as exc:
                    raise SelfExpansionCycleError(
                        f"state authority changed during cycle: {authority}"
                    ) from exc
                if (
                    stat.S_ISLNK(current.st_mode)
                    or not stat.S_ISDIR(current.st_mode)
                    or (current.st_dev, current.st_ino)
                    != (fingerprint.st_dev, fingerprint.st_ino)
                ):
                    raise SelfExpansionCycleError(
                        f"state authority changed during cycle: {authority}"
                    )

        guard()
        bound_root = Path(f"/proc/self/fd/{root_fd}")
        bound_artifacts = Path(f"/proc/self/fd/{artifacts_fd}")
        bound_receipts = Path(f"/proc/self/fd/{receipts_fd}")
        yield {
            "root": root,
            "ledger": bound_root / "discovery-ledger.jsonl",
            "graph": bound_root / "source-graph.jsonl",
            "watch": bound_root / "watch-projection.json",
            "artifacts": bound_artifacts,
            "receipts": bound_receipts,
            "canonical_artifacts": root / "artifacts",
            "canonical_receipts": root / "receipts",
            "guard": guard,
        }
    finally:
        for fd in reversed(held):
            try:
                os.close(fd)
            except OSError:
                pass


def _candidate_count(ledger: Path) -> int:
    return len(DiscoveryEngine(ledger).candidates)


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
    with _paths(
        Path(config["state_root"]), domain, domain_config.get("state_path")
    ) as paths:
        paths["guard"]()
        return _run_domain_bound(
            domain_config,
            config=config,
            now=now,
            started=started,
            monotonic=monotonic,
            paths=paths,
        )


def _run_domain_bound(
    domain_config: dict[str, Any],
    *,
    config: dict[str, Any],
    now: datetime,
    started: float,
    monotonic,
    paths: dict[str, Any],
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
    if len(source_artifacts) > config["max_source_artifacts_per_cycle"]:
        raise SelfExpansionCycleError(
            "source_artifacts exceed max_source_artifacts_per_cycle"
        )

    paths["guard"]()
    pre_due_engine = DiscoveryEngine(paths["ledger"])
    initial_candidate_count = len(pre_due_engine.candidates)
    leased_at_start = {
        item.work_id for item in pre_due_engine.work_items.values() if item.state == "leased"
    }
    # Only work that existed before this global-cycle phase is eligible.  This
    # forces a newly persisted Cycle-A work_id to be consumed by a later cycle.
    due_at_start = _due_work_ids(paths["ledger"], now)
    failures: list[dict[str, str]] = []
    source_receipts: list[dict[str, Any]] = []
    source_relationships = 0
    artifact_bytes_used = 0
    relationships_used = 0
    appended = 0
    produced = []

    for index, artifact in enumerate(source_artifacts):
        _deadline_check(started, config["max_wall_seconds"], monotonic)
        try:
            paths["guard"]()
            remaining_bytes = config["max_total_artifact_bytes_per_cycle"] - artifact_bytes_used
            remaining_relationships = config["max_relationships_per_cycle"] - relationships_used
            if remaining_bytes <= 0 or remaining_relationships <= 0:
                raise SelfExpansionCycleError("self-expansion aggregate budget exhausted")
            receipt, relationships = project_preserved_source_artifact(
                domain=domain,
                request=artifact,
                preserve_root=paths["artifacts"],
                max_artifact_bytes=min(config["max_artifact_bytes"], remaining_bytes),
                max_relationships=min(
                    config["max_relationships_per_artifact"], remaining_relationships
                ),
            )
            paths["guard"]()
            _deadline_check(started, config["max_wall_seconds"], monotonic)
            artifact_bytes_used += receipt["byte_length"]
            relationships_used += len(relationships)
            source_receipts.append(receipt)
            source_relationships += len(relationships)
            produced.extend(relationships)
        except SelfExpansionDeadlineExceeded:
            raise
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
        if (
            identity not in projected_identities
            and len(projected_identities) - len(existing_identities)
            >= config["max_candidates_per_cycle"]
        ):
            failures.append({
                "stage": "candidate_cap",
                "error": "relationship projection would exceed max_candidates_per_cycle",
            })
            continue
        projected_identities.add(identity)
        admitted_produced.append(relationship)
        by_identity.setdefault(identity, relationship)
    produced = admitted_produced
    for relationship in produced:
        paths["guard"]()
        result = ingest_relationships(
            [relationship],
            graph_path=paths["graph"],
            discovery_ledger_path=paths["ledger"],
            max_items=config["max_relationships_per_artifact"],
        )
        paths["guard"]()
        appended += result["relationships_appended"]

    rights_engine = DiscoveryEngine(paths["ledger"])
    for index, assertion in enumerate(rights_assertions):
        try:
            paths["guard"]()
            if not isinstance(assertion, dict) or set(assertion) != {"canonical_url", "rights_state"}:
                raise SelfExpansionCycleError("rights assertion fields must be canonical_url and rights_state")
            identity = canonical_entity_identity(assertion["canonical_url"])
            rights = assertion["rights_state"]
            if rights not in _BODY_RIGHTS:
                raise SelfExpansionCycleError("rights assertion is not body-authorized")
            relationship = by_identity.get(identity)
            existing = next(
                (item for item in rights_engine.candidates.values()
                 if canonical_entity_identity(item.canonical_url) == identity),
                None,
            )
            if existing is None:
                raise SelfExpansionCycleError("rights assertion does not bind a durable candidate")
            if relationship is None and existing.rights_state != rights:
                # A config replay may restate already-durable rights, but it may
                # not upgrade an unrelated candidate without a source artifact
                # produced and grounded in this cycle.
                raise SelfExpansionCycleError("rights assertion does not bind a produced candidate")
            rights_engine.assert_rights(
                existing.candidate_id,
                rights,
                asserted_by="self-expansion-config",
                basis="explicit-body-authorization",
                asserted_at=_iso(now),
            )
            paths["guard"]()
            _deadline_check(started, config["max_wall_seconds"], monotonic)
        except SelfExpansionDeadlineExceeded:
            raise
        except Exception as exc:
            failures.append({"stage": f"rights_assertion[{index}]", "error": f"{type(exc).__name__}: {exc}"})

    candidate_count = _candidate_count(paths["ledger"])
    policy = None
    if config["promotion_mode"] == "live":
        paths["guard"]()
        policy = evaluate_candidates(
            ledger_path=paths["ledger"],
            graph_path=paths["graph"],
            watch_projection_path=paths["watch"],
            evaluated_at=_iso(now),
        )
        paths["guard"]()

    inspection_results: list[dict[str, Any]] = []
    consumed = 0
    attempted = 0
    recovered = 0
    for index, inspection in enumerate(inspections):
        _deadline_check(started, config["max_wall_seconds"], monotonic)
        try:
            work_id = _inspection_work_id(paths["ledger"], paths["watch"], inspection)
        except SelfExpansionDeadlineExceeded:
            raise
        except Exception as exc:
            failures.append({"stage": f"inspection[{index}]", "error": f"{type(exc).__name__}: {exc}"})
            continue
        if work_id is None or work_id not in due_at_start:
            continue
        if attempted >= config["max_inspections_per_cycle"]:
            break
        attempted += 1
        try:
            if work_id in leased_at_start:
                recovered += 1
            remaining_bytes = config["max_total_artifact_bytes_per_cycle"] - artifact_bytes_used
            remaining_relationships = config["max_relationships_per_cycle"] - relationships_used
            if remaining_bytes <= 0 or remaining_relationships <= 0:
                raise SelfExpansionCycleError("self-expansion aggregate budget exhausted")
            result = run_watch_inspection(
                domain=domain,
                watch_projection_path=paths["watch"],
                ledger_path=paths["ledger"],
                graph_path=paths["graph"],
                inspections=[inspection],
                preserve_root=paths["artifacts"],
                receipts_root=paths["receipts"],
                now=now,
                max_artifact_bytes=min(config["max_artifact_bytes"], remaining_bytes),
                max_artifacts_per_inspection=config["max_artifacts_per_inspection"],
                max_total_artifact_bytes=remaining_bytes,
                max_relationships_per_artifact=min(
                    config["max_relationships_per_artifact"], remaining_relationships
                ),
                max_total_relationships=remaining_relationships,
                max_candidate_count=initial_candidate_count + config["max_candidates_per_cycle"],
                authority_guard=paths["guard"],
                receipt_reference_root=paths["canonical_receipts"],
            )
            _deadline_check(started, config["max_wall_seconds"], monotonic)
            artifact_bytes_used += result["artifact_bytes_preserved"]
            relationships_used += result["relationships_derived"]
            inspection_results.append(result)
            consumed += result["processed_inspections"]
        except SelfExpansionDeadlineExceeded:
            raise
        except Exception as exc:
            failures.append({"stage": f"inspection[{index}]", "error": f"{type(exc).__name__}: {exc}"})

    candidate_count_after = _candidate_count(paths["ledger"])
    if candidate_count_after - initial_candidate_count > config["max_candidates_per_cycle"]:
        raise SelfExpansionCycleError(
            "self-expansion exceeded max_candidates_per_cycle additions"
        )
    post_policy = None
    if config["promotion_mode"] == "live" and consumed:
        paths["guard"]()
        post_policy = evaluate_candidates(
            ledger_path=paths["ledger"],
            graph_path=paths["graph"],
            watch_projection_path=paths["watch"],
            evaluated_at=_iso(now),
        )
        paths["guard"]()
    return {
        "domain": domain,
        "status": "partial_failure" if failures else "completed",
        "failures": failures,
        "source_artifacts_processed": len(source_receipts),
        "source_artifact_receipts": source_receipts,
        "source_relationships_derived": source_relationships,
        "artifact_bytes_used": artifact_bytes_used,
        "relationships_used": relationships_used,
        "relationships_appended": appended + sum(
            item["relationships_appended"] for item in inspection_results
        ),
        "candidate_count_before_inspection": candidate_count,
        "candidate_count_after": candidate_count_after,
        "policy": policy,
        "post_inspection_policy": post_policy,
        "due_work_ids_at_cycle_start": sorted(due_at_start),
        "inspections_attempted": attempted,
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
    with _hard_wall_deadline(
        config["max_wall_seconds"], enabled=monotonic is time.monotonic
    ):
        for index, domain_config in enumerate(config["domains"]):
            try:
                _deadline_check(started, config["max_wall_seconds"], monotonic)
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
                _deadline_check(started, config["max_wall_seconds"], monotonic)
            except SelfExpansionDeadlineExceeded as exc:
                failures.append({
                    "domain_index": str(index),
                    "error": f"{type(exc).__name__}: {exc}",
                })
                break
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
                "max_source_artifacts_per_cycle",
                "max_artifacts_per_inspection",
                "max_relationships_per_artifact",
                "max_relationships_per_cycle",
                "max_candidates_per_cycle",
                "max_artifact_bytes",
                "max_total_artifact_bytes_per_cycle",
                "max_wall_seconds",
            )
        },
        "failures": failures,
        "domains": domains,
    }
