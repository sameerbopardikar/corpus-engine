from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


def _load(name: str, filename: str):
    path = SRC_ROOT / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


models = _load("corpus_engine_models", "corpus_engine_models.py")
discovery = _load("corpus_discovery", "corpus_discovery.py")
NOW = datetime(2026, 7, 16, tzinfo=timezone.utc)


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


class DiscoveryEngineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.ledger_path = Path(self.tmpdir.name) / "discovery-ledger.jsonl"

    def engine(self) -> "discovery.DiscoveryEngine":
        return discovery.DiscoveryEngine(self.ledger_path)


class ScoringTests(DiscoveryEngineTestCase):
    def test_score_observation_is_deterministic(self):
        obs = make_observation()
        scores_a, rationale_a = discovery.score_observation(obs)
        scores_b, rationale_b = discovery.score_observation(obs)
        self.assertEqual(dict(scores_a), dict(scores_b))
        self.assertEqual(rationale_a, rationale_b)
        self.assertTrue(rationale_a)
        self.assertEqual(set(scores_a), set(models.SCORE_COMPONENTS))

    def test_score_observation_varies_with_evidence_lane_and_topics(self):
        canonical = make_observation(evidence_lane="canonical")
        zeitgeist = make_observation(evidence_lane="zeitgeist", evidence_pointer="https://x.com/status/1")
        scores_canonical, _ = discovery.score_observation(canonical)
        scores_zeitgeist, _ = discovery.score_observation(zeitgeist)
        self.assertGreater(scores_canonical["authority"], scores_zeitgeist["authority"])

        few_topics = make_observation(topics=("only-one",), evidence_pointer="https://x.com/status/2")
        many_topics = make_observation(topics=("a", "b", "c", "d"), evidence_pointer="https://x.com/status/3")
        scores_few, _ = discovery.score_observation(few_topics)
        scores_many, _ = discovery.score_observation(many_topics)
        self.assertGreater(scores_many["relevance"], scores_few["relevance"])


class CandidateReplayTests(DiscoveryEngineTestCase):
    def test_first_observation_creates_discovered_candidate(self):
        engine = self.engine()
        obs = make_observation()
        record = engine.observe(obs)
        self.assertEqual(record.status, "discovered")
        self.assertEqual(record.occurrences, 1)
        self.assertEqual(engine.candidates[obs.candidate_key].candidate_id, record.candidate_id)

    def test_duplicate_observation_is_strict_noop(self):
        engine = self.engine()
        obs = make_observation()
        first = engine.observe(obs)
        events_after_first = len(engine.ledger.read_events())
        duplicate = make_observation(observed_at="2026-07-16T12:00:00Z")
        second = engine.observe(duplicate)
        self.assertEqual(second.to_dict(), first.to_dict())
        self.assertEqual(engine.candidates[obs.candidate_key].occurrences, 1)
        self.assertEqual(len(engine.ledger.read_events()), events_after_first)

    def test_independent_observation_converges_and_bumps_corroboration(self):
        engine = self.engine()
        obs = make_observation()
        first = engine.observe(obs)
        independent = make_observation(
            discovery_source="x_discovery:mention",
            evidence_pointer="https://x.com/status/999",
        )
        second = engine.observe(independent)
        self.assertEqual(second.occurrences, 2)
        self.assertGreater(second.score_components["corroboration"], first.score_components["corroboration"])
        self.assertEqual(second.candidate_id, first.candidate_id)

    def test_replay_reconstructs_identical_projection(self):
        engine = self.engine()
        obs = make_observation()
        engine.observe(obs)
        independent = make_observation(
            discovery_source="x_discovery:mention",
            evidence_pointer="https://x.com/status/999",
        )
        engine.observe(independent)
        engine.transition_candidate(obs.candidate_key, "probationary")

        replayed = self.engine()
        self.assertEqual(
            replayed.candidates[obs.candidate_key].occurrences,
            engine.candidates[obs.candidate_key].occurrences,
        )
        self.assertEqual(replayed.candidates[obs.candidate_key].status, "probationary")

    def test_candidate_key_stability_across_engine_instances(self):
        engine = self.engine()
        obs = make_observation()
        record = engine.observe(obs)
        self.assertEqual(record.candidate_id, obs.candidate_key)
        reloaded = self.engine()
        self.assertIn(obs.candidate_key, reloaded.candidates)


