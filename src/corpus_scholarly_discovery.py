#!/usr/bin/env python3
"""Unseeded scholarly discovery over OpenAlex metadata, no API keys required.

This is one generic discovery path that can surface candidates that are *not*
present in any checked-in seed bundle. It queries OpenAlex's public works
endpoint by domain topic, and for each returned work it derives:

* a canonical :class:`CandidateObservation` (entity ``paper``, keyed by the
  stable OpenAlex work URL);
* fail-closed :class:`RightsEvidence` from the work's open-access metadata — an
  explicit open license yields content-acquirable evidence, everything else
  (bronze/closed/unlicensed) is metadata-only;
* the lawful open-access content locator, when one exists.

All network I/O is injected via ``http_get`` so tests use a local fixture client
and the path is fully deterministic. Credentials never appear in a query URL.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote_plus, urlsplit

from defusedxml.ElementTree import fromstring as safe_fromstring

from corpus_engine_models import CandidateObservation
from corpus_rights_resolver import RightsEvidence

DEFAULT_WORKS_ENDPOINT = "https://api.openalex.org/works"
DEFAULT_EUROPEPMC_ENDPOINT = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
DEFAULT_NCBI_OA_ENDPOINT = "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi"
# OpenAlex open-access statuses whose best location carries a redistributable
# license. "bronze" (free to read, no license) and "closed" never qualify.
_LICENSED_OA_STATUSES = frozenset({"gold", "hybrid", "green"})
_COMPATIBLE_LICENSES = frozenset({"cc0", "cc-by", "cc-by-sa", "public-domain", "pd"})


def _is_fulltext_html_locator(url) -> bool:
    """True only for an allowlisted full-text HTML page.

    OpenAlex ``best_oa_location.landing_page_url`` is not universally a full-text
    HTML page — most are journal landing shells. Only a small allowlist of known
    full-text HTML repositories/paths is treated as directly-normalizable text:

    * PubMed Central article HTML (``.../pmc/articles/PMC…`` / ``…/articles/PMC…``);
    * Europe PMC full-text article HTML (``europepmc.org/article…``/``/articles…``);
    * arXiv rendered HTML (``arxiv.org/html/…``).

    Everything else is a landing shell and must never be claimed as full text.
    """
    if not isinstance(url, str) or not url.strip():
        return False
    parsed = urlsplit(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    path = (parsed.path or "").lower()
    if host in {"www.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "pmc.ncbi.nlm.nih.gov"}:
        return "/pmc/articles/pmc" in path or "/articles/pmc" in path
    if host == "europepmc.org" or host.endswith(".europepmc.org"):
        return "/article/" in path or "/articles/" in path
    if host == "arxiv.org" or host.endswith(".arxiv.org"):
        return path.startswith("/html/")
    return False


def _topic_queries(topics: list[str], max_queries: int) -> list[list[str]]:
    """Choose bounded, spread-out topic queries instead of one impossible conjunction.

    OpenAlex full-text search treats a long space-separated term list conjunctively;
    sending an entire domain ontology can therefore return zero even when every
    individual topic has substantial literature. Query single topics, bounded and
    evenly sampled across the declared ontology, then dedupe by stable work id.
    """
    if max_queries < 1:
        raise ScholarlyDiscoveryError("max_topic_queries must be >= 1")
    if len(topics) <= max_queries:
        return [[topic] for topic in topics]
    if max_queries == 1:
        return [[topics[0]]]
    indexes = [round(i * (len(topics) - 1) / (max_queries - 1)) for i in range(max_queries)]
    return [[topics[index]] for index in dict.fromkeys(indexes)]


def _normalized_fulltext_html_locator(url) -> str | None:
    """Return a fetchable canonical URL for an allowlisted full-text HTML page."""
    if not _is_fulltext_html_locator(url):
        return None
    parsed = urlsplit(url.strip())
    host = parsed.hostname.lower()
    path = parsed.path or ""
    # Europe PMC article pages currently redirect to a navigation-only shell.
    # When OpenAlex gives us an explicit PMCID path, rewrite it to PMC's stable
    # full-text HTML route. Keep MED/DOI article identities on Europe PMC: they
    # are not interchangeable with a PMCID and must not be guessed.
    if host == "europepmc.org" or host.endswith(".europepmc.org"):
        parts = [part for part in path.split("/") if part]
        pmcid = None
        if any(part.lower() in {"article", "articles"} for part in parts):
            for index, part in enumerate(parts):
                lowered = part.lower()
                if lowered.startswith("pmc") and lowered[3:].isdigit():
                    pmcid = f"PMC{lowered[3:]}"
                    break
                if lowered == "pmc" and index + 1 < len(parts) and parts[index + 1].isdigit():
                    pmcid = f"PMC{parts[index + 1]}"
                    break
            if pmcid is None and parts[-1].isdigit() and "articles" in {part.lower() for part in parts}:
                pmcid = f"PMC{parts[-1]}"
        if pmcid:
            return f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
    return url.strip()


class ScholarlyDiscoveryError(ValueError):
    """Raised when a scholarly discovery response cannot be parsed safely."""


@dataclass(frozen=True)
class ScholarlyCandidate:
    observation: CandidateObservation
    rights_evidence: RightsEvidence
    openalex_id: str
    title: str
    content_locator: str | None
    metadata: dict


def _build_query_url(endpoint: str, topics: list[str], per_page: int, filter_expression: str | None = None) -> str:
    search = " ".join(topics)
    suffix = f"&filter={filter_expression}" if filter_expression else ""
    return f"{endpoint}?search={quote_plus(search)}&per-page={per_page}{suffix}"


def _work_id(work: dict) -> str:
    raw = work.get("id")
    if not isinstance(raw, str) or not raw.startswith("https://openalex.org/W"):
        raise ScholarlyDiscoveryError("OpenAlex work id must be a stable openalex.org/W URL")
    return raw.rsplit("/", 1)[-1]


def _matched_topics(title: str, topics: list[str]) -> tuple[str, ...]:
    lowered = title.lower()
    matched = tuple(topic for topic in topics if topic.lower() in lowered)
    return matched or (topics[0],)


def _rights_evidence(work: dict) -> tuple[RightsEvidence, str | None]:
    open_access = work.get("open_access") or {}
    best = work.get("best_oa_location") or {}
    locations = work.get("locations") or []
    is_oa = bool(open_access.get("is_oa"))
    oa_status = open_access.get("oa_status")

    # Prefer an alternate allowlisted full-text HTML location when it carries its
    # own explicit compatible license. OpenAlex's "best" location is often a DOI
    # landing shell even when the same work has licensed Europe PMC/PMC HTML.
    if is_oa and oa_status in _LICENSED_OA_STATUSES and isinstance(locations, list):
        for location in locations:
            if not isinstance(location, dict):
                continue
            landing = location.get("landing_page_url")
            license_value = location.get("license")
            normalized_license = license_value.strip().lower() if isinstance(license_value, str) else None
            normalized_locator = _normalized_fulltext_html_locator(landing)
            if normalized_locator and normalized_license in _COMPATIBLE_LICENSES:
                return (
                    RightsEvidence(
                        license=normalized_license,
                        access_class="open_content",
                        repository="openalex",
                        is_publicly_visible=True,
                        site_license_grant=False,
                    ),
                    normalized_locator,
                )

    license_value = best.get("license") if isinstance(best, dict) else None
    if is_oa and oa_status in _LICENSED_OA_STATUSES and isinstance(license_value, str) and license_value:
        landing = best.get("landing_page_url") if isinstance(best, dict) else None
        pdf = best.get("pdf_url") if isinstance(best, dict) else None
        open_content = RightsEvidence(
            license=license_value,
            access_class="open_content",
            repository="openalex",
            is_publicly_visible=True,
            site_license_grant=False,
        )
        # Use a landing URL as full text ONLY when it is an allowlisted full-text
        # HTML page (PMC/Europe PMC/arXiv HTML). Otherwise fall back to the PDF —
        # which the executor routes to an extractor/human gate — never fabricating
        # full text from a journal landing shell.
        if _is_fulltext_html_locator(landing):
            return open_content, landing
        if isinstance(pdf, str) and pdf:
            return open_content, pdf
        # A generic landing shell with no PDF: metadata-only, so the executor
        # never fetches and normalizes a landing page as if it were full text.
        return (
            RightsEvidence(
                license=None,
                access_class="metadata",
                repository="openalex",
                is_publicly_visible=True,
                site_license_grant=False,
            ),
            None,
        )
    # No confirmable redistribution license: metadata only.
    return (
        RightsEvidence(
            license=None,
            access_class="metadata",
            repository="openalex",
            is_publicly_visible=True,
            site_license_grant=False,
        ),
        None,
    )


def discover_scholarly(
    *,
    domain: str,
    topics: list[str],
    http_get,
    fetched_at,
    endpoint: str = DEFAULT_WORKS_ENDPOINT,
    per_page: int = 25,
    max_candidates: int = 25,
    max_topic_queries: int = 8,
    timeout_seconds: float = 30.0,
) -> list[ScholarlyCandidate]:
    if not topics:
        raise ScholarlyDiscoveryError("topics must be a non-empty list")
    if max_candidates < 1:
        raise ScholarlyDiscoveryError("max_candidates must be >= 1")

    when = fetched_at()
    candidates: list[ScholarlyCandidate] = []
    seen: set[str] = set()
    # Split the bounded request budget between two complementary lanes per topic:
    # a CC-BY-bearing location lane (to make lawful positive acquisition possible)
    # and an unfiltered lane (to retain metadata-only / gated discovery truth).
    topic_budget = max(1, max_topic_queries // 2)
    sampled_topics = _topic_queries(list(topics), topic_budget)
    query_plans: list[tuple[list[str], str | None]] = []
    for query_topics in sampled_topics:
        query_plans.append((query_topics, "locations.license:cc-by"))
        if len(query_plans) < max_topic_queries:
            query_plans.append((query_topics, None))
    query_plans = query_plans[:max_topic_queries]
    per_query_limit = min(per_page, max(1, (max_candidates + len(query_plans) - 1) // len(query_plans)))
    for query_topics, filter_expression in query_plans:
        if len(candidates) >= max_candidates:
            break
        remaining = max_candidates - len(candidates)
        query_page = min(per_query_limit, remaining)
        url = _build_query_url(endpoint, query_topics, query_page, filter_expression)
        response = http_get(url, timeout_seconds)
        try:
            document = json.loads(response.content)
        except (ValueError, AttributeError) as exc:
            raise ScholarlyDiscoveryError(f"invalid OpenAlex response: {exc}") from exc
        if not isinstance(document, dict) or not isinstance(document.get("results"), list):
            raise ScholarlyDiscoveryError("OpenAlex response must contain a results list")

        for work in document["results"]:
            if len(candidates) >= max_candidates:
                break
            if not isinstance(work, dict):
                raise ScholarlyDiscoveryError("OpenAlex result must be an object")
            work_id = _work_id(work)
            canonical = str(work["id"])
            if canonical in seen:
                continue
            seen.add(canonical)
            title = work.get("title") or work_id
            evidence, content_locator = _rights_evidence(work)
            landing = ((work.get("primary_location") or {}).get("landing_page_url")) or canonical
            observation = CandidateObservation.create(
                domain=domain,
                entity_type="paper",
                canonical_url=canonical,
                discovery_source=f"scholarly_discovery:openalex:{work_id}",
                evidence_pointer=landing,
                evidence_lane="primary-study",
                topics=_matched_topics(str(title), list(topics)),
                observed_at=when,
            )
            candidates.append(
                ScholarlyCandidate(
                    observation=observation,
                    rights_evidence=evidence,
                    openalex_id=work_id,
                    title=str(title),
                    content_locator=content_locator,
                    metadata={
                        "openalex_id": work_id,
                        "doi": work.get("doi"),
                        "publication_date": work.get("publication_date"),
                        "updated_date": work.get("updated_date"),
                        "cited_by_count": work.get("cited_by_count"),
                        "landing_page_url": landing,
                    },
                )
            )
    return candidates


def discover_europepmc(
    *,
    domain: str,
    topics: list[str],
    http_get,
    fetched_at,
    endpoint: str = DEFAULT_EUROPEPMC_ENDPOINT,
    oa_endpoint: str = DEFAULT_NCBI_OA_ENDPOINT,
    max_candidates: int = 8,
    timeout_seconds: float = 30.0,
) -> list[ScholarlyCandidate]:
    """Independent public fallback when OpenAlex is throttled or unavailable.

    Europe PMC supplies the topic search and PMCID.  NCBI's OA service supplies
    the explicit license; only compatible licenses receive a content locator.
    """
    if not topics:
        raise ScholarlyDiscoveryError("topics must be a non-empty list")
    query = quote_plus(f"{domain.replace('-', ' ')} AND OPEN_ACCESS:Y AND IN_EPMC:Y")
    url = f"{endpoint}?query={query}&format=json&pageSize={max_candidates}&resultType=core"
    response = http_get(url, timeout_seconds)
    try:
        document = json.loads(response.content)
        rows = document["resultList"]["result"]
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ScholarlyDiscoveryError(f"invalid Europe PMC response: {exc}") from exc
    if not isinstance(rows, list):
        raise ScholarlyDiscoveryError("Europe PMC response must contain a result list")

    when = fetched_at()
    candidates: list[ScholarlyCandidate] = []
    for row in rows:
        if len(candidates) >= max_candidates or not isinstance(row, dict):
            break
        pmcid = row.get("pmcid")
        if not isinstance(pmcid, str) or not pmcid.upper().startswith("PMC"):
            continue
        pmcid = pmcid.upper()
        license_url = f"{oa_endpoint}?id={quote_plus(pmcid)}"
        try:
            license_response = http_get(license_url, timeout_seconds)
            root = safe_fromstring(license_response.content)
            record = root.find(".//record")
        except (ET.ParseError, AttributeError, ValueError):
            continue
        raw_license = record.get("license") if record is not None else None
        normalized = raw_license.strip().lower().replace(" ", "-") if isinstance(raw_license, str) else ""
        if normalized not in _COMPATIBLE_LICENSES:
            continue
        title = str(row.get("title") or pmcid)
        canonical = f"https://europepmc.org/article/PMC/{pmcid}"
        locator = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
        observation = CandidateObservation.create(
            domain=domain,
            entity_type="paper",
            canonical_url=canonical,
            discovery_source=f"scholarly_discovery:europepmc:{pmcid}",
            evidence_pointer=locator,
            evidence_lane="primary-study",
            topics=_matched_topics(title, topics),
            observed_at=when,
        )
        candidates.append(
            ScholarlyCandidate(
                observation=observation,
                rights_evidence=RightsEvidence(
                    license=normalized,
                    access_class="open_content",
                    repository="europepmc",
                    is_publicly_visible=True,
                    site_license_grant=False,
                ),
                openalex_id=pmcid,
                title=title,
                content_locator=locator,
                metadata={
                    "pmcid": pmcid,
                    "doi": row.get("doi"),
                    "publication_date": row.get("firstPublicationDate"),
                    "source": "europepmc",
                },
            )
        )
    return candidates
