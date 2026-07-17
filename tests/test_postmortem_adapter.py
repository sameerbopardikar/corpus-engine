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
from corpus_adapters.postmortem import PostmortemAdapter
from corpus_adapters.types import InventoryRequest, RightsState, SourceSpec


class Response:
    url = "https://ops.example/reports.json"
    content = json.dumps({"reports": [
        {"id": "vendor-1", "title": "Vendor agent case study", "url": "https://vendor.example/case-study", "published_at": "2026-06-01", "evidence_class": "vendor_case_study", "summary": "Vendor reports lower handling time."},
        {"id": "operator-1", "title": "Operator deployment report", "url": "https://operator.example/report", "published_at": "2026-06-02", "evidence_class": "operator_report", "summary": "Operator reports retry failures."},
        {"id": "independent-1", "title": "Independent incident analysis", "url": "https://research.example/postmortem", "published_at": "2026-06-03", "evidence_class": "independent_postmortem", "summary": "Independent analysis reproduces the failure."}
    ]}).encode()

    def json(self):
        return json.loads(self.content)


class PostmortemAdapterTests(unittest.TestCase):
    def test_vendor_operator_and_independent_evidence_classes_remain_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            adapter = PostmortemAdapter(root / "raw", http_get=lambda url, timeout: Response(), fetched_at=lambda: "2026-07-17T07:00:00Z")
            spec = SourceSpec("production-reports", "agentic-engineering", "postmortem", Response.url, "production-evidence", RightsState.PUBLIC_RIGHTS_CLEAR)
            batch = AdapterRunner(root / "state.json").run(adapter, spec, InventoryRequest(max_items=3))
            self.assertEqual([item.content_kind for item in batch.observations], ["vendor_case_study", "operator_report", "independent_postmortem"])
            for item in batch.observations:
                text = Path(item.normalized_pointer).read_text(encoding="utf-8")
                self.assertIn('"adoption_status": "external_evidence_only"', text)
                self.assertIn('"claim_status": "reported_not_verified"', text)

    def test_unknown_evidence_class_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            document = json.loads(Response.content)
            document["reports"][0]["evidence_class"] = "sameer_doctrine"
            response = type("Response", (), {"url": Response.url, "content": json.dumps(document).encode(), "json": lambda self: document})()
            adapter = PostmortemAdapter(root / "raw", http_get=lambda url, timeout: response, fetched_at=lambda: "2026-07-17T07:00:00Z")
            spec = SourceSpec("production-reports", "agentic-engineering", "postmortem", Response.url, "production-evidence", RightsState.PUBLIC_RIGHTS_CLEAR)
            with self.assertRaisesRegex(Exception, "evidence_class"):
                AdapterRunner(root / "state.json").run(adapter, spec, InventoryRequest(max_items=3))


if __name__ == "__main__":
    unittest.main()
