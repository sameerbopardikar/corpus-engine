#!/usr/bin/env python3
"""Ground source-family artifacts into shared source-graph relationships.

Structured metadata is parsed deterministically. Unstructured transcript claims
may be proposed by an agent, but they are admitted only when an exact quote is
present in the preserved artifact. This module produces relationships; it does
not grant rights, promote candidates, or mutate doctrine.
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import urlsplit

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


def extract_semantic_relationships(
    *,
    source_family: str,
    artifact_url: str,
    artifact_text: str,
    claims: Iterable[dict[str, Any]],
    domain: str,
    observed_at: str,
    evidence_lane: str,
    topics: Iterable[str],
) -> list[SourceRelationship]:
    source_family = _text("source_family", source_family, 256)
    artifact_url = _text("artifact_url", artifact_url, 4096)
    artifact_text = _text("artifact_text", artifact_text)
    common = _common(
        domain=domain, observed_at=observed_at, evidence_lane=evidence_lane, topics=topics
    )
    relationships: list[SourceRelationship] = []
    for raw_claim in claims:
        if not isinstance(raw_claim, dict):
            raise RelationshipExtractionError("semantic claim must be an object")
        expected = {
            "relationship_type", "entity_type", "canonical_url", "title", "evidence_quote"
        }
        unknown = set(raw_claim) - expected
        missing = expected - set(raw_claim)
        if unknown or missing:
            raise RelationshipExtractionError(
                f"semantic claim fields invalid; missing={sorted(missing)} unknown={sorted(unknown)}"
            )
        quote = _text("evidence_quote", raw_claim["evidence_quote"], 10_000)
        if quote not in artifact_text:
            raise RelationshipExtractionError("semantic claim quote is not present in artifact")
        quote_digest = hashlib.sha256(quote.encode("utf-8")).hexdigest()
        separator = "&" if urlsplit(artifact_url).query else "?"
        evidence_pointer = f"{artifact_url}{separator}evidence_sha256={quote_digest}"
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
