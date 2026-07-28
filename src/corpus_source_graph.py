#!/usr/bin/env python3
"""Durable source/creator relationship graph feeding the shared candidate ledger.

Semantic discovery (an X thread, podcast transcript, paper metadata, repository,
conference page, or another adapter) emits first-order relationship observations.
This module validates and preserves those edges, then projects every discovered
entity into the existing DiscoveryEngine. The graph is source-family agnostic:
platform adapters discover relationships; one shared engine owns identity,
deduplication, scoring, lifecycle, and prioritization.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from corpus_discovery import DiscoveryEngine
from corpus_engine_models import CandidateObservation, ENTITY_TYPES
from corpus_entity_identity import EntityIdentityError, canonical_entity_identity

SCHEMA_VERSION = 1
RELATIONSHIP_TYPES = frozenset(
    {
        "authored",
        "authored_signal",
        "cited",
        "cites",
        "collaborated_with",
        "contributed_to",
        "depends_on",
        "guest_of",
        "hosted",
        "linked_primary_source",
        "maintains",
        "mentioned",
        "presented_at",
        "related_project",
        "replied_to",
        "speaker_at",
    }
)


class SourceGraphContractError(ValueError):
    """A relationship observation or durable source-graph projection is invalid."""


def _text(name: str, value: Any, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceGraphContractError(f"{name} must be non-blank text")
    value = value.strip()
    if len(value) > maximum:
        raise SourceGraphContractError(f"{name} exceeds {maximum} characters")
    return value


def _url(name: str, value: Any) -> str:
    value = _text(name, value)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SourceGraphContractError(f"{name} must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise SourceGraphContractError(f"{name} must not contain credentials")
    host = parsed.hostname.lower()
    port = parsed.port
    netloc = host if port is None else f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def _entity_url(name: str, value: Any) -> str:
    """Normalize an entity URL through the one canonical identity resolver."""
    value = _text(name, value)
    try:
        return canonical_entity_identity(value)
    except EntityIdentityError as exc:
        raise SourceGraphContractError(f"{name} is not a resolvable entity URL: {exc}") from exc


def _aware_iso(name: str, value: Any) -> str:
    value = _text(name, value, 64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceGraphContractError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SourceGraphContractError(f"{name} must include a timezone")
    return value


def _topics(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise SourceGraphContractError("topics must be a list/tuple")
    normalized = tuple(dict.fromkeys(_text("topic", item, 256) for item in value))
    if len(normalized) > 50:
        raise SourceGraphContractError("topics exceeds 50 values")
    return normalized


@dataclass(frozen=True, slots=True)
class SourceRelationship:
    domain: str
    source_family: str
    relationship_type: str
    entity_type: str
    canonical_url: str
    title: str
    discovered_from_url: str
    evidence_pointer: str
    evidence_lane: str
    topics: tuple[str, ...]
    observed_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain", _text("domain", self.domain, 256))
        object.__setattr__(self, "source_family", _text("source_family", self.source_family, 256))
        relation = _text("relationship_type", self.relationship_type, 256)
        if relation not in RELATIONSHIP_TYPES:
            raise SourceGraphContractError(f"unsupported relationship_type: {relation!r}")
        object.__setattr__(self, "relationship_type", relation)
        entity_type = _text("entity_type", self.entity_type, 256)
        if entity_type not in ENTITY_TYPES:
            raise SourceGraphContractError(f"unsupported entity_type: {entity_type!r}")
        object.__setattr__(self, "entity_type", entity_type)
        object.__setattr__(self, "canonical_url", _entity_url("canonical_url", self.canonical_url))
        object.__setattr__(self, "title", _text("title", self.title, 1024))
        object.__setattr__(self, "discovered_from_url", _url("discovered_from_url", self.discovered_from_url))
        object.__setattr__(self, "evidence_pointer", _url("evidence_pointer", self.evidence_pointer))
        object.__setattr__(self, "evidence_lane", _text("evidence_lane", self.evidence_lane, 256))
        object.__setattr__(self, "topics", _topics(self.topics))
        object.__setattr__(self, "observed_at", _aware_iso("observed_at", self.observed_at))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SourceRelationship":
        expected = {
            "domain",
            "source_family",
            "relationship_type",
            "entity_type",
            "canonical_url",
            "title",
            "discovered_from_url",
            "evidence_pointer",
            "evidence_lane",
            "topics",
            "observed_at",
        }
        if not isinstance(value, dict):
            raise SourceGraphContractError("relationship must be an object")
        unknown = set(value) - expected
        missing = expected - set(value)
        if unknown:
            raise SourceGraphContractError(f"unknown relationship fields: {sorted(unknown)}")
        if missing:
            raise SourceGraphContractError(f"missing relationship fields: {sorted(missing)}")
        return cls(**{**value, "topics": tuple(value["topics"])})

    @property
    def relation_id(self) -> str:
        semantic = [
            self.domain,
            self.source_family,
            self.relationship_type,
            self.entity_type,
            self.canonical_url,
            self.discovered_from_url,
            self.evidence_pointer,
        ]
        digest = hashlib.sha256(
            json.dumps(semantic, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        return f"rel_{digest}"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["topics"] = list(self.topics)
        return {"schema_version": SCHEMA_VERSION, "relation_id": self.relation_id, **value}


def _read_graph(path: Path) -> list[SourceRelationship]:
    if not path.exists():
        return []
    relationships: list[SourceRelationship] = []
    seen: set[str] = set()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SourceGraphContractError(f"malformed source graph at line {line_number}: {exc}") from exc
        if value.get("schema_version") != SCHEMA_VERSION:
            raise SourceGraphContractError(f"unsupported source graph schema at line {line_number}")
        relation_id = value.get("relation_id")
        payload = {key: item for key, item in value.items() if key not in {"schema_version", "relation_id"}}
        relationship = SourceRelationship.from_dict(payload)
        if relation_id != relationship.relation_id:
            raise SourceGraphContractError(f"source graph identity mismatch at line {line_number}")
        if relation_id in seen:
            raise SourceGraphContractError(f"duplicate relation_id at line {line_number}: {relation_id}")
        seen.add(relation_id)
        relationships.append(relationship)
    return relationships


def _append_new(path: Path, relationships: list[SourceRelationship]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    os.fchmod(lock_fd, 0o600)
    with os.fdopen(lock_fd, "r+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        existing = _read_graph(path)
        known = {item.relation_id for item in existing}
        # Deduplicate the incoming batch itself before opening the append
        # descriptor. Two identical relationships in one call are one edge;
        # appending both would satisfy this call and then permanently break
        # every later read of the durable graph.
        novel: list[SourceRelationship] = []
        batch_ids: set[str] = set()
        for item in relationships:
            if item.relation_id in known or item.relation_id in batch_ids:
                continue
            batch_ids.add(item.relation_id)
            novel.append(item)
        if not novel:
            return 0
        projected = known | batch_ids
        if len(projected) != len(known) + len(novel):
            raise SourceGraphContractError("projected source graph would contain duplicate relation IDs")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "ab") as handle:
                for relationship in novel:
                    encoded = json.dumps(
                        relationship.to_dict(), sort_keys=True, ensure_ascii=False, allow_nan=False,
                        separators=(",", ":"),
                    ).encode("utf-8") + b"\n"
                    handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            pass
        return len(novel)


def relationship_to_observation(relationship: SourceRelationship) -> CandidateObservation:
    return CandidateObservation.create(
        domain=relationship.domain,
        entity_type=relationship.entity_type,
        canonical_url=relationship.canonical_url,
        discovery_source=(
            f"source_graph:{relationship.source_family}:"
            f"{relationship.relationship_type}:{relationship.relation_id}"
        ),
        evidence_pointer=relationship.evidence_pointer,
        evidence_lane=relationship.evidence_lane,
        topics=relationship.topics,
        observed_at=relationship.observed_at,
    )


def reconcile_source_graph(graph_path: Path, discovery_ledger_path: Path) -> dict[str, int]:
    """Project every durable graph edge into the shared candidate ledger.

    Reconciliation is a rights-neutral, replayable recovery step: it may create
    a new ``rights_unclear`` candidate, but observing an entity again never
    restates the rights another authorized resolver already granted. That keeps
    graph append plus projection recoverable after an interruption instead of
    wedging on a candidate whose rights were legitimately upgraded.
    """
    relationships = _read_graph(Path(graph_path))
    engine = DiscoveryEngine(Path(discovery_ledger_path))
    before = len(engine.candidates)
    for relationship in relationships:
        observation = relationship_to_observation(relationship)
        known = observation.candidate_key in engine.candidates
        try:
            engine.observe(observation, rights_state="unknown" if known else "rights_unclear")
        except ValueError:
            # The candidate was created concurrently between replay and append;
            # re-observe rights-neutrally rather than failing the projection.
            engine.observe(observation, rights_state="unknown")
    return {
        "relationships": len(relationships),
        "candidates_before": before,
        "candidates_after": len(engine.candidates),
        "candidates_added": len(engine.candidates) - before,
    }


def ingest_relationships(
    values: Iterable[dict[str, Any] | SourceRelationship],
    *,
    graph_path: Path,
    discovery_ledger_path: Path,
    max_items: int = 100,
) -> dict[str, int]:
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 1000:
        raise SourceGraphContractError("max_items must be an integer from 1 through 1000")
    material = list(values)
    if len(material) > max_items:
        raise SourceGraphContractError("relationship batch exceeds max_items")
    relationships = [
        item if isinstance(item, SourceRelationship) else SourceRelationship.from_dict(item)
        for item in material
    ]
    appended = _append_new(Path(graph_path), relationships)
    result = reconcile_source_graph(Path(graph_path), Path(discovery_ledger_path))
    return {"relationships_appended": appended, **result}


def canonical_candidate_url(url: str) -> tuple[str, str]:
    """Classify a linked candidate and resolve it to its canonical identity.

    Classification is the only thing decided here; identity comes from the one
    shared resolver so a linked URL, a seed registry entry, and a rediscovered
    watch source all compare as the same entity.
    """
    _url("candidate URL", url)
    normalized = _entity_url("candidate URL", url)
    parsed = urlsplit(normalized)
    host = parsed.hostname or ""
    parts = [part for part in parsed.path.split("/") if part]
    if host == "github.com" and len(parts) >= 2:
        return "repository", normalized
    if host == "arxiv.org" and len(parts) >= 2 and parts[0] == "abs":
        return "paper", normalized
    return "document", normalized


def relationships_from_x_projection(
    projection: dict[str, Any], *, domain: str, topics: tuple[str, ...] = ()
) -> list[SourceRelationship]:
    if not isinstance(projection, dict) or not isinstance(projection.get("signals"), list):
        raise SourceGraphContractError("invalid X projection")
    relationships: list[SourceRelationship] = []
    for signal in projection["signals"]:
        author = _text("author", signal.get("author"), 256)
        handle = author.lstrip("@").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", handle):
            raise SourceGraphContractError(f"invalid X author handle: {author!r}")
        signal_url = _url("X signal URL", signal.get("url"))
        observed_at = _aware_iso("published_at", signal.get("published_at"))
        relationships.append(
            SourceRelationship(
                domain=domain,
                source_family="x",
                relationship_type="authored_signal",
                entity_type="creator",
                canonical_url=f"https://x.com/{handle}",
                title=author,
                discovered_from_url=signal_url,
                evidence_pointer=signal_url,
                evidence_lane="unverified-discovery-signal",
                topics=topics,
                observed_at=observed_at,
            )
        )
        links = signal.get("linked_primary_sources")
        if not isinstance(links, list):
            raise SourceGraphContractError("X linked_primary_sources must be a list")
        for link in links:
            entity_type, canonical = canonical_candidate_url(link)
            relationships.append(
                SourceRelationship(
                    domain=domain,
                    source_family="x",
                    relationship_type="linked_primary_source",
                    entity_type=entity_type,
                    canonical_url=canonical,
                    title=canonical,
                    discovered_from_url=signal_url,
                    evidence_pointer=signal_url,
                    evidence_lane="unverified-discovery-signal",
                    topics=topics,
                    observed_at=observed_at,
                )
            )
    return relationships
