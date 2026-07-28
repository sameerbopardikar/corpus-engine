#!/usr/bin/env python3
"""Durable discovery ledger, scoring, deduplication, and priority queue.

One append-only strict-JSONL ledger is authoritative. Every mutating engine
operation takes a cross-process transaction lock, replays current state, checks
invariants, appends and fsyncs one event, then updates the in-memory projection.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import replace
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
    "candidate_rights_asserted",
    "candidate_transitioned",
    "work_enqueued",
    "work_state_changed",
}
_RIGHTS_STATES = {
    "public_rights_clear", "public_metadata_only", "private_authorized",
    "rights_unclear", "unknown",
}
_MATERIAL_ACTIONS = {"acquire", "normalize", "synthesize"}
_DEFAULT_AUTHORIZED_VERIFIERS = frozenset({"corpus-engine-verifier"})
_PROOF_FIELDS = {
    "schema_version", "work_id", "candidate_id", "action", "verifier",
    "verified", "artifact_path", "artifact_sha256",
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


def _validate_work_transition(
    previous: WorkItem,
    current: WorkItem,
    transition: Any,
    proof: Any = None,
) -> None:
    """Reject valid-looking snapshots that are not the declared next state."""
    if not isinstance(transition, str):
        raise LedgerCorruptionError("work transition label must be a string")
    if current.work_id != previous.work_id:
        raise LedgerCorruptionError("work transition changed work identity")
    moment = parse_iso(current.updated_at)
    assert moment is not None
    try:
        if transition == "leased":
            expiry = parse_iso(current.lease_expires_at)
            if expiry is None:
                raise ValueError("leased transition lacks expiry")
            ttl = (expiry - moment).total_seconds()
            if not ttl.is_integer() or ttl <= 0:
                raise ValueError("leased transition has invalid ttl")
            expected = previous.lease(
                owner=current.lease_owner or "",
                ttl_seconds=int(ttl),
                now=moment,
                lease_token=current.lease_token or "",
            )
        elif transition == "lease_expired":
            expected = previous.release_if_expired(now=moment)
            if expected is previous:
                raise ValueError("lease was not expired")
        elif transition == "failed":
            retry_after = parse_iso(current.retry_after)
            retry_seconds = 1
            if retry_after is not None:
                delta = (retry_after - moment).total_seconds()
                if not delta.is_integer() or delta <= 0:
                    raise ValueError("failed transition has invalid retry delay")
                retry_seconds = int(delta)
            expected = previous.fail(
                owner=previous.lease_owner or "",
                lease_token=previous.lease_token or "",
                lease_generation=previous.lease_generation,
                error=current.last_error or "",
                retry_after_seconds=retry_seconds,
                now=moment,
            )
        elif transition == "completed":
            if len(current.proof_receipts) != len(previous.proof_receipts) + 1:
                raise ValueError("completed transition must add exactly one proof receipt")
            if not isinstance(proof, dict):
                raise ValueError("completed transition requires durable proof metadata")
            if proof.get("receipt_path") != current.proof_receipts[-1]:
                raise ValueError("completion proof does not bind the stored receipt")
            if proof.get("work_id") != current.work_id:
                raise ValueError("completion proof does not bind the work item")
            if proof.get("candidate_id") != current.candidate_id or proof.get("action") != current.action:
                raise ValueError("completion proof does not bind candidate and action")
            for digest_name in ("receipt_sha256", "artifact_sha256"):
                digest = proof.get(digest_name)
                if not isinstance(digest, str) or len(digest) != 64 or any(
                    char not in "0123456789abcdef" for char in digest
                ):
                    raise ValueError(f"completion proof has invalid {digest_name}")
            if not isinstance(proof.get("verifier"), str) or not proof["verifier"].strip():
                raise ValueError("completion proof lacks verifier identity")
            expected = previous.complete(
                owner=previous.lease_owner or "",
                lease_token=previous.lease_token or "",
                lease_generation=previous.lease_generation,
                proof_receipt=current.proof_receipts[-1],
                now=moment,
            )
        else:
            raise ValueError(f"unknown work transition: {transition!r}")
    except ValueError as exc:
        raise LedgerCorruptionError(
            f"invalid {transition!r} transition for {current.work_id}: {exc}"
        ) from exc
    if expected.to_dict() != current.to_dict():
        raise LedgerCorruptionError(
            f"work snapshot does not match {transition!r} transition for {current.work_id}"
        )


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

    def _readback_appended_event(self, file_handle, *, offset: int, expected: dict[str, Any]) -> None:
        file_handle.seek(offset)
        raw = file_handle.read()
        if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
            raise LedgerCorruptionError("appended event readback has an invalid frame")
        try:
            decoded = json.loads(raw[:-1].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LedgerCorruptionError("appended event failed strict readback") from exc
        validated = self._validate_event(decoded, location=f"{self.path}:append-readback")
        if validated != expected:
            raise LedgerCorruptionError("appended event readback differs from requested event")

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
                file_handle.seek(0, os.SEEK_END)
                original_size = file_handle.tell()
                try:
                    self._repair_valid_unterminated_tail(file_handle)
                    file_handle.seek(0, os.SEEK_END)
                    append_offset = file_handle.tell()
                    file_handle.write(encoded)
                    file_handle.flush()
                    os.fsync(file_handle.fileno())
                    self._readback_appended_event(
                        file_handle,
                        offset=append_offset,
                        expected=event,
                    )
                except BaseException:
                    file_handle.seek(0)
                    file_handle.truncate(original_size)
                    file_handle.flush()
                    try:
                        os.fsync(file_handle.fileno())
                    except OSError:
                        pass
                    raise
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
    def __init__(
        self,
        ledger_path: Path | str,
        *,
        authorized_verifiers: frozenset[str] | set[str] | tuple[str, ...] = _DEFAULT_AUTHORIZED_VERIFIERS,
    ):
        self.ledger = DiscoveryLedger(ledger_path)
        self.authorized_verifiers = frozenset(authorized_verifiers)
        if not self.authorized_verifiers or any(
            not isinstance(verifier, str) or not verifier.strip()
            for verifier in self.authorized_verifiers
        ):
            raise ValueError("authorized_verifiers must contain non-empty verifier identities")
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
                            rights_state=payload.get("rights_state", "unknown"),
                        )
                    else:
                        candidates[candidate_id] = candidates[candidate_id].observe(observation)
                elif event_type == "candidate_rights_asserted":
                    candidate_id = payload["candidate_id"]
                    if candidate_id not in candidates:
                        raise LedgerCorruptionError(
                            f"rights assertion references unknown candidate: {candidate_id}"
                        )
                    prior = payload["prior_rights_state"]
                    if candidates[candidate_id].rights_state != prior:
                        raise LedgerCorruptionError(
                            f"rights assertion prior state mismatch for {candidate_id}"
                        )
                    rights_state = payload["rights_state"]
                    if rights_state not in _RIGHTS_STATES or rights_state == "unknown":
                        raise LedgerCorruptionError(
                            f"invalid asserted rights_state: {rights_state!r}"
                        )
                    if not payload.get("asserted_by") or not payload.get("basis"):
                        raise LedgerCorruptionError("rights assertion lacks provenance")
                    parse_iso(payload["asserted_at"])
                    candidates[candidate_id] = replace(
                        candidates[candidate_id], rights_state=rights_state
                    )
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
                    proof = payload.get("proof")
                    if payload.get("transition") == "completed" and isinstance(proof, dict):
                        durable_proof = self._validate_completion_proof(
                            previous,
                            proof.get("receipt_path", ""),
                        )
                        if durable_proof != proof:
                            raise LedgerCorruptionError(
                                "completion proof metadata differs from durable receipt/artifact"
                            )
                    _validate_work_transition(
                        previous,
                        work,
                        payload.get("transition"),
                        proof,
                    )
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
        rights_state: str = "unknown",
    ) -> CandidateRecord:
        if rights_state not in _RIGHTS_STATES:
            raise ValueError(f"unknown rights_state: {rights_state!r}")
        with self._mutation():
            existing = self.candidates.get(observation.candidate_key)
            if existing is not None:
                if rights_state != "unknown" and rights_state != existing.rights_state:
                    raise ValueError("observation cannot silently change candidate rights_state")
                updated = existing.observe(observation)
                if updated is existing:
                    return existing
                self.ledger.append("candidate_observed", {
                    "observation": observation.to_dict(),
                    "score_components": dict(existing.score_components),
                    "rationale": existing.rationale,
                    "rights_state": existing.rights_state,
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
                rights_state=rights_state,
            )
            self.ledger.append("candidate_observed", {
                "observation": observation.to_dict(),
                "score_components": dict(record.score_components),
                "rationale": record.rationale,
                "rights_state": record.rights_state,
            })
            self.candidates[record.candidate_id] = record
            return record

    def assert_rights(
        self,
        candidate_id: str,
        rights_state: str,
        *,
        asserted_by: str,
        basis: str,
        asserted_at: str,
    ) -> CandidateRecord:
        """Apply an explicit, durable rights assertion with provenance.

        Ordinary observations remain unable to alter rights. This separate event
        makes an owner or policy assertion auditable, replay-safe, and idempotent.
        """
        if rights_state not in _RIGHTS_STATES or rights_state == "unknown":
            raise ValueError(f"invalid asserted rights_state: {rights_state!r}")
        asserted_by = str(asserted_by).strip()
        basis = str(basis).strip()
        if not asserted_by or not basis:
            raise ValueError("rights assertion requires asserted_by and basis")
        asserted_at = iso(parse_iso(asserted_at))
        with self._mutation():
            existing = self.candidates[candidate_id]
            if existing.rights_state == rights_state:
                return existing
            updated = replace(existing, rights_state=rights_state)
            self.ledger.append("candidate_rights_asserted", {
                "candidate_id": candidate_id,
                "prior_rights_state": existing.rights_state,
                "rights_state": rights_state,
                "asserted_by": asserted_by,
                "basis": basis,
                "asserted_at": asserted_at,
            })
            self.candidates[candidate_id] = updated
            return updated

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
            candidate = self.candidates.get(candidate_id)
            if candidate is None:
                raise ValueError(f"work references unknown candidate: {candidate_id}")
            if candidate.domain != domain:
                raise ValueError("work domain does not match candidate domain")
            if action in _MATERIAL_ACTIONS and candidate.rights_state != "public_rights_clear":
                raise ValueError(
                    f"{action} requires candidate rights_state='public_rights_clear'"
                )
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

    def _append_work_state(
        self,
        work: WorkItem,
        transition: str,
        *,
        recorded_at,
        proof: Mapping[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"work": work.to_dict(), "transition": transition}
        if proof is not None:
            payload["proof"] = dict(proof)
        self.ledger.append(
            "work_state_changed",
            payload,
            recorded_at=recorded_at,
        )

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _validate_completion_proof(self, work: WorkItem, proof_receipt: str) -> dict[str, Any]:
        receipt_path = Path(proof_receipt)
        if not receipt_path.is_absolute():
            raise ValueError("proof_receipt must be an absolute path")
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise ValueError("proof_receipt must be a regular non-symlink file")
        raw = receipt_path.read_bytes()
        if not raw or len(raw) > 64 * 1024:
            raise ValueError("proof_receipt must be non-empty and at most 64 KiB")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("proof_receipt must contain valid JSON") from exc
        if not isinstance(document, dict) or set(document) != _PROOF_FIELDS:
            raise ValueError("proof_receipt has missing or unknown fields")
        expected_identity = {
            "schema_version": 1,
            "work_id": work.work_id,
            "candidate_id": work.candidate_id,
            "action": work.action,
            "verified": True,
        }
        for field_name, expected in expected_identity.items():
            if document.get(field_name) != expected:
                raise ValueError(f"proof_receipt does not bind {field_name}")
        verifier = document.get("verifier")
        if verifier not in self.authorized_verifiers:
            raise ValueError("proof_receipt verifier is not authorized")
        artifact_path = Path(document.get("artifact_path", ""))
        if not artifact_path.is_absolute():
            raise ValueError("proof artifact_path must be absolute")
        if artifact_path.is_symlink() or not artifact_path.is_file():
            raise ValueError("proof artifact must be a regular non-symlink file")
        artifact_digest = self._sha256_file(artifact_path)
        if artifact_digest != document.get("artifact_sha256"):
            raise ValueError("proof artifact SHA-256 mismatch")
        return {
            "receipt_path": str(receipt_path),
            "receipt_sha256": hashlib.sha256(raw).hexdigest(),
            "work_id": work.work_id,
            "candidate_id": work.candidate_id,
            "action": work.action,
            "verifier": verifier,
            "artifact_path": str(artifact_path),
            "artifact_sha256": artifact_digest,
        }

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
            current = self.work_items[work_id]
            proof = self._validate_completion_proof(current, proof_receipt)
            done = current.complete(
                owner=owner,
                lease_token=lease_token,
                lease_generation=lease_generation,
                proof_receipt=proof_receipt,
                now=now,
            )
            self._append_work_state(done, "completed", recorded_at=now, proof=proof)
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
            for work_id, work in list(self.work_items.items()):
                released = work.release_if_expired(now=moment)
                if released is work:
                    continue
                self._append_work_state(released, "lease_expired", recorded_at=moment)
                self.work_items[work_id] = released
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
