#!/usr/bin/env python3
"""Durable discovery ledger, scoring, deduplication, and priority queue.

One append-only strict-JSONL ledger is authoritative. Every mutating engine
operation takes a cross-process transaction lock, replays current state, checks
invariants, appends and fsyncs one event, then updates the in-memory projection.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from corpus_engine_models import (
    CandidateObservation,
    CandidateRecord,
    SCORE_COMPONENTS,
    WorkItem,
    iso,
    parse_iso,
    utcnow,
)

LEDGER_SCHEMA_VERSION = 1
PRIORITY_KEY = "priority_score"
_REQUIRED_EVENT_FIELDS = ("schema_version", "event_id", "event_type", "recorded_at", "payload")
_EVENT_TYPES = {
    "candidate_observed",
    "candidate_transitioned",
    "work_enqueued",
    "work_state_changed",
}


class DiscoveryLedgerError(Exception):
    """Base error for durable discovery-ledger failures."""


class LedgerCorruptionError(DiscoveryLedgerError):
    """Raised when a ledger cannot be replayed safely."""


def _ensure_private_regular_file(path: Path) -> None:
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise DiscoveryLedgerError(f"ledger path is not a regular file: {path}")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def serialize_observation(observation: CandidateObservation) -> dict[str, Any]:
    return observation.to_dict()


def deserialize_observation(data: Mapping[str, Any]) -> CandidateObservation:
    return CandidateObservation.from_dict(data)


def serialize_candidate(record: CandidateRecord) -> dict[str, Any]:
    return record.to_dict()


def deserialize_candidate(data: Mapping[str, Any]) -> CandidateRecord:
    return CandidateRecord.from_dict(data)


def serialize_work(item: WorkItem) -> dict[str, Any]:
    return item.to_dict()


def deserialize_work(data: Mapping[str, Any]) -> WorkItem:
    return WorkItem.from_dict(data)


_LANE_AUTHORITY = {
    "canonical": 0.9,
    "scientific": 0.85,
    "scientific-evaluation": 0.85,
    "production": 0.75,
    "production-architecture": 0.75,
    "production-reliability": 0.8,
    "production-deployment": 0.75,
    "security-evaluation": 0.85,
    "security-incident": 0.85,
    "foundational": 0.9,
    "practitioner": 0.55,
    "practitioner-implementation": 0.65,
    "implementation-research": 0.75,
    "zeitgeist": 0.35,
}
_DEFAULT_LANE_AUTHORITY = 0.4
_ENTITY_PRODUCTION_VALUE = {
    "paper": 0.85,
    "benchmark": 0.8,
    "repository": 0.7,
    "channel": 0.55,
    "community": 0.45,
    "document": 0.5,
    "creator": 0.5,
    "other": 0.3,
}
_DEFAULT_ENTITY_PRODUCTION_VALUE = 0.3
_DEMONSTRATED_PRACTICE_ENTITY_TYPES = {"repository", "benchmark"}


def score_observation(observation: CandidateObservation) -> tuple[Mapping[str, float], str]:
    """Derive deterministic baseline scores from inspectable observation fields."""
    lane = observation.evidence_lane.strip().lower()
    authority = _LANE_AUTHORITY.get(lane, _DEFAULT_LANE_AUTHORITY)
    production_value = _ENTITY_PRODUCTION_VALUE.get(
        observation.entity_type, _DEFAULT_ENTITY_PRODUCTION_VALUE
    )
    relevance = min(0.4 + 0.1 * len(observation.topics), 1.0)
    demonstrated = 0.6 if observation.entity_type in _DEMONSTRATED_PRACTICE_ENTITY_TYPES else 0.4
    scores = MappingProxyType({
        "authority": authority,
        "demonstrated_practice": demonstrated,
        "novelty": 0.5,
        "relevance": relevance,
        "corroboration": 0.2,
        "production_or_scientific_value": production_value,
        "cost": 0.3,
    })
    assert set(scores) == set(SCORE_COMPONENTS)
    rationale = (
        f"authority={authority} from evidence_lane={lane!r}; "
        f"production_or_scientific_value={production_value} from entity_type={observation.entity_type!r}; "
        f"relevance={relevance} from topic_count={len(observation.topics)}; "
        f"demonstrated_practice={demonstrated} from entity_type; "
        "novelty=0.5, corroboration=0.2, cost=0.3 are deterministic baseline priors."
    )
    return scores, rationale


class DiscoveryLedger:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        _ensure_private_regular_file(self.path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        _ensure_private_regular_file(self.lock_path)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with open(self.lock_path, "a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _validate_event(event: Any, *, location: str) -> dict[str, Any]:
        if not isinstance(event, dict):
            raise LedgerCorruptionError(f"ledger event at {location} is not a JSON object")
        for field_name in _REQUIRED_EVENT_FIELDS:
            if field_name not in event:
                raise LedgerCorruptionError(
                    f"ledger event at {location} missing required field {field_name!r}"
                )
        if event["schema_version"] != LEDGER_SCHEMA_VERSION:
            raise LedgerCorruptionError(
                f"ledger event at {location} has unsupported schema_version {event['schema_version']!r}"
            )
        if not isinstance(event["event_id"], str) or not event["event_id"].startswith("evt_"):
            raise LedgerCorruptionError(f"ledger event at {location} has invalid event_id")
        if event["event_type"] not in _EVENT_TYPES:
            raise LedgerCorruptionError(
                f"ledger event at {location} has unknown event_type {event['event_type']!r}"
            )
        try:
            parse_iso(event["recorded_at"])
        except (TypeError, ValueError) as exc:
            raise LedgerCorruptionError(f"ledger event at {location} has invalid recorded_at") from exc
        if not isinstance(event["payload"], dict):
            raise LedgerCorruptionError(f"ledger event at {location} payload is not an object")
        return event

    def _repair_valid_unterminated_tail(self, file_handle) -> None:
        file_handle.seek(0, os.SEEK_END)
        size = file_handle.tell()
        if size == 0:
            return
        file_handle.seek(-1, os.SEEK_END)
        if file_handle.read(1) == b"\n":
            return
        file_handle.seek(0)
        raw = file_handle.read()
        tail = raw.rsplit(b"\n", 1)[-1]
        try:
            decoded = json.loads(tail.decode("utf-8"))
            self._validate_event(decoded, location=f"{self.path}:unterminated-tail")
        except (UnicodeDecodeError, json.JSONDecodeError, LedgerCorruptionError) as exc:
            raise LedgerCorruptionError(
                f"cannot append: malformed or truncated final event in {self.path}"
            ) from exc
        file_handle.seek(0, os.SEEK_END)
        file_handle.write(b"\n")
        file_handle.flush()
        os.fsync(file_handle.fileno())

    def append(self, event_type: str, payload: Mapping[str, Any], *, recorded_at=None) -> dict[str, Any]:
        if event_type not in _EVENT_TYPES:
            raise ValueError(f"unsupported event_type: {event_type!r}")
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping")
        event = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "event_id": f"evt_{uuid.uuid4().hex}",
            "event_type": event_type,
            "recorded_at": iso(recorded_at),
            "payload": dict(payload),
        }
        try:
            encoded = json.dumps(
                event,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise ValueError(f"event is not strict-JSON serializable: {exc}") from exc
        with open(self.path, "r+b") as file_handle:
            fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX)
            try:
                self._repair_valid_unterminated_tail(file_handle)
                file_handle.seek(0, os.SEEK_END)
                file_handle.write(encoded)
                file_handle.flush()
                os.fsync(file_handle.fileno())
            finally:
                fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)
        return event

    def read_events(self) -> list[dict[str, Any]]:
        with open(self.path, "rb") as file_handle:
            fcntl.flock(file_handle.fileno(), fcntl.LOCK_SH)
            try:
                raw = file_handle.read()
            finally:
                fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)
        events: list[dict[str, Any]] = []
        event_ids: set[str] = set()
        for line_number, raw_line in enumerate(raw.splitlines(), start=1):
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LedgerCorruptionError(
                    f"malformed or truncated ledger event at {self.path}:{line_number}: {exc}"
                ) from exc
            validated = self._validate_event(event, location=f"{self.path}:{line_number}")
            event_id = validated["event_id"]
            if event_id in event_ids:
                raise LedgerCorruptionError(f"duplicate event_id at {self.path}:{line_number}: {event_id}")
            event_ids.add(event_id)
            events.append(validated)
        return events


class DiscoveryEngine:
    def __init__(self, ledger_path: Path | str):
        self.ledger = DiscoveryLedger(ledger_path)
        self.candidates: dict[str, CandidateRecord] = {}
        self.work_items: dict[str, WorkItem] = {}
        self._replay()

    def _replay(self) -> None:
        candidates: dict[str, CandidateRecord] = {}
        work_items: dict[str, WorkItem] = {}
        try:
            events = self.ledger.read_events()
            for event in events:
                event_type = event["event_type"]
                payload = event["payload"]
                if event_type == "candidate_observed":
                    observation = deserialize_observation(payload["observation"])
                    candidate_id = observation.candidate_key
                    if candidate_id not in candidates:
                        candidates[candidate_id] = CandidateRecord.from_observation(
                            observation,
                            payload["score_components"],
                            rationale=payload.get("rationale", ""),
                        )
                    else:
                        candidates[candidate_id] = candidates[candidate_id].observe(observation)
                elif event_type == "candidate_transitioned":
                    candidate_id = payload["candidate_id"]
                    if candidate_id not in candidates:
                        raise LedgerCorruptionError(f"transition references unknown candidate: {candidate_id}")
                    candidates[candidate_id] = candidates[candidate_id].transition(
                        payload["new_status"],
                        rejection_reason=payload.get("rejection_reason"),
                    )
                elif event_type == "work_enqueued":
                    work = deserialize_work(payload["work"])
                    existing = work_items.get(work.work_id)
                    if existing is not None and existing.to_dict() != work.to_dict():
                        raise LedgerCorruptionError(f"conflicting work enqueue for {work.work_id}")
                    work_items.setdefault(work.work_id, work)
                elif event_type == "work_state_changed":
                    work = deserialize_work(payload["work"])
                    previous = work_items.get(work.work_id)
                    if previous is None:
                        raise LedgerCorruptionError(f"state transition references unknown work: {work.work_id}")
                    if work.lease_generation < previous.lease_generation:
                        raise LedgerCorruptionError(f"lease generation regressed for {work.work_id}")
                    work_items[work.work_id] = work
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, LedgerCorruptionError):
                raise
            raise LedgerCorruptionError(f"invalid ledger payload: {exc}") from exc
        self.candidates = candidates
        self.work_items = work_items

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        with self.ledger.transaction():
            self._replay()
            yield

    def observe(
        self,
        observation: CandidateObservation,
        *,
        score_components: Mapping[str, float] | None = None,
        rationale: str | None = None,
    ) -> CandidateRecord:
        with self._mutation():
            existing = self.candidates.get(observation.candidate_key)
            if existing is not None:
                updated = existing.observe(observation)
                if updated is existing:
                    return existing
                self.ledger.append("candidate_observed", {
                    "observation": observation.to_dict(),
                    "score_components": dict(existing.score_components),
                    "rationale": existing.rationale,
                })
                self.candidates[observation.candidate_key] = updated
                return updated
            if score_components is None:
                scores, auto_rationale = score_observation(observation)
                rationale = auto_rationale if rationale is None else rationale
            else:
                scores = score_components
                rationale = rationale or ""
            record = CandidateRecord.from_observation(
                observation,
                scores,
                rationale=rationale,
            )
            self.ledger.append("candidate_observed", {
                "observation": observation.to_dict(),
                "score_components": dict(record.score_components),
                "rationale": record.rationale,
            })
            self.candidates[record.candidate_id] = record
            return record

    def transition_candidate(
        self,
        candidate_id: str,
        new_status: str,
        *,
        rejection_reason: str | None = None,
    ) -> CandidateRecord:
        with self._mutation():
            updated = self.candidates[candidate_id].transition(
                new_status,
                rejection_reason=rejection_reason,
            )
            self.ledger.append("candidate_transitioned", {
                "candidate_id": candidate_id,
                "new_status": new_status,
                "rejection_reason": rejection_reason,
            })
            self.candidates[candidate_id] = updated
            return updated

    def enqueue_work(
        self,
        *,
        domain: str,
        candidate_id: str,
        action: str,
        score_components: Mapping[str, float],
        budget_estimate: float,
        idempotency_key: str | None = None,
        now=None,
    ) -> WorkItem:
        with self._mutation():
            work = WorkItem.create(
                domain=domain,
                candidate_id=candidate_id,
                action=action,
                score_components=score_components,
                budget_estimate=budget_estimate,
                idempotency_key=idempotency_key,
                now=now,
            )
            existing = self.work_items.get(work.work_id)
            if existing is not None:
                return existing
            self.ledger.append("work_enqueued", {"work": work.to_dict()}, recorded_at=now)
            self.work_items[work.work_id] = work
            return work

    def _append_work_state(self, work: WorkItem, transition: str, *, recorded_at) -> None:
        self.ledger.append(
            "work_state_changed",
            {"work": work.to_dict(), "transition": transition},
            recorded_at=recorded_at,
        )

    def lease_work(
        self,
        work_id: str,
        *,
        owner: str,
        ttl_seconds: int,
        now=None,
        lease_token: str | None = None,
    ) -> WorkItem:
        with self._mutation():
            leased = self.work_items[work_id].lease(
                owner=owner,
                ttl_seconds=ttl_seconds,
                now=now,
                lease_token=lease_token,
            )
            self._append_work_state(leased, "leased", recorded_at=now)
            self.work_items[work_id] = leased
            return leased

    def release_expired_work(self, work_id: str, *, now=None) -> WorkItem:
        with self._mutation():
            current = self.work_items[work_id]
            released = current.release_if_expired(now=now)
            if released is current:
                return current
            self._append_work_state(released, "lease_expired", recorded_at=now)
            self.work_items[work_id] = released
            return released

    def fail_work(
        self,
        work_id: str,
        *,
        owner: str,
        lease_token: str,
        lease_generation: int,
        error: str,
        retry_after_seconds: int = 60,
        now=None,
    ) -> WorkItem:
        with self._mutation():
            failed = self.work_items[work_id].fail(
                owner=owner,
                lease_token=lease_token,
                lease_generation=lease_generation,
                error=error,
                retry_after_seconds=retry_after_seconds,
                now=now,
            )
            self._append_work_state(failed, "failed", recorded_at=now)
            self.work_items[work_id] = failed
            return failed

    def complete_work(
        self,
        work_id: str,
        *,
        owner: str,
        lease_token: str,
        lease_generation: int,
        proof_receipt: str,
        now=None,
    ) -> WorkItem:
        with self._mutation():
            done = self.work_items[work_id].complete(
                owner=owner,
                lease_token=lease_token,
                lease_generation=lease_generation,
                proof_receipt=proof_receipt,
                now=now,
            )
            self._append_work_state(done, "completed", recorded_at=now)
            self.work_items[work_id] = done
            return done

    def select_work(
        self,
        *,
        action: str | None = None,
        budget: float | None = None,
        now=None,
        limit: int | None = None,
    ) -> list[WorkItem]:
        if budget is not None and (
            not isinstance(budget, (int, float))
            or isinstance(budget, bool)
            or not math.isfinite(float(budget))
            or budget < 0
        ):
            raise ValueError("budget must be finite and non-negative")
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
        ):
            raise ValueError("limit must be a positive integer")
        moment = utcnow() if now is None else now
        with self.ledger.transaction():
            self._replay()
            eligible: list[WorkItem] = []
            for work in self.work_items.values():
                if work.state != "pending":
                    continue
                if action is not None and work.action != action:
                    continue
                retry_after = parse_iso(work.retry_after)
                if retry_after is not None and moment < retry_after:
                    continue
                eligible.append(work)
        eligible.sort(
            key=lambda work: (
                -work.score_components.get(PRIORITY_KEY, 0.0),
                work.created_at,
                work.work_id,
            )
        )
        selected: list[WorkItem] = []
        spent = 0.0
        for work in eligible:
            if budget is not None and spent + work.budget_estimate > budget:
                continue
            if budget is not None:
                spent += work.budget_estimate
            selected.append(work)
            if limit is not None and len(selected) >= limit:
                break
        return selected
