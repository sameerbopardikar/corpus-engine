"""Shared bounded source-adapter contracts for the corpus engine."""

from pathlib import Path
from typing import Any, Callable

from .base import AdapterFailure, AdapterRunner, SourceAdapter
from .types import (
    FailureKind,
    InventoryRequest,
    NormalizedObservation,
    ObservationBatch,
    RightsState,
    SourceSpec,
    TransportPayload,
)

from .arxiv import ArxivAdapter
from .benchmark import BenchmarkAdapter
from .github import GitHubRepositoryAdapter
from .openalex import OpenAlexAdapter
from .postmortem import PostmortemAdapter
from .rss import RSSAdapter
from .web import WebDocumentAdapter
from .youtube import YouTubeFeedAdapter


class AdapterFamilyError(ValueError):
    """Raised when a source family has no registered adapter factory."""


# Declarative family -> factory registry. Every factory shares one signature so
# the engine can construct any adapter without a per-family branch. A domain
# spec selects an adapter purely by naming its family; no Python edits needed.
ADAPTER_FACTORIES: dict[str, Callable[..., SourceAdapter]] = {
    "web": lambda raw_root, *, http_get, fetched_at, entry: WebDocumentAdapter(
        raw_root, http_get=http_get, fetched_at=fetched_at,
        minimum_text_chars=int(entry.get("minimum_text_chars", 200)),
    ),
    "github": lambda raw_root, *, http_get, fetched_at, entry: GitHubRepositoryAdapter(
        raw_root, repository=entry["repo"], http_get=http_get, fetched_at=fetched_at,
    ),
    "youtube": lambda raw_root, *, http_get, fetched_at, entry: YouTubeFeedAdapter(
        raw_root, channel_id=entry["channel_id"], http_get=http_get, fetched_at=fetched_at,
    ),
    "arxiv": lambda raw_root, *, http_get, fetched_at, entry: ArxivAdapter(
        raw_root, http_get=http_get, fetched_at=fetched_at,
    ),
    "openalex": lambda raw_root, *, http_get, fetched_at, entry: OpenAlexAdapter(
        raw_root, http_get=http_get, fetched_at=fetched_at,
    ),
    "benchmark": lambda raw_root, *, http_get, fetched_at, entry: BenchmarkAdapter(
        raw_root, http_get=http_get, fetched_at=fetched_at,
    ),
    "rss": lambda raw_root, *, http_get, fetched_at, entry: RSSAdapter(
        raw_root, feed_kind=entry.get("feed_kind", "operator_feed"), http_get=http_get, fetched_at=fetched_at,
    ),
    "postmortem": lambda raw_root, *, http_get, fetched_at, entry: PostmortemAdapter(
        raw_root, http_get=http_get, fetched_at=fetched_at,
    ),
}

# Families the engine drives through the uniform generic refresh path (they read
# their target purely from the spec's canonical locator). web/github/youtube keep
# their bespoke engine handlers for family-specific preservation/card shaping.
STANDARD_ADAPTER_FAMILIES = ("arxiv", "openalex", "benchmark", "rss", "postmortem")


def build_adapter(
    family: str,
    raw_root: Path,
    *,
    http_get: Callable[..., Any],
    fetched_at: Callable[[], str],
    entry: dict[str, Any],
) -> SourceAdapter:
    """Construct the adapter for a family, failing closed on unknown families."""
    factory = ADAPTER_FACTORIES.get(family)
    if factory is None:
        raise AdapterFamilyError(f"unknown adapter family: {family!r}")
    return factory(raw_root, http_get=http_get, fetched_at=fetched_at, entry=entry)


__all__ = [
    "ADAPTER_FACTORIES",
    "AdapterFailure",
    "AdapterFamilyError",
    "AdapterRunner",
    "ArxivAdapter",
    "BenchmarkAdapter",
    "FailureKind",
    "GitHubRepositoryAdapter",
    "InventoryRequest",
    "NormalizedObservation",
    "ObservationBatch",
    "OpenAlexAdapter",
    "PostmortemAdapter",
    "RSSAdapter",
    "RightsState",
    "STANDARD_ADAPTER_FAMILIES",
    "SourceAdapter",
    "SourceSpec",
    "TransportPayload",
    "WebDocumentAdapter",
    "YouTubeFeedAdapter",
    "build_adapter",
]
