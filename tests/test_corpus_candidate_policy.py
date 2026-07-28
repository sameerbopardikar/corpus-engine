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
        observed_at: str = "2026-07-27T12:00:00Z",
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
            observed_at=observed_at,
        )

    def promotable(self) -> list[SourceRelationship]:
        return [
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
        ]

    def evaluate(self, evaluated_at: str | None = None):
        return evaluate_candidates(
            ledger_path=self.ledger,
            graph_path=self.graph,
            watch_projection_path=self.watch,
            evaluated_at=self.evaluated_at if evaluated_at is None else evaluated_at,
        )

    def test_corroborated_candidate_promotes_and_enqueues_recurring_inspection(self):
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
        work = engine.select_work(action="inspect", limit=10)
        self.assertEqual(len(work), 1)
        self.assertEqual(work[0].candidate_id, candidate.candidate_id)
        self.assertEqual(result["watch_work_ids"], [work[0].work_id])

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

    # --- P1: forgeable corroboration, evaluation time, and durable admission ---

    def test_query_aliases_of_one_document_are_not_independent_corroboration(self):
        page = "https://api.github.com/search/repositories"
        ingest_relationships(
            [
                self.relation(
                    family="paper",
                    source=f"{page}?q=agent%20security%20benchmark",
                    evidence=f"{page}?q=agent%20security%20benchmark",
                    lane="scientific",
                ),
                self.relation(
                    family="podcast",
                    source=f"{page}?q=llm%20agent%20reliability",
                    evidence=f"{page}?q=llm%20agent%20reliability",
                ),
            ],
            graph_path=self.graph,
            discovery_ledger_path=self.ledger,
        )
        result = self.evaluate()
        disposition = result["dispositions"][0]
        self.assertEqual(disposition["independent_evidence_documents"], 1)
        self.assertEqual(disposition["independent_publishers"], 1)
        self.assertNotEqual(disposition["status_after"], "promoted")
        self.assertEqual(result["counts"]["promoted"], 0)

    def test_one_publisher_relabelled_as_two_families_does_not_promote(self):
        ingest_relationships(
            [
                self.relation(
                    family="paper",
                    source="https://example.com/blog/post-a",
                    evidence="https://example.com/blog/post-a",
                    lane="scientific",
                ),
                self.relation(
                    family="podcast",
                    source="https://example.com/blog/post-b",
                    evidence="https://example.com/blog/post-b",
                ),
            ],
            graph_path=self.graph,
            discovery_ledger_path=self.ledger,
        )
        result = self.evaluate()
        disposition = result["dispositions"][0]
        self.assertEqual(disposition["independent_source_families"], 2)
        self.assertEqual(disposition["independent_evidence_documents"], 2)
        self.assertEqual(disposition["independent_publishers"], 1)
        self.assertNotEqual(disposition["status_after"], "promoted")

    def test_observations_after_the_evaluation_cutoff_cannot_promote(self):
        ingest_relationships(
            [
                self.relation(
                    family="podcast",
                    source="https://example.com/podcast/12",
                    evidence="https://example.com/podcast/12?span=1",
                    observed_at="2035-01-01T00:00:00Z",
                ),
                self.relation(
                    family="paper",
                    source="https://arxiv.org/abs/2607.12345",
                    evidence="https://arxiv.org/abs/2607.12345?section=4",
                    lane="scientific",
                    observed_at="2035-01-02T00:00:00Z",
                ),
            ],
            graph_path=self.graph,
            discovery_ledger_path=self.ledger,
        )
        result = self.evaluate()
        disposition = result["dispositions"][0]
        self.assertEqual(disposition["deferred_future_observations"], 2)
        self.assertEqual(disposition["independent_evidence_documents"], 0)
        self.assertNotEqual(disposition["status_after"], "promoted")
        self.assertEqual(result["counts"]["promoted"], 0)
        self.assertEqual(result["watch_entries"], [])

    def test_future_evidence_promotes_only_once_the_cutoff_has_passed(self):
        ingest_relationships(self.promotable(), graph_path=self.graph, discovery_ledger_path=self.ledger)
        early = self.evaluate("2026-07-27T00:00:00Z")
        self.assertEqual(early["counts"]["promoted"], 0)
        later = self.evaluate("2026-07-27T20:00:00Z")
        self.assertEqual(later["counts"]["promoted"], 1)

    def test_evaluated_at_must_be_an_aware_timestamp(self):
        for value in ("not-a-timestamp", "2026-07-27T20:00:00", ""):
            with self.assertRaises(ValueError):
                self.evaluate(value)

    def test_promotion_enqueues_exactly_one_idempotent_daily_inspection(self):
        ingest_relationships(self.promotable(), graph_path=self.graph, discovery_ledger_path=self.ledger)
        first = self.evaluate("2026-07-27T20:00:00Z")
        engine = DiscoveryEngine(self.ledger)
        self.assertEqual(len(engine.work_items), 1)
        work_id = first["watch_work_ids"][0]

        ledger_bytes = self.ledger.read_bytes()
        same_day = self.evaluate("2026-07-27T23:59:59Z")
        self.assertEqual(same_day["watch_work_ids"], [work_id])
        self.assertEqual(len(DiscoveryEngine(self.ledger).work_items), 1)
        self.assertEqual(self.ledger.read_bytes(), ledger_bytes)

        next_day = self.evaluate("2026-07-28T00:00:01Z")
        engine = DiscoveryEngine(self.ledger)
        self.assertEqual(len(engine.work_items), 2)
        self.assertNotEqual(next_day["watch_work_ids"], [work_id])
        self.assertEqual(
            {item.action for item in engine.work_items.values()}, {"inspect"}
        )
        self.assertEqual(
            {item.candidate_id for item in engine.work_items.values()},
            {next(iter(engine.candidates))},
        )

    def test_watch_projection_publishes_the_durable_work_id(self):
        ingest_relationships(self.promotable(), graph_path=self.graph, discovery_ledger_path=self.ledger)
        result = self.evaluate()
        projection = json.loads(self.watch.read_text(encoding="utf-8"))
        entry = projection["entries"][0]
        self.assertEqual(entry["status"], "promoted")
        self.assertEqual(entry["work_id"], result["watch_work_ids"][0])
        self.assertEqual(entry["work_action"], "inspect")
        engine = DiscoveryEngine(self.ledger)
        self.assertIn(entry["work_id"], engine.work_items)
        self.assertEqual(engine.work_items[entry["work_id"]].candidate_id, entry["candidate_id"])

    def test_recurring_inspection_never_grants_material_rights(self):
        ingest_relationships(self.promotable(), graph_path=self.graph, discovery_ledger_path=self.ledger)
        self.evaluate()
        engine = DiscoveryEngine(self.ledger)
        self.assertTrue(
            all(record.rights_state == "rights_unclear" for record in engine.candidates.values())
        )
        self.assertTrue(all(item.action == "inspect" for item in engine.work_items.values()))


if __name__ == "__main__":
    unittest.main()
