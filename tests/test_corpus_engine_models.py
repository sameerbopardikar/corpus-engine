from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "corpus_engine_models.py"
spec = importlib.util.spec_from_file_location("corpus_engine_models", MODULE_PATH)
assert spec and spec.loader
models = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = models
spec.loader.exec_module(models)


def make_observation(**overrides):
    fields = dict(
        domain="agentic-engineering",
        entity_type="repository",
        canonical_url="https://github.com/example/repo",
        discovery_source="github_repository:example/repo",
        evidence_pointer="https://github.com/example/repo/commit/abc123",
        evidence_lane="canonical",
        topics=("tool-use", "memory"),
    )
    fields.update(overrides)
    return models.CandidateObservation.create(**fields)


class CandidateObservationTests(unittest.TestCase):
    def test_valid_observation_round_trips_fields(self):
        obs = make_observation()
        self.assertEqual(obs.domain, "agentic-engineering")
        self.assertEqual(obs.schema_version, models.SCHEMA_VERSION)
        self.assertTrue(obs.observed_at.endswith("Z"))

    def test_malformed_observation_rejects_blank_canonical_url(self):
        with self.assertRaises(ValueError):
            make_observation(canonical_url="   ")

    def test_malformed_observation_rejects_unknown_entity_type(self):
        with self.assertRaises(ValueError):
            make_observation(entity_type="wizard")

    def test_malformed_observation_rejects_blank_discovery_source(self):
        with self.assertRaises(ValueError):
            make_observation(discovery_source="")

    def test_candidate_key_is_stable_and_host_case_insensitive(self):
        a = make_observation(canonical_url="https://GITHUB.COM/example/repo")
        b = make_observation(canonical_url="https://github.com/example/repo/")
        self.assertEqual(a.candidate_key, b.candidate_key)

    def test_candidate_key_preserves_case_sensitive_url_path(self):
        a = make_observation(canonical_url="https://example.com/Paper")
        b = make_observation(canonical_url="https://EXAMPLE.COM/paper")
        self.assertNotEqual(a.candidate_key, b.candidate_key)

    def test_observation_rejects_relative_url_blank_topic_and_naive_timestamp(self):
        with self.assertRaises(ValueError):
            make_observation(canonical_url="/relative")
        with self.assertRaises(ValueError):
            make_observation(topics=("valid", " "))
        with self.assertRaises(ValueError):
            make_observation(observed_at="2026-07-16T12:00:00")

    def test_candidate_key_differs_across_entity_type(self):
        a = make_observation(entity_type="repository")
        b = make_observation(entity_type="paper")
        self.assertNotEqual(a.candidate_key, b.candidate_key)