class WorkQueueTests(DiscoveryEngineTestCase):
    SCORES = {"priority_score": 0.5}

    def test_enqueue_work_is_idempotent(self):
        engine = self.engine()
        first = engine.enqueue_work(
            domain="agentic-engineering", candidate_id="cand_abc", action="inspect",
            score_components={"priority_score": 0.2}, budget_estimate=1.0, now=NOW,
        )
        events_after_first = len(engine.ledger.read_events())
        second = engine.enqueue_work(
            domain="agentic-engineering", candidate_id="cand_abc", action="inspect",
            score_components={"priority_score": 0.9}, budget_estimate=5.0, now=NOW,
        )
        self.assertEqual(first.work_id, second.work_id)
        self.assertEqual(second.to_dict(), first.to_dict())
        self.assertEqual(len(engine.ledger.read_events()), events_after_first)

    def test_select_work_orders_by_priority_and_respects_budget(self):
        engine = self.engine()
        low = engine.enqueue_work(
            domain="d", candidate_id="cand_low", action="inspect",
            score_components={"priority_score": 0.2}, budget_estimate=3.0, now=NOW,
        )
        high = engine.enqueue_work(
            domain="d", candidate_id="cand_high", action="inspect",
            score_components={"priority_score": 0.9}, budget_estimate=3.0, now=NOW,
        )
        mid = engine.enqueue_work(
            domain="d", candidate_id="cand_mid", action="inspect",
            score_components={"priority_score": 0.5}, budget_estimate=3.0, now=NOW,
        )
        selected = engine.select_work(action="inspect", budget=6.0)
        self.assertEqual([w.work_id for w in selected], [high.work_id, mid.work_id])
        self.assertNotIn(low.work_id, [w.work_id for w in selected])

    def test_select_work_filters_by_action(self):
        engine = self.engine()
        engine.enqueue_work(
            domain="d", candidate_id="cand_a", action="inspect",
            score_components={"priority_score": 0.9}, budget_estimate=1.0, now=NOW,
        )
        acquire = engine.enqueue_work(
            domain="d", candidate_id="cand_b", action="acquire",
            score_components={"priority_score": 0.1}, budget_estimate=1.0, now=NOW,
        )
        selected = engine.select_work(action="acquire", budget=10.0)
        self.assertEqual([w.work_id for w in selected], [acquire.work_id])

    def test_double_lease_raises(self):
        engine = self.engine()
        work = engine.enqueue_work(
            domain="d", candidate_id="cand_abc", action="acquire",
            score_components=self.SCORES, budget_estimate=1.0, now=NOW,
        )
        now = datetime(2026, 7, 16, tzinfo=timezone.utc)
        engine.lease_work(work.work_id, owner="worker-1", ttl_seconds=60, now=now, lease_token="token-1")
        with self.assertRaises(ValueError):
            engine.lease_work(work.work_id, owner="worker-2", ttl_seconds=60, now=now)

    def test_exact_expiry_boundary_allows_recovery(self):
        engine = self.engine()
        work = engine.enqueue_work(
            domain="d", candidate_id="cand_abc", action="acquire",
            score_components=self.SCORES, budget_estimate=1.0, now=NOW,
        )
        now = datetime(2026, 7, 16, tzinfo=timezone.utc)
        engine.lease_work(work.work_id, owner="worker-1", ttl_seconds=60, now=now, lease_token="token-1")
        exact_expiry = now + timedelta(seconds=60)
        released = engine.release_expired_work(work.work_id, now=exact_expiry)
        self.assertEqual(released.state, "pending")
        self.assertEqual(released.attempts, 1)

    def test_retry_after_delays_next_lease(self):
        engine = self.engine()
        work = engine.enqueue_work(
            domain="d", candidate_id="cand_abc", action="verify",
            score_components=self.SCORES, budget_estimate=1.0, now=NOW,
        )
        now = datetime(2026, 7, 16, tzinfo=timezone.utc)
        leased = engine.lease_work(work.work_id, owner="worker-1", ttl_seconds=60, now=now, lease_token="token-1")
        engine.fail_work(work.work_id, owner="worker-1", lease_token="token-1", lease_generation=leased.lease_generation, error="test failure", retry_after_seconds=30, now=now)
        with self.assertRaises(ValueError):
            engine.lease_work(work.work_id, owner="worker-2", ttl_seconds=60, now=now + timedelta(seconds=29))
        recovered = engine.lease_work(work.work_id, owner="worker-2", ttl_seconds=60, now=now + timedelta(seconds=30))
        self.assertEqual(recovered.state, "leased")

    def test_max_attempts_reaches_dead_letter(self):
        engine = self.engine()
        work = engine.enqueue_work(
            domain="d", candidate_id="cand_abc", action="verify",
            score_components=self.SCORES, budget_estimate=1.0, now=NOW,
        )
        started = datetime(2026, 7, 16, tzinfo=timezone.utc)
        work_id = work.work_id
        for attempt in range(models.MAX_ATTEMPTS):
            moment = started + timedelta(seconds=attempt * 60)
            leased = engine.lease_work(work_id, owner="worker-1", ttl_seconds=1, now=moment, lease_token=f"token-{attempt}")
            engine.fail_work(work_id, owner="worker-1", lease_token=f"token-{attempt}", lease_generation=leased.lease_generation, error="test failure", retry_after_seconds=1, now=moment)
        final = engine.work_items[work_id]
        self.assertEqual(final.state, "dead_letter")
        self.assertEqual(final.attempts, models.MAX_ATTEMPTS)

    def test_completion_requires_proof_receipt(self):
        engine = self.engine()
        work = engine.enqueue_work(
            domain="d", candidate_id="cand_abc", action="verify",
            score_components=self.SCORES, budget_estimate=1.0, now=NOW,
        )
        now = datetime(2026, 7, 16, tzinfo=timezone.utc)
        leased = engine.lease_work(work.work_id, owner="worker-1", ttl_seconds=60, now=now, lease_token="token-1")
        with self.assertRaises(ValueError):
            engine.complete_work(work.work_id, owner="worker-1", lease_token="token-1", lease_generation=leased.lease_generation, proof_receipt="   ", now=now)
        done = engine.complete_work(work.work_id, owner="worker-1", lease_token="token-1", lease_generation=leased.lease_generation, proof_receipt="receipts/verify/cand_abc.json", now=now)
        self.assertEqual(done.state, "done")
        self.assertIn("receipts/verify/cand_abc.json", done.proof_receipts)

    def test_restart_replay_reconstructs_work_queue(self):
        engine = self.engine()
        work = engine.enqueue_work(
            domain="d", candidate_id="cand_abc", action="acquire",
            score_components=self.SCORES, budget_estimate=1.0, now=NOW,
        )
        now = datetime(2026, 7, 16, tzinfo=timezone.utc)
        leased = engine.lease_work(work.work_id, owner="worker-1", ttl_seconds=60, now=now, lease_token="token-1")
        engine.complete_work(work.work_id, owner="worker-1", lease_token="token-1", lease_generation=leased.lease_generation, proof_receipt="receipts/acquire/cand_abc.json", now=now)

        restarted = self.engine()
        restored = restarted.work_items[work.work_id]
        self.assertEqual(restored.state, "done")
        self.assertIn("receipts/acquire/cand_abc.json", restored.proof_receipts)


