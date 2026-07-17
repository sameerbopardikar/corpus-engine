from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Callable

import requests

from .base import SourceAdapter
from .common import preserve_bytes, sha256_bytes
from .types import InventoryRequest, NormalizedObservation, SourceSpec, TransportPayload

_OPENALEX_ID = re.compile(r"^https://openalex\.org/(?P<id>W\d+)$")


def _default_get(url: str, timeout: float):
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "GBrainCorpusEngine/1.0"})
    response.raise_for_status()
    return response


def _document(response) -> dict:
    value = response.json()
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        raise ValueError("OpenAlex response must contain results")
    return value


def _identity(work: dict) -> tuple[str, str]:
    match = _OPENALEX_ID.fullmatch(work.get("id") or "")
    if match is None:
        raise ValueError("OpenAlex work id must be stable")
    updated = work.get("updated_date")
    if not isinstance(updated, str) or not updated:
        raise ValueError("OpenAlex work must include updated_date")
    return match.group("id"), f"{match.group('id')}@{updated}"


class OpenAlexAdapter(SourceAdapter):
    family = "openalex"

    def __init__(self, raw_root: Path, *, http_get: Callable = _default_get, fetched_at: Callable[[], str]):
        self.raw_root = Path(raw_root)
        self.http_get = http_get
        self.fetched_at = fetched_at

    def fetch(self, spec: SourceSpec, request: InventoryRequest) -> TransportPayload:
        response = self.http_get(spec.canonical_locator, request.timeout_seconds)
        body = response.content
        document = json.loads(body)
        if not isinstance(document, dict) or not isinstance(document.get("results"), list):
            raise ValueError("OpenAlex response must contain results")
        revisions = [_identity(work)[1] for work in document["results"][: request.max_items]]
        if not revisions:
            raise ValueError("OpenAlex response contains no works")
        revision = revisions[0] if len(revisions) == 1 else hashlib.sha256("\n".join(revisions).encode()).hexdigest()
        raw_path = preserve_bytes(self.raw_root, prefix="openalex", suffix=".json", body=body)
        return TransportPayload(body=body, final_url=response.url, fetched_at=self.fetched_at(), source_revision=revision, raw_pointer=str(raw_path))

    def parse(self, spec: SourceSpec, request: InventoryRequest, payload: TransportPayload):
        document = json.loads(payload.body)
        observations = []
        for work in document["results"][: request.max_items]:
            work_id, item_revision = _identity(work)
            canonical = work["id"]
            normalized_document = {
                "openalex_id": work_id,
                "item_revision": item_revision,
                "title": work.get("title"),
                "publication_date": work.get("publication_date"),
                "updated_date": work.get("updated_date"),
                "doi": work.get("doi"),
                "ids": work.get("ids") or {},
                "landing_page_url": (work.get("primary_location") or {}).get("landing_page_url"),
                "cited_by_count": work.get("cited_by_count"),
                "claim_status": "reported_not_verified",
                "adoption_status": "external_evidence_only",
            }
            normalized = (json.dumps(normalized_document, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()
            normalized_path = preserve_bytes(self.raw_root, prefix=f"work-{work_id}", suffix=".json", body=normalized)
            observations.append(NormalizedObservation(canonical_locator=canonical, title=work.get("title") or work_id, evidence_pointer=normalized_document["landing_page_url"] or canonical, raw_pointer=payload.raw_pointer, raw_sha256=payload.raw_sha256, normalized_pointer=str(normalized_path), normalized_sha256=sha256_bytes(normalized), content_kind="scholarly_metadata_reported_claim", fetched_at=payload.fetched_at, source_revision=payload.source_revision, rights_state=spec.rights_state))
        cursor = (document.get("meta") or {}).get("next_cursor")
        return tuple(observations), cursor
