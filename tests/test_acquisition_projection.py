import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_acquisition_executor import (
    AcquisitionCandidate,
    AcquisitionExecutor,
    FetchResult,
)
from corpus_acquisition_projection import build_acquisition_projection
from corpus_engine_models import CandidateObservation
from corpus_rights_resolver import RightsEvidence


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


class ProjectionHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.roots = root
        self.executor = AcquisitionExecutor(
            raw_root=root / "archive", staging_root=root / "staging",
            receipts_root=root / "receipts", quarantine_root=root / "quarantine",
            http_fetch=lambda url, *, timeout, max_bytes=None: FetchResult(
                body=b"<html><body><p>hypertrophy adaptation strength evidence text long enough</p></body></html>",
                final_url=url, content_type="text/html"),
            now=lambda: "2026-07-19T01:00:00Z",
        )

    def _receipts(self):
        acquired = self.executor.acquire(_candidate("https://example.org/oa", RightsEvidence(license="cc-by", access_class="open_content"), "W1"))
        gated = self.executor.acquire(_candidate("https://example.org/paid", RightsEvidence(access_class="paid"), "W2"))
        return [acquired, gated]


class ContractTests(ProjectionHarness):
    def test_projection_has_required_sections(self):
        projection = build_acquisition_projection(
            receipts=self._receipts(), ranked_candidates={}, corpus_revision="rev-abc",
            generated_at="2026-07-19T01:05:00Z", cycle_time_seconds=12.5,
        )
        for key in ("next_best", "coverage_pressure", "ranked_candidates", "human_gates",
                    "in_flight", "recent_receipts", "corpus_revision", "generated_at",
                    "cycle_time_seconds", "totals"):
            self.assertIn(key, projection)
        self.assertEqual(projection["corpus_revision"], "rev-abc")

    def test_projection_is_self_describing(self):
        # The Atlas must be able to read the projection without guessing: it must
        # carry its schema version, the domain it describes, and the cycle id.
        projection = build_acquisition_projection(
            receipts=self._receipts(), ranked_candidates={}, corpus_revision="rev-abc",
            generated_at="2026-07-19T01:05:00Z", cycle_time_seconds=12.5,
            domain="training", cycle_id="global-2026-07-19-xyz",
        )
        for key in ("schema_version", "domain", "cycle_id", "queued_candidates"):
            self.assertIn(key, projection)
        self.assertEqual(projection["domain"], "training")
        self.assertEqual(projection["cycle_id"], "global-2026-07-19-xyz")
        self.assertIsInstance(projection["schema_version"], int)

    def test_cards_include_public_topics(self):
        projection = build_acquisition_projection(
            receipts=self._receipts(), ranked_candidates={}, corpus_revision="rev",
            generated_at="2026-07-19T01:05:00Z", cycle_time_seconds=1.0,
        )
        card = next(c for c in projection["ranked_candidates"] if c["disposition"] == "probationary")
        # Public topics explain expected coverage impact to the Atlas.
        self.assertIn("topics", card)
        self.assertEqual(list(card["topics"]), ["hypertrophy"])

    def test_totals_count_dispositions(self):
        projection = build_acquisition_projection(
            receipts=self._receipts(), ranked_candidates={}, corpus_revision="rev",
            generated_at="2026-07-19T01:05:00Z", cycle_time_seconds=1.0,
        )
        self.assertEqual(projection["totals"]["acquired"], 1)
        self.assertEqual(projection["totals"]["gated"], 1)

    def test_human_gates_are_separated(self):
        projection = build_acquisition_projection(
            receipts=self._receipts(), ranked_candidates={}, corpus_revision="rev",
            generated_at="2026-07-19T01:05:00Z", cycle_time_seconds=1.0,
        )
        self.assertEqual(len(projection["human_gates"]), 1)
        self.assertEqual(projection["human_gates"][0]["disposition"], "human_gate")


