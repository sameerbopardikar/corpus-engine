from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPT = ROOT / "scripts" / "corpus_candidate_policy.py"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_source_graph import SourceRelationship, ingest_relationships


class CandidatePolicyCliTests(unittest.TestCase):
    def test_cli_promotes_corroborated_candidate_and_persists_watch_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = root / "source-graph.jsonl"
            ledger = root / "discovery-ledger.jsonl"
            watch = root / "watch-projection.json"
            candidate = "https://github.com/example/recursive-engine"
            common = {
                "domain": "agentic-engineering",
                "canonical_url": candidate,
                "title": "Recursive Engine",
                "entity_type": "repository",
                "topics": ("agentic-engineering", "reliability"),
                "observed_at": "2026-07-28T00:00:00Z",
            }
            relationships = [
                SourceRelationship(
                    source_family="paper",
                    relationship_type="linked_primary_source",
                    discovered_from_url="https://arxiv.org/abs/2601.00001",
                    evidence_pointer="https://arxiv.org/abs/2601.00001",
                    evidence_lane="scientific",
                    **common,
                ),
                SourceRelationship(
                    source_family="conference",
                    relationship_type="related_project",
                    discovered_from_url="https://conf.example/talks/recursive-engine",
                    evidence_pointer="https://conf.example/talks/recursive-engine",
                    evidence_lane="security-evaluation",
                    **common,
                ),
            ]
            ingest_relationships(
                relationships,
                graph_path=graph,
                discovery_ledger_path=ledger,
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--discovery-ledger", str(ledger),
                    "--source-graph", str(graph),
                    "--watch-projection", str(watch),
                    "--evaluated-at", "2026-07-28T01:00:00Z",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                self.fail(f"candidate policy CLI failed: {result.stderr}")
            receipt = json.loads(result.stdout)
            projection = json.loads(watch.read_text(encoding="utf-8"))
            self.assertEqual(receipt["counts"]["promoted"], 1)
            self.assertEqual(projection["entries"][0]["canonical_url"], candidate)
            self.assertEqual(projection["entries"][0]["status"], "promoted")
            self.assertTrue(projection["entries"][0]["work_id"].startswith("work_"))


if __name__ == "__main__":
    unittest.main()
