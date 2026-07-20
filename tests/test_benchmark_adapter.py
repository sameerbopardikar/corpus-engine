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
from corpus_adapters.benchmark import BenchmarkAdapter
from corpus_adapters.types import InventoryRequest, RightsState, SourceSpec


class Response:
    url = "https://example.org/benchmarks/agentbench/results.json"
    content = json.dumps({"benchmark": "AgentBench", "revision": "2026.07", "results": [
        {"id": "official", "title": "Official leaderboard", "score": 0.72, "unit": "pass_rate", "code_url": "https://github.com/acme/agentbench/tree/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        {"id": "corrected", "title": "Corrected evaluation fork", "score": 0.61, "unit": "pass_rate", "code_url": "https://github.com/reviewer/agentbench/tree/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "is_corrected_fork": True, "corrected_from": "official"}
    ]}).encode()

    def json(self):
        return json.loads(self.content)


class BenchmarkAdapterTests(unittest.TestCase):
    def test_corrected_fork_is_distinct_and_results_are_reported_not_verified(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            adapter = BenchmarkAdapter(root / "raw", http_get=lambda url, timeout: Response(), fetched_at=lambda: "2026-07-17T07:00:00Z")
            spec = SourceSpec("agentbench", "agentic-engineering", "benchmark", Response.url, "benchmark", RightsState.PUBLIC_RIGHTS_CLEAR)
            batch = AdapterRunner(root / "state.json").run(adapter, spec, InventoryRequest(max_items=2))
            self.assertEqual(len(batch.observations), 2)
            self.assertNotEqual(batch.observations[0].canonical_locator, batch.observations[1].canonical_locator)
            corrected = Path(batch.observations[1].normalized_pointer).read_text(encoding="utf-8")
            self.assertEqual(batch.observations[1].content_kind, "benchmark_reported_result_corrected_fork")
            self.assertIn('"corrected_from": "official"', corrected)
            self.assertIn('"claim_status": "reported_not_verified"', corrected)

    def test_preserved_raw_bytes_are_authoritative_over_response_helper(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            class LyingResponse(Response):
                def json(self):
                    document = super().json()
                    document["revision"] = "transport-helper-lie"
                    return document

            adapter = BenchmarkAdapter(root / "raw", http_get=lambda url, timeout: LyingResponse(), fetched_at=lambda: "2026-07-17T07:00:00Z")
            spec = SourceSpec("agentbench", "agentic-engineering", "benchmark", Response.url, "benchmark", RightsState.PUBLIC_RIGHTS_CLEAR)
            batch = AdapterRunner(root / "state.json").run(adapter, spec, InventoryRequest(max_items=1))
            self.assertEqual(batch.source_revision, "2026.07")

    def test_non_correction_substrings_remain_ordinary_results(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            document = {
                "benchmark": "AgentBench",
                "revision": "2026.07",
                "results": [
                    {"id": "incorrect-baseline", "title": "Ordinary baseline", "score": 0.5},
                    {"id": "warehouse", "title": "Forklift benchmark", "score": 0.6},
                ],
            }
            response = type("Response", (), {"url": Response.url, "content": json.dumps(document).encode()})()
            adapter = BenchmarkAdapter(root / "raw", http_get=lambda url, timeout: response, fetched_at=lambda: "2026-07-17T07:00:00Z")
            spec = SourceSpec("agentbench", "agentic-engineering", "benchmark", Response.url, "benchmark", RightsState.PUBLIC_RIGHTS_CLEAR)
            batch = AdapterRunner(root / "state.json").run(adapter, spec, InventoryRequest(max_items=2))
            self.assertEqual(
                [observation.content_kind for observation in batch.observations],
                ["benchmark_reported_result", "benchmark_reported_result"],
            )

    def test_fork_without_correction_lineage_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            document = json.loads(Response.content)
            document["results"][1].pop("corrected_from")
            response = type("Response", (), {"url": Response.url, "content": json.dumps(document).encode(), "json": lambda self: document})()
            adapter = BenchmarkAdapter(root / "raw", http_get=lambda url, timeout: response, fetched_at=lambda: "2026-07-17T07:00:00Z")
            spec = SourceSpec("agentbench", "agentic-engineering", "benchmark", Response.url, "benchmark", RightsState.PUBLIC_RIGHTS_CLEAR)
            with self.assertRaisesRegex(Exception, "corrected_from"):
                AdapterRunner(root / "state.json").run(adapter, spec, InventoryRequest(max_items=2))


if __name__ == "__main__":
    unittest.main()
