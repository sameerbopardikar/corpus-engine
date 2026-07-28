from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_candidate_policy import CandidatePolicy, evaluate_candidates
from corpus_discovery import DiscoveryEngine
from corpus_engine_models import CandidateObservation
from corpus_source_graph import SourceRelationship, ingest_relationships


class CandidatePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.graph = self.root / "source-graph.jsonl"
        self.ledger = self.root / "discovery-ledger.jsonl"
        self.watch = self.root / "watch-projection.json"
        self.evaluated_at = "2026-07-27T20:00:00Z"

    def relation(
        self,
        *,
        family: str,
        source: str,
        evidence: str,
        lane: str = "practitioner-implementation",
        candidate: str = "https://github.com/example/recovery-controller",
    ) -> SourceRelationship:
        return SourceRelationship(
            domain="agentic-engineering",
            source_family=family,
            relationship_type="linked_primary_source",
            entity_type="repository",
            canonical_url=candidate,
            title=candidate,
            discovered_from_url=source,
            evidence_pointer=evidence,
            evidence_lane=lane,
            topics=("recovery", "verification", "tool-use"),
            observed_at="2026-07-27T12:00:00Z",
        )

    def test_corroborated_candidate_promotes_and_enters_shadow_watch_projection(self):
        ingest_relationships(
            [
                self.relation(
                    family="podcast",
                    source="https://example.com/podcast/12",
                    evidence="https://example.com/podcast/12?span=1",
                ),
                self.relation(
                    family="paper",
                    source="https://arxiv.org/abs/2607.12345",
                    evidence="https://arxiv.org/abs/2607.12345?section=4",
                    lane="scientific",
                ),
            ],
            graph_path=self.graph,
            discovery_ledger_path=self.ledger,
        )
        result = evaluate_candidates(
            ledger_path=self.ledger,
            graph_path=self.graph,
            watch_projection_path=self.watch,
            evaluated_at=self.evaluated_at,
        )
        engine = DiscoveryEngine(self.ledger)
        candidate = next(iter(engine.candidates.values()))
        self.assertEqual(candidate.status, "promoted")
        self.assertEqual(result["counts"]["promoted"], 1)
        self.assertIn(candidate.canonical_url, self.watch.read_text())

    def test_single_viral_x_mention_does_not_promote(self):
        ingest_relationships(
            [
                self.relation(
                    family="x",
                    source="https://x.com/noisy/status/1",
                    evidence="https://x.com/noisy/status/1",
                    lane="unverified-discovery-signal",
                )
            ],
            graph_path=self.graph,
            discovery_ledger_path=self.ledger,
        )
        result = evaluate_candidates(
            ledger_path=self.ledger,
            graph_path=self.graph,
            watch_projection_path=self.watch,
            evaluated_at=self.evaluated_at,
        )
        candidate = next(iter(DiscoveryEngine(self.ledger).candidates.values()))
        self.assertNotEqual(candidate.status, "promoted")
        self.assertEqual(result["counts"]["promoted"], 0)

    def test_same_source_family_duplicates_do_not_count_as_independent_corroboration(self):
        ingest_relationships(
            [
                self.relation(
                    family="x",
                    source="https://x.com/builder/status/1",
                    evidence="https://x.com/builder/status/1",
                ),
                self.relation(
                    family="x",
                    source="https://x.com/builder/status/2",
                    evidence="https://x.com/builder/status/2",
                ),
            ],
            graph_path=self.graph,
            discovery_ledger_path=self.ledger,
        )
        result = evaluate_candidates(
            ledger_path=self.ledger,
            graph_path=self.graph,
            watch_projection_path=self.watch,
            evaluated_at=self.evaluated_at,
        )
        disposition = result["dispositions"][0]
        self.assertEqual(disposition["independent_source_families"], 1)
        self.assertNotEqual(disposition["status_after"], "promoted")

    def test_rejected_candidate_retains_explicit_reason(self):
        engine = DiscoveryEngine(self.ledger)
        first = CandidateObservation.create(
            domain="agentic-engineering",
            entity_type="other",
            canonical_url="https://example.com/noise",
            discovery_source="discovery-feed:one",
            evidence_pointer="https://example.com/noise?one",
            evidence_lane="zeitgeist",
            topics=(),
            observed_at="2026-07-27T12:00:00Z",
        )
        low_scores = {
            "authority": 0.1,
            "demonstrated_practice": 0.1,
            "novelty": 0.1,
            "relevance": 0.1,
            "corroboration": 0.1,
            "production_or_scientific_value": 0.1,
            "cost": 1.0,
        }
        engine.observe(first, score_components=low_scores, rights_state="rights_unclear")
        engine.observe(
            CandidateObservation.create(
                domain=first.domain,
                entity_type=first.entity_type,
                canonical_url=first.canonical_url,
                discovery_source="discovery-feed:two",
                evidence_pointer="https://example.com/noise?two",
                evidence_lane="zeitgeist",
                topics=(),
                observed_at="2026-07-27T13:00:00Z",
            ),
            rights_state="rights_unclear",
        )
        result = evaluate_candidates(
            ledger_path=self.ledger,
            graph_path=self.graph,
            watch_projection_path=self.watch,
            evaluated_at=self.evaluated_at,
        )
        candidate = next(iter(DiscoveryEngine(self.ledger).candidates.values()))
        self.assertEqual(candidate.status, "rejected")
        self.assertIsNotNone(candidate.rejection_reason)
        assert candidate.rejection_reason is not None
        self.assertIn("quality", candidate.rejection_reason)
        self.assertEqual(result["counts"]["rejected"], 1)

    def test_replay_is_idempotent_and_watch_bytes_are_stable(self):
        ingest_relationships(
            [
                self.relation(
                    family="podcast",
                    source="https://example.com/podcast/12",
                    evidence="https://example.com/podcast/12?span=1",
                ),
                self.relation(
                    family="paper",
                    source="https://arxiv.org/abs/2607.12345",
                    evidence="https://arxiv.org/abs/2607.12345?section=4",
                    lane="scientific",
                ),
            ],
            graph_path=self.graph,
            discovery_ledger_path=self.ledger,
        )
        first = evaluate_candidates(
            ledger_path=self.ledger,
            graph_path=self.graph,
            watch_projection_path=self.watch,
            evaluated_at=self.evaluated_at,
        )
        ledger_bytes = self.ledger.read_bytes()
        watch_bytes = self.watch.read_bytes()
        second = evaluate_candidates(
            ledger_path=self.ledger,
            graph_path=self.graph,
            watch_projection_path=self.watch,
            evaluated_at=self.evaluated_at,
        )
        self.assertEqual(second, first)
        self.assertEqual(self.ledger.read_bytes(), ledger_bytes)
        self.assertEqual(self.watch.read_bytes(), watch_bytes)


if __name__ == "__main__":
    unittest.main()
