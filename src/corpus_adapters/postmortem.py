from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

from .base import SourceAdapter
from .common import preserve_bytes, sha256_bytes
from .http import safe_get
from .types import InventoryRequest, NormalizedObservation, SourceSpec, TransportPayload

_ALLOWED_CLASSES = {"vendor_case_study", "operator_report", "independent_postmortem"}


def _default_get(url: str, timeout: float):
    return safe_get(url, timeout)


def _reports(document: dict, limit: int) -> list[dict]:
    reports = document.get("reports") if isinstance(document, dict) else None
    if not isinstance(reports, list):
        raise ValueError("production evidence response must contain reports")
    selected = reports[:limit]
    if not selected:
        raise ValueError("production evidence response contains no reports")
    return selected


class PostmortemAdapter(SourceAdapter):
    family = "postmortem"

    def __init__(self, raw_root: Path, *, http_get: Callable = _default_get, fetched_at: Callable[[], str]):
        self.raw_root = Path(raw_root)
        self.http_get = http_get
        self.fetched_at = fetched_at

    def fetch(self, spec: SourceSpec, request: InventoryRequest) -> TransportPayload:
        response = self.http_get(spec.canonical_locator, request.timeout_seconds)
        body = response.content
        document = json.loads(body)
        reports = _reports(document, request.max_items)
        revisions = []
        for report in reports:
            if report.get("evidence_class") not in _ALLOWED_CLASSES:
                raise ValueError("unknown evidence_class")
            if not report.get("id") or not report.get("published_at"):
                raise ValueError("reports require id and published_at")
            revisions.append(f"{report['id']}@{report['published_at']}")
        revision = revisions[0] if len(revisions) == 1 else hashlib.sha256("\n".join(revisions).encode()).hexdigest()
        raw_path = preserve_bytes(self.raw_root, prefix="production-evidence", suffix=".json", body=body)
        return TransportPayload(body=body, final_url=response.url, fetched_at=self.fetched_at(), source_revision=revision, raw_pointer=str(raw_path))

    def parse(self, spec: SourceSpec, request: InventoryRequest, payload: TransportPayload):
        observations = []
        for report in _reports(json.loads(payload.body), request.max_items):
            evidence_class = report.get("evidence_class")
            if evidence_class not in _ALLOWED_CLASSES:
                raise ValueError("unknown evidence_class")
            url = report.get("url")
            if not isinstance(url, str) or not url:
                raise ValueError("report requires url")
            document = {**report, "item_revision": f"{report.get('id')}@{report.get('published_at')}", "claim_status": "reported_not_verified", "adoption_status": "external_evidence_only"}
            normalized = (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()
            normalized_path = preserve_bytes(self.raw_root, prefix=f"report-{sha256_bytes(str(report.get('id')).encode())[:12]}", suffix=".json", body=normalized)
            observations.append(NormalizedObservation(canonical_locator=url, title=report.get("title") or report["id"], evidence_pointer=url, raw_pointer=payload.raw_pointer, raw_sha256=payload.raw_sha256, normalized_pointer=str(normalized_path), normalized_sha256=sha256_bytes(normalized), content_kind=evidence_class, fetched_at=payload.fetched_at, source_revision=payload.source_revision, rights_state=spec.rights_state))
        return tuple(observations), payload.source_revision
