from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_behavior_eval import load_behavior_suite
from corpus_domain_spec import bootstrap_domain, load_domain_spec
from corpus_feedback import load_feedback_profile
from corpus_seed_loader import load_candidate_seed

SPEC = ROOT / "config" / "domains" / "training.json"
SEED = ROOT / "docs" / "source-maps" / "training-seed-candidates.json"
PROFILE = ROOT / "config" / "profiles" / "training-default.json"
RETRIEVAL = ROOT / "evals" / "training" / "retrieval-suite.jsonl"
BEHAVIOR = ROOT / "evals" / "training" / "behavior-suite.jsonl"

# The full training evidence hierarchy that must be represented without flattening.
REQUIRED_LANES = {
    "systematic-review", "primary-study", "position-stand", "foundational-text",
    "institutional-source", "practitioner-doctrine", "experimental-doctrine",
    "mechanism", "testimonial", "corpus-synthesis", "private-local-proof",
}


class TrainingRunnerReadyConfigTests(unittest.TestCase):
    def test_all_artifacts_load_from_checked_in_files(self):
        spec = load_domain_spec(SPEC)
        self.assertEqual(spec.domain, "training")
        bundle = load_candidate_seed(SEED)
        self.assertEqual(bundle.domain, "training")
        self.assertGreaterEqual(len(bundle.candidates), 8)
        profile = load_feedback_profile(PROFILE)
        self.assertEqual(profile.domain, "training")

    def test_spec_is_multi_axis(self):
        spec = load_domain_spec(SPEC)
        axis_keys = {axis.key for axis in spec.ontology_axes}
        self.assertEqual(
            axis_keys,
            {"mechanical-force-velocity", "adaptation", "method", "performance-expression", "constraints"},
        )

    def test_evidence_lanes_represent_full_hierarchy_without_flattening(self):
        spec = load_domain_spec(SPEC)
        self.assertTrue(REQUIRED_LANES <= set(spec.evidence_lanes), REQUIRED_LANES - set(spec.evidence_lanes))
        # Distinct weights: the hierarchy is graded, not collapsed to one value.
        distinct = {spec.evidence_lanes[lane] for lane in REQUIRED_LANES}
        self.assertGreaterEqual(len(distinct), 8)


class HyperarchPrivacyTests(unittest.TestCase):
    def test_hyperarch_family_is_pointer_only_and_non_exportable(self):
        spec = load_domain_spec(SPEC)
        families = {f.family: f for f in spec.source_families}
        self.assertIn("hyperarch-private-membership", families)
        hyperarch = families["hyperarch-private-membership"]
        self.assertEqual(hyperarch.acquisition_mode, "pointer_only_non_exportable")
        self.assertFalse(hyperarch.shared_corpus_eligible)
        self.assertEqual(hyperarch.rights_state, "private_authorized")

    def test_no_private_family_is_shared_corpus_eligible(self):
        spec = load_domain_spec(SPEC)
        for family in spec.source_families:
            if family.rights_state != "public_rights_clear":
                self.assertFalse(family.shared_corpus_eligible, family.family)


class TrainingProfilePrivacyTests(unittest.TestCase):
    def test_profile_separates_scientific_from_private_n_of_1(self):
        profile = load_feedback_profile(PROFILE)
        shared = set(profile.shared_metric_keys)
        private = set(profile.private_metric_keys)
        self.assertIn("n_of_1_response", private)
        self.assertTrue(shared)  # scientific/corpus metrics are shared
        self.assertFalse(shared & private)


class TrainingEvalSuitesTests(unittest.TestCase):
    def test_retrieval_suite_loads_and_covers_key_topics(self):
        cases = [json.loads(line) for line in RETRIEVAL.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertGreaterEqual(len(cases), 6)
        for case in cases:
            self.assertEqual(set(case), {"case_id", "query", "expected_topics", "expected_page_slugs"})
            for slug in case["expected_page_slugs"]:
                self.assertTrue(slug.startswith("training/"))  # source-scoped to the training corpus
        covered = {topic for case in cases for topic in case["expected_topics"]}
        for essential in ("force-velocity", "hypertrophy", "vo2-max", "vertical-jump"):
            self.assertIn(essential, covered)

    def test_behavior_suite_loads_with_required_training_cases(self):
        suite = load_behavior_suite(BEHAVIOR)
        ids = {case.case_id for case in suite}
        self.assertIn("hyperarch-testimonial-not-efficacy", ids)
        self.assertIn("private-n-of-1-not-exported", ids)
        # The private N-of-1 case is marked private so its inputs never export.
        private_case = next(c for c in suite if c.case_id == "private-n-of-1-not-exported")
        self.assertTrue(private_case.private)


class TrainingBootstrapTests(unittest.TestCase):
    def test_training_bootstraps_dirs_only_no_source_or_scheduler(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            spec = load_domain_spec(SPEC)
            receipt = bootstrap_domain(spec, Path(td), now="2026-07-18T00:00:00Z")
            self.assertEqual(receipt["source_id"], "corpora")  # shared source, never per-domain
            self.assertFalse(receipt["gbrain_source_created"])
            self.assertFalse(receipt["scheduler_created"])


if __name__ == "__main__":
    unittest.main()
