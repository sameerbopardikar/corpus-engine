#!/usr/bin/env python3
"""One canonical identity resolver for entities, evidence documents, publishers.

Seed registries, watch projections, source-graph edges, and discovered
candidates must all compare on the same identity. Tracking aliases, host
aliases, and platform URL variants of one entity resolve to one string, so a
rediscovered source cannot masquerade as novel and query aliases of one page
cannot masquerade as independent corroboration.

Three identity levels are exposed:

* ``canonical_entity_identity`` - the thing itself (a repository, a person
  page, a video, a search endpoint response).
* ``evidence_document_identity`` - the underlying document an evidence pointer
  quotes; span, digest, and query aliases of one page collapse together.
* ``publisher_identity`` - the owner/publisher account behind a document, so
  two pages by one owner are not two independent publishers.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class EntityIdentityError(ValueError):
    """A URL cannot be resolved to a canonical identity."""


_TRACKING_PREFIXES = ("utm_", "at_", "pk_", "mtm_", "hsa_", "_hs")
_TRACKING_PARAMS = frozenset(
    {
        "cmpid", "campaign", "campaignid", "fbclid", "gbraid", "gclid", "igshid",
        "mc_cid", "mc_eid", "msclkid", "originalsubdomain", "ref", "ref_src",
        "ref_url", "referrer", "scid", "si", "source", "spm", "trk", "twclid",
        "wbraid", "yclid",
    }
)

# Hosts where the first path segment identifies the publishing owner/account
# rather than a page within one publisher's site.
_OWNER_SCOPED_HOSTS = frozenset(
    {
        "bitbucket.org", "codeberg.org", "gist.github.com", "github.com",
        "gitlab.com", "huggingface.co", "medium.com", "substack.com",
        "twitter.com", "x.com",
    }
)

_YOUTUBE_HOSTS = frozenset({"youtube.com", "m.youtube.com", "music.youtube.com"})
_ARXIV_HOSTS = frozenset({"arxiv.org"})
_GITHUB_HOSTS = frozenset({"github.com"})

_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{5,64}$")


def _require_url(value: object) -> tuple[str, str, str, list[tuple[str, str]]]:
    if not isinstance(value, str) or not value.strip():
        raise EntityIdentityError("URL must be non-blank text")
    raw = value.strip()
    if len(raw) > 4096:
        raise EntityIdentityError("URL exceeds 4096 characters")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise EntityIdentityError("URL must use http or https")
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise EntityIdentityError("URL has an invalid authority") from exc
    if not host:
        raise EntityIdentityError("URL must have a host")
    if parsed.username or parsed.password:
        raise EntityIdentityError("URL must not contain credentials")
    scheme = parsed.scheme.lower()
    host = host.lower()
    if host.startswith("www.") and len(host) > 4:
        host = host[4:]
    if port is not None and not (
        (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    ):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking(key)
    ]
    return scheme, host, path, query


def _is_tracking(key: str) -> bool:
    folded = key.strip().lower()
    return folded in _TRACKING_PARAMS or folded.startswith(_TRACKING_PREFIXES)


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _compose(scheme: str, host: str, path: str, query: list[tuple[str, str]]) -> str:
    encoded = urlencode(sorted(query), doseq=False)
    return urlunsplit((scheme, host, "" if path == "/" else path, encoded, ""))


def _platform_identity(
    host: str, path: str, query: list[tuple[str, str]]
) -> tuple[str, str, list[tuple[str, str]]] | None:
    """Collapse known platform URL variants onto one stable resource form."""
    segments = _segments(path)
    if host in _GITHUB_HOSTS and len(segments) >= 2:
        repository = re.sub(r"\.git$", "", segments[1])
        if repository:
            return "github.com", f"/{segments[0]}/{repository}", []
    if host in _ARXIV_HOSTS and len(segments) >= 2 and segments[0] in {"abs", "pdf", "html"}:
        paper = segments[1].removesuffix(".pdf")
        if paper:
            return "arxiv.org", f"/abs/{paper}", []
    if host == "youtu.be" and segments:
        if _YOUTUBE_ID.fullmatch(segments[0]):
            return "youtube.com", "/watch", [("v", segments[0])]
    if host in _YOUTUBE_HOSTS and segments == ["watch"]:
        video = dict(query).get("v", "")
        if _YOUTUBE_ID.fullmatch(video):
            return "youtube.com", "/watch", [("v", video)]
    return None


def canonical_entity_identity(url: object) -> str:
    """Resolve one entity URL to its canonical, alias-free identity string."""
    scheme, host, path, query = _require_url(url)
    platform = _platform_identity(host, path, query)
    if platform is not None:
        host, path, query = platform
        scheme = "https"
    return _compose(scheme, host, path, query)


def evidence_document_identity(url: object) -> str:
    """Resolve an evidence pointer to the underlying document it quotes.

    Span selectors, digest annotations, and query aliases of one page are
    provenance detail, not independent documents. Platform resources whose
    identity genuinely lives in the query string (a video id) are preserved by
    the canonical platform form.
    """
    scheme, host, path, query = _require_url(url)
    platform = _platform_identity(host, path, query)
    if platform is not None:
        host, path, query = platform
        scheme = "https"
    else:
        query = []
    return _compose(scheme, host, path, query)


def publisher_identity(url: object) -> str:
    """Resolve an evidence pointer to the owner/publisher accountable for it."""
    _, host, path, _ = _require_url(url)
    segments = _segments(path)
    if host in _OWNER_SCOPED_HOSTS and segments:
        return f"{host}/{segments[0].lower()}"
    return host
