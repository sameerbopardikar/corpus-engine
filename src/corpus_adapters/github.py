from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import requests

from .base import SourceAdapter
from .common import preserve_bytes, sha256_bytes
from .types import InventoryRequest, NormalizedObservation, SourceSpec, TransportPayload


_SHA1 = re.compile(r"^[0-9a-f]{40}$")


def _default_get(url: str, timeout: float):
    response = requests.get(url, timeout=timeout, headers={"Accept": "application/vnd.github+json", "User-Agent": "GBrainCorpusEngine/1.0"})
    response.raise_for_status()
    return response


class GitHubRepositoryAdapter(SourceAdapter):
    family = "github"

    def __init__(self, raw_root: Path, *, repository: str, http_get: Callable = _default_get, fetched_at: Callable[[], str]):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("repository must be owner/name")
        self.raw_root = Path(raw_root)
        self.repository = repository
        self.http_get = http_get
        self.fetched_at = fetched_at

    def fetch(self, spec: SourceSpec, request: InventoryRequest) -> TransportPayload:
        api = f"https://api.github.com/repos/{self.repository}"
        meta_response = self.http_get(api, request.timeout_seconds)
        metadata = meta_response.json()
        branch = metadata.get("default_branch", "main")
        commit_response = self.http_get(f"{api}/commits/{quote(branch, safe='')}", request.timeout_seconds)
        commit = commit_response.json()
        revision = commit.get("sha", "")
        if not isinstance(revision, str) or not _SHA1.fullmatch(revision):
            raise ValueError("GitHub ref did not resolve to an immutable commit SHA")
        readme_response = self.http_get(f"{api}/readme?ref={revision}", request.timeout_seconds)
        readme_document = readme_response.json()
        envelope = {
            "repository": self.repository,
            "revision": revision,
            "branch_observed": branch,
            "metadata_raw_base64": base64.b64encode(meta_response.content).decode("ascii"),
            "commit_raw_base64": base64.b64encode(commit_response.content).decode("ascii"),
            "readme_raw_base64": base64.b64encode(readme_response.content).decode("ascii"),
            "metadata": metadata,
            "readme": readme_document,
        }
        body = (json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        raw_path = preserve_bytes(self.raw_root, prefix="snapshot", suffix=".json", body=body)
        return TransportPayload(
            body=body,
            final_url=f"https://github.com/{self.repository}/tree/{revision}",
            fetched_at=self.fetched_at(),
            source_revision=revision,
            raw_pointer=str(raw_path),
        )

    def parse(self, spec: SourceSpec, request: InventoryRequest, payload: TransportPayload):
        envelope = json.loads(payload.body)
        if envelope.get("revision") != payload.source_revision:
            raise ValueError("snapshot revision mismatch")
        readme_document = envelope.get("readme")
        if not isinstance(readme_document, dict) or readme_document.get("encoding") != "base64":
            raise ValueError("README response must contain base64 content")
        try:
            encoded = readme_document["content"]
            if not isinstance(encoded, str):
                raise ValueError("README content must be text")
            normalized = base64.b64decode("".join(encoded.split()), validate=True)
        except (KeyError, ValueError) as exc:
            raise ValueError("README content is not valid base64") from exc
        normalized_path = preserve_bytes(self.raw_root, prefix="readme", suffix=".md", body=normalized)
        metadata = envelope.get("metadata") or {}
        title = metadata.get("full_name") or self.repository
        evidence_pointer = readme_document.get("html_url") or payload.final_url
        observation = NormalizedObservation(
            canonical_locator=payload.final_url,
            title=title,
            evidence_pointer=evidence_pointer,
            raw_pointer=payload.raw_pointer,
            raw_sha256=payload.raw_sha256,
            normalized_pointer=str(normalized_path),
            normalized_sha256=sha256_bytes(normalized),
            content_kind="repository_readme",
            fetched_at=payload.fetched_at,
            source_revision=payload.source_revision,
            rights_state=spec.rights_state,
        )
        return (observation,), payload.source_revision
