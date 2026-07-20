"""Exact projection-contract test for an external (e.g. JS) Atlas reader.

A separate reader must be able to consume the acquisition projection WITHOUT
guessing its shape. This test pins the exact top-level contract and the exact
card schema, exercises one queued + one human-gated + one completed candidate,
and checks the committed golden fixture (``tests/fixtures/acquisition-projection
-contract.json``) matches the live builder byte-for-byte. If the contract
changes on purpose, regenerate the fixture with ``REWRITE_CONTRACT_FIXTURE=1``.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_acquisition_executor import AcquisitionCandidate, AcquisitionExecutor, FetchResult
from corpus_acquisition_projection import _CARD_KEYS, build_acquisition_projection
from corpus_engine_models import CandidateObservation
from corpus_rights_resolver import RightsEvidence

FIXTURE = ROOT / "tests" / "fixtures" / "acquisition-projection-contract.json"

# The exact top-level keys an external reader may rely on.
EXPECTED_TOP_LEVEL_KEYS = {
    "schema_version", "domain", "cycle_id", "generated_at", "corpus_revision",
    "cycle_time_seconds", "next_best", "coverage_pressure", "ranked_candidates",
    "human_gates", "queued_candidates", "in_flight", "recent_receipts", "totals",
}
EXPECTED_TOTALS_KEYS = {
    "discovered", "rights_resolved", "acquired", "metadata_only", "gated",
    "failed", "queued",
}


def _candidate(canonical, rights, key, topics=("hypertrophy",)):
    obs = CandidateObservation.create(
        domain="training", entity_type="paper", canonical_url=canonical,
        discovery_source="scholarly_discovery:openalex:" + key, evidence_pointer=canonical,
        evidence_lane="primary-study", topics=topics, observed_at="2026-07-19T00:00:00Z",
    )
    return AcquisitionCandidate(
        candidate_id=obs.candidate_key, domain="training", canonical_locator=canonical,
        content_locator=canonical, evidence_lane="primary-study",
        source_family="peer-reviewed-literature", title="Study " + key, topics=obs.topics,
        rights_evidence=rights, metadata={"openalex_id": key}, idempotency_key="cyc:" + key,
    )


def _build_projection(tmp_root):
    executor = AcquisitionExecutor(
        raw_root=tmp_root / "archive", staging_root=tmp_root / "staging",
        receipts_root=tmp_root / "receipts", quarantine_root=tmp_root / "quarantine",
        http_fetch=lambda url, *, timeout, max_bytes=None: FetchResult(
            body=b"<html><body><p>hypertrophy adaptation strength evidence text long enough</p></body></html>",
            final_url=url, content_type="text/html"),
        now=lambda: "2026-07-19T01:00:00Z",
    )
    completed = executor.acquire(
        _candidate("https://example.org/oa", RightsEvidence(license="cc-by", access_class="open_content"), "W1")
    )
    gated = executor.acquire(
        _candidate("https://example.org/paid", RightsEvidence(access_class="paid"), "W2")
    )
    queued = {
        "candidate_id": "cand-queued-W3",
        "domain": "training",
        "title": "Queued open study",
        "topics": ["velocity", "hypertrophy"],
        "canonical_locator": "https://example.org/queued",
        "evidence_lane": "primary-study",
        "source_family": "peer-reviewed-literature",
        "rights_state": "public_rights_clear",
        "rights_basis": "open_access_license:cc-by",
        "disposition": "deferred",
        "status": "queued",
        "why_now": "budget-capped this cycle",
        "gap_served": "velocity-training",
        "estimated_cost_usd": 0.4,
        "priority_score": 9.0,
    }
    return build_acquisition_projection(
        receipts=[completed, gated], ranked_candidates={}, pending=[queued],
        corpus_revision="rev-contract", generated_at="2026-07-19T01:05:00Z",
        cycle_time_seconds=2.5, domain="training", cycle_id="global-2026-07-19-contract",
    )


class ProjectionContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.projection = _build_projection(Path(self._tmp.name))

    def test_top_level_keys_are_exact(self):
        self.assertEqual(set(self.projection), EXPECTED_TOP_LEVEL_KEYS)
        self.assertEqual(set(self.projection["totals"]), EXPECTED_TOTALS_KEYS)

    def test_one_of_each_lifecycle_state_is_represented(self):
        # Exactly one completed (probationary), one human-gated, one queued.
        completed = [c for c in self.projection["recent_receipts"] if c["disposition"] == "probationary"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(len(self.projection["human_gates"]), 1)
        self.assertEqual(self.projection["human_gates"][0]["disposition"], "human_gate")
        self.assertEqual(len(self.projection["queued_candidates"]), 1)
        self.assertEqual(self.projection["queued_candidates"][0]["status"], "queued")
        # Next best is actionable-only: the queued candidate, never the completed.
        self.assertEqual(self.projection["next_best"]["status"], "queued")

    def test_every_card_has_the_exact_card_schema(self):
        cards = (
            self.projection["ranked_candidates"]
            + self.projection["recent_receipts"]
            + self.projection["queued_candidates"]
            + ([self.projection["next_best"]] if self.projection["next_best"] else [])
        )
        for card in cards:
            self.assertEqual(set(card), set(_CARD_KEYS), card.get("candidate_id"))

    def test_projection_never_leaks_private_paths(self):
        blob = json.dumps(self.projection)
        self.assertNotIn(str(self._tmp.name), blob)
        self.assertNotIn("pointer", blob)
        self.assertNotIn("staging", blob)

    def test_matches_committed_golden_fixture(self):
        if os.environ.get("REWRITE_CONTRACT_FIXTURE") == "1":
            FIXTURE.parent.mkdir(parents=True, exist_ok=True)
            FIXTURE.write_text(json.dumps(self.projection, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.assertTrue(FIXTURE.is_file(), "golden fixture is missing; regenerate with REWRITE_CONTRACT_FIXTURE=1")
        golden = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(self.projection, golden)


if __name__ == "__main__":
    unittest.main()
