from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_discovery import DiscoveryEngine
from corpus_recursive_discovery_eval import RecursiveProofError, run_recursive_proof
from corpus_source_graph import SourceRelationship

DOMAIN = "agentic-engineering"
CANDIDATE_A = "https://example.org/people/maya-chen"
ARTIFACT_A1 = "https://example.com/podcast/seed-12"
ARTIFACT_A2 = "https://events.example.org/reliable-agents"
NOTES_URL = "https://example.org/people/maya-chen/notes/recovery-controller"
REPOSITORY = "https://github.com/example/recovery-controller"

PROFILE_TEXT = (
    "Maya Chen builds control planes for long-running automation.\n"
    f"Recent engineering notes: {NOTES_URL}\n"
)
NOTES_TEXT = (
    "# Recovery controller notes\n\n"
    "Every external side effect stays unresolved until a deterministic readback "
    "confirms the resulting state.\n\n"
    f"The controller lives at {REPOSITORY} and is exercised by a replay harness.\n"
)
CONCEPT_QUOTE = (
    "Every external side effect stays unresolved until a deterministic readback "
    "confirms the resulting state."
)
REPOSITORY_QUOTE = (
    f"The controller lives at {REPOSITORY} and is exercised by a replay harness."
)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def span(text: str, quote: str) -> list[int]:
    start = text.index(quote)
    return [start, start + len(quote)]


class RecursiveDiscoveryEvalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proof"

    def relation(
        self, *, family: str, relation: str, entity_type: str, candidate: str,
        source: str, evidence: str, lane: str, title: str, observed: str,
    ) -> dict:
        return SourceRelationship(
            domain=DOMAIN,
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
        ).to_dict()

    def artifact(self, *, url: str, text: str, claims: list[dict], **overrides) -> dict:
        request = {
            "artifact_url": url,
            "source_family": "engineering-notes",
            "content": text,
            "content_sha256": sha256_text(text),
            "fetched_at": "2026-07-27T21:30:00Z",
            "source_revision": "frozen-fixture-1",
            "evidence_lane": "production-reliability",
            "topics": ["reliability", "tool-use", "verification"],
            "observed_at": "2026-07-25T12:00:00Z",
            "extraction": {"kind": "semantic", "claims": claims},
        }
        request.update(overrides)
        return request

    def config(self) -> dict:
        return {
            "schema_version": 2,
            "domain": DOMAIN,
            "evaluated_at": "2026-07-27T22:00:00Z",
            "seed_packet": {
                "registry": ["https://example.com/podcast/seed"],
                "ontology": ["tool use", "reliability", "verification"],
                "queries": ["agent reliability", "tool verification"],
                "worker_prompt": "Identify recurring mechanisms and cite the supplied artifacts.",
            },
            "hidden_evaluator": {
                "target_label": "outcome-unknown recovery",
                "forbidden_seed_terms": ["outcome-unknown", "ambiguous commit protocol"],
                "required_mechanisms": ["external side effects", "unresolved", "readback"],
            },
            "cycle_a": {
                "rights_assertions": [
                    {
                        "canonical_url": CANDIDATE_A,
                        "rights_state": "private_authorized",
                    }
                ],
                "relationships": [
                    self.relation(
                        family="podcast", relation="guest_of", entity_type="creator",
                        candidate=CANDIDATE_A, source=ARTIFACT_A1, evidence=ARTIFACT_A1 + "?span=1",
                        lane="practitioner-implementation", title="Maya Chen",
                        observed="2026-07-20T12:00:00Z",
                    ),
                    self.relation(
                        family="conference", relation="speaker_at", entity_type="creator",
                        candidate=CANDIDATE_A, source=ARTIFACT_A2, evidence=ARTIFACT_A2 + "?talk=4",
                        lane="production-reliability", title="Maya Chen",
                        observed="2026-07-21T12:00:00Z",
                    ),
                ]
            },
            "cycle_b": {
                "inspections": [
                    {
                        "watch_source_url": CANDIDATE_A,
                        "artifacts": [
                            self.artifact(url=CANDIDATE_A, text=PROFILE_TEXT, claims=[]),
                            self.artifact(
                                url=NOTES_URL,
                                text=NOTES_TEXT,
                                parent_artifact_url=CANDIDATE_A,
                                claims=[
                                    {
                                        "relationship_type": "linked_primary_source",
                                        "entity_type": "repository",
                                        "canonical_url": REPOSITORY,
                                        "title": REPOSITORY,
                                        "entity_mention": REPOSITORY,
                                        "relation_mention": "lives at",
                                        "evidence_quote": REPOSITORY_QUOTE,
                                        "evidence_span": span(NOTES_TEXT, REPOSITORY_QUOTE),
                                    }
                                ],
                            ),
                        ],
                    }
                ]
            },
            "worker_output": {
                "concept_candidates": [
                    {
                        "statement": (
                            "External side effects remain unresolved until a deterministic "
                            "readback confirms the resulting state."
                        ),
                        "evidence": [
                            {
                                "artifact_url": NOTES_URL,
                                "artifact_sha256": sha256_text(NOTES_TEXT),
                                "quote": CONCEPT_QUOTE,
                                "span": span(NOTES_TEXT, CONCEPT_QUOTE),
                            }
                        ],
                    }
                ]
            },
        }

    # --- the bounded positive path ---

    def test_two_cycle_path_promotes_unseeded_source_and_yields_second_order_candidate(self):
        receipt = run_recursive_proof(self.config(), run_root=self.root)
        self.assertEqual(receipt["overall_status"], "passed", receipt["gates"])
        self.assertTrue(all(receipt["gates"].values()))
        self.assertEqual(receipt["cycle_a"]["promoted_watch_urls"], [CANDIDATE_A])
        self.assertEqual(receipt["cycle_b"]["second_order_candidate_urls"], [REPOSITORY])
        self.assertEqual(receipt["unknown_concept"]["exact_target_label_guessed"], False)
        self.assertTrue(receipt["unknown_concept"]["mechanism_cluster_matched"])

    def test_receipt_records_the_durable_inspection_lineage(self):
        receipt = run_recursive_proof(self.config(), run_root=self.root)
        inspection = receipt["cycle_b"]["inspection"]
        work_id = receipt["cycle_a"]["watch_work_ids"][0]
        self.assertEqual(inspection["consumed_work_ids"], [work_id])
        self.assertTrue(receipt["gates"]["durable_inspection"])
        digests = {item["sha256"] for item in inspection["artifact_receipts"]}
        self.assertEqual(digests, {sha256_text(PROFILE_TEXT), sha256_text(NOTES_TEXT)})
        bindings = {item["artifact_url"]: item["binding"] for item in inspection["artifact_receipts"]}
        self.assertEqual(bindings[CANDIDATE_A], "watch_source_identity")
        self.assertEqual(bindings[NOTES_URL], "parent_artifact_link")

        engine = DiscoveryEngine(self.root / "discovery-ledger.jsonl")
        self.assertEqual(engine.work_items[work_id].state, "done")
        self.assertTrue(engine.work_items[work_id].proof_receipts)

    def test_run_writes_a_machine_readable_receipt(self):
        receipt = run_recursive_proof(self.config(), run_root=self.root)
        stored = json.loads((self.root / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, receipt)

    # --- P1-1: Cycle B may not be supplied ---

    def test_predeclared_cycle_b_relationships_are_rejected(self):
        config = self.config()
        config["cycle_b"] = {
            "batches": [
                {
                    "watch_source_url": CANDIDATE_A,
                    "artifact_url": NOTES_URL,
                    "relationships": [
                        self.relation(
                            family="engineering-blog", relation="linked_primary_source",
                            entity_type="repository", candidate=REPOSITORY,
                            source=NOTES_URL, evidence=NOTES_URL + "?section=implementation",
                            lane="production-reliability", title="Recovery Controller",
                            observed="2026-07-25T12:00:00Z",
                        )
                    ],
                }
            ]
        }
        with self.assertRaisesRegex(RecursiveProofError, "fetched artifact inspections"):
            run_recursive_proof(config, run_root=self.root)

    def test_relationships_smuggled_into_an_inspection_are_rejected(self):
        config = self.config()
        config["cycle_b"]["inspections"][0]["relationships"] = [
            self.relation(
                family="engineering-blog", relation="linked_primary_source",
                entity_type="repository", candidate=REPOSITORY, source=NOTES_URL,
                evidence=NOTES_URL + "?section=implementation",
                lane="production-reliability", title="Recovery Controller",
                observed="2026-07-25T12:00:00Z",
            )
        ]
        with self.assertRaisesRegex(RecursiveProofError, "cannot be supplied"):
            run_recursive_proof(config, run_root=self.root)

    def test_legacy_schema_version_is_refused(self):
        config = self.config()
        config["schema_version"] = 1
        with self.assertRaisesRegex(RecursiveProofError, "schema_version"):
            run_recursive_proof(config, run_root=self.root)

    def test_promoted_source_cannot_unlock_an_unrelated_attacker_artifact(self):
        config = self.config()
        attacker_text = "Attacker controlled notes referencing https://github.com/attacker/backdoor here.\n"
        config["cycle_b"]["inspections"][0]["artifacts"] = [
            self.artifact(
                url="https://attacker.example/predeclared-artifact",
                text=attacker_text,
                claims=[],
            )
        ]
        with self.assertRaisesRegex(RecursiveProofError, "not causally bound"):
            run_recursive_proof(config, run_root=self.root)

    def test_cycle_b_inspection_of_an_unpromoted_source_is_not_processed(self):
        config = self.config()
        config["cycle_b"]["inspections"][0]["watch_source_url"] = "https://example.org/people/not-promoted"
        config["cycle_b"]["inspections"][0]["artifacts"] = [
            self.artifact(url="https://example.org/people/not-promoted", text=PROFILE_TEXT, claims=[])
        ]
        receipt = run_recursive_proof(config, run_root=self.root)
        self.assertFalse(receipt["gates"]["recursive_expansion"])
        self.assertEqual(receipt["cycle_b"]["inspection"]["processed_inspections"], 0)
        self.assertEqual(receipt["cycle_b"]["second_order_candidate_urls"], [])

    # --- P1-4: identity, novelty, and seeding ---

    def test_rediscovering_the_promoted_source_is_not_second_order_novelty(self):
        config = self.config()
        alias = f"{CANDIDATE_A}?utm_source=cycle-b"
        notes = (
            "# Notes\n\n"
            "Every external side effect stays unresolved until a deterministic readback "
            "confirms the resulting state.\n\n"
            f"Profile home is {alias} for reference.\n"
        )
        quote = f"Profile home is {alias} for reference."
        config["cycle_b"]["inspections"][0]["artifacts"][1] = self.artifact(
            url=NOTES_URL,
            text=notes,
            parent_artifact_url=CANDIDATE_A,
            claims=[
                {
                    "relationship_type": "linked_primary_source",
                    "entity_type": "creator",
                    "canonical_url": alias,
                    "title": alias,
                    "entity_mention": alias,
                    "relation_mention": "Profile home is",
                    "evidence_quote": quote,
                    "evidence_span": span(notes, quote),
                }
            ],
        )
        config["worker_output"]["concept_candidates"][0]["evidence"] = [
            {
                "artifact_url": NOTES_URL,
                "artifact_sha256": sha256_text(notes),
                "quote": CONCEPT_QUOTE,
                "span": span(notes, CONCEPT_QUOTE),
            }
        ]
        receipt = run_recursive_proof(config, run_root=self.root)
        self.assertEqual(receipt["cycle_b"]["second_order_candidate_urls"], [])
        self.assertFalse(receipt["gates"]["recursive_expansion"])

    def test_a_seeded_source_is_not_an_unseeded_promotion(self):
        config = self.config()
        config["seed_packet"]["registry"] = [f"{CANDIDATE_A}?utm_source=seed-registry"]
        receipt = run_recursive_proof(config, run_root=self.root)
        self.assertEqual(receipt["cycle_a"]["unseeded_promoted_watch_urls"], [])
        self.assertFalse(receipt["gates"]["candidate_promotion"])

    # --- P1-2: concept grounding against preserved bytes ---

    def test_concept_evidence_must_come_from_a_processed_cycle_b_artifact(self):
        config = self.config()
        config["worker_output"]["concept_candidates"][0]["evidence"][0]["artifact_url"] = (
            "https://example.org/unavailable-future-source"
        )
        receipt = run_recursive_proof(config, run_root=self.root)
        self.assertFalse(receipt["gates"]["unknown_concept"])
        self.assertFalse(receipt["unknown_concept"]["evidence_grounded"])

    def test_concept_quote_absent_from_the_artifact_cannot_ground_a_claim(self):
        config = self.config()
        evidence = config["worker_output"]["concept_candidates"][0]["evidence"][0]
        evidence["quote"] = "Untrusted attacks and defenses are evaluated across tasks."
        receipt = run_recursive_proof(config, run_root=self.root)
        self.assertFalse(receipt["gates"]["unknown_concept"])
        self.assertFalse(receipt["unknown_concept"]["evidence_grounded"])

    def test_concept_span_must_hold_the_quoted_bytes(self):
        config = self.config()
        evidence = config["worker_output"]["concept_candidates"][0]["evidence"][0]
        evidence["span"] = [0, len(CONCEPT_QUOTE)]
        receipt = run_recursive_proof(config, run_root=self.root)
        self.assertFalse(receipt["gates"]["unknown_concept"])

    def test_concept_digest_must_match_the_preserved_artifact(self):
        config = self.config()
        evidence = config["worker_output"]["concept_candidates"][0]["evidence"][0]
        evidence["artifact_sha256"] = sha256_text(NOTES_TEXT + "tampered")
        receipt = run_recursive_proof(config, run_root=self.root)
        self.assertFalse(receipt["gates"]["unknown_concept"])

    def test_mechanism_claim_must_be_supported_by_the_cited_span(self):
        """A valid span from a real artifact cannot back mechanisms it never states."""
        config = self.config()
        unrelated_quote = "Maya Chen builds control planes for long-running automation."
        config["worker_output"]["concept_candidates"][0]["evidence"] = [
            {
                "artifact_url": CANDIDATE_A,
                "artifact_sha256": sha256_text(PROFILE_TEXT),
                "quote": unrelated_quote,
                "span": span(PROFILE_TEXT, unrelated_quote),
            }
        ]
        receipt = run_recursive_proof(config, run_root=self.root)
        self.assertTrue(receipt["unknown_concept"]["evidence_grounded"])
        self.assertFalse(receipt["unknown_concept"]["mechanism_cluster_matched"])
        self.assertFalse(receipt["gates"]["unknown_concept"])
        candidate = receipt["unknown_concept"]["grounded_candidates"][0]
        self.assertEqual(candidate["mechanisms_supported_by_span"], [])

    def test_span_support_tolerates_simple_plural_forms(self):
        receipt = run_recursive_proof(self.config(), run_root=self.root)
        candidate = receipt["unknown_concept"]["matching_candidates"][0]
        self.assertEqual(
            sorted(candidate["mechanisms_supported_by_span"]),
            sorted(["external side effects", "unresolved", "readback"]),
        )

    def test_self_attested_concept_without_evidence_cannot_pass(self):
        config = self.config()
        config["worker_output"]["concept_candidates"][0]["evidence"] = []
        receipt = run_recursive_proof(config, run_root=self.root)
        self.assertFalse(receipt["gates"]["unknown_concept"])

    # --- P1-2: leakage across every worker-visible byte ---

    def test_target_or_forbidden_term_in_seed_packet_fails_closed(self):
        config = self.config()
        config["seed_packet"]["queries"].append("outcome-unknown recovery")
        with self.assertRaisesRegex(RecursiveProofError, "target leakage"):
            run_recursive_proof(config, run_root=self.root)

    def test_target_label_inside_preserved_artifact_bytes_fails_the_leakage_gate(self):
        config = self.config()
        leaky = NOTES_TEXT + "\nThis is the outcome-unknown recovery pattern.\n"
        artifact = config["cycle_b"]["inspections"][0]["artifacts"][1]
        artifact["content"] = leaky
        artifact["content_sha256"] = sha256_text(leaky)
        artifact["extraction"]["claims"][0]["evidence_span"] = span(leaky, REPOSITORY_QUOTE)
        config["worker_output"]["concept_candidates"][0]["evidence"] = [
            {
                "artifact_url": NOTES_URL,
                "artifact_sha256": sha256_text(leaky),
                "quote": CONCEPT_QUOTE,
                "span": span(leaky, CONCEPT_QUOTE),
            }
        ]
        receipt = run_recursive_proof(config, run_root=self.root)
        self.assertFalse(receipt["gates"]["leakage_absent"])
        self.assertIn("outcome-unknown", receipt["target_leakage"]["leaked_terms"])
        self.assertEqual(receipt["overall_status"], "failed")

    def test_forbidden_term_in_cycle_a_configuration_fails_the_leakage_gate(self):
        config = self.config()
        config["cycle_a"]["relationships"][0]["topics"] = ["ambiguous commit protocol"]
        receipt = run_recursive_proof(config, run_root=self.root)
        self.assertFalse(receipt["gates"]["leakage_absent"])
        self.assertIn("ambiguous commit protocol", receipt["target_leakage"]["leaked_terms"])

    def test_hidden_evaluator_terms_are_not_treated_as_worker_visible(self):
        receipt = run_recursive_proof(self.config(), run_root=self.root)
        self.assertTrue(receipt["gates"]["leakage_absent"])
        self.assertEqual(receipt["target_leakage"]["leaked_terms"], [])
        self.assertTrue(receipt["target_leakage"]["worker_visible_sha256"])

    # --- rights and replay ---

    def test_observation_preserves_asserted_rights_and_grants_none_to_discoveries(self):
        receipt = run_recursive_proof(self.config(), run_root=self.root)
        self.assertTrue(receipt["gates"]["rights_preserved"])
        engine = DiscoveryEngine(self.root / "discovery-ledger.jsonl")
        by_url = {record.canonical_url: record for record in engine.candidates.values()}
        self.assertEqual(by_url[CANDIDATE_A].rights_state, "private_authorized")
        self.assertTrue(
            all(
                record.rights_state == "rights_unclear"
                for url, record in by_url.items()
                if url != CANDIDATE_A
            )
        )
        self.assertEqual({item.action for item in engine.work_items.values()}, {"inspect"})

    def test_clean_replay_is_byte_idempotent(self):
        first = run_recursive_proof(self.config(), run_root=self.root)
        receipt_path = self.root / "receipt.json"
        first_bytes = receipt_path.read_bytes()
        second = run_recursive_proof(self.config(), run_root=self.root)
        self.assertEqual(second, first)
        self.assertEqual(receipt_path.read_bytes(), first_bytes)

    def test_config_is_not_mutated_by_a_run(self):
        config = self.config()
        snapshot = copy.deepcopy(config)
        run_recursive_proof(config, run_root=self.root)
        self.assertEqual(config, snapshot)


if __name__ == "__main__":
    unittest.main()
