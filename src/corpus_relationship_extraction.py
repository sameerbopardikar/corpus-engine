#!/usr/bin/env python3
"""Ground source-family artifacts into shared source-graph relationships.

Structured metadata is parsed deterministically. Unstructured transcript claims
may be proposed by an agent, but they are admitted only when an exact quote is
present in the preserved artifact. This module produces relationships; it does
not grant rights, promote candidates, or mutate doctrine.
"""
from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from corpus_entity_identity import EntityIdentityError, canonical_entity_identity
from corpus_source_graph import (
    SourceGraphContractError,
    SourceRelationship,
    canonical_candidate_url,
    relationships_from_x_projection,
)


class RelationshipExtractionError(ValueError):
    """A source artifact or proposed semantic relationship is ungrounded."""


def _text(name: str, value: Any, maximum: int = 100_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RelationshipExtractionError(f"{name} must be non-blank text")
    value = value.strip()
    if len(value) > maximum:
        raise RelationshipExtractionError(f"{name} exceeds {maximum} characters")
    return value


def _common(
    *, domain: str, observed_at: str, evidence_lane: str, topics: Iterable[str]
) -> dict[str, Any]:
    return {
        "domain": _text("domain", domain, 256),
        "observed_at": _text("observed_at", observed_at, 64),
        "evidence_lane": _text("evidence_lane", evidence_lane, 256),
        "topics": tuple(topics),
    }


def _make(
    *,
    source_family: str,
    relationship_type: str,
    entity_type: str,
    canonical_url: str,
    title: str,
    discovered_from_url: str,
    evidence_pointer: str,
    common: dict[str, Any],
) -> SourceRelationship:
    try:
        return SourceRelationship(
            source_family=source_family,
            relationship_type=relationship_type,
            entity_type=entity_type,
            canonical_url=canonical_url,
            title=title,
            discovered_from_url=discovered_from_url,
            evidence_pointer=evidence_pointer,
            **common,
        )
    except SourceGraphContractError as exc:
        raise RelationshipExtractionError(str(exc)) from exc


def extract_x_relationships(
    projection: dict[str, Any],
    *,
    domain: str,
    observed_at: str,
    evidence_lane: str,
    topics: Iterable[str],
) -> list[SourceRelationship]:
    del observed_at, evidence_lane
    try:
        return relationships_from_x_projection(
            projection, domain=domain, topics=tuple(topics)
        )
    except SourceGraphContractError as exc:
        raise RelationshipExtractionError(str(exc)) from exc


_SEMANTIC_CLAIM_FIELDS = frozenset(
    {
        "relationship_type",
        "entity_type",
        "canonical_url",
        "title",
        "entity_mention",
        "relation_mention",
        "evidence_quote",
        "evidence_span",
    }
)


def _normalize_mention(value: str) -> str:
    return " ".join(value.split()).casefold()


_URL_IN_TEXT = re.compile(r"https?://[^\s<>()\[\]{}\"']+")


def _span_binds_canonical_target(quote: str, canonical_url: str, entity_mention: str) -> bool:
    """Require the span to identify the same entity as the claimed locator.

    When a span contains locators, at least one must canonicalize to the claimed
    target. A different URL plus a caller-controlled matching title must never
    authorize target substitution. For natural-language spans without a URL,
    the terminal locator slug must exactly name the normalized entity mention;
    structured adapters remain the preferred route for looser entity resolution.
    """
    try:
        target = canonical_entity_identity(canonical_url)
    except EntityIdentityError as exc:
        raise RelationshipExtractionError(f"canonical_url is invalid: {exc}") from exc
    locators = []
    for raw in _URL_IN_TEXT.findall(quote):
        candidate = raw.rstrip(".,;:!?")
        try:
            locators.append(canonical_entity_identity(candidate))
        except EntityIdentityError:
            continue
    if locators:
        return target in locators
    path_parts = [part for part in urlsplit(target).path.split("/") if part]
    if not path_parts:
        return False
    slug = unquote(path_parts[-1]).replace("-", " ").replace("_", " ")
    return _normalize_mention(slug) == _normalize_mention(entity_mention)


def _validated_span(value: Any, artifact_text: str, quote: str) -> tuple[int, int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        raise RelationshipExtractionError("evidence_span must be a [start, end] integer pair")
    start, end = int(value[0]), int(value[1])
    if not 0 <= start < end <= len(artifact_text):
        raise RelationshipExtractionError("evidence_span is outside the preserved artifact")
    if artifact_text[start:end] != quote:
        raise RelationshipExtractionError(
            "evidence_span does not contain the exact quoted artifact bytes"
        )
    return start, end


def extract_semantic_relationships(
    *,
    source_family: str,
    artifact_url: str,
    artifact_text: str,
    artifact_sha256: str,
    claims: Iterable[dict[str, Any]],
    domain: str,
    observed_at: str,
    evidence_lane: str,
    topics: Iterable[str],
) -> list[SourceRelationship]:
    """Admit proposed semantic relationships only against preserved bytes.

    A claim survives when the preserved artifact still hashes to the declared
    digest, the declared span holds the exact quote, the span names the claimed
    entity, and the span also expresses the relation. An exact sentence taken
    from somewhere else in the artifact can therefore no longer be reused to
    ground an entity it never mentions or a relation it never states.
    """
    source_family = _text("source_family", source_family, 256)
    artifact_url = _text("artifact_url", artifact_url, 4096)
    if not isinstance(artifact_text, str) or not artifact_text.strip():
        raise RelationshipExtractionError("artifact_text must be non-blank text")
    if len(artifact_text) > 4_000_000:
        raise RelationshipExtractionError("artifact_text exceeds 4000000 characters")
    artifact_sha256 = _text("artifact_sha256", artifact_sha256, 64)
    actual_digest = hashlib.sha256(artifact_text.encode("utf-8")).hexdigest()
    if artifact_sha256.lower() != actual_digest:
        raise RelationshipExtractionError(
            "preserved artifact digest does not match the declared artifact_sha256"
        )
    common = _common(
        domain=domain, observed_at=observed_at, evidence_lane=evidence_lane, topics=topics
    )
    relationships: list[SourceRelationship] = []
    for raw_claim in claims:
        if not isinstance(raw_claim, dict):
            raise RelationshipExtractionError("semantic claim must be an object")
        unknown = set(raw_claim) - _SEMANTIC_CLAIM_FIELDS
        missing = _SEMANTIC_CLAIM_FIELDS - set(raw_claim)
        if unknown or missing:
            raise RelationshipExtractionError(
                f"semantic claim fields invalid; missing={sorted(missing)} unknown={sorted(unknown)}"
            )
        quote = _text("evidence_quote", raw_claim["evidence_quote"], 10_000)
        if quote not in artifact_text:
            raise RelationshipExtractionError("semantic claim quote is not present in artifact")
        start, end = _validated_span(raw_claim["evidence_span"], artifact_text, quote)
        span_text = _normalize_mention(quote)
        entity_mention = _normalize_mention(_text("entity_mention", raw_claim["entity_mention"], 1024))
        title = _normalize_mention(_text("title", raw_claim["title"], 1024))
        if entity_mention not in span_text:
            raise RelationshipExtractionError(
                "claimed entity mention does not appear in the validated span"
            )
        if entity_mention != title:
            raise RelationshipExtractionError(
                "claimed entity mention does not match the claimed entity title"
            )
        if not _span_binds_canonical_target(
            quote, _text("canonical_url", raw_claim["canonical_url"], 4096), raw_claim["entity_mention"]
        ):
            raise RelationshipExtractionError(
                "validated span does not bind the claimed canonical target"
            )
        relation_mention = _normalize_mention(
            _text("relation_mention", raw_claim["relation_mention"], 1024)
        )
        if relation_mention not in span_text:
            raise RelationshipExtractionError(
                "claimed relation mention does not appear in the validated span"
            )
        if relation_mention == entity_mention:
            raise RelationshipExtractionError(
                "claimed relation mention must bind the relation, not repeat the entity"
            )
        separator = "&" if urlsplit(artifact_url).query else "?"
        evidence_pointer = (
            f"{artifact_url}{separator}artifact_sha256={actual_digest}"
            f"&evidence_span={start}-{end}"
        )
        relationships.append(
            _make(
                source_family=source_family,
                relationship_type=raw_claim["relationship_type"],
                entity_type=raw_claim["entity_type"],
                canonical_url=raw_claim["canonical_url"],
                title=raw_claim["title"],
                discovered_from_url=artifact_url,
                evidence_pointer=evidence_pointer,
                common=common,
            )
        )
    return relationships


def extract_openalex_relationships(
    work: dict[str, Any],
    *,
    domain: str,
    observed_at: str,
    evidence_lane: str,
    topics: Iterable[str],
) -> list[SourceRelationship]:
    if not isinstance(work, dict):
        raise RelationshipExtractionError("OpenAlex work must be an object")
    work_url = _text("OpenAlex work id", work.get("id"), 4096)
    common = _common(
        domain=domain, observed_at=observed_at, evidence_lane=evidence_lane, topics=topics
    )
    relationships: list[SourceRelationship] = []
    authorships = work.get("authorships")
    if not isinstance(authorships, list):
        raise RelationshipExtractionError("OpenAlex work authorships must be a list")
    for authorship in authorships:
        author = authorship.get("author") if isinstance(authorship, dict) else None
        if not isinstance(author, dict):
            raise RelationshipExtractionError("OpenAlex authorship lacks author metadata")
        author_url = _text("OpenAlex author id", author.get("id"), 4096)
        author_name = _text("OpenAlex author name", author.get("display_name"), 1024)
        relationships.append(
            _make(
                source_family="paper",
                relationship_type="authored",
                entity_type="creator",
                canonical_url=author_url,
                title=author_name,
                discovered_from_url=work_url,
                evidence_pointer=work_url,
                common=common,
            )
        )
    references = work.get("referenced_works")
    if not isinstance(references, list):
        raise RelationshipExtractionError("OpenAlex referenced_works must be a list")
    for reference in references:
        reference_url = _text("referenced work", reference, 4096)
        relationships.append(
            _make(
                source_family="paper",
                relationship_type="cited",
                entity_type="paper",
                canonical_url=reference_url,
                title=reference_url,
                discovered_from_url=work_url,
                evidence_pointer=work_url,
                common=common,
            )
        )
    return relationships


def extract_github_relationships(
    repository: dict[str, Any],
    *,
    domain: str,
    observed_at: str,
    evidence_lane: str,
    topics: Iterable[str],
) -> list[SourceRelationship]:
    if not isinstance(repository, dict):
        raise RelationshipExtractionError("GitHub repository artifact must be an object")
    repository_url = _text("repository_url", repository.get("repository_url"), 4096)
    common = _common(
        domain=domain, observed_at=observed_at, evidence_lane=evidence_lane, topics=topics
    )
    relationships: list[SourceRelationship] = []
    owner = repository.get("owner")
    if not isinstance(owner, dict):
        raise RelationshipExtractionError("GitHub repository lacks owner metadata")
    owner_url = _text("GitHub owner URL", owner.get("html_url"), 4096)
    owner_name = _text("GitHub owner login", owner.get("login"), 1024)
    relationships.append(
        _make(
            source_family="github",
            relationship_type="maintains",
            entity_type="creator",
            canonical_url=owner_url,
            title=owner_name,
            discovered_from_url=repository_url,
            evidence_pointer=repository_url,
            common=common,
        )
    )
    contributors = repository.get("contributors")
    if not isinstance(contributors, list):
        raise RelationshipExtractionError("GitHub contributors must be a list")
    for contributor in contributors:
        if not isinstance(contributor, dict):
            raise RelationshipExtractionError("GitHub contributor must be an object")
        contributor_url = _text("GitHub contributor URL", contributor.get("html_url"), 4096)
        contributor_name = _text("GitHub contributor login", contributor.get("login"), 1024)
        relationships.append(
            _make(
                source_family="github",
                relationship_type="contributed_to",
                entity_type="creator",
                canonical_url=contributor_url,
                title=contributor_name,
                discovered_from_url=repository_url,
                evidence_pointer=repository_url,
                common=common,
            )
        )
    dependencies = repository.get("dependencies")
    if not isinstance(dependencies, list):
        raise RelationshipExtractionError("GitHub dependencies must be a list")
    for dependency in dependencies:
        entity_type, canonical_url = canonical_candidate_url(dependency)
        if entity_type != "repository":
            raise RelationshipExtractionError("GitHub dependency must identify a repository")
        relationships.append(
            _make(
                source_family="github",
                relationship_type="depends_on",
                entity_type="repository",
                canonical_url=canonical_url,
                title=canonical_url,
                discovered_from_url=repository_url,
                evidence_pointer=repository_url,
                common=common,
            )
        )
    return relationships


class _ConferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.talks: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None
        self.in_speaker = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "article" and "talk" in attributes.get("class", "").split():
            self.current = {
                "speaker_url": attributes.get("data-speaker-url", ""),
                "project_url": attributes.get("data-project-url", ""),
                "speaker": "",
            }
        elif self.current is not None and tag == "span" and "speaker" in attributes.get("class", "").split():
            self.in_speaker = True

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.in_speaker:
            self.current["speaker"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "span":
            self.in_speaker = False
        elif tag == "article" and self.current is not None:
            self.current["speaker"] = self.current["speaker"].strip()
            self.talks.append(self.current)
            self.current = None
            self.in_speaker = False


def extract_conference_relationships(
    html: str,
    *,
    artifact_url: str,
    domain: str,
    observed_at: str,
    evidence_lane: str,
    topics: Iterable[str],
) -> list[SourceRelationship]:
    parser = _ConferenceParser()
    try:
        parser.feed(_text("conference html", html))
        parser.close()
    except Exception as exc:
        raise RelationshipExtractionError(f"conference HTML parse failed: {exc}") from exc
    if not parser.talks:
        raise RelationshipExtractionError("conference artifact contains no structured talks")
    common = _common(
        domain=domain, observed_at=observed_at, evidence_lane=evidence_lane, topics=topics
    )
    relationships: list[SourceRelationship] = []
    for talk in parser.talks:
        relationships.append(
            _make(
                source_family="conference",
                relationship_type="speaker_at",
                entity_type="creator",
                canonical_url=talk["speaker_url"],
                title=talk["speaker"],
                discovered_from_url=artifact_url,
                evidence_pointer=artifact_url,
                common=common,
            )
        )
        project_type, project_url = canonical_candidate_url(talk["project_url"])
        relationships.append(
            _make(
                source_family="conference",
                relationship_type="related_project",
                entity_type=project_type,
                canonical_url=project_url,
                title=project_url,
                discovered_from_url=artifact_url,
                evidence_pointer=artifact_url,
                common=common,
            )
        )
    return relationships