class CandidateRecordTests(unittest.TestCase):
    def test_from_observation_creates_discovered_record_with_transparent_scores(self):
        obs = make_observation()
        scores = {
            "authority": 0.8,
            "demonstrated_practice": 0.6,
            "novelty": 0.5,
            "relevance": 0.9,
            "corroboration": 0.2,
            "production_or_scientific_value": 0.7,
            "cost": 0.3,
        }
        record = models.CandidateRecord.from_observation(obs, scores)
        self.assertEqual(record.status, "discovered")
        self.assertEqual(record.occurrences, 1)
        self.assertEqual(record.first_seen_at, record.last_seen_at)
        self.assertEqual(set(record.score_components), set(scores))
        self.assertIn("total", record.compute_score())
        with self.assertRaises(TypeError):
            record.score_components["authority"] = 0.1

    def test_candidate_id_is_deterministic_for_equivalent_inputs(self):
        obs1 = make_observation(canonical_url="https://github.com/example/repo")
        obs2 = make_observation(canonical_url="HTTPS://GITHUB.COM/example/repo/")
        scores = {
            "authority": 0.5, "demonstrated_practice": 0.5, "novelty": 0.5,
            "relevance": 0.5, "corroboration": 0.5, "production_or_scientific_value": 0.5,
            "cost": 0.5,
        }
        r1 = models.CandidateRecord.from_observation(obs1, scores)
        r2 = models.CandidateRecord.from_observation(obs2, scores)
        self.assertEqual(r1.candidate_id, r2.candidate_id)

    def test_malformed_scores_out_of_range_are_rejected(self):
        obs = make_observation()
        scores = {
            "authority": 1.5, "demonstrated_practice": 0.5, "novelty": 0.5,
            "relevance": 0.5, "corroboration": 0.5, "production_or_scientific_value": 0.5,
            "cost": 0.5,
        }
        with self.assertRaises(ValueError):
            models.CandidateRecord.from_observation(obs, scores)

    def test_malformed_scores_missing_component_is_rejected(self):
        obs = make_observation()
        scores = {"authority": 0.5}
        with self.assertRaises(ValueError):
            models.CandidateRecord.from_observation(obs, scores)

    def test_deterministic_score_computation_is_order_independent(self):
        obs = make_observation()
        scores = {
            "authority": 0.8, "demonstrated_practice": 0.6, "novelty": 0.5,
            "relevance": 0.9, "corroboration": 0.4, "production_or_scientific_value": 0.7,
            "cost": 0.2,
        }
        record = models.CandidateRecord.from_observation(obs, scores)
        reordered_scores = dict(reversed(list(scores.items())))
        record2 = models.CandidateRecord.from_observation(obs, reordered_scores)
        self.assertEqual(record.compute_score(), record2.compute_score())

    def test_observe_increments_occurrences_and_corroboration(self):
        obs = make_observation()
        scores = {
            "authority": 0.5, "demonstrated_practice": 0.5, "novelty": 0.5,
            "relevance": 0.5, "corroboration": 0.2, "production_or_scientific_value": 0.5,
            "cost": 0.5,
        }
        record = models.CandidateRecord.from_observation(obs, scores)
        second_obs = make_observation(discovery_source="x_discovery:mention")
        updated = record.observe(second_obs)
        self.assertEqual(updated.occurrences, 2)
        self.assertGreater(updated.score_components["corroboration"], record.score_components["corroboration"])
        self.assertEqual(updated.candidate_id, record.candidate_id)

    def test_observe_rejects_mismatched_candidate_key(self):
        obs = make_observation()
        scores = {
            "authority": 0.5, "demonstrated_practice": 0.5, "novelty": 0.5,
            "relevance": 0.5, "corroboration": 0.5, "production_or_scientific_value": 0.5,
            "cost": 0.5,
        }
        record = models.CandidateRecord.from_observation(obs, scores)
        other_obs = make_observation(canonical_url="https://github.com/other/repo")
        with self.assertRaises(ValueError):
            record.observe(other_obs)

    def test_transition_enforces_lifecycle_rules(self):
        obs = make_observation()
        scores = {
            "authority": 0.5, "demonstrated_practice": 0.5, "novelty": 0.5,
            "relevance": 0.5, "corroboration": 0.5, "production_or_scientific_value": 0.5,
            "cost": 0.5,
        }
        record = models.CandidateRecord.from_observation(obs, scores)
        promoted = record.transition("probationary").transition("promoted")
        self.assertEqual(promoted.status, "promoted")
        with self.assertRaises(ValueError):
            record.transition("promoted")

    def test_transition_to_rejected_requires_reason(self):
        obs = make_observation()
        scores = {
            "authority": 0.5, "demonstrated_practice": 0.5, "novelty": 0.5,
            "relevance": 0.5, "corroboration": 0.5, "production_or_scientific_value": 0.5,
            "cost": 0.5,
        }
        record = models.CandidateRecord.from_observation(obs, scores)
        with self.assertRaises(ValueError):
            record.transition("rejected")
        rejected = record.transition("rejected", rejection_reason="dead link")
        self.assertEqual(rejected.rejection_reason, "dead link")


