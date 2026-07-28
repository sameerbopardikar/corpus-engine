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

from corpus_recursive_discovery_eval import RecursiveProofError, run_recursive_proof
from corpus_source_graph import SourceRelationship


class RecursiveDiscoveryEvalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proof"

    def relation(
        self,
        *,
        family: str,
        relation: str,
        entity_type: str,
        candidate: str,
        source: str,
        evidence: str,
        lane: str,
        title: str,
        observed: str,
    ) -> dict:
        return SourceRelationship(
            domain="agentic-engineering",
            source_family=family,
            relationship_type=relation,
            entity_type=entity_type,
            canonical_url=candidate,
            title=title,
            discovered_from_url=source,
            evidence_pointer=evidence,
            evidence_lane=lane,
            topics=("reliability", "tool-use", "verification"),
            observed_at=observed,
        ).to_dict() | {}

    def config(self):
        candidate_a = "https://example.org/people/maya-chen"
        artifact_a = "https://example.com/podcast/seed-12"
        artifact_b = "https://example.org/events/reliable-agents"
        cycle_b_artifact = "https://example.org/people/maya-chen/notes/recovery-controller"
        return {
            "schema_version": 1,
            "domain": "agentic-engineering",
            "evaluated_at": "2026-07-27T22:00:00Z",
            "seed_packet": {
                "registry": ["https://example.com/podcast/seed"],
                "ontology": ["tool use", "reliability", "verification"],
                "queries": ["agent reliability", "tool verification"],
                "worker_prompt": "Identify important recurring mechanisms and cite the supplied artifacts.",
            },
            "hidden_evaluator": {
                "target_label": "outcome-unknown recovery",
                "forbidden_seed_terms": ["outcome-unknown", "ambiguous commit protocol"],
                "required_mechanisms": ["external side effects", "unresolved", "readback"],
            },
            "cycle_a": {
                "relationships": [
                    self.relation(
                        family="podcast", relation="guest_of", entity_type="creator",
                        candidate=candidate_a, source=artifact_a, evidence=artifact_a + "?span=1",
                        lane="practitioner-implementation", title="Maya Chen",
                        observed="2026-07-20T12:00:00Z",
                    ),
                    self.relation(
                        family="conference", relation="speaker_at", entity_type="creator",
                        candidate=candidate_a, source=artifact_b, evidence=artifact_b + "?talk=4",
                        lane="production-reliability", title="Maya Chen",
                        observed="2026-07-21T12:00:00Z",
                    ),
                ]
            },
            "cycle_b": {
                "batches": [
                    {
                        "watch_source_url": candidate_a,
                        "artifact_url": cycle_b_artifact,
                        "relationships": [
                            self.relation(
                                family="engineering-blog", relation="linked_primary_source",
                                entity_type="repository",
                                candidate="https://github.com/example/recovery-controller",
                                source=cycle_b_artifact, evidence=cycle_b_artifact + "?section=implementation",
                                lane="production-reliability", title="Recovery Controller",
                                observed="2026-07-25T12:00:00Z",
                            )
                        ],
                    }
                ]
            },
            "worker_output": {
                "concept_candidates": [
                    {
                        "statement": "External side effects remain unresolved until a deterministic readback confirms the resulting state.",
                        "evidence_pointers": [cycle_b_artifact],
                    }
                ]
            },
        }

    def test_two_cycle_replay_promotes_unseeded_source_and_yields_second_order_candidate(self):
        receipt = run_recursive_proof(self.config(), run_root=self.root)
        self.assertTrue(all(receipt["gates"].values()))
        self.assertEqual(
            receipt["cycle_a"]["promoted_watch_urls"],
            ["https://example.org/people/maya-chen"],
        )
        self.assertEqual(
            receipt["cycle_b"]["second_order_candidate_urls"],
            ["https://github.com/example/recovery-controller"],
        )
        self.assertEqual(receipt["unknown_concept"]["exact_target_label_guessed"], False)
        self.assertTrue(receipt["unknown_concept"]["mechanism_cluster_matched"])

    def test_target_or_forbidden_term_in_seed_packet_fails_leakage_gate(self):
        config = self.config()
        config["seed_packet"]["queries"].append("outcome-unknown recovery")
        with self.assertRaisesRegex(RecursiveProofError, "target leakage"):
            run_recursive_proof(config, run_root=self.root)

    def test_cycle_b_batch_from_unpromoted_source_is_not_processed(self):
        config = self.config()
        config["cycle_b"]["batches"][0]["watch_source_url"] = "https://example.org/people/not-promoted"
        receipt = run_recursive_proof(config, run_root=self.root)
        self.assertFalse(receipt["gates"]["recursive_expansion"])
        self.assertEqual(receipt["cycle_b"]["processed_batches"], 0)
        self.assertEqual(receipt["cycle_b"]["second_order_candidate_urls"], [])

    def test_concept_evidence_must_come_from_processed_cycle_b_artifact(self):
        config = self.config()
        config["worker_output"]["concept_candidates"][0]["evidence_pointers"] = [
            "https://example.org/unavailable-future-source"
        ]
        receipt = run_recursive_proof(config, run_root=self.root)
        self.assertFalse(receipt["gates"]["unknown_concept"])
        self.assertFalse(receipt["unknown_concept"]["evidence_grounded"])

    def test_clean_replay_is_byte_idempotent(self):
        first = run_recursive_proof(self.config(), run_root=self.root)
        receipt_path = self.root / "receipt.json"
        first_bytes = receipt_path.read_bytes()
        second = run_recursive_proof(self.config(), run_root=self.root)
        self.assertEqual(second, first)
        self.assertEqual(receipt_path.read_bytes(), first_bytes)


if __name__ == "__main__":
    unittest.main()
