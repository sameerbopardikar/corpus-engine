from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

import requests

from .base import SourceAdapter
from .common import preserve_bytes, sha256_bytes
from .types import InventoryRequest, NormalizedObservation, SourceSpec, TransportPayload

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"
_VERSIONED_ID = re.compile(r"^(?P<paper>\d{4}\.\d{4,5})(?P<version>v\d+)$")
_GITHUB_CODE = re.compile(r"^https://github\.com/[^/]+/[^/]+/tree/(?P<revision>[0-9a-f]{40})/?$")


def _default_get(url: str, timeout: float):
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "GBrainCorpusEngine/1.0"})
    response.raise_for_status()
    return response


def _entries(body: bytes) -> list[ET.Element]:
    root = ET.fromstring(body)
    return root.findall(f"{_ATOM}entry")


def _entry_identity(entry: ET.Element) -> tuple[str, re.Match[str]]:
    identity = (entry.findtext(f"{_ATOM}id") or "").rstrip("/").rsplit("/", 1)[-1]
    match = _VERSIONED_ID.fullmatch(identity)
    if match is None:
        raise ValueError("arXiv paper identity must include an immutable version")
    return identity, match


class ArxivAdapter(SourceAdapter):
    family = "arxiv"

    def __init__(self, raw_root: Path, *, http_get: Callable = _default_get, fetched_at: Callable[[], str]):
        self.raw_root = Path(raw_root)
        self.http_get = http_get
        self.fetched_at = fetched_at

    def fetch(self, spec: SourceSpec, request: InventoryRequest) -> TransportPayload:
        response = self.http_get(spec.canonical_locator, request.timeout_seconds)
        body = response.content
        entries = _entries(body)[: request.max_items]
        if not entries:
            raise ValueError("arXiv response contains no entries")
        identities = [_entry_identity(entry)[0] for entry in entries]
        revision = identities[0] if len(identities) == 1 else hashlib.sha256("\n".join(identities).encode()).hexdigest()
        raw_path = preserve_bytes(self.raw_root, prefix="arxiv", suffix=".xml", body=body)
        return TransportPayload(body=body, final_url=response.url, fetched_at=self.fetched_at(), source_revision=revision, raw_pointer=str(raw_path))

    def parse(self, spec: SourceSpec, request: InventoryRequest, payload: TransportPayload):
        observations = []
        for entry in _entries(payload.body)[: request.max_items]:
            identity, match = _entry_identity(entry)
            links = {link.attrib.get("title") or link.attrib.get("rel", ""): link.attrib.get("href", "") for link in entry.findall(f"{_ATOM}link")}
            canonical = links.get("alternate") or f"https://arxiv.org/abs/{identity}"
            if not canonical.endswith(identity):
                canonical = f"https://arxiv.org/abs/{identity}"
            code_url = links.get("code")
            code_revision = None
            if code_url:
                code_match = _GITHUB_CODE.fullmatch(code_url)
                if code_match is None:
                    raise ValueError("linked code must resolve to an immutable GitHub commit")
                code_revision = code_match.group("revision")
            document = {
                "arxiv_id": match.group("paper"),
                "paper_version": match.group("version"),
                "item_revision": identity,
                "title": " ".join((entry.findtext(f"{_ATOM}title") or "").split()),
                "abstract": " ".join((entry.findtext(f"{_ATOM}summary") or "").split()),
                "authors": [node.findtext(f"{_ATOM}name") for node in entry.findall(f"{_ATOM}author")],
                "published": entry.findtext(f"{_ATOM}published"),
                "updated": entry.findtext(f"{_ATOM}updated"),
                "doi": entry.findtext(f"{_ARXIV}doi"),
                "code_url": code_url,
                "code_revision": code_revision,
                "claim_status": "reported_not_verified",
                "adoption_status": "external_evidence_only",
            }
            normalized = (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()
            normalized_path = preserve_bytes(self.raw_root, prefix=f"paper-{identity}", suffix=".json", body=normalized)
            observations.append(NormalizedObservation(canonical_locator=canonical, title=document["title"] or identity, evidence_pointer=canonical, raw_pointer=payload.raw_pointer, raw_sha256=payload.raw_sha256, normalized_pointer=str(normalized_path), normalized_sha256=sha256_bytes(normalized), content_kind="paper_abstract_reported_claim", fetched_at=payload.fetched_at, source_revision=payload.source_revision, rights_state=spec.rights_state))
        return tuple(observations), payload.source_revision
