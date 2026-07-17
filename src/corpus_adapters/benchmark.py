from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import requests

from .base import SourceAdapter
from .common import preserve_bytes, sha256_bytes
from .types import InventoryRequest, NormalizedObservation, SourceSpec, TransportPayload

_GITHUB_REVISION = re.compile(r"^https://github\.com/[^/]+/[^/]+/tree/[0-9a-f]{40}/?$")


def _default_get(url: str, timeout: float):
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "GBrainCorpusEngine/1.0"})
    response.raise_for_status()
    return response


class BenchmarkAdapter(SourceAdapter):
    family = "benchmark"

    def __init__(self, raw_root: Path, *, http_get: Callable = _default_get, fetched_at: Callable[[], str]):
        self.raw_root = Path(raw_root)
        self.http_get = http_get
        self.fetched_at = fetched_at

    def fetch(self, spec: SourceSpec, request: InventoryRequest) -> TransportPayload:
        response = self.http_get(spec.canonical_locator, request.timeout_seconds)
        body = response.content
        document = json.loads(body)
        revision = document.get("revision") if isinstance(document, dict) else None
        if not isinstance(revision, str) or not revision.strip():
            raise ValueError("benchmark must include an immutable revision")
        raw_path = preserve_bytes(self.raw_root, prefix="benchmark", suffix=".json", body=body)
        return TransportPayload(body=body, final_url=response.url, fetched_at=self.fetched_at(), source_revision=revision, raw_pointer=str(raw_path))

    def parse(self, spec: SourceSpec, request: InventoryRequest, payload: TransportPayload):
        document = json.loads(payload.body)
        benchmark = document.get("benchmark")
        results = document.get("results")
        if not isinstance(benchmark, str) or not benchmark or not isinstance(results, list):
            raise ValueError("benchmark response requires benchmark and results")
        observations = []
        seen: set[str] = set()
        for result in results[: request.max_items]:
            result_id = result.get("id")
            if not isinstance(result_id, str) or not result_id or result_id in seen:
                raise ValueError("benchmark result id must be unique and non-blank")
            seen.add(result_id)
            corrected = "correct" in result_id.lower() or "correct" in str(result.get("title", "")).lower() or "fork" in str(result.get("title", "")).lower()
            if corrected and not result.get("corrected_from"):
                raise ValueError("corrected benchmark fork must include corrected_from")
            if result.get("corrected_from") and result["corrected_from"] not in {item.get("id") for item in results}:
                raise ValueError("corrected_from must resolve within the benchmark snapshot")
            code_url = result.get("code_url")
            if code_url and not _GITHUB_REVISION.fullmatch(code_url):
                raise ValueError("benchmark code_url must pin an immutable GitHub commit")
            normalized_document = {**result, "benchmark": benchmark, "benchmark_revision": payload.source_revision, "claim_status": "reported_not_verified", "adoption_status": "external_evidence_only"}
            normalized = (json.dumps(normalized_document, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()
            normalized_path = preserve_bytes(self.raw_root, prefix=f"result-{result_id}", suffix=".json", body=normalized)
            kind = "benchmark_reported_result_corrected_fork" if corrected else "benchmark_reported_result"
            locator = f"{payload.final_url}#result-{quote(result_id, safe='')}"
            observations.append(NormalizedObservation(canonical_locator=locator, title=result.get("title") or f"{benchmark}: {result_id}", evidence_pointer=locator, raw_pointer=payload.raw_pointer, raw_sha256=payload.raw_sha256, normalized_pointer=str(normalized_path), normalized_sha256=sha256_bytes(normalized), content_kind=kind, fetched_at=payload.fetched_at, source_revision=payload.source_revision, rights_state=spec.rights_state))
        return tuple(observations), payload.source_revision
