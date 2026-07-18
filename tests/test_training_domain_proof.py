from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import corpus_domain_runner as runner_mod
import corpus_domain_spec as spec_mod
import corpus_seed_loader as seed_loader

SPEC_PATH = REPO_ROOT / "config" / "domains" / "training.json"
SEED_PATH = REPO_ROOT / "docs" / "source-maps" / "training-seed-candidates.json"
FIXTURE_CORPUS = REPO_ROOT / "tests" / "fixtures" / "training-corpus"
NOW = "2026-07-18T12:00:00Z"

# Sameer's named training scope that the spec must cover.
NAMED_SCOPE = {
    "hypertrophy", "strength", "power", "speed", "endurance",
    "vertical-jump", "explosiveness", "fascia-training", "vo2-max", "cardio",
}
REQUIRED_AXES = {
    "mechanical-force-velocity", "adaptation", "method",
    "performance-expression", "constraints",
}


class TrainingDomainSpecProofTests(unittest.TestCase):
    def test_training_spec_loads_and_covers_named_scope(self):
        spec = spec_mod.load_domain_spec(SPEC_PATH)
        self.assertEqual(spec.domain, "training")
        self.assertEqual({axis.key for axis in spec.ontology_axes}, REQUIRED_AXES)
        all_topics = set(spec.axis_topics)
        missing = NAMED_SCOPE - all_topics
        self.assertEqual(missing, set(), f"training spec is missing named-scope topics: {missing}")
        self.assertFalse(spec.promotion["automatic_promotion_enabled"])

    def test_training_seed_loads_as_metadata_not_promoted(self):
        bundle = seed_loader.load_candidate_seed(SEED_PATH)
        self.assertEqual(bundle.domain, "training")
        self.assertEqual(bundle.status, "candidate-seed-not-promoted")
        self.assertGreaterEqual(len(bundle.candidates), 8)

    def test_spec_seed_ref_resolves_to_real_seed(self):
        spec = spec_mod.load_domain_spec(SPEC_PATH)
        self.assertTrue((REPO_ROOT / spec.seed_ref).exists())
        self.assertEqual((REPO_ROOT / spec.seed_ref).resolve(), SEED_PATH.resolve())


class TrainingDeterministicCycleProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = Path(self.tmp.name)

    def _run(self):
        spec = spec_mod.load_domain_spec(SPEC_PATH)
        return runner_mod.run_domain_cycle(
            spec=spec,
            seed_path=SEED_PATH,
            corpus_root=FIXTURE_CORPUS,
            state_root=self.workdir / "state",
            output_root=self.workdir / "output",
            cycle_id="2026-07-18-training-proof",
            now=NOW,
        )

    def test_bounded_cycle_receipt_has_doctrine_change_and_next_gap(self):
        receipt = self._run()
        self.assertEqual(receipt["domain"], "training")
        self.assertTrue(receipt["validate"]["ok"])
        self.assertEqual(receipt["doctrine"]["decision"], "proposed")
        self.assertEqual(receipt["doctrine"]["concept_key"], "mechanical-force-velocity")
        self.assertFalse(receipt["doctrine"]["sameer_adopted"])
        self.assertEqual(receipt["doctrine"]["epistemic_layer"], "external_corpus_synthesis")
        self.assertIsNotNone(receipt["next_gap"])
        self.assertTrue(receipt["evaluate"]["passed"])
        self.assertFalse(receipt["promotion_enabled"])
        self.assertFalse(receipt["production_mutation"])

    def test_cycle_is_deterministic_across_runs(self):
        first = self._run()
        second = self._run()
        self.assertEqual(first["receipt_sha256"], second["receipt_sha256"])

    def test_proof_does_not_write_into_repo_fixture(self):
        before = sorted(p.name for p in FIXTURE_CORPUS.rglob("*"))
        self._run()
        after = sorted(p.name for p in FIXTURE_CORPUS.rglob("*"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
