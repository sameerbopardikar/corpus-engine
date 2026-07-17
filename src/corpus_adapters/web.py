from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

import requests

from .base import AdapterFailure, SourceAdapter
from .common import preserve_bytes, sha256_bytes
from .types import FailureKind, InventoryRequest, NormalizedObservation, RightsState, SourceSpec, TransportPayload


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.in_title = False
        self.title = ""
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "svg", "noscript", "template"}:
            self.skip += 1
        if tag == "title":
            self.in_title = True
        if tag in {"p", "div", "section", "article", "main", "li", "h1", "h2", "h3", "h4", "pre", "code", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "noscript", "template"} and self.skip:
            self.skip -= 1
        if tag == "title":
            self.in_title = False
        if tag in {"p", "div", "section", "article", "main", "li", "h1", "h2", "h3", "h4", "pre", "code"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        text = data.strip()
        if not text:
            return
        if self.in_title:
            self.title = f"{self.title} {text}".strip()
        self.parts.append(text)

    def text(self) -> str:
        value = html.unescape(" ".join(self.parts))
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r" *\n *", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip() + "\n"


def _default_get(url: str, timeout: float):
    response = requests.get(url, timeout=timeout, allow_redirects=True, headers={"User-Agent": "GBrainCorpusEngine/1.0"})
    response.raise_for_status()
    return response


class WebDocumentAdapter(SourceAdapter):
    family = "web"

    def __init__(
        self,
        raw_root: Path,
        *,
        http_get: Callable = _default_get,
        fetched_at: Callable[[], str],
        minimum_text_chars: int = 1,
    ):
        if isinstance(minimum_text_chars, bool) or not isinstance(minimum_text_chars, int) or minimum_text_chars < 1:
            raise ValueError("minimum_text_chars must be a positive integer")
        self.raw_root = Path(raw_root)
        self.http_get = http_get
        self.fetched_at = fetched_at
        self.minimum_text_chars = minimum_text_chars

    def fetch(self, spec: SourceSpec, request: InventoryRequest) -> TransportPayload:
        if spec.rights_state not in {RightsState.PUBLIC_RIGHTS_CLEAR, RightsState.PRIVATE_AUTHORIZED}:
            raise AdapterFailure(FailureKind.CONTRACT, "full web documents require body-acquisition rights")
        response = self.http_get(spec.canonical_locator, request.timeout_seconds)
        body = response.content
        content_type = response.headers.get("content-type", "").lower()
        suffix = ".md" if "markdown" in content_type or response.url.endswith(".md") else ".html"
        raw_path = preserve_bytes(self.raw_root, prefix="snapshot", suffix=suffix, body=body)
        return TransportPayload(
            body=body,
            final_url=response.url,
            fetched_at=self.fetched_at(),
            source_revision=sha256_bytes(body),
            raw_pointer=str(raw_path),
        )

    def parse(self, spec: SourceSpec, request: InventoryRequest, payload: TransportPayload):
        raw_path = Path(payload.raw_pointer)
        if raw_path.suffix == ".md":
            text = payload.body.decode("utf-8", errors="replace")
            title = next((line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#")), spec.source_id)
        else:
            parser = _VisibleTextParser()
            parser.feed(payload.body.decode("utf-8", errors="replace"))
            text = parser.text()
            title = parser.title or spec.source_id
        if len(text.strip()) < self.minimum_text_chars:
            raise ValueError(f"normalized text too short: {len(text.strip())} chars")
        normalized = text.encode("utf-8")
        normalized_path = preserve_bytes(self.raw_root, prefix="normalized", suffix=".txt", body=normalized)
        observation = NormalizedObservation(
            canonical_locator=payload.final_url,
            title=title,
            evidence_pointer=payload.final_url,
            raw_pointer=payload.raw_pointer,
            raw_sha256=payload.raw_sha256,
            normalized_pointer=str(normalized_path),
            normalized_sha256=sha256_bytes(normalized),
            content_kind="document_text",
            fetched_at=payload.fetched_at,
            source_revision=payload.source_revision,
            rights_state=spec.rights_state,
        )
        return (observation,), payload.source_revision
