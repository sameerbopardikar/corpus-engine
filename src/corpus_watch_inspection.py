#!/usr/bin/env python3
"""The durable watch consumer: promoted source -> fetched artifact -> edges.

This is the only path by which a promoted source expands the graph. It reads
the persisted watch projection, leases that entry's ``inspect`` item from the
one shared discovery queue, fetches each artifact through the adapter transport
contract, preserves the exact bytes, proves the artifact is causally bound to
the promoted source, derives relationships with the production extractors, and
completes the work item with a proof receipt bound to those preserved bytes.

Relationship objects are never accepted from a caller. An artifact that the
promoted source neither *is* nor *links to* cannot enter, so promoting one
source does not unlock unrelated material. Inspection is metadata-only work and
never changes any candidate's rights.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from corpus_adapters.types import TransportPayload
from corpus_discovery import DiscoveryEngine
from corpus_entity_identity import EntityIdentityError, canonical_entity_identity
from corpus_relationship_extraction import (
    RelationshipExtractionError,
    extract_conference_relationships,
    extract_github_relationships,
    extract_openalex_relationships,
    extract_semantic_relationships,
    extract_x_relationships,
)
from corpus_source_graph import SourceRelationship, ingest_relationships

SCHEMA_VERSION = 1
INSPECTION_ACTION = "inspect"
DEFAULT_LEASE_OWNER = "corpus-watch-inspector"
DEFAULT_VERIFIER = "corpus-engine-verifier"
DEFAULT_LEASE_TTL_SECONDS = 900
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_BODY_AUTHORIZED_RIGHTS = frozenset({"public_rights_clear", "private_authorized"})

_EXTRACTOR_KINDS = frozenset({"github", "openalex", "conference", "x", "semantic"})


class WatchInspectionError(ValueError):
    """A watch inspection input, binding, or durable transition is invalid."""


def _text(name: str, value: Any, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WatchInspectionError(f"{name} must be non-blank text")
    value = value.strip()
    if len(value) > maximum:
        raise WatchInspectionError(f"{name} exceeds {maximum} characters")
    return value


def _object(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WatchInspectionError(f"{name} must be an object")
    return value


def _list(name: str, value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise WatchInspectionError(f"{name} must be a list")
    return value


def _identity(name: str, value: Any) -> str:
    try:
        return canonical_entity_identity(_text(name, value))
    except EntityIdentityError as exc:
        raise WatchInspectionError(f"{name} is not a resolvable URL: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ArtifactReceipt:
    """Authoritative record that these exact bytes were fetched for this source."""

    artifact_url: str
    artifact_identity: str
    source_family: str
    fetched_at: str
    source_revision: str
    preserved_path: str
    sha256: str
    byte_length: int
    watch_source_url: str
    watch_candidate_id: str
    work_id: str
    binding: str
    transport: str
    parent_artifact_sha256: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, **asdict(self)}


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


_TRANSPORT_MODES = {
    "content_bytes": "fixture-inline",
    "content": "fixture-inline",
    "content_path": "fixture-file",
}


def _request_bytes(
    request: dict[str, Any], *, max_artifact_bytes: int = MAX_ARTIFACT_BYTES
) -> tuple[bytes, str]:
    """Read the artifact body and report how it was obtained.

    The transport mode travels into the receipt so a frozen-fixture fetch can
    never be read back as a live network retrieval.
    """
    supplied = [key for key in ("content_bytes", "content", "content_path") if key in request]
    if len(supplied) != 1:
        raise WatchInspectionError(
            "artifact request must supply exactly one of content_bytes, content, content_path"
        )
    transport = _TRANSPORT_MODES[supplied[0]]
    if "content_bytes" in request:
        body = request["content_bytes"]
        if not isinstance(body, (bytes, bytearray)):
            raise WatchInspectionError("content_bytes must be bytes")
        body = bytes(body)
    elif "content" in request:
        if not isinstance(request["content"], str):
            raise WatchInspectionError("content must be text")
        body = request["content"].encode("utf-8")
    else:
        path = Path(_text("content_path", request["content_path"]))
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise WatchInspectionError(
                "content_path must be an openable regular non-symlink file"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise WatchInspectionError("content_path must be a regular file")
            if metadata.st_size > max_artifact_bytes:
                raise WatchInspectionError("artifact exceeds the preserved-byte limit")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                body = handle.read(max_artifact_bytes + 1)
        finally:
            os.close(descriptor)
    if not body:
        raise WatchInspectionError("artifact bytes must be non-empty")
    if len(body) > max_artifact_bytes:
        raise WatchInspectionError("artifact exceeds the preserved-byte limit")
    return body, transport


def _fetch_artifact(
    request: dict[str, Any], *, preserve_root: Path, max_artifact_bytes: int = MAX_ARTIFACT_BYTES
) -> tuple[TransportPayload, bytes, str]:
    """Fetch through the adapter transport contract and preserve the exact bytes."""
    artifact_url = _text("artifact_url", request.get("artifact_url"))
    declared = _text("content_sha256", request.get("content_sha256"), 64).lower()
    body, transport = _request_bytes(request, max_artifact_bytes=max_artifact_bytes)
    actual = hashlib.sha256(body).hexdigest()
    if declared != actual:
        raise WatchInspectionError(
            f"artifact digest mismatch for {artifact_url}: declared {declared}, fetched {actual}"
        )
    preserved = Path(preserve_root).resolve() / f"{actual}.bin"
    _atomic_bytes(preserved, body)
    readback = preserved.read_bytes()
    if hashlib.sha256(readback).hexdigest() != actual:
        raise WatchInspectionError("preserved artifact digest changed on readback")
    try:
        payload = TransportPayload(
            body=readback,
            final_url=artifact_url,
            fetched_at=_text("fetched_at", request.get("fetched_at"), 64),
            source_revision=_text("source_revision", request.get("source_revision"), 256),
            raw_pointer=str(preserved),
        )
    except ValueError as exc:
        raise WatchInspectionError(f"invalid artifact transport receipt: {exc}") from exc
    return payload, readback, transport


def _binding_for(
    *,
    artifact_url: str,
    artifact_identity: str,
    watch_identity: str,
    request: dict[str, Any],
    admitted: dict[str, tuple[ArtifactReceipt, bytes]],
) -> tuple[str, str | None]:
    """Prove the artifact is the promoted source or is linked from its bytes."""
    if artifact_identity == watch_identity:
        return "watch_source_identity", None
    parent_url = request.get("parent_artifact_url")
    if parent_url is not None:
        parent_identity = _identity("parent_artifact_url", parent_url)
        parent = admitted.get(parent_identity)
        if parent is not None and parent[0].binding == "watch_source_identity":
            parent_receipt, parent_bytes = parent
            candidates = {artifact_url, artifact_identity}
            if any(value.encode("utf-8") in parent_bytes for value in candidates):
                return "parent_artifact_link", parent_receipt.sha256
    raise WatchInspectionError(
        f"artifact {artifact_url} is not causally bound to promoted watch source "
        f"{watch_identity}: it is neither that source nor linked from its preserved bytes"
    )


def _derive_relationships(
    *,
    request: dict[str, Any],
    artifact_url: str,
    body: bytes,
    domain: str,
) -> list[SourceRelationship]:
    extraction = _object("extraction", request.get("extraction"))
    kind = _text("extraction.kind", extraction.get("kind"), 64)
    if kind not in _EXTRACTOR_KINDS:
        raise WatchInspectionError(f"unsupported extraction kind: {kind!r}")
    common = {
        "domain": domain,
        "observed_at": _text("observed_at", request.get("observed_at"), 64),
        "evidence_lane": _text("evidence_lane", request.get("evidence_lane"), 256),
        "topics": tuple(
            _text("topic", topic, 256) for topic in _list("topics", request.get("topics", []))
        ),
    }
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WatchInspectionError("preserved artifact bytes are not valid UTF-8") from exc
    try:
        if kind == "semantic":
            return extract_semantic_relationships(
                source_family=_text("source_family", request.get("source_family"), 256),
                artifact_url=artifact_url,
                artifact_text=text,
                artifact_sha256=hashlib.sha256(body).hexdigest(),
                claims=_list("extraction.claims", extraction.get("claims")),
                **common,
            )
        if kind == "conference":
            return extract_conference_relationships(text, artifact_url=artifact_url, **common)
        document = json.loads(text)
        if kind == "github":
            return extract_github_relationships(document, **common)
        if kind == "openalex":
            return extract_openalex_relationships(document, **common)
        return extract_x_relationships(document, **common)
    except json.JSONDecodeError as exc:
        raise WatchInspectionError(f"preserved artifact is not valid JSON: {exc}") from exc
    except RelationshipExtractionError as exc:
        raise WatchInspectionError(f"relationship extraction failed: {exc}") from exc


def _read_watch_entries(path: Path) -> dict[str, dict[str, Any]]:
    path = Path(path)
    if not path.is_file():
        raise WatchInspectionError(f"watch projection does not exist: {path}")
    try:
        projection = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WatchInspectionError(f"watch projection is unreadable: {exc}") from exc
    projection = _object("watch projection", projection)
    if projection.get("schema_version") != 1:
        raise WatchInspectionError("unsupported watch projection schema_version")
    entries: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(_list("watch projection entries", projection.get("entries"))):
        entry = _object(f"watch entry {index}", raw)
        if entry.get("status") != "promoted":
            continue
        identity = _identity(f"watch entry {index} canonical_url", entry.get("canonical_url"))
        entries[identity] = entry
    return entries


def _resolve_work(engine: DiscoveryEngine, entry: dict[str, Any], *, domain: str, watch_identity: str):
    """Re-derive the entry's authority from the ledger, never from the file.

    The watch projection is a published convenience view. Every claim it makes -
    which candidate, which work item, which URL - is checked back against the
    durable ledger, so rewriting the projection cannot aim a real inspection at
    a URL the policy never promoted.
    """
    work_id = _text("watch entry work_id", entry.get("work_id"), 128)
    work = engine.work_items.get(work_id)
    if work is None:
        raise WatchInspectionError(
            f"watch entry references a work item that is not in the durable queue: {work_id}"
        )
    if work.action != INSPECTION_ACTION:
        raise WatchInspectionError(f"watch work item {work_id} is not an inspection")
    if work.candidate_id != entry.get("candidate_id"):
        raise WatchInspectionError(f"watch work item {work_id} does not bind the promoted candidate")
    if work.domain != domain:
        raise WatchInspectionError(f"watch work item {work_id} belongs to another domain")
    candidate = engine.candidates.get(work.candidate_id)
    if candidate is None:
        raise WatchInspectionError(f"watch work item {work_id} references an unknown candidate")
    if candidate.status != "promoted":
        raise WatchInspectionError(
            f"candidate {candidate.candidate_id} is {candidate.status}, no longer promoted"
        )
    if canonical_entity_identity(candidate.canonical_url) != watch_identity:
        raise WatchInspectionError(
            "watch entry canonical_url does not match the promoted candidate in the ledger: "
            f"{watch_identity} vs {candidate.canonical_url}"
        )
    return work


def project_preserved_source_artifact(
    *,
    domain: str,
    request: dict[str, Any],
    preserve_root: Path,
    max_artifact_bytes: int = MAX_ARTIFACT_BYTES,
    max_relationships: int = 100,
) -> tuple[dict[str, Any], list[SourceRelationship]]:
    """Preserve one explicitly body-authorized source artifact and derive edges.

    This is the first-order counterpart to the durable watch consumer. It does
    not enqueue, promote, or grant rights; callers must project the returned
    relationships through ``ingest_relationships``.
    """
    rights_state = _text("rights_state", request.get("rights_state"), 64)
    if rights_state not in _BODY_AUTHORIZED_RIGHTS:
        raise WatchInspectionError(
            "source artifact body projection requires explicit public_rights_clear "
            "or private_authorized rights"
        )
    payload, body, transport = _fetch_artifact(
        request,
        preserve_root=Path(preserve_root),
        max_artifact_bytes=max_artifact_bytes,
    )
    artifact_url = _text("artifact_url", request.get("artifact_url"))
    relationships = _derive_relationships(
        request=request, artifact_url=artifact_url, body=body, domain=domain
    )
    if len(relationships) > max_relationships:
        raise WatchInspectionError("artifact relationship count exceeds max_relationships")
    return (
        {
            "artifact_url": artifact_url,
            "artifact_identity": _identity("artifact_url", artifact_url),
            "preserved_path": payload.raw_pointer,
            "sha256": payload.raw_sha256,
            "byte_length": len(body),
            "transport": transport,
            "rights_state": rights_state,
            "relationships_derived": len(relationships),
        },
        relationships,
    )


def run_watch_inspection(
    *,
    domain: str,
    watch_projection_path: Path,
    ledger_path: Path,
    graph_path: Path,
    inspections: Iterable[dict[str, Any]],
    preserve_root: Path,
    receipts_root: Path,
    now: datetime,
    lease_owner: str = DEFAULT_LEASE_OWNER,
    verifier: str = DEFAULT_VERIFIER,
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    max_artifact_bytes: int = MAX_ARTIFACT_BYTES,
    max_artifacts_per_inspection: int = 4,
    max_total_artifact_bytes: int | None = None,
    max_relationships_per_artifact: int = 100,
    max_total_relationships: int | None = None,
    max_candidate_count: int | None = None,
    authority_guard: Callable[[], None] | None = None,
    receipt_reference_root: Path | None = None,
) -> dict[str, Any]:
    guard = authority_guard or (lambda: None)
    guard()
    receipt_reference_root = Path(receipt_reference_root or receipts_root).resolve()
    domain = _text("domain", domain, 256)
    if max_artifacts_per_inspection < 1:
        raise WatchInspectionError("max_artifacts_per_inspection must be positive")
    max_total_artifact_bytes = max_total_artifact_bytes or max_artifact_bytes
    max_total_relationships = max_total_relationships or max_relationships_per_artifact
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise WatchInspectionError("now must be a timezone-aware datetime")
    now = now.astimezone(timezone.utc)
    guard()
    watch_entries = _read_watch_entries(Path(watch_projection_path))
    guard()
    engine = DiscoveryEngine(Path(ledger_path), authorized_verifiers=frozenset({verifier}))
    guard()

    receipts: list[dict[str, Any]] = []
    consumed: list[str] = []
    already_completed: list[str] = []
    skipped: list[str] = []
    derived_total = 0
    appended_total = 0
    artifact_bytes_total = 0

    for index, raw_inspection in enumerate(inspections):
        inspection = _object(f"inspection {index}", raw_inspection)
        watch_source_url = _text(f"inspection {index} watch_source_url", inspection.get("watch_source_url"))
        watch_identity = _identity(f"inspection {index} watch_source_url", watch_source_url)
        entry = watch_entries.get(watch_identity)
        if entry is None:
            # Not promoted by the persisted policy: nothing is fetched at all.
            skipped.append(watch_source_url)
            continue
        work = _resolve_work(engine, entry, domain=domain, watch_identity=watch_identity)
        candidate = engine.candidates[work.candidate_id]
        if candidate.rights_state not in _BODY_AUTHORIZED_RIGHTS:
            raise WatchInspectionError(
                "full artifact-body inspection requires candidate rights_state "
                f"in {sorted(_BODY_AUTHORIZED_RIGHTS)}, got {candidate.rights_state!r}"
            )
        if work.state == "done":
            already_completed.append(work.work_id)
            continue
        if work.state == "leased":
            work = engine.release_expired_work(work.work_id, now=now)
        if work.state != "pending":
            raise WatchInspectionError(
                f"watch work item {work.work_id} is {work.state}, not available for inspection"
            )

        guard()
        leased = engine.lease_work(
            work.work_id, owner=lease_owner, ttl_seconds=lease_ttl_seconds, now=now
        )
        guard()
        try:
            prepared = _load_prepared_transaction(
                Path(receipts_root),
                receipt_reference_root,
                Path(preserve_root),
                work,
                entry,
                verifier,
            )
            if prepared is not None:
                manifest_path, proof_path, inspection_receipts, relationships = prepared
                if len(inspection_receipts) > max_artifacts_per_inspection:
                    raise WatchInspectionError(
                        "prepared transaction exceeds max_artifacts_per_inspection"
                    )
                prepared_bytes = sum(item.byte_length for item in inspection_receipts)
                if prepared_bytes > max_total_artifact_bytes:
                    raise WatchInspectionError(
                        "prepared transaction exceeds max_total_artifact_bytes"
                    )
                if (
                    len(relationships) > max_total_relationships
                    or any(
                        sum(1 for rel in relationships if rel.discovered_from_url == receipt.artifact_url)
                        > max_relationships_per_artifact
                        for receipt in inspection_receipts
                    )
                ):
                    raise WatchInspectionError(
                        "prepared transaction exceeds relationship bounds"
                    )
                if max_candidate_count is not None:
                    current_identities = {
                        canonical_entity_identity(item.canonical_url)
                        for item in DiscoveryEngine(Path(ledger_path)).candidates.values()
                    }
                    projected_identities = current_identities | {
                        canonical_entity_identity(item.canonical_url) for item in relationships
                    }
                    if len(projected_identities) > max_candidate_count:
                        raise WatchInspectionError(
                            "prepared transaction would exceed max_candidates_per_cycle"
                        )
                guard()
                ingested = ingest_relationships(
                    relationships,
                    graph_path=Path(graph_path),
                    discovery_ledger_path=Path(ledger_path),
                )
                guard()
                engine = DiscoveryEngine(
                    Path(ledger_path), authorized_verifiers=frozenset({verifier})
                )
                engine.complete_work(
                    work.work_id,
                    owner=lease_owner,
                    lease_token=leased.lease_token or "",
                    lease_generation=leased.lease_generation,
                    proof_receipt=str(proof_path),
                    now=now,
                )
                guard()
                artifact_bytes_total += prepared_bytes
                derived_total += len(relationships)
                appended_total += ingested["relationships_appended"]
                receipts.extend(item.to_dict() for item in inspection_receipts)
                consumed.append(work.work_id)
                continue

            admitted: dict[str, tuple[ArtifactReceipt, bytes]] = {}
            inspection_receipts: list[ArtifactReceipt] = []
            relationships: list[SourceRelationship] = []
            artifact_requests = _list(
                f"inspection {index} artifacts", inspection.get("artifacts")
            )
            if len(artifact_requests) > max_artifacts_per_inspection:
                raise WatchInspectionError(
                    "inspection artifact count exceeds max_artifacts_per_inspection"
                )
            for artifact_index, raw_request in enumerate(artifact_requests):
                request = _object(f"artifact {artifact_index}", raw_request)
                artifact_url = _text("artifact_url", request.get("artifact_url"))
                artifact_identity = _identity("artifact_url", artifact_url)
                binding, parent_digest = _binding_for(
                    artifact_url=artifact_url,
                    artifact_identity=artifact_identity,
                    watch_identity=watch_identity,
                    request=request,
                    admitted=admitted,
                )
                remaining_bytes = max_total_artifact_bytes - artifact_bytes_total
                if remaining_bytes <= 0:
                    raise WatchInspectionError(
                        "inspection cycle exhausted max_total_artifact_bytes"
                    )
                guard()
                payload, body, transport = _fetch_artifact(
                    request,
                    preserve_root=Path(preserve_root),
                    max_artifact_bytes=min(max_artifact_bytes, remaining_bytes),
                )
                guard()
                artifact_bytes_total += len(body)
                receipt = ArtifactReceipt(
                    artifact_url=artifact_url,
                    artifact_identity=artifact_identity,
                    source_family=_text("source_family", request.get("source_family"), 256),
                    fetched_at=payload.fetched_at,
                    source_revision=payload.source_revision,
                    preserved_path=payload.raw_pointer,
                    sha256=payload.raw_sha256,
                    byte_length=len(body),
                    watch_source_url=watch_source_url,
                    watch_candidate_id=str(entry.get("candidate_id")),
                    work_id=work.work_id,
                    binding=binding,
                    transport=transport,
                    parent_artifact_sha256=parent_digest,
                )
                admitted[artifact_identity] = (receipt, body)
                inspection_receipts.append(receipt)

                derived = _derive_relationships(
                    request=request, artifact_url=artifact_url, body=body, domain=domain
                )
                if len(derived) > max_relationships_per_artifact:
                    raise WatchInspectionError(
                        "artifact relationship count exceeds max_relationships_per_artifact"
                    )
                if derived_total + len(relationships) + len(derived) > max_total_relationships:
                    raise WatchInspectionError(
                        "inspection cycle relationship count exceeds max_total_relationships"
                    )
                for relationship in derived:
                    if relationship.domain != domain:
                        raise WatchInspectionError("derived relationship domain mismatch")
                    if canonical_entity_identity(relationship.discovered_from_url) != artifact_identity:
                        raise WatchInspectionError(
                            "derived relationship is not grounded in the fetched artifact: "
                            f"{relationship.discovered_from_url}"
                        )
                relationships.extend(derived)

            if not inspection_receipts:
                raise WatchInspectionError("inspection produced no artifact receipts")
            if max_candidate_count is not None:
                current_identities = {
                    canonical_entity_identity(item.canonical_url)
                    for item in DiscoveryEngine(Path(ledger_path)).candidates.values()
                }
                projected_identities = current_identities | {
                    canonical_entity_identity(item.canonical_url) for item in relationships
                }
                if len(projected_identities) > max_candidate_count:
                    raise WatchInspectionError(
                        "inspection would exceed max_candidates_per_cycle"
                    )
            # Persist a deterministic prepared transaction journal and proof
            # before canonical graph mutation. If mutation or completion is
            # interrupted, replay reuses the same journal, re-applies edges
            # idempotently, and finalizes the leased work item.
            guard()
            manifest_path = _write_manifest(
                Path(receipts_root),
                work.work_id,
                inspection_receipts,
                relationships,
                entry,
            )
            guard()
            manifest_reference_path = receipt_reference_root / f"inspection-{work.work_id}.json"
            proof_path = _write_proof(
                Path(receipts_root),
                receipt_reference_root,
                work.work_id,
                manifest_path,
                manifest_reference_path,
                work,
                verifier,
            )
            guard()
            ingested = ingest_relationships(
                relationships, graph_path=Path(graph_path), discovery_ledger_path=Path(ledger_path)
            )
            guard()
            derived_total += len(relationships)
            appended_total += ingested["relationships_appended"]

            engine = DiscoveryEngine(Path(ledger_path), authorized_verifiers=frozenset({verifier}))
            engine.complete_work(
                work.work_id,
                owner=lease_owner,
                lease_token=leased.lease_token or "",
                lease_generation=leased.lease_generation,
                proof_receipt=str(proof_path),
                now=now,
            )
            guard()
        except Exception as exc:
            # Release only while the original identity-bound authority still
            # exists. If it changed, fail without writing through the replacement.
            guard()
            engine = DiscoveryEngine(Path(ledger_path), authorized_verifiers=frozenset({verifier}))
            try:
                engine.fail_work(
                    work.work_id,
                    owner=lease_owner,
                    lease_token=leased.lease_token or "",
                    lease_generation=leased.lease_generation,
                    error=f"{type(exc).__name__}: {exc}"[:500],
                    now=now,
                )
            except (ValueError, KeyError):
                pass
            raise

        receipts.extend(item.to_dict() for item in inspection_receipts)
        consumed.append(work.work_id)

    return {
        "schema_version": SCHEMA_VERSION,
        "domain": domain,
        "processed_inspections": len(consumed),
        "consumed_work_ids": consumed,
        "already_completed_work_ids": already_completed,
        "skipped_unpromoted_sources": skipped,
        "artifact_receipts": receipts,
        "artifact_bytes_preserved": artifact_bytes_total,
        "relationships_derived": derived_total,
        "relationships_appended": appended_total,
    }


def _write_manifest(
    receipts_root: Path,
    work_id: str,
    receipts: list[ArtifactReceipt],
    relationships: list[SourceRelationship],
    entry: dict[str, Any],
) -> Path:
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "transaction_phase": "prepared",
        "work_id": work_id,
        "candidate_id": entry.get("candidate_id"),
        "watch_canonical_url": entry.get("canonical_url"),
        "artifact_receipts": [item.to_dict() for item in receipts],
        "intended_relationships": [item.to_dict() for item in relationships],
    }
    path = receipts_root.resolve() / f"inspection-{work_id}.json"
    _atomic_bytes(
        path,
        (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode(
            "utf-8"
        ),
    )
    return path


def _write_proof(
    receipts_root: Path,
    receipt_reference_root: Path,
    work_id: str,
    manifest_path: Path,
    manifest_reference_path: Path,
    work,
    verifier: str,
) -> Path:
    proof = {
        "schema_version": 1,
        "work_id": work_id,
        "candidate_id": work.candidate_id,
        "action": work.action,
        "verifier": verifier,
        "verified": True,
        "artifact_path": str(manifest_reference_path),
        "artifact_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }
    path = receipts_root.resolve() / f"proof-{work_id}.json"
    _atomic_bytes(
        path,
        (json.dumps(proof, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode(
            "utf-8"
        ),
    )
    return receipt_reference_root / f"proof-{work_id}.json"


def _read_regular_bounded(path: Path, *, max_bytes: int, label: str) -> bytes:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise WatchInspectionError(f"{label} is not a no-follow regular file") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 1 or before.st_size > max_bytes:
            raise WatchInspectionError(f"{label} size is invalid")
        chunks: list[bytes] = []
        length = 0
        while length <= max_bytes:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
        after = os.fstat(descriptor)
        if (
            length < 1
            or length > max_bytes
            or (before.st_dev, before.st_ino, before.st_size)
            != (after.st_dev, after.st_ino, after.st_size)
            or length != after.st_size
        ):
            raise WatchInspectionError(f"{label} changed during bounded read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _load_prepared_transaction(
    receipts_root: Path,
    receipt_reference_root: Path,
    preserve_root: Path,
    work,
    entry: dict[str, Any],
    verifier: str,
) -> tuple[Path, Path, list[ArtifactReceipt], list[SourceRelationship]] | None:
    manifest_path = receipts_root.resolve() / f"inspection-{work.work_id}.json"
    proof_path = receipts_root.resolve() / f"proof-{work.work_id}.json"
    manifest_reference_path = receipt_reference_root / f"inspection-{work.work_id}.json"
    proof_reference_path = receipt_reference_root / f"proof-{work.work_id}.json"
    manifest_exists = manifest_path.exists()
    proof_exists = proof_path.exists()
    if not manifest_exists and not proof_exists:
        return None
    if manifest_exists != proof_exists:
        raise WatchInspectionError("prepared transaction journal is incomplete")
    if manifest_path.is_symlink() or proof_path.is_symlink():
        raise WatchInspectionError("prepared transaction journal cannot be symlinked")
    manifest_raw = _read_regular_bounded(
        manifest_path, max_bytes=2 * 1024 * 1024, label="prepared manifest"
    )
    proof_raw = _read_regular_bounded(
        proof_path, max_bytes=64 * 1024, label="prepared proof"
    )
    try:
        reject_constant = lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON constant: {value}")
        )
        manifest = json.loads(manifest_raw.decode("utf-8"), parse_constant=reject_constant)
        proof = json.loads(proof_raw.decode("utf-8"), parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WatchInspectionError("prepared transaction journal is not valid JSON") from exc
    manifest_fields = {
        "schema_version", "transaction_phase", "work_id", "candidate_id",
        "watch_canonical_url", "artifact_receipts", "intended_relationships",
    }
    proof_fields = {
        "schema_version", "work_id", "candidate_id", "action", "verifier",
        "verified", "artifact_path", "artifact_sha256",
    }
    if not isinstance(manifest, dict) or set(manifest) != manifest_fields:
        raise WatchInspectionError("prepared manifest fields are invalid")
    if not isinstance(proof, dict) or set(proof) != proof_fields:
        raise WatchInspectionError("prepared proof fields are invalid")
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("transaction_phase") != "prepared"
        or manifest.get("work_id") != work.work_id
        or manifest.get("candidate_id") != work.candidate_id
        or manifest.get("watch_canonical_url") != entry.get("canonical_url")
    ):
        raise WatchInspectionError("prepared manifest identity mismatch")
    if (
        proof.get("schema_version") != 1
        or proof.get("work_id") != work.work_id
        or proof.get("candidate_id") != work.candidate_id
        or proof.get("action") != work.action
        or proof.get("verifier") != verifier
        or proof.get("verified") is not True
        or proof.get("artifact_path") != str(manifest_reference_path)
        or proof.get("artifact_sha256") != hashlib.sha256(manifest_raw).hexdigest()
    ):
        raise WatchInspectionError("prepared proof identity or digest mismatch")
    raw_receipts = manifest.get("artifact_receipts")
    raw_relationships = manifest.get("intended_relationships")
    if not isinstance(raw_receipts, list) or not raw_receipts:
        raise WatchInspectionError("prepared manifest has no artifact receipts")
    if not isinstance(raw_relationships, list):
        raise WatchInspectionError("prepared manifest relationships are invalid")
    try:
        receipts = []
        for item in raw_receipts:
            payload = dict(_object("prepared artifact receipt", item))
            if payload.pop("schema_version", None) != SCHEMA_VERSION:
                raise ValueError("prepared artifact receipt schema mismatch")
            receipts.append(ArtifactReceipt(**payload))
        relationships = []
        for item in raw_relationships:
            payload = dict(_object("prepared relationship", item))
            if payload.pop("schema_version", None) != SCHEMA_VERSION:
                raise ValueError("prepared relationship schema mismatch")
            relation_id = payload.pop("relation_id", None)
            relationship = SourceRelationship.from_dict(payload)
            if relation_id != relationship.relation_id:
                raise ValueError("prepared relationship identity mismatch")
            relationships.append(relationship)
    except (TypeError, ValueError, KeyError) as exc:
        raise WatchInspectionError("prepared transaction payload is invalid") from exc
    preserve_base = preserve_root.resolve()
    for receipt in receipts:
        artifact_path = Path(receipt.preserved_path)
        if (
            receipt.work_id != work.work_id
            or receipt.watch_candidate_id != work.candidate_id
            or artifact_path.is_symlink()
            or not artifact_path.is_file()
        ):
            raise WatchInspectionError("prepared artifact receipt identity is invalid")
        try:
            artifact_path.resolve().relative_to(preserve_base)
        except ValueError as exc:
            raise WatchInspectionError("prepared artifact escaped preserve_root") from exc
        descriptor = os.open(
            artifact_path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            info = os.fstat(descriptor)
            digest = hashlib.sha256()
            length = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                length += len(chunk)
        finally:
            os.close(descriptor)
        if not stat.S_ISREG(info.st_mode) or length != receipt.byte_length or digest.hexdigest() != receipt.sha256:
            raise WatchInspectionError("prepared artifact receipt bytes do not match")
    return manifest_reference_path, proof_reference_path, receipts, relationships
