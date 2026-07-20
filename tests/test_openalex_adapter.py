from __future__ import annotations

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
from corpus_adapters.openalex import OpenAlexAdapter
from corpus_adapters.types import InventoryRequest, RightsState, SourceSpec


class Response:
    url = "https://api.openalex.org/works?search=agents"
    content = json.dumps({"meta": {"next_cursor": "next-2"}, "results": [{
        "id": "https://openalex.org/W123", "title": "Reliable Agents", "publication_date": "2026-06-01",
        "doi": "https://doi.org/10.1234/reliable", "updated_date": "2026-07-02T00:00:00Z",
        "primary_location": {"landing_page_url": "https://doi.org/10.1234/reliable"},
        "ids": {"openalex": "https://openalex.org/W123", "doi": "https://doi.org/10.1234/reliable"},
        "cited_by_count": 9
    }]}).encode()

    def json(self):
        return json.loads(self.content)


class OpenAlexAdapterTests(unittest.TestCase):
    def test_metadata_remains_reported_claim_and_cursor_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            adapter = OpenAlexAdapter(root / "raw", http_get=lambda url, timeout: Response(), fetched_at=lambda: "2026-07-17T07:00:00Z")
            spec = SourceSpec("openalex-agent", "agentic-engineering", "openalex", Response.url, "scientific-index", RightsState.PUBLIC_METADATA_ONLY)
            batch = AdapterRunner(root / "state.json").run(adapter, spec, InventoryRequest(max_items=1))
            observation = batch.observations[0]
            normalized = Path(observation.normalized_pointer).read_text(encoding="utf-8")
            self.assertEqual(batch.cursor_after, "next-2")
            self.assertEqual(observation.source_revision, "W123@2026-07-02T00:00:00Z")
            self.assertEqual(observation.content_kind, "scholarly_metadata_reported_claim")
            self.assertIn('"claim_status": "reported_not_verified"', normalized)
            self.assertIn('"doi": "https://doi.org/10.1234/reliable"', normalized)

    def test_missing_stable_work_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            response = Response()
            response.content = response.content.replace(b'"https://openalex.org/W123"', b'null')
            adapter = OpenAlexAdapter(root / "raw", http_get=lambda url, timeout: response, fetched_at=lambda: "2026-07-17T07:00:00Z")
            spec = SourceSpec("openalex-agent", "agentic-engineering", "openalex", Response.url, "scientific-index", RightsState.PUBLIC_METADATA_ONLY)
            with self.assertRaisesRegex(Exception, "OpenAlex work id"):
                AdapterRunner(root / "state.json").run(adapter, spec, InventoryRequest(max_items=1))


if __name__ == "__main__":
    unittest.main()
