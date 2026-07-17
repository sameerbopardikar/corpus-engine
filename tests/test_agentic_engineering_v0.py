import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentic_engineering_v0 import DEFAULT_STATE_ROOT, run_v0
from corpus_discovery import DiscoveryEngine
from corpus_engine_models import CandidateObservation


class AgenticEngineeringV0Tests(unittest.TestCase):
    def test_default_state_uses_proof_enforced_v1_namespace(self):
        self.assertEqual(
            DEFAULT_STATE_ROOT,
            Path("/root/exports/thinker-corpora/agentic-engineering/self-expansion-v1"),
        )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.corpus_root = self.root / "corpus"
        self.state_root = self.root / "state"
        self.output_root = self.corpus_root / "discovery" / "personal-v0"
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

    def test_acquired_source_card_reconciles_dashboard_rights_and_status(self):
        source = self.corpus_root / "sources" / "gap.md"
        source.parent.mkdir()
        source.write_text(
            "---\n"
            "source_url: https://example.com/gap\n"
            "source_revision: abc123\n"
            "rights_state: public_rights_clear\n"
            "candidate_status: probationary\n"
            "---\n\nAcquired evidence.\n",
            encoding="utf-8",
        )
        result = run_v0(
            seed_path=self.seed,
            corpus_root=self.corpus_root,
            state_root=self.state_root,
            output_root=self.output_root,
            cycle_id="acquired",
            queue_top=0,
        )
        gap = next(item for item in result["ranked_candidates"] if item["seed_id"] == "gap")
        self.assertTrue(gap["acquired"])
        self.assertEqual(gap["rights_state"], "public_rights_clear")
        self.assertEqual(gap["status"], "probationary")
        self.assertEqual(gap["source_revision"], "abc123")
        self.assertEqual(gap["source_page"], "sources/gap.md")
        dashboard = (self.output_root / "latest.md").read_text(encoding="utf-8")
        self.assertIn("Acquisition: `probationary`; rights: `public_rights_clear`; revision: `abc123`", dashboard)

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

    def test_foreign_ledger_candidate_does_not_crash_or_enter_seed_ranking(self):
        engine = DiscoveryEngine(self.state_root / "discovery-ledger.jsonl")
        engine.observe(
            CandidateObservation.create(
                domain="agentic-engineering",
                entity_type="document",
                canonical_url="https://example.com/foreign",
                discovery_source="external:test",
                evidence_pointer="https://example.com/evidence",
                evidence_lane="production-reliability",
                topics=("foreign-topic",),
                observed_at="2026-07-16T00:00:00Z",
            )
        )

        result = run_v0(
            seed_path=self.seed,
            corpus_root=self.corpus_root,
            state_root=self.state_root,
            output_root=self.output_root,
            cycle_id="2026-07-16-personal-v0",
            queue_top=2,
        )

        self.assertEqual(result["candidate_count"], 2)
        self.assertNotIn(
            "https://example.com/foreign",
            [candidate["canonical_url"] for candidate in result["ranked_candidates"]],
        )
        self.assertTrue((self.output_root / "latest.json").exists())


if __name__ == "__main__":
    unittest.main()
