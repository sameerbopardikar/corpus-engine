from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

from defusedxml.ElementTree import fromstring as safe_fromstring

from .base import SourceAdapter
from .common import preserve_bytes, sha256_bytes
from .http import safe_get
from .types import InventoryRequest, NormalizedObservation, SourceSpec, TransportPayload

_ALLOWED_KINDS = {"official_changelog", "operator_feed"}


def _default_get(url: str, timeout: float):
    return safe_get(url, timeout)


def _items(body: bytes) -> list[dict[str, str]]:
    root = safe_fromstring(body)
    values = []
    for item in root.findall("./channel/item"):
        values.append({name: (item.findtext(name) or "").strip() for name in ("guid", "title", "link", "pubDate", "description")})
    return values


class RSSAdapter(SourceAdapter):
    family = "rss"

    def __init__(self, raw_root: Path, *, feed_kind: str, http_get: Callable = _default_get, fetched_at: Callable[[], str]):
        if feed_kind not in _ALLOWED_KINDS:
            raise ValueError(f"feed_kind must be one of {sorted(_ALLOWED_KINDS)}")
        self.raw_root = Path(raw_root)
        self.feed_kind = feed_kind
        self.http_get = http_get
        self.fetched_at = fetched_at

    def fetch(self, spec: SourceSpec, request: InventoryRequest) -> TransportPayload:
        response = self.http_get(spec.canonical_locator, request.timeout_seconds)
        body = response.content
        items = _items(body)[: request.max_items]
        if not items or any(not item["guid"] for item in items):
            raise ValueError("RSS entries require stable guid values")
        revisions = [item["guid"] for item in items]
        revision = revisions[0] if len(revisions) == 1 else hashlib.sha256("\n".join(revisions).encode()).hexdigest()
        raw_path = preserve_bytes(self.raw_root, prefix="feed", suffix=".xml", body=body)
        return TransportPayload(body=body, final_url=response.url, fetched_at=self.fetched_at(), source_revision=revision, raw_pointer=str(raw_path))

    def parse(self, spec: SourceSpec, request: InventoryRequest, payload: TransportPayload):
        observations = []
        for item in _items(payload.body)[: request.max_items]:
            if not item["guid"] or not item["link"]:
                raise ValueError("RSS entries require guid and link")
            document = {**item, "feed_kind": self.feed_kind, "item_revision": item["guid"], "claim_status": "reported_not_verified", "adoption_status": "external_evidence_only"}
            normalized = (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()
            normalized_path = preserve_bytes(self.raw_root, prefix=f"entry-{sha256_bytes(item['guid'].encode())[:12]}", suffix=".json", body=normalized)
            kind = "official_changelog_entry" if self.feed_kind == "official_changelog" else "operator_feed_entry"
            observations.append(NormalizedObservation(canonical_locator=item["link"], title=item["title"] or item["guid"], evidence_pointer=item["link"], raw_pointer=payload.raw_pointer, raw_sha256=payload.raw_sha256, normalized_pointer=str(normalized_path), normalized_sha256=sha256_bytes(normalized), content_kind=kind, fetched_at=payload.fetched_at, source_revision=payload.source_revision, rights_state=spec.rights_state))
        return tuple(observations), payload.source_revision
