from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_feedback import (
    FeedbackConflictError,
    FeedbackLedger,
    FeedbackProfileError,
    load_feedback_profile,
    parse_feedback_profile,
    profile_binding,
    rank_candidates,
    shared_outcome_projection,
)

CONFIG_PROFILES = ROOT / "config" / "profiles"


def _profile(profile_id: str, version: int, weights: dict[str, float]):
    return parse_feedback_profile(
        {
            "schema_version": 1,
            "profile_id": profile_id,
            "version": version,
            "domain": "training",
            "title": profile_id,
            "target_decisions": ["acquisition_ranking"],
            "score_dimensions": weights,
            "applicability": {"evidence_lanes_allow": [], "evidence_lanes_deny": [], "rights_allow": []},
            "outcome_metrics": [
                {"key": "corpus_recall", "direction": "maximize", "scope": "shared"},
                {"key": "n_of_1_response", "direction": "maximize", "scope": "private"},
            ],
            "time_horizon_days": 90,
            "confounders": ["training-age", "nutrition"],
            "privacy": {"export_scope": "shared", "forbid_private_outcome_export": True},
            "min_evidence": {"min_corroborations": 1, "min_distinct_sources": 1},
            "regression_thresholds": {"max_outcome_regression": 0.1},
        }
    )


CANDIDATES = [
    {"candidate_id": "c-scientific", "dimensions": {"scientific": 1.0, "practitioner": 0.0}},
    {"candidate_id": "c-practitioner", "dimensions": {"scientific": 0.0, "practitioner": 1.0}},
]


class ProfileRankingTests(unittest.TestCase):
    def test_same_candidates_rank_differently_under_two_profiles(self):
        science = _profile("science-first", 1, {"scientific": 1.0, "practitioner": 0.1})
        practice = _profile("practice-first", 1, {"scientific": 0.1, "practitioner": 1.0})
        top_science = rank_candidates(science, CANDIDATES)[0]["candidate_id"]
        top_practice = rank_candidates(practice, CANDIDATES)[0]["candidate_id"]
        self.assertEqual(top_science, "c-scientific")
        self.assertEqual(top_practice, "c-practitioner")

    def test_every_ranked_item_binds_profile_id_version_and_digest(self):
        science = _profile("science-first", 3, {"scientific": 1.0})
        binding = profile_binding(science)
        for item in rank_candidates(science, CANDIDATES):
            self.assertEqual(item["profile_id"], binding["profile_id"])
            self.assertEqual(item["profile_version"], binding["profile_version"])
            self.assertEqual(item["profile_digest"], binding["profile_digest"])


class ProfileVersioningTests(unittest.TestCase):
    def test_revised_profile_has_new_digest_and_old_receipt_stays_bound(self):
        v1 = _profile("science-first", 1, {"scientific": 1.0})
        receipt = {"decision": "acquire", "binding": profile_binding(v1)}
        v2 = _profile("science-first", 2, {"scientific": 1.0, "practitioner": 0.5})
        self.assertNotEqual(profile_binding(v1)["profile_digest"], profile_binding(v2)["profile_digest"])
        # A historical receipt remains bound to the version-1 digest.
        self.assertEqual(receipt["binding"]["profile_version"], 1)
        self.assertEqual(receipt["binding"]["profile_digest"], profile_binding(v1)["profile_digest"])


class FeedbackLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "feedback.jsonl"
        self.profile = _profile("science-first", 1, {"scientific": 1.0})

    def _event(self, event_id="fb-1", outcome=0.4, extra=None):
        payload = {
            "event_id": event_id,
            "target_decision": "acquisition_ranking",
            "candidate_id": "c-scientific",
            "outcome_metric": "corpus_recall",
            "outcome_value": outcome,
            "provenance": {"source": "scientific-eval", "recorded_at": "2026-07-18T00:00:00Z"},
        }
        if extra:
            payload.update(extra)
        return payload

    def test_duplicate_event_converges(self):
        ledger = FeedbackLedger(self.path)
        ledger.record(self.profile, self._event())
        ledger.record(self.profile, self._event())
        self.assertEqual(len(ledger.events), 1)

    def test_conflicting_event_id_fails_closed(self):
        ledger = FeedbackLedger(self.path)
        ledger.record(self.profile, self._event(outcome=0.4))
        with self.assertRaises(FeedbackConflictError):
            ledger.record(self.profile, self._event(outcome=0.9))

    def test_recorded_event_binds_profile(self):
        ledger = FeedbackLedger(self.path)
        stored = ledger.record(self.profile, self._event())
        self.assertEqual(stored["profile_id"], "science-first")
        self.assertEqual(stored["profile_version"], 1)
        self.assertEqual(stored["profile_digest"], profile_binding(self.profile)["profile_digest"])

    def test_feedback_cannot_alter_rights_or_promote_doctrine(self):
        ledger = FeedbackLedger(self.path)
        with self.assertRaises(FeedbackProfileError):
            ledger.record(self.profile, self._event(extra={"rights_state": "public_rights_clear"}))
        with self.assertRaises(FeedbackProfileError):
            ledger.record(self.profile, self._event(event_id="fb-2", extra={"sameer_adopted": True}))

    def test_ledger_replays_across_instances(self):
        FeedbackLedger(self.path).record(self.profile, self._event())
        reopened = FeedbackLedger(self.path)
        self.assertEqual(len(reopened.events), 1)


class PrivacyTests(unittest.TestCase):
    def test_shared_projection_excludes_private_outcomes(self):
        profile = _profile("science-first", 1, {"scientific": 1.0})
        outcomes = {"corpus_recall": 0.7, "n_of_1_response": 0.9}
        projected = shared_outcome_projection(profile, outcomes)
        self.assertIn("corpus_recall", projected)
        self.assertNotIn("n_of_1_response", projected)


class CheckedInProfileTests(unittest.TestCase):
    def test_default_profiles_load_and_distinguish_scientific_from_private(self):
        training = load_feedback_profile(CONFIG_PROFILES / "training-default.json")
        self.assertEqual(training.domain, "training")
        scopes = {metric["key"]: metric["scope"] for metric in training.outcome_metrics}
        # Training must separate scientific/corpus evaluation from private N-of-1.
        self.assertTrue(any(scope == "shared" for scope in scopes.values()))
        self.assertTrue(any(scope == "private" for scope in scopes.values()))
        agentic = load_feedback_profile(CONFIG_PROFILES / "agentic-engineering-default.json")
        self.assertEqual(agentic.domain, "agentic-engineering")


if __name__ == "__main__":
    unittest.main()
