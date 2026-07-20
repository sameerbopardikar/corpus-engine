"""SSRF-safe HTTP response shim shared by every source adapter."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

@dataclass(frozen=True)
class SafeHttpResponse:
    """Minimal requests-compatible surface used by corpus adapters."""

    content: bytes
    url: str
    headers: Mapping[str, str]

    def json(self):
        return json.loads(self.content)

    def raise_for_status(self) -> None:
        """Compatibility no-op: ``live_fetch`` already rejects HTTP errors."""


def safe_get(
    url: str,
    timeout: float,
    *,
    headers: Mapping[str, str] | None = None,
) -> SafeHttpResponse:
    """Fetch through the DNS-pinned transport and expose an adapter response."""

    # Lazy import avoids the package-initialization cycle:
    # acquisition_executor -> corpus_adapters.common -> corpus_adapters.__init__
    # -> adapters -> this module -> corpus_live_fetch -> acquisition_executor.
    from corpus_live_fetch import live_fetch

    fetched = live_fetch(url, timeout=timeout, headers=headers)
    response_headers = {"content-type": fetched.content_type or ""}
    return SafeHttpResponse(content=fetched.body, url=fetched.final_url, headers=response_headers)
