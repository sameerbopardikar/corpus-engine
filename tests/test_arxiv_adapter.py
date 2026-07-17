from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_adapters.base import AdapterRunner
from corpus_adapters.arxiv import ArxivAdapter
from corpus_adapters.types import InventoryRequest, RightsState, SourceSpec


ARXIV_ATOM = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry><id>http://arxiv.org/abs/2401.01234v3</id><updated>2026-07-01T00:00:00Z</updated>
  <published>2024-01-02T00:00:00Z</published><title>Agent Reliability</title>
  <summary>We report a 12 percent improvement on a benchmark.</summary>
  <author><name>A. Researcher</name></author>
  <link rel="alternate" href="https://arxiv.org/abs/2401.01234v3"/>
  <link rel="related" title="code" href="https://github.com/acme/agent-reliability/tree/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"/>
  <arxiv:doi>10.1234/example</arxiv:doi></entry>
</feed>'''


class Response:
    content = ARXIV_ATOM
    url = "https://export.arxiv.org/api/query?search_query=all:agent"


class ArxivAdapterTests(unittest.TestCase):
    def test_preserves_paper_version_and_linked_code_as_reported_claim(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            adapter = ArxivAdapter(root / "raw", http_get=lambda url, timeout: Response(), fetched_at=lambda: "2026-07-17T07:00:00Z")
            spec = SourceSpec("arxiv-agent", "agentic-engineering", "arxiv", "https://export.arxiv.org/api/query?search_query=all:agent", "scientific", RightsState.PUBLIC_METADATA_ONLY)
            batch = AdapterRunner(root / "state.json").run(adapter, spec, InventoryRequest(max_items=1))
            observation = batch.observations[0]
            normalized = Path(observation.normalized_pointer).read_text(encoding="utf-8")
            self.assertEqual(observation.canonical_locator, "https://arxiv.org/abs/2401.01234v3")
            self.assertEqual(observation.source_revision, "2401.01234v3")
            self.assertEqual(observation.content_kind, "paper_abstract_reported_claim")
            self.assertIn('"paper_version": "v3"', normalized)
            self.assertIn('"code_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"', normalized)
            self.assertIn('"claim_status": "reported_not_verified"', normalized)

    def test_unversioned_paper_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bad = ARXIV_ATOM.replace(b"2401.01234v3", b"2401.01234")
            response = type("Response", (), {"content": bad, "url": Response.url})()
            adapter = ArxivAdapter(root / "raw", http_get=lambda url, timeout: response, fetched_at=lambda: "2026-07-17T07:00:00Z")
            spec = SourceSpec("arxiv-agent", "agentic-engineering", "arxiv", Response.url, "scientific", RightsState.PUBLIC_METADATA_ONLY)
            with self.assertRaisesRegex(Exception, "version"):
                AdapterRunner(root / "state.json").run(adapter, spec, InventoryRequest(max_items=1))
            self.assertFalse((root / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
