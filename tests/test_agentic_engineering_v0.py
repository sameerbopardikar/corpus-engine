import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentic_engineering_v0 import run_v0


class AgenticEngineeringV0Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.corpus_root = self.root / "corpus"
        self.state_root = self.root / "state"
        self.output_root = self.root / "output"
        self.corpus_root.mkdir()
        (self.corpus_root / "index.md").write_text(
            "# Existing doctrine\n\nVerification and leases are already covered.\n",
            encoding="utf-8",
        )
        self.registry = self.corpus_root / "registry" / "sources.json"
        self.registry.parent.mkdir()
        self.registry.write_text('{"schema_version": 1, "domain": "agentic-engineering", "sources": []}\n', encoding="utf-8")
        self.registry_before = self.registry.read_bytes()
        self.seed = self.root / "seed.json"
        self.seed.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "domain": "agentic-engineering",
                    "status": "candidate-seed-not-promoted",
                    "generated_at": "2026-07-16T00:00:00Z",
                    "policy": "test candidates only",
                    "candidates": [
                        {
                            "id": "covered",
                            "entity_type": "document",
                            "canonical_url": "https://example.com/covered/",
                            "source_type": "web_document",
                            "evidence_lane": "production-reliability",
                            "authority_tier": "A1",
                            "refresh_class": "frozen",
                            "topics": ["verification"],
                        },
                        {
                            "id": "gap",
                            "entity_type": "document",
                            "canonical_url": "https://example.com/gap",
                            "source_type": "web_document",
                            "evidence_lane": "production-reliability",
                            "authority_tier": "A1",
                            "refresh_class": "frozen",
                            "topics": ["context-routing"],
                        },
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_ranks_uncovered_topics_and_writes_inspectable_artifacts(self):
        result = run_v0(
            seed_path=self.seed,
            corpus_root=self.corpus_root,
            state_root=self.state_root,
            output_root=self.output_root,
            cycle_id="2026-07-16-personal-v0",
            queue_top=1,
        )

        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["ranked_candidates"][0]["seed_id"], "gap")
        self.assertEqual(result["ranked_candidates"][0]["topic_page_hits"]["context-routing"], 0)
        self.assertEqual(result["queued_work_count"], 1)
        self.assertIn("context-routing", result["doctrine_proposals"])
        self.assertTrue((self.output_root / "latest.json").exists())
        self.assertTrue((self.output_root / "latest.md").exists())
        self.assertEqual(self.registry.read_bytes(), self.registry_before)

    def test_same_cycle_is_physically_idempotent(self):
        first = run_v0(
            seed_path=self.seed,
            corpus_root=self.corpus_root,
            state_root=self.state_root,
            output_root=self.output_root,
            cycle_id="2026-07-16-personal-v0",
            queue_top=2,
        )
        ledger = self.state_root / "discovery-ledger.jsonl"
        first_ledger = ledger.read_bytes()
        second = run_v0(
            seed_path=self.seed,
            corpus_root=self.corpus_root,
            state_root=self.state_root,
            output_root=self.output_root,
            cycle_id="2026-07-16-personal-v0",
            queue_top=2,
        )
        self.assertEqual(ledger.read_bytes(), first_ledger)
        self.assertEqual(first["queued_work_ids"], second["queued_work_ids"])
        self.assertEqual(first["ranked_candidates"], second["ranked_candidates"])

    def test_dry_run_writes_nothing(self):
        result = run_v0(
            seed_path=self.seed,
            corpus_root=self.corpus_root,
            state_root=self.state_root,
            output_root=self.output_root,
            cycle_id="dry",
            queue_top=1,
            dry_run=True,
        )
        self.assertEqual(result["candidate_count"], 2)
        self.assertFalse(self.state_root.exists())
        self.assertFalse(self.output_root.exists())
        self.assertEqual(self.registry.read_bytes(), self.registry_before)


if __name__ == "__main__":
    unittest.main()
