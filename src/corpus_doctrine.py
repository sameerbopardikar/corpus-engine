#!/usr/bin/env python3
"""Append-only, citation-preserving doctrine evolution for corpus synthesis.

The ledger is the authority. Doctrine is an external-corpus synthesis projection,
never Sameer-adopted belief. Structural mutations preserve aliases and lineage;
rejected proposals are durable decisions but do not alter the doctrine projection.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

SCHEMA_VERSION = 1
EVENT_TYPES = {
    "propose", "add_alias", "merge", "split", "revise", "bound",
    "supersede", "deprecate", "reject", "restore", "adopt",
}
PAYLOAD_FIELDS = {
    "propose": {"concept_key", "title", "statement", "citations", "rationale"},
    "add_alias": {"concept_key", "alias", "rationale"},
    "merge": {"source_keys", "target_key", "title", "statement", "citations", "rationale"},
    "split": {"source_key", "children", "primary_key", "citations", "rationale"},
    "revise": {"concept_key", "statement", "citations", "rationale", "contradictory"},
    "bound": {"concept_key", "statement", "citations", "rationale"},
    "supersede": {"source_key", "target_key", "title", "statement", "citations", "rationale"},
    "deprecate": {"concept_key", "citations", "rationale"},
    "reject": {"proposal_key", "citations", "rationale"},
    "restore": {"concept_key", "version", "citations", "rationale"},
    "adopt": {"concept_key", "holder_id", "adoption_receipt", "citations", "rationale"},
}
CITATION_FIELDS = {
    "source_id", "page_slug", "locator", "claim_sha256", "evidence_class",
}


class DoctrineError(Exception):
    """Base doctrine error."""


class DoctrineCorruptionError(DoctrineError):
    """The append-only doctrine ledger cannot be replayed safely."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _key(value: Any, field: str = "concept_key") -> str:
    text = _text(value, field)
    if text != text.lower() or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in text):
        raise ValueError(f"{field} must be lowercase kebab-case")
    if text.startswith("-") or text.endswith("-") or "--" in text:
        raise ValueError(f"{field} must be lowercase kebab-case")
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp(value: Any) -> str:
    text = _text(value, "recorded_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("recorded_at must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("recorded_at must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _strict_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not strict-JSON serializable: {exc}") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_strict_bytes(value)).hexdigest()


