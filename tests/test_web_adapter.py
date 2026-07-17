from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_adapters.base import AdapterRunner
from corpus_adapters.types import InventoryRequest, RightsState, SourceSpec
from corpus_adapters.web import WebDocumentAdapter


class Response:
    def __init__(self, body: bytes):
        self.content = body
        self.url = "https://example.com/guide"
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"


class WebDocumentAdapterTests(unittest.TestCase):
    def test_raw_and_normalized_hashes_are_distinct_and_both_resolve(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw = b"<html><title>Guide</title><script>ignore()</script><main><h1>Agent loop</h1><p>Preserve evidence.</p></main></html>"
            adapter = WebDocumentAdapter(
                root / "raw",
                http_get=lambda url, timeout: Response(raw),
                fetched_at=lambda: "2026-07-17T06:00:00Z",
            )
            spec = SourceSpec(
                source_id="guide",
                domain="agentic-engineering",
                source_family="web",
                canonical_locator="https://example.com/guide",
                evidence_lane="canonical",
                rights_state=RightsState.PUBLIC_RIGHTS_CLEAR,
            )
            batch = AdapterRunner(root / "state.json").run(adapter, spec, InventoryRequest(max_items=1))
            observation = batch.observations[0]

            self.assertEqual(observation.raw_sha256, hashlib.sha256(raw).hexdigest())
            self.assertNotEqual(observation.raw_sha256, observation.normalized_sha256)
            self.assertEqual(hashlib.sha256(Path(observation.normalized_pointer).read_bytes()).hexdigest(), observation.normalized_sha256)
            normalized = Path(observation.normalized_pointer).read_text(encoding="utf-8")
            self.assertIn("Agent loop", normalized)
            self.assertNotIn("ignore()", normalized)
            self.assertEqual(observation.content_kind, "document_text")

    def test_retry_with_identical_bytes_is_state_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw = b"# Stable guide\n\nSame bytes.\n"
            response = Response(raw)
            response.headers = {"content-type": "text/markdown"}
            fetched_times = iter(["2026-07-17T06:00:00Z", "2026-07-17T06:01:00Z"])
            adapter = WebDocumentAdapter(root / "raw", http_get=lambda url, timeout: response, fetched_at=lambda: next(fetched_times))
            spec = SourceSpec("guide", "agentic-engineering", "web", "https://example.com/guide", "canonical", RightsState.PUBLIC_RIGHTS_CLEAR)
            runner = AdapterRunner(root / "state.json")
            first = runner.run(adapter, spec, InventoryRequest(max_items=1))
            before = (root / "state.json").read_bytes()
            second = runner.run(adapter, spec, InventoryRequest(max_items=1, cursor=first.cursor_after))
            self.assertEqual(first.batch_id, second.batch_id)
            self.assertEqual((root / "state.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