class NextBestAndCompletenessTests(ProjectionHarness):
    def _pending(self, candidate_id="cand-queued", priority=9.0):
        return {
            "candidate_id": candidate_id,
            "domain": "training",
            "title": "Queued open study",
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
            "priority_score": priority,
        }

    def test_completed_probationary_is_not_next_best(self):
        # A single already-acquired card plus a queued candidate: next best must
        # be the queued work, never the completed probationary card.
        acquired = self.executor.acquire(
            _candidate("https://example.org/oa", RightsEvidence(license="cc-by", access_class="open_content"), "W1")
        )
        projection = build_acquisition_projection(
            receipts=[acquired], ranked_candidates={}, pending=[self._pending()],
            corpus_revision="rev", generated_at="2026-07-19T01:05:00Z", cycle_time_seconds=1.0,
        )
        self.assertIsNotNone(projection["next_best"])
        self.assertEqual(projection["next_best"]["candidate_id"], "cand-queued")
        self.assertNotEqual(projection["next_best"]["disposition"], "probationary")

    def test_next_best_is_null_when_only_completed_or_failed(self):
        acquired = self.executor.acquire(
            _candidate("https://example.org/oa", RightsEvidence(license="cc-by", access_class="open_content"), "W1")
        )
        projection = build_acquisition_projection(
            receipts=[acquired], ranked_candidates={}, pending=[],
            corpus_revision="rev", generated_at="2026-07-19T01:05:00Z", cycle_time_seconds=1.0,
        )
        self.assertIsNone(projection["next_best"])

    def test_human_gate_is_next_best_over_completed_when_no_queued(self):
        acquired = self.executor.acquire(
            _candidate("https://example.org/oa", RightsEvidence(license="cc-by", access_class="open_content"), "W1")
        )
        gated = self.executor.acquire(
            _candidate("https://example.org/paid", RightsEvidence(access_class="paid"), "W2")
        )
        projection = build_acquisition_projection(
            receipts=[acquired, gated], ranked_candidates={}, pending=[],
            corpus_revision="rev", generated_at="2026-07-19T01:05:00Z", cycle_time_seconds=1.0,
        )
        self.assertEqual(projection["next_best"]["disposition"], "human_gate")

    def test_queued_beats_gate_for_next_best(self):
        gated = self.executor.acquire(
            _candidate("https://example.org/paid", RightsEvidence(access_class="paid"), "W2")
        )
        projection = build_acquisition_projection(
            receipts=[gated], ranked_candidates={}, pending=[self._pending()],
            corpus_revision="rev", generated_at="2026-07-19T01:05:00Z", cycle_time_seconds=1.0,
        )
        self.assertEqual(projection["next_best"]["candidate_id"], "cand-queued")

    def test_pending_candidates_appear_in_ranked_view(self):
        projection = build_acquisition_projection(
            receipts=[], ranked_candidates={}, pending=[self._pending()],
            corpus_revision="rev", generated_at="2026-07-19T01:05:00Z", cycle_time_seconds=1.0,
        )
        ids = {c["candidate_id"] for c in projection["ranked_candidates"]}
        self.assertIn("cand-queued", ids)
        card = next(c for c in projection["ranked_candidates"] if c["candidate_id"] == "cand-queued")
        self.assertEqual(card["status"], "queued")
        self.assertEqual(card["why_now"], "budget-capped this cycle")
        self.assertEqual(card["gap_served"], "velocity-training")
        self.assertEqual(card["rights_state"], "public_rights_clear")
        self.assertEqual(projection["totals"]["queued"], 1)

    def test_pending_cards_never_leak_paths(self):
        projection = build_acquisition_projection(
            receipts=[], ranked_candidates={}, pending=[self._pending()],
            corpus_revision="rev", generated_at="2026-07-19T01:05:00Z", cycle_time_seconds=1.0,
        )
        blob = json.dumps(projection)
        self.assertNotIn("pointer", blob)
        self.assertNotIn(str(self.roots), blob)


class RedactionTests(ProjectionHarness):
    def test_projection_never_leaks_private_paths_or_credentials(self):
        projection = build_acquisition_projection(
            receipts=self._receipts(), ranked_candidates={}, corpus_revision="rev",
            generated_at="2026-07-19T01:05:00Z", cycle_time_seconds=1.0,
        )
        blob = json.dumps(projection)
        # No archive/staging/receipts/quarantine filesystem paths.
        self.assertNotIn(str(self.roots / "archive"), blob)
        self.assertNotIn(str(self.roots / "staging"), blob)
        self.assertNotIn(str(self.roots / "quarantine"), blob)
        self.assertNotIn(str(self.roots), blob)
        # No raw pointer keys survive.
        self.assertNotIn("pointer", blob)

    def test_cards_keep_only_safe_digests_and_public_urls(self):
        projection = build_acquisition_projection(
            receipts=self._receipts(), ranked_candidates={}, corpus_revision="rev",
            generated_at="2026-07-19T01:05:00Z", cycle_time_seconds=1.0,
        )
        card = next(c for c in projection["ranked_candidates"] if c["disposition"] == "probationary")
        self.assertEqual(len(card["raw_sha256"]), 64)
        self.assertTrue(card["canonical_locator"].startswith("https://"))
        self.assertNotIn("staged_path", card)
        self.assertNotIn("path", card)


if __name__ == "__main__":
    unittest.main()