def _citation(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != CITATION_FIELDS:
        raise ValueError(f"citation fields must be exactly {sorted(CITATION_FIELDS)}")
    result = {field: _text(value[field], field) for field in sorted(CITATION_FIELDS)}
    digest = result["claim_sha256"]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("claim_sha256 must be a lowercase SHA-256 digest")
    if result["source_id"] != "corpora":
        raise ValueError("doctrine citations must resolve through source_id='corpora'")
    return result


def _citations(values: Any) -> list[dict[str, str]]:
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("citations must be a non-empty list")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        item = _citation(value)
        identity = _digest(item)
        if identity not in seen:
            seen.add(identity)
            result.append(item)
    return result


def _citation_union(*groups: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for group in groups:
        for raw in group:
            item = _citation(raw)
            identity = _digest(item)
            if identity not in seen:
                seen.add(identity)
                result.append(item)
    return result


def _ensure_private_regular(path: Path) -> None:
    if path.is_symlink():
        raise DoctrineError(f"refusing symlink path: {path}")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise DoctrineError(f"path is not a regular file: {path}")
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
            os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


class DoctrineEngine:
    """Replayable doctrine projection backed by a chained strict-JSONL ledger."""

    def __init__(self, ledger_path: Path | str, *, domain: str | None = None):
        self.domain = domain
        self.path = Path(ledger_path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        _ensure_private_regular(self.path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        _ensure_private_regular(self.lock_path)
        self.events: list[dict[str, Any]] = []
        self.concepts: dict[str, dict[str, Any]] = {}
        self.aliases: dict[str, str] = {}
        self.histories: dict[str, list[dict[str, Any]]] = {}
        self.edges: list[dict[str, Any]] = []
        self.rejections: list[dict[str, Any]] = []
        self.command_results: dict[str, tuple[str, Any]] = {}
        self._replay()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with open(self.lock_path, "r+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _reset(self) -> None:
        self.events = []
        self.concepts = {}
        self.aliases = {}
        self.histories = {}
        self.edges = []
        self.rejections = []
        self.command_results = {}

    def _read_events(self) -> list[dict[str, Any]]:
        raw = self.path.read_bytes()
        events: list[dict[str, Any]] = []
        previous_digest = "0" * 64
        for line_number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise DoctrineCorruptionError(f"malformed event at line {line_number}") from exc
            required = {
                "schema_version", "sequence", "event_id", "command_id", "command_digest",
                "event_type", "recorded_at", "previous_digest", "payload", "event_digest",
            }
            if not isinstance(event, dict) or set(event) != required:
                raise DoctrineCorruptionError(f"invalid event envelope at line {line_number}")
            if event["schema_version"] != SCHEMA_VERSION or event["sequence"] != len(events) + 1:
                raise DoctrineCorruptionError(f"invalid event sequence/schema at line {line_number}")
            if event["event_type"] not in EVENT_TYPES:
                raise DoctrineCorruptionError(f"unknown event type at line {line_number}")
            if (
                not isinstance(event["payload"], dict)
                or set(event["payload"]) != PAYLOAD_FIELDS[event["event_type"]]
            ):
                raise DoctrineCorruptionError(f"invalid payload fields at line {line_number}")
            if event["previous_digest"] != previous_digest:
                raise DoctrineCorruptionError(f"broken digest chain at line {line_number}")
            body = {key: value for key, value in event.items() if key != "event_digest"}
            if _digest(body) != event["event_digest"]:
                raise DoctrineCorruptionError(f"event digest mismatch at line {line_number}")
            if event["command_digest"] != _digest({"event_type": event["event_type"], "payload": event["payload"]}):
                raise DoctrineCorruptionError(f"command digest mismatch at line {line_number}")
            expected_event_id = "doctrine_evt_" + hashlib.sha256(event["command_id"].encode("utf-8")).hexdigest()[:24]
            if event["event_id"] != expected_event_id:
                raise DoctrineCorruptionError(f"event identity mismatch at line {line_number}")
            try:
                _timestamp(event["recorded_at"])
            except ValueError as exc:
                raise DoctrineCorruptionError(f"invalid event timestamp at line {line_number}") from exc
            previous_digest = event["event_digest"]
            events.append(event)
        return events

    def _replay(self) -> None:
        events = self._read_events()
        self._reset()
        for event in events:
            command_id = event["command_id"]
            if command_id in self.command_results:
                raise DoctrineCorruptionError(f"duplicate command_id in ledger: {command_id}")
            try:
                result = self._apply(event)
            except (KeyError, TypeError, ValueError) as exc:
                raise DoctrineCorruptionError(
                    f"invalid {event['event_type']} event at sequence {event['sequence']}: {exc}"
                ) from exc
            self.events.append(event)
            self.command_results[command_id] = (event["command_digest"], copy.deepcopy(result))

    def _resolve_key(self, key: str) -> str | None:
        current = _key(key)
        seen: set[str] = set()
        while current in self.aliases:
            if current in seen:
                raise DoctrineCorruptionError("alias cycle")
            seen.add(current)
            current = self.aliases[current]
        return current if current in self.concepts else None

    def resolve(self, key: str) -> dict[str, Any] | None:
        canonical = self._resolve_key(key)
        return copy.deepcopy(self.concepts[canonical]) if canonical is not None else None

    def _require_concept(self, key: str) -> tuple[str, dict[str, Any]]:
        canonical = self._resolve_key(key)
        if canonical is None:
            raise ValueError(f"unknown concept: {key}")
        return canonical, self.concepts[canonical]

    def _ensure_free(self, key: str) -> str:
        candidate = _key(key)
        if self._resolve_key(candidate) is not None or candidate in self.aliases:
            raise ValueError(f"concept or alias {candidate!r} already resolves")
        return candidate

    def _record_version(self, concept: dict[str, Any]) -> None:
        self.histories.setdefault(concept["concept_key"], []).append(copy.deepcopy(concept))

    def _new_concept(
        self, *, key: str, title: str, statement: str, citations: list[dict[str, str]],
        moment: str, aliases: Iterable[str] = (), status: str = "probationary",
    ) -> dict[str, Any]:
        normalized_aliases = sorted({_key(alias, "alias") for alias in aliases})
        concept = {
            "schema_version": SCHEMA_VERSION,
            "concept_key": key,
            "title": _text(title, "title"),
            "statement": _text(statement, "statement"),
            "version": 1,
            "status": status,
            "aliases": normalized_aliases,
            "citations": copy.deepcopy(citations),
            "epistemic_layer": "external_corpus_synthesis",
            # Generic holder/adoption metadata. Synthesis is never adopted on
            # creation; adoption happens only via an explicit, receipt-bound
            # adopt command. sameer_adopted stays as a derived compatibility
            # projection so existing private Agentic checks keep working.
            "holder_id": None,
            "adoption_state": "not_adopted",
            "adoption_receipt": None,
            "sameer_adopted": False,
            "created_at": moment,
            "updated_at": moment,
        }
        if self.domain is not None:
            concept["domain"] = self.domain
        return concept

    def _advance(
        self, concept: dict[str, Any], *, moment: str, statement: str | None = None,
        citations: list[dict[str, str]] | None = None, status: str | None = None,
        aliases: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        updated = copy.deepcopy(concept)
        updated["version"] += 1
        updated["updated_at"] = moment
        if statement is not None:
            updated["statement"] = _text(statement, "statement")
        if citations is not None:
            updated["citations"] = _citation_union(concept["citations"], citations)
        if status is not None:
            updated["status"] = status
        if aliases is not None:
            updated["aliases"] = sorted({_key(alias, "alias") for alias in aliases})
        return updated

    def _route_alias(self, alias: str, target: str) -> None:
        name = _key(alias, "alias")
        destination = _key(target)
        existing = self._resolve_key(name)
        if existing is not None and existing != destination:
            raise ValueError(f"alias {name!r} already resolves to {existing!r}")
        if name != destination:
            self.aliases[name] = destination

    def _apply(self, event: Mapping[str, Any]) -> Any:
        event_type = event["event_type"]
        payload = event["payload"]
        moment = event["recorded_at"]
        rationale = _text(payload["rationale"], "rationale")
        del rationale

        if event_type == "propose":
            key = self._ensure_free(payload["concept_key"])
            citations = _citations(payload["citations"])
            concept = self._new_concept(
                key=key, title=payload["title"], statement=payload["statement"],
                citations=citations, moment=moment,
            )
            self.concepts[key] = concept
            self._record_version(concept)
            return concept

        if event_type == "adopt":
            canonical, concept = self._require_concept(payload["concept_key"])
            holder_id = _text(payload["holder_id"], "holder_id")
            receipt = payload["adoption_receipt"]
            if not isinstance(receipt, Mapping):
                raise ValueError("adoption_receipt must be an object")
            for required in ("receipt_id", "authorized_by", "granted_at"):
                if not isinstance(receipt.get(required), str) or not receipt[required].strip():
                    raise ValueError(f"adoption_receipt.{required} must be a non-blank string")
            updated = self._advance(concept, moment=moment, citations=payload["citations"])
            updated["holder_id"] = holder_id
            updated["adoption_state"] = "adopted"
            updated["adoption_receipt"] = copy.deepcopy(dict(receipt))
            updated["sameer_adopted"] = holder_id == "sameer"
            self.concepts[canonical] = updated
            self._record_version(updated)
            return updated

        if event_type == "reject":
            proposal_key = _key(payload["proposal_key"], "proposal_key")
            citations = _citations(payload["citations"])
            decision = {
                "decision": "rejected", "proposal_key": proposal_key,
                "citations": citations, "recorded_at": moment,
            }
            self.rejections.append(decision)
            return decision

        if event_type == "add_alias":
            canonical, concept = self._require_concept(payload["concept_key"])
            alias = _key(payload["alias"], "alias")
            existing = self._resolve_key(alias)
            if existing is not None and existing != canonical:
                raise ValueError(f"alias {alias!r} already resolves to {existing!r}")
            if alias == canonical or alias in concept["aliases"]:
                raise ValueError("alias already resolves to concept")
            aliases = [*concept["aliases"], alias]
            updated = self._advance(concept, moment=moment, aliases=aliases)
            self.concepts[canonical] = updated
            self._route_alias(alias, canonical)
            self._record_version(updated)
            return updated

        if event_type in {"revise", "bound", "deprecate", "restore"}:
            canonical, concept = self._require_concept(payload["concept_key"])
            citations = _citations(payload["citations"])
            if event_type == "restore":
                version = payload["version"]
                if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                    raise ValueError("version must be a positive integer")
                matches = [row for row in self.histories.get(canonical, []) if row["version"] == version]
                if len(matches) != 1:
                    raise ValueError(f"unknown historical version {version}")
                historical = matches[0]
                updated = self._advance(
                    concept, moment=moment, statement=historical["statement"],
                    citations=_citation_union(historical["citations"], citations),
                    status=historical["status"], aliases=historical["aliases"],
                )
                restored_aliases = set(updated["aliases"])
                for alias, destination in list(self.aliases.items()):
                    if destination == canonical and alias not in restored_aliases:
                        del self.aliases[alias]
                for alias in restored_aliases:
                    self._route_alias(alias, canonical)
            elif event_type == "deprecate":
                updated = self._advance(concept, moment=moment, citations=citations, status="deprecated")
            else:
                if event_type == "revise" and payload.get("contradictory") is True:
                    raise ValueError("contradictory evidence requires bound or revise with an explicit non-overwrite event")
                status = "bounded" if event_type == "bound" else concept["status"]
                updated = self._advance(
                    concept, moment=moment, statement=payload["statement"],
                    citations=citations, status=status,
                )
            self.concepts[canonical] = updated
            self._record_version(updated)
            return updated

        if event_type in {"merge", "supersede"}:
            source_values = payload["source_keys"] if event_type == "merge" else [payload["source_key"]]
            if not isinstance(source_values, list) or not source_values:
                raise ValueError("source_keys must be a non-empty list")
            source_keys: list[str] = []
            source_concepts: list[dict[str, Any]] = []
            for value in source_values:
                canonical, concept = self._require_concept(value)
                if canonical not in source_keys:
                    source_keys.append(canonical)
                    source_concepts.append(concept)
            target = _key(payload["target_key"], "target_key")
            if target not in source_keys:
                self._ensure_free(target)
            combined = _citation_union(*(concept["citations"] for concept in source_concepts), _citations(payload["citations"]))
            inherited_aliases = sorted({
                alias for concept in source_concepts for alias in [concept["concept_key"], *concept["aliases"]]
                if alias != target
            })
            for source in source_keys:
                del self.concepts[source]
            concept = self._new_concept(
                key=target, title=payload["title"], statement=payload["statement"],
                citations=combined, moment=moment, aliases=inherited_aliases,
            )
            self.concepts[target] = concept
            for alias in inherited_aliases:
                self.aliases.pop(alias, None)
                self._route_alias(alias, target)
            for alias, destination in list(self.aliases.items()):
                if destination in source_keys:
                    self.aliases[alias] = target
            self._record_version(concept)
            relation = "merged_into" if event_type == "merge" else "superseded_by"
            for source in source_keys:
                self.edges.append({"from": source, "to": target, "relation": relation, "recorded_at": moment})
            return concept

        if event_type == "split":
            source, parent = self._require_concept(payload["source_key"])
            children = payload["children"]
            if not isinstance(children, list) or len(children) < 2:
                raise ValueError("split requires at least two children")
            child_keys = [_key(child["concept_key"]) for child in children]
            if len(set(child_keys)) != len(child_keys) or source in child_keys:
                raise ValueError("split child keys must be unique and new")
            for child_key in child_keys:
                self._ensure_free(child_key)
            primary = _key(payload["primary_key"], "primary_key")
            if primary not in child_keys:
                raise ValueError("primary_key must name one child")
            combined = _citation_union(parent["citations"], _citations(payload["citations"]))
            del self.concepts[source]
            created: list[dict[str, Any]] = []
            for child, child_key in zip(children, child_keys):
                aliases = child.get("aliases", [])
                if not isinstance(aliases, list):
                    raise ValueError("child aliases must be a list")
                concept = self._new_concept(
                    key=child_key, title=child["title"], statement=child["statement"],
                    citations=combined, moment=moment, aliases=aliases,
                )
                self.concepts[child_key] = concept
                for alias in concept["aliases"]:
                    self._route_alias(alias, child_key)
                self._record_version(concept)
                self.edges.append({"from": source, "to": child_key, "relation": "split_into", "recorded_at": moment})
                created.append(concept)
            inherited = [source, *parent["aliases"]]
            for alias in inherited:
                self.aliases.pop(alias, None)
                self._route_alias(alias, primary)
            for alias, destination in list(self.aliases.items()):
                if destination == source:
                    self.aliases[alias] = primary
            primary_concept = self.concepts[primary]
            primary_aliases = sorted(set(primary_concept["aliases"]) | set(inherited))
            primary_concept["aliases"] = primary_aliases
            self.histories[primary][-1] = copy.deepcopy(primary_concept)
            return created

        raise ValueError(f"unsupported event type: {event_type}")

    def _mutate(self, event_type: str, command_id: str, payload: Mapping[str, Any]) -> Any:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unsupported event type: {event_type}")
        command = _text(command_id, "command_id")
        normalized_payload = copy.deepcopy(dict(payload))
        command_digest = _digest({"event_type": event_type, "payload": normalized_payload})
        with self._transaction():
            self._replay()
            previous = self.command_results.get(command)
            if previous is not None:
                if previous[0] != command_digest:
                    raise ValueError(f"command_id conflict: {command}")
                return copy.deepcopy(previous[1])
            event = {
                "schema_version": SCHEMA_VERSION,
                "sequence": len(self.events) + 1,
                "event_id": "doctrine_evt_" + hashlib.sha256(command.encode("utf-8")).hexdigest()[:24],
                "command_id": command,
                "command_digest": command_digest,
                "event_type": event_type,
                "recorded_at": _now(),
                "previous_digest": self.events[-1]["event_digest"] if self.events else "0" * 64,
                "payload": normalized_payload,
            }
            event["event_digest"] = _digest(event)
            state = (
                copy.deepcopy(self.concepts), copy.deepcopy(self.aliases), copy.deepcopy(self.histories),
                copy.deepcopy(self.edges), copy.deepcopy(self.rejections),
            )
            try:
                result = self._apply(event)
            except BaseException:
                self.concepts, self.aliases, self.histories, self.edges, self.rejections = state
                raise
            encoded = _strict_bytes(event) + b"\n"
            with open(self.path, "r+b") as file_handle:
                fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX)
                try:
                    file_handle.seek(0, os.SEEK_END)
                    original_size = file_handle.tell()
                    if original_size:
                        file_handle.seek(-1, os.SEEK_END)
                        if file_handle.read(1) != b"\n":
                            raise DoctrineCorruptionError("ledger has an unterminated tail")
                    file_handle.seek(0, os.SEEK_END)
                    try:
                        file_handle.write(encoded)
                        file_handle.flush()
                        os.fsync(file_handle.fileno())
                        file_handle.seek(original_size)
                        if file_handle.read() != encoded:
                            raise DoctrineCorruptionError("append readback mismatch")
                    except BaseException:
                        file_handle.seek(0)
                        file_handle.truncate(original_size)
                        file_handle.flush()
                        os.fsync(file_handle.fileno())
                        raise
                finally:
                    fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)
            self.events.append(event)
            self.command_results[command] = (command_digest, copy.deepcopy(result))
            return copy.deepcopy(result)

    def propose(self, *, command_id: str, concept_key: str, title: str, statement: str, citations: list[dict[str, Any]], rationale: str) -> dict[str, Any]:
        return self._mutate("propose", command_id, {"concept_key": concept_key, "title": title, "statement": statement, "citations": citations, "rationale": rationale})

    def add_alias(self, *, command_id: str, concept_key: str, alias: str, rationale: str = "Alias preserves retrieval continuity.") -> dict[str, Any]:
        return self._mutate("add_alias", command_id, {"concept_key": concept_key, "alias": alias, "rationale": rationale})

    def revise(self, *, command_id: str, concept_key: str, statement: str, citations: list[dict[str, Any]], rationale: str, contradictory: bool = False) -> dict[str, Any]:
        if contradictory:
            raise ValueError("contradictory evidence requires bound or revise through an explicit bound event")
        return self._mutate("revise", command_id, {"concept_key": concept_key, "statement": statement, "citations": citations, "rationale": rationale, "contradictory": False})

    def bound(self, *, command_id: str, concept_key: str, statement: str, citations: list[dict[str, Any]], rationale: str) -> dict[str, Any]:
        return self._mutate("bound", command_id, {"concept_key": concept_key, "statement": statement, "citations": citations, "rationale": rationale})

    def merge(self, *, command_id: str, source_keys: list[str], target_key: str, title: str, statement: str, citations: list[dict[str, Any]], rationale: str) -> dict[str, Any]:
        return self._mutate("merge", command_id, {"source_keys": source_keys, "target_key": target_key, "title": title, "statement": statement, "citations": citations, "rationale": rationale})

    def split(self, *, command_id: str, source_key: str, children: list[dict[str, Any]], primary_key: str, citations: list[dict[str, Any]], rationale: str) -> list[dict[str, Any]]:
        return self._mutate("split", command_id, {"source_key": source_key, "children": children, "primary_key": primary_key, "citations": citations, "rationale": rationale})

    def supersede(self, *, command_id: str, source_key: str, target_key: str, title: str, statement: str, citations: list[dict[str, Any]], rationale: str) -> dict[str, Any]:
        return self._mutate("supersede", command_id, {"source_key": source_key, "target_key": target_key, "title": title, "statement": statement, "citations": citations, "rationale": rationale})

    def deprecate(self, *, command_id: str, concept_key: str, citations: list[dict[str, Any]], rationale: str) -> dict[str, Any]:
        return self._mutate("deprecate", command_id, {"concept_key": concept_key, "citations": citations, "rationale": rationale})

    def adopt(self, *, command_id: str, concept_key: str, holder_id: str, adoption_receipt: dict[str, Any] | None, rationale: str, citations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return self._mutate("adopt", command_id, {"concept_key": concept_key, "holder_id": holder_id, "adoption_receipt": adoption_receipt, "citations": citations or [], "rationale": rationale})

    def reject(self, *, command_id: str, proposal_key: str, citations: list[dict[str, Any]], rationale: str) -> dict[str, Any]:
        return self._mutate("reject", command_id, {"proposal_key": proposal_key, "citations": citations, "rationale": rationale})

    def restore(self, *, command_id: str, concept_key: str, version: int, rationale: str, citations: list[dict[str, Any]]) -> dict[str, Any]:
        return self._mutate("restore", command_id, {"concept_key": concept_key, "version": version, "citations": citations, "rationale": rationale})

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "concepts": [copy.deepcopy(self.concepts[key]) for key in sorted(self.concepts)],
            "aliases": dict(sorted(self.aliases.items())),
            "lineage_edges": sorted(copy.deepcopy(self.edges), key=lambda row: (row["recorded_at"], row["from"], row["to"])),
        }

    def lineage(self, key: str) -> dict[str, Any]:
        original = _key(key)
        canonical = self._resolve_key(original)
        related = {original}
        changed = True
        while changed:
            changed = False
            for edge in self.edges:
                if edge["from"] in related and edge["to"] not in related:
                    related.add(edge["to"])
                    changed = True
        versions: list[dict[str, Any]] = []
        for concept_key in related:
            versions.extend(copy.deepcopy(self.histories.get(concept_key, [])))
        versions.sort(key=lambda row: (row["created_at"], row["updated_at"], row["concept_key"], row["version"]))
        return {
            "requested_key": original,
            "current_key": canonical,
            "versions": versions,
            "descendants": sorted(related - {original}),
            "edges": [copy.deepcopy(edge) for edge in self.edges if edge["from"] in related or edge["to"] in related],
        }
