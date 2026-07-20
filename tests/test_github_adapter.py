from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_adapters.base import AdapterRunner
from corpus_adapters.github import GitHubRepositoryAdapter
from corpus_adapters.types import InventoryRequest, RightsState, SourceSpec


class Response:
    def __init__(self, *, url: str, body: bytes = b"", document=None):
        self.url = url
        self.content = body if document is None else json.dumps(document).encode("utf-8")
        self._document = document
        self.headers = {"content-type": "application/json"}
        self.encoding = "utf-8"

    def json(self):
        return self._document


class GitHubRepositoryAdapterTests(unittest.TestCase):
    def test_moving_ref_resolves_to_immutable_commit_and_readme_is_fetched_at_that_commit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            calls: list[str] = []
            sha = "a" * 40

            def get(url: str, timeout: float):
                calls.append(url)
                if url.endswith("/repos/acme/widget"):
                    return Response(url=url, document={"full_name": "acme/widget", "default_branch": "main", "html_url": "https://github.com/acme/widget"})
                if url.endswith("/commits/main"):
                    return Response(url=url, document={"sha": sha})
                if "/readme?" in url:
                    self.assertEqual(parse_qs(urlsplit(url).query).get("ref"), [sha])
                    return Response(url=url, document={"content": "IyBXaWRn\nZXQK", "encoding": "base64", "html_url": f"https://github.com/acme/widget/blob/{sha}/README.md"})
                raise AssertionError(f"unexpected URL: {url}")

            adapter = GitHubRepositoryAdapter(root / "raw", repository="acme/widget", http_get=get, fetched_at=lambda: "2026-07-17T06:00:00Z")
            spec = SourceSpec("widget", "agentic-engineering", "github", "https://github.com/acme/widget", "production", RightsState.PUBLIC_RIGHTS_CLEAR)
            batch = AdapterRunner(root / "state.json").run(adapter, spec, InventoryRequest(max_items=1))
            observation = batch.observations[0]

            self.assertEqual(batch.source_revision, sha)
            self.assertEqual(observation.source_revision, sha)
            self.assertEqual(observation.canonical_locator, f"https://github.com/acme/widget/tree/{sha}")
            self.assertIn("# Widget", Path(observation.normalized_pointer).read_text(encoding="utf-8"))
            self.assertTrue(any(url.endswith("/commits/main") for url in calls))
            self.assertFalse(any(url.endswith("/commits?per_page=1") for url in calls))

    def test_non_sha_ref_from_api_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def get(url: str, timeout: float):
                if url.endswith("/repos/acme/widget"):
                    return Response(url=url, document={"default_branch": "main"})
                if url.endswith("/commits/main"):
                    return Response(url=url, document={"sha": "moving-main"})
                raise AssertionError(url)

            adapter = GitHubRepositoryAdapter(root / "raw", repository="acme/widget", http_get=get, fetched_at=lambda: "2026-07-17T06:00:00Z")
            spec = SourceSpec("widget", "agentic-engineering", "github", "https://github.com/acme/widget", "production", RightsState.PUBLIC_RIGHTS_CLEAR)
            with self.assertRaisesRegex(Exception, "immutable commit"):
                AdapterRunner(root / "state.json").run(adapter, spec, InventoryRequest(max_items=1))
            self.assertFalse((root / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