class WorkItemTests(unittest.TestCase):
    def test_create_produces_stable_deterministic_work_id(self):
        w1 = models.WorkItem.create(
            domain="agentic-engineering", candidate_id="cand_abc", action="inspect",
            score_components={"priority_score": 0.5}, budget_estimate=1.0,
        )
        w2 = models.WorkItem.create(
            domain="agentic-engineering", candidate_id="cand_abc", action="inspect",
            score_components={"priority_score": 0.9}, budget_estimate=2.0,
        )
        self.assertEqual(w1.work_id, w2.work_id)
        self.assertEqual(w1.idempotency_key, w2.idempotency_key)
        self.assertEqual(w1.state, "pending")
        self.assertEqual(w1.attempts, 0)

    def test_create_rejects_unknown_action(self):
        with self.assertRaises(ValueError):
            models.WorkItem.create(
                domain="agentic-engineering", candidate_id="cand_abc", action="teleport",
                score_components={}, budget_estimate=1.0,
            )

    def test_create_rejects_negative_budget(self):
        with self.assertRaises(ValueError):
            models.WorkItem.create(
                domain="agentic-engineering", candidate_id="cand_abc", action="inspect",
                score_components={}, budget_estimate=-1.0,
            )

    def test_lease_sets_owner_and_expiry(self):
        w = models.WorkItem.create(
            domain="agentic-engineering", candidate_id="cand_abc", action="acquire",
            score_components={}, budget_estimate=1.0,
        )
        now = datetime(2026, 7, 16, tzinfo=timezone.utc)
        leased = w.lease(owner="worker-1", ttl_seconds=60, now=now)
        self.assertEqual(leased.state, "leased")
        self.assertEqual(leased.lease_owner, "worker-1")
        self.assertFalse(leased.is_lease_expired(now=now + timedelta(seconds=30)))
        self.assertTrue(leased.is_lease_expired(now=now + timedelta(seconds=61)))

    def test_lease_rejects_double_lease(self):
        w = models.WorkItem.create(
            domain="agentic-engineering", candidate_id="cand_abc", action="acquire",
            score_components={}, budget_estimate=1.0,
        )
        now = datetime(2026, 7, 16, tzinfo=timezone.utc)
        leased = w.lease(owner="worker-1", ttl_seconds=60, now=now)
        with self.assertRaises(ValueError):
            leased.lease(owner="worker-2", ttl_seconds=60, now=now)

    def test_expired_lease_releases_back_to_pending_and_increments_attempts(self):
        w = models.WorkItem.create(
            domain="agentic-engineering", candidate_id="cand_abc", action="acquire",
            score_components={}, budget_estimate=1.0,
        )
        now = datetime(2026, 7, 16, tzinfo=timezone.utc)
        leased = w.lease(owner="worker-1", ttl_seconds=60, now=now)
        later = now + timedelta(seconds=120)
        released = leased.release_if_expired(now=later)
        self.assertEqual(released.state, "pending")
        self.assertIsNone(released.lease_owner)
        self.assertIsNone(released.lease_expires_at)
        self.assertEqual(released.attempts, 1)

    def test_expired_leases_reach_dead_letter_at_attempt_limit(self):
        current = models.WorkItem.create(
            domain="agentic-engineering", candidate_id="cand_abc", action="acquire",
            score_components={}, budget_estimate=1.0,
        )
        started = datetime(2026, 7, 16, tzinfo=timezone.utc)
        for attempt in range(models.MAX_ATTEMPTS):
            moment = started + timedelta(seconds=attempt * 2)
            current = current.lease(owner="worker-1", ttl_seconds=1, now=moment)
            current = current.release_if_expired(now=moment + timedelta(seconds=1))
        self.assertEqual(current.state, "dead_letter")
        self.assertEqual(current.attempts, models.MAX_ATTEMPTS)

    def test_release_if_expired_is_noop_when_not_expired(self):
        w = models.WorkItem.create(
            domain="agentic-engineering", candidate_id="cand_abc", action="acquire",
            score_components={}, budget_estimate=1.0,
        )
        now = datetime(2026, 7, 16, tzinfo=timezone.utc)
        leased = w.lease(owner="worker-1", ttl_seconds=60, now=now)
        still_leased = leased.release_if_expired(now=now + timedelta(seconds=10))
        self.assertEqual(still_leased.state, "leased")
        self.assertEqual(still_leased.lease_owner, "worker-1")

    def test_complete_records_proof_receipt(self):
        w = models.WorkItem.create(
            domain="agentic-engineering", candidate_id="cand_abc", action="verify",
            score_components={}, budget_estimate=1.0,
        )
        now = datetime(2026, 7, 16, tzinfo=timezone.utc)
        leased = w.lease(owner="worker-1", ttl_seconds=60, now=now)
        done = leased.complete(proof_receipt="receipts/verify/cand_abc.json", now=now)
        self.assertEqual(done.state, "done")
        self.assertIn("receipts/verify/cand_abc.json", done.proof_receipts)

    def test_complete_requires_leased_state(self):
        w = models.WorkItem.create(
            domain="agentic-engineering", candidate_id="cand_abc", action="verify",
            score_components={}, budget_estimate=1.0,
        )
        with self.assertRaises(ValueError):
            w.complete(proof_receipt="receipts/verify/cand_abc.json")

    def test_fail_sets_retry_after_and_moves_to_dead_letter_after_max_attempts(self):
        w = models.WorkItem.create(
            domain="agentic-engineering", candidate_id="cand_abc", action="verify",
            score_components={}, budget_estimate=1.0,
        )
        started = datetime(2026, 7, 16, tzinfo=timezone.utc)
        current = w
        for attempt in range(models.MAX_ATTEMPTS):
            moment = started + timedelta(seconds=attempt * 30)
            current = current.lease(owner="worker-1", ttl_seconds=60, now=moment)
            current = current.fail(retry_after_seconds=30, now=moment)
        self.assertEqual(current.state, "dead_letter")
        self.assertEqual(current.attempts, models.MAX_ATTEMPTS)

    def test_retry_after_blocks_early_release_and_invalid_backoff(self):
        now = datetime(2026, 7, 16, tzinfo=timezone.utc)
        leased = models.WorkItem.create(
            domain="agentic-engineering", candidate_id="cand_abc", action="verify",
            score_components={}, budget_estimate=1.0,
        ).lease(owner="worker-1", ttl_seconds=60, now=now)
        with self.assertRaises(ValueError):
            leased.fail(retry_after_seconds=0, now=now)
        failed = leased.fail(retry_after_seconds=30, now=now)
        with self.assertRaises(ValueError):
            failed.lease(owner="worker-2", ttl_seconds=60, now=now + timedelta(seconds=29))
        self.assertEqual(
            failed.lease(owner="worker-2", ttl_seconds=60, now=now + timedelta(seconds=30)).state,
            "leased",
        )

    def test_work_scores_are_validated_and_immutable(self):
        with self.assertRaises(ValueError):
            models.WorkItem.create(
                domain="agentic-engineering", candidate_id="cand_abc", action="inspect",
                score_components={"priority": float("nan")}, budget_estimate=1.0,
            )
        work = models.WorkItem.create(
            domain="agentic-engineering", candidate_id="cand_abc", action="inspect",
            score_components={"priority": 1.0}, budget_estimate=1.0,
        )
        with self.assertRaises(TypeError):
            work.score_components["priority"] = 2.0

    def test_idempotency_key_can_be_overridden_for_distinct_retries(self):
        w1 = models.WorkItem.create(
            domain="agentic-engineering", candidate_id="cand_abc", action="inspect",
            score_components={}, budget_estimate=1.0, idempotency_key="manual-key-1",
        )
        self.assertEqual(w1.idempotency_key, "manual-key-1")
        self.assertNotEqual(w1.work_id, w1.idempotency_key)


if __name__ == "__main__":
    unittest.main()
