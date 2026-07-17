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

from corpus_eval import EvaluationError, evaluate_corpus


class CorpusEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.raw_a = self.root / "raw-a.bin"
        self.raw_b = self.root / "raw-b.bin"
        self.norm_a = self.root / "norm-a.md"
        self.norm_b = self.root / "norm-b.md"
        self.raw_a.write_bytes(b"paper source bytes")
        self.raw_b.write_bytes(b"production source bytes")
        self.norm_a.write_text("Independent verification improves reliability.\n", encoding="utf-8")
        self.norm_b.write_text("Self-checking is sufficient for low-impact transforms.\n", encoding="utf-8")
        self.records = [
            self.record(
                "paper-a", "agentic-engineering/sources/paper-a", self.raw_a, self.norm_a,
                lane="scientific-evaluation", claim=b"independent-verification", stance="supports",
            ),
            self.record(
                "incident-b", "agentic-engineering/sources/incident-b", self.raw_b, self.norm_b,
                lane="production-reliability", claim=b"bounded-self-check", stance="opposes",
            ),
        ]
        citations = [self.citation(self.records[0]), self.citation(self.records[1])]
        self.doctrine = {
            "schema_version": 1,
            "concepts": [{
                "schema_version": 1,
                "concept_key": "verification-boundary",
                "title": "Verification Boundary",
                "statement": "Independent verification is required for high-impact work; bounded self-checking may cover low-impact transforms.",
                "version": 2,
                "status": "bounded",
                "aliases": ["verification-loops"],
                "citations": citations,
                "epistemic_layer": "external_corpus_synthesis",
                "sameer_adopted": False,
                "created_at": "2026-07-15T00:00:00Z",
                "updated_at": "2026-07-16T00:00:00Z",
            }],
            "aliases": {"verification-loops": "verification-boundary"},
            "lineage_edges": [{
                "from": "verification-loops", "to": "verification-boundary",
                "relation": "superseded_by", "recorded_at": "2026-07-16T00:00:00Z",
            }],
        }
        self.lineages = {
            "verification-loops": {
                "requested_key": "verification-loops",
                "current_key": "verification-boundary",
                "versions": [
                    {"concept_key": "verification-loops", "version": 1},
                    {"concept_key": "verification-boundary", "version": 2},
                ],
                "descendants": ["verification-boundary"],
                "edges": copy.deepcopy(self.doctrine["lineage_edges"]),
            }
        }
        self.retrieval = [{
            "case_id": "retrieval-verification",
            "query": "how should agents verify work",
            "expected_page_slugs": ["agentic-engineering/sources/paper-a"],
            "results": [
                {"source_id": "corpora", "page_slug": "agentic-engineering/sources/paper-a"},
                {"source_id": "corpora", "page_slug": "agentic-engineering/sources/incident-b"},
            ],
        }]
        self.usage = {
            "deep_acquisitions": 2,
            "llm_tasks": 0,
            "llm_tokens": 0,
            "estimated_cost_usd": 0.0,
            "actual_cost_usd": 0.0,
        }
        self.policy = {
            "max_deep_acquisitions_per_utc_day": 3,
            "max_llm_tasks_per_utc_day": 1,
            "max_llm_tokens_per_utc_day": 50000,
            "max_cost_usd_per_utc_day": 1.0,
            "max_freshness_days": 30,
            "min_evidence_lanes": 2,
        }

    @staticmethod
    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def record(self, record_id, slug, raw, normalized, *, lane, claim, stance):
        return {
            "record_id": record_id,
            "source_id": "corpora",
            "page_slug": slug,
            "locator": "section-1",
            "claim_sha256": hashlib.sha256(claim).hexdigest(),
            "raw_pointer": str(raw),
            "raw_sha256": self.sha(raw),
            "normalized_pointer": str(normalized),
            "normalized_sha256": self.sha(normalized),
            "canonical_url": f"https://example.com/{record_id}",
            "source_revision": "rev-1",
            "retrieved_at": "2026-07-16T00:00:00Z",
            "evidence_lane": lane,
            "evidence_class": "scientific" if lane.startswith("scientific") else "production-incident",
            "epistemic_layer": "primary_source",
            "rights_state": "public_rights_clear",
            "candidate_status": "probationary",
            "contradiction_group": "verification-scope",
            "contradiction_stance": stance,
        }

    @staticmethod
    def citation(record):
        return {
            "source_id": record["source_id"],
            "page_slug": record["page_slug"],
            "locator": record["locator"],
            "claim_sha256": record["claim_sha256"],
            "evidence_class": record["evidence_class"],
        }

    def run_eval(self, **overrides):
        values = {
            "records": self.records,
            "doctrine_snapshot": self.doctrine,
            "doctrine_lineages": self.lineages,
            "retrieval_cases": self.retrieval,
            "usage": self.usage,
            "policy": self.policy,
            "evaluated_at": "2026-07-17T00:00:00Z",
        }
        values.update(overrides)
        return evaluate_corpus(**values)

    def test_green_report_is_deterministic_under_reordered_inputs_and_uses_no_llm(self):
        first = self.run_eval()
        second = self.run_eval(records=list(reversed(self.records)))
        self.assertEqual(first, second)
        self.assertTrue(first["passed"])
        self.assertEqual(first["llm_calls"], 0)
        self.assertEqual(first["metrics"]["integrity"]["valid_records"], 2)
        self.assertEqual(first["metrics"]["retrieval"]["recall"], 1.0)
        self.assertEqual(first["metrics"]["material_yield"]["new_material_records"], 2)
        self.assertEqual(len(first["report_sha256"]), 64)

    def test_raw_or_normalized_pointer_hash_tamper_fails_integrity(self):
        self.raw_a.write_bytes(b"tampered")
        report = self.run_eval()
        self.assertFalse(report["passed"])
        self.assertIn("raw_hash_mismatch:paper-a", report["failures"])
        self.norm_b.unlink()
        report = self.run_eval()
        self.assertIn("normalized_pointer_missing:incident-b", report["failures"])

    def test_citation_must_resolve_exact_source_slug_locator_claim_and_class(self):
        doctrine = copy.deepcopy(self.doctrine)
        doctrine["concepts"][0]["citations"][0]["claim_sha256"] = "f" * 64
        report = self.run_eval(doctrine_snapshot=doctrine)
        self.assertIn("citation_unresolved:verification-boundary", report["failures"])
        doctrine = copy.deepcopy(self.doctrine)
        doctrine["concepts"][0]["citations"][0]["source_id"] = "default"
        report = self.run_eval(doctrine_snapshot=doctrine)
        self.assertIn("citation_not_corpora:verification-boundary", report["failures"])

    def test_attribution_layers_cannot_promote_external_evidence_to_sameer_belief(self):
        records = copy.deepcopy(self.records)
        records[0]["epistemic_layer"] = "sameer_integration"
        doctrine = copy.deepcopy(self.doctrine)
        doctrine["concepts"][0]["sameer_adopted"] = True
        report = self.run_eval(records=records, doctrine_snapshot=doctrine)
        self.assertIn("invalid_evidence_attribution:paper-a", report["failures"])
        self.assertIn("sameer_adoption_forbidden:verification-boundary", report["failures"])

    def test_opposing_evidence_requires_a_bounded_cited_doctrine_concept(self):
        doctrine = copy.deepcopy(self.doctrine)
        doctrine["concepts"][0]["status"] = "probationary"
        report = self.run_eval(doctrine_snapshot=doctrine)
        self.assertIn("contradiction_not_surfaced:verification-scope", report["failures"])
        doctrine["concepts"][0]["status"] = "bounded"
        doctrine["concepts"][0]["citations"] = [doctrine["concepts"][0]["citations"][0]]
        report = self.run_eval(doctrine_snapshot=doctrine)
        self.assertIn("contradiction_not_surfaced:verification-scope", report["failures"])

    def test_freshness_duplicate_rate_lane_diversity_and_prior_delta_are_explicit(self):
        previous = self.run_eval(records=[self.records[0]], doctrine_snapshot={**self.doctrine, "concepts": []}, doctrine_lineages={}, policy={**self.policy, "min_evidence_lanes": 1})
        report = self.run_eval(previous_report=previous)
        self.assertEqual(report["comparison"]["new_record_ids"], ["incident-b"])
        self.assertEqual(report["comparison"]["new_normalized_sha256"], [self.records[1]["normalized_sha256"]])
        stale = copy.deepcopy(self.records)
        stale[0]["retrieved_at"] = "2025-01-01T00:00:00Z"
        duplicate = copy.deepcopy(stale[0])
        duplicate["record_id"] = "paper-a-copy"
        report = self.run_eval(records=[*stale, duplicate])
        self.assertIn("stale_record:paper-a", report["failures"])
        self.assertIn("duplicate_material:paper-a-copy", report["failures"])

    def test_source_scoped_retrieval_must_recover_an_expected_page(self):
        retrieval = copy.deepcopy(self.retrieval)
        retrieval[0]["results"][0]["source_id"] = "default"
        retrieval[0]["results"][1]["page_slug"] = "unrelated"
        report = self.run_eval(retrieval_cases=retrieval)
        self.assertIn("retrieval_source_scope:retrieval-verification", report["failures"])
        self.assertIn("retrieval_miss:retrieval-verification", report["failures"])

    def test_lineage_aliases_edges_and_versions_must_reconstruct_current_concepts(self):
        doctrine = copy.deepcopy(self.doctrine)
        doctrine["aliases"]["dangling"] = "missing-concept"
        lineages = copy.deepcopy(self.lineages)
        lineages["verification-loops"]["versions"] = []
        report = self.run_eval(doctrine_snapshot=doctrine, doctrine_lineages=lineages)
        self.assertIn("dangling_alias:dangling", report["failures"])
        self.assertIn("lineage_versions_missing:verification-loops", report["failures"])

    def test_cost_caps_and_false_promotion_fail_closed(self):
        records = copy.deepcopy(self.records)
        records[0]["candidate_status"] = "promoted"
        records[0]["rights_state"] = "rights_unclear"
        usage = {**self.usage, "deep_acquisitions": 4, "llm_tasks": 2, "llm_tokens": 60000, "estimated_cost_usd": 1.01}
        report = self.run_eval(records=records, usage=usage)
        self.assertIn("false_promotion:paper-a", report["failures"])
        self.assertIn("deep_acquisition_cap_exceeded", report["failures"])
        self.assertIn("llm_task_cap_exceeded", report["failures"])
        self.assertIn("llm_token_cap_exceeded", report["failures"])
        self.assertIn("cost_cap_exceeded", report["failures"])

    def test_x_signal_can_never_be_doctrine_eligible_or_promoted(self):
        records = copy.deepcopy(self.records)
        records[0]["evidence_lane"] = "unverified-discovery-signal"
        records[0]["candidate_status"] = "promoted"
        report = self.run_eval(records=records)
        self.assertIn("false_promotion:paper-a", report["failures"])

    def test_unknown_fields_nonfinite_cost_and_mutated_previous_report_fail_closed(self):
        records = copy.deepcopy(self.records)
        records[0]["authority_override"] = True
        with self.assertRaisesRegex(EvaluationError, "record fields"):
            self.run_eval(records=records)
        with self.assertRaisesRegex(EvaluationError, "finite"):
            self.run_eval(usage={**self.usage, "actual_cost_usd": float("nan")})
        previous = self.run_eval()
        forged = copy.deepcopy(previous)
        forged["metrics"]["integrity"]["valid_records"] = 999
        with self.assertRaisesRegex(EvaluationError, "previous report digest"):
            self.run_eval(previous_report=forged)


if __name__ == "__main__":
    unittest.main()
