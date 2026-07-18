from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_discovery import DiscoveryEngine
from corpus_feedback import parse_feedback_profile, profile_binding
from corpus_suggestion_bridge import (
    SuggestionBridgeError,
    bridge_acquisition_suggestion,
)

PROFILE = parse_feedback_profile(
    {
        "schema_version": 1, "profile_id": "training-default", "version": 1, "domain": "training",
        "title": "t", "target_decisions": ["acquisition_ranking"],
        "score_dimensions": {"evidence_strength": 1.0},
        "applicability": {"evidence_lanes_allow": [], "evidence_lanes_deny": [], "rights_allow": []},
        "outcome_metrics": [{"key": "corpus_recall", "direction": "maximize", "scope": "shared"}],
        "time_horizon_days": 90, "confounders": [],
        "privacy": {"export_scope": "shared", "forbid_private_outcome_export": True},
        "min_evidence": {"min_corroborations": 1, "min_distinct_sources": 1},
        "regression_thresholds": {"max_outcome_regression": 0.1},
    }
)


def _suggestion(**overrides):
    base = {
        "family": "peer-reviewed-literature",
        "locator": "https://pubmed.ncbi.nlm.nih.gov/20847704/",
        "title": "Hypertrophy mechanisms",
        "confidence": 0.8,
        "rationale": "Systematic review",
        "rights_hint": "public_rights_clear",
        "rank_score": 0.9,
        "requires_human_gate": False,
        "shared_corpus_eligible": True,
        "provenance": {"source": "scout", "recorded_at": "2026-07-18T00:00:00Z"},
    }
    base.update(overrides)
    return base


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.discovery = DiscoveryEngine(Path(self.tempdir.name) / "ledger.jsonl")

    def _bridge(self, suggestion, discovery_source="scout-a"):
        return bridge_acquisition_suggestion(
            self.discovery, suggestion, domain="training", profile=PROFILE,
            entity_type="paper", evidence_lane="scientific-evaluation",
            discovery_source=discovery_source, observed_at="2026-07-18T00:00:00Z",
        )

    def test_valid_suggestion_creates_discovery_only_candidate(self):
        receipt = self._bridge(_suggestion())
        self.assertEqual(receipt["status"], "observed")
        self.assertIn(receipt["candidate_id"], self.discovery.candidates)
        self.assertEqual(receipt["binding"], profile_binding(PROFILE))
        self.assertEqual(receipt["domain"], "training")

    def test_suggestion_never_upgrades_rights(self):
        receipt = self._bridge(_suggestion(rights_hint="public_rights_clear"))
        record = self.discovery.candidates[receipt["candidate_id"]]
        # Discovery candidate stays unclassified; a suggestion never sets rights.
        self.assertEqual(record.rights_state, "unknown")

    def test_duplicate_origins_converge_but_retain_receipts(self):
        r1 = self._bridge(_suggestion(), discovery_source="scout-a")
        r2 = self._bridge(_suggestion(), discovery_source="scout-b")
        self.assertEqual(r1["candidate_id"], r2["candidate_id"])
        self.assertEqual(len(self.discovery.candidates), 1)
        self.assertNotEqual(r1["origin"], r2["origin"])

    def test_human_gated_suggestion_is_non_automatic(self):
        receipt = self._bridge(_suggestion(requires_human_gate=True))
        self.assertIn(receipt["candidate_id"], self.discovery.candidates)  # still discovered
        self.assertFalse(receipt["auto_actionable"])
        self.assertEqual(receipt["queued_work_ids"], [])

    def test_private_or_unclear_suggestion_is_non_automatic(self):
        for hint in ("private_authorized", "rights_unclear"):
            receipt = self._bridge(_suggestion(rights_hint=hint, shared_corpus_eligible=False, locator=f"https://example.org/{hint}"))
            self.assertFalse(receipt["auto_actionable"])
            self.assertEqual(receipt["queued_work_ids"], [])

    def test_clear_public_suggestion_is_auto_actionable(self):
        receipt = self._bridge(_suggestion())
        self.assertTrue(receipt["auto_actionable"])
        self.assertEqual(len(receipt["queued_work_ids"]), 1)

    def test_missing_profile_fails_closed(self):
        with self.assertRaises(SuggestionBridgeError):
            bridge_acquisition_suggestion(
                self.discovery, _suggestion(), domain="training", profile=None,
                entity_type="paper", evidence_lane="scientific-evaluation",
                discovery_source="scout-a",
            )

    def test_domain_mismatch_with_profile_fails_closed(self):
        with self.assertRaises(SuggestionBridgeError):
            bridge_acquisition_suggestion(
                self.discovery, _suggestion(), domain="agentic-engineering", profile=PROFILE,
                entity_type="paper", evidence_lane="scientific-evaluation",
                discovery_source="scout-a",
            )


if __name__ == "__main__":
    unittest.main()