class LedgerCorruptionTests(DiscoveryEngineTestCase):
    def test_malformed_json_line_fails_closed(self):
        engine = self.engine()
        obs = make_observation()
        engine.observe(obs)
        with open(self.ledger_path, "a", encoding="utf-8") as fh:
            fh.write("{not valid json\n")
        with self.assertRaises(discovery.LedgerCorruptionError):
            self.engine()
        raw_lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(raw_lines), 2)
        json.loads(raw_lines[0])

    def test_truncated_final_event_fails_closed_without_discarding_earlier_events(self):
        engine = self.engine()
        obs = make_observation()
        engine.observe(obs)
        independent = make_observation(
            discovery_source="x_discovery:mention",
            evidence_pointer="https://x.com/status/999",
        )
        engine.observe(independent)
        with open(self.ledger_path, "a", encoding="utf-8") as fh:
            fh.write('{"schema_version": 1, "event_id": "evt_broken", "event_type": "candidate_obs')

        with self.assertRaises(discovery.LedgerCorruptionError):
            self.engine()

        raw_lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(raw_lines), 3)
        json.loads(raw_lines[0])
        json.loads(raw_lines[1])

    def test_unknown_schema_version_fails_closed(self):
        engine = self.engine()
        engine.observe(make_observation())
        with open(self.ledger_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "schema_version": 99,
                "event_id": "evt_future",
                "event_type": "candidate_observed",
                "recorded_at": "2026-07-16T00:00:00Z",
                "payload": {},
            }) + "\n")
        with self.assertRaises(discovery.LedgerCorruptionError):
            self.engine()


class LedgerConcurrencyTests(DiscoveryEngineTestCase):
    def test_two_ledger_instances_append_safely(self):
        ledger_a = discovery.DiscoveryLedger(self.ledger_path)
        ledger_b = discovery.DiscoveryLedger(self.ledger_path)
        for i in range(20):
            ledger_a.append("candidate_observed", {"seq": f"a{i}"})
            ledger_b.append("candidate_observed", {"seq": f"b{i}"})
        events = ledger_a.read_events()
        self.assertEqual(len(events), 40)
        seqs = {event["payload"]["seq"] for event in events}
        self.assertEqual(len(seqs), 40)


if __name__ == "__main__":
    unittest.main()
