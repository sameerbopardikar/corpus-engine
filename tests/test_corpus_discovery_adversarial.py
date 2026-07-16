from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
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
discovery = _load("corpus_discovery_adversarial_target", "corpus_discovery.py")
NOW = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)


class DiscoveryAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.ledger_path = Path(self.tmpdir.name) / "private" / "ledger.jsonl"

    def engine(self):
        return discovery.DiscoveryEngine(self.ledger_path)

    def enqueue(self, engine, *, key="cycle-1"):
        return engine.enqueue_work(
            domain="agentic-engineering",
            candidate_id="cand_abc",
            action="verify",
            score_components={"priority_score": 0.9},
            budget_estimate=1.0,
            idempotency_key=key,
            now=NOW,
        )

    def pending_record(self):
        return models.WorkItem.create(
            domain="agentic-engineering",
            candidate_id="cand_abc",
            action="verify",
            score_components={"priority_score": 0.9},
            budget_estimate=1.0,
            idempotency_key="direct-ledger-test",
            now=NOW,
        )

    def test_ledger_and_lock_permissions_are_private(self):
        engine = self.engine()
        self.assertEqual(stat.S_IMODE(engine.ledger.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(engine.ledger.lock_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(engine.ledger.path.parent.stat().st_mode), 0o700)

    def test_strict_json_rejects_non_finite_payload_without_append(self):
        ledger = self.engine().ledger
        with self.assertRaises(ValueError):
            ledger.append("candidate_observed", {"score": float("nan")}, recorded_at=NOW)
        self.assertEqual(ledger.read_events(), [])

    def test_valid_unterminated_event_is_repaired_before_append(self):
        ledger = self.engine().ledger
        first = ledger.append(
            "work_enqueued",
            {"work": self.pending_record().to_dict()},
            recorded_at=NOW,
        )
        raw = ledger.path.read_bytes().rstrip(b"\n")
        ledger.path.write_bytes(raw)
        os.chmod(ledger.path, 0o600)
        ledger.append(
            "candidate_observed",
            {
                "observation": models.CandidateObservation.create(
                    domain="d",
                    entity_type="repository",
                    canonical_url="https://example.com/repo",
                    discovery_source="test",
                    evidence_pointer="receipt-1",
                    evidence_lane="canonical",
                    observed_at=models.iso(NOW),
                ).to_dict(),
                "score_components": {
                    "authority": 0.5,
                    "demonstrated_practice": 0.5,
                    "novelty": 0.5,
                    "relevance": 0.5,
                    "corroboration": 0.5,
                    "production_or_scientific_value": 0.5,
                    "cost": 0.5,
                },
                "rationale": "test",
            },
            recorded_at=NOW,
        )
        self.assertTrue(ledger.path.read_bytes().endswith(b"\n"))
        self.assertEqual(len(ledger.read_events()), 2)
        self.assertEqual(ledger.read_events()[0]["event_id"], first["event_id"])

    def test_malformed_unterminated_tail_blocks_append_without_rewrite(self):
        ledger = self.engine().ledger
        ledger.path.write_bytes(b'{"broken":')
        before = ledger.path.read_bytes()
        with self.assertRaises(discovery.LedgerCorruptionError):
            ledger.append("work_enqueued", {"work": {}}, recorded_at=NOW)
        self.assertEqual(ledger.path.read_bytes(), before)

    def test_duplicate_event_id_fails_closed(self):
        ledger = self.engine().ledger
        event = ledger.append("work_enqueued", {"work": self.pending_record().to_dict()}, recorded_at=NOW)
        with open(ledger.path, "ab") as handle:
            handle.write(json.dumps(event, separators=(",", ":")).encode() + b"\n")
        with self.assertRaises(discovery.LedgerCorruptionError):
            ledger.read_events()

    def test_concurrent_engines_physically_deduplicate_same_run(self):
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def worker():
            try:
                engine = self.engine()
                barrier.wait()
                self.enqueue(engine, key="same-run")
            except BaseException as exc:  # surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        events = self.engine().ledger.read_events()
        self.assertEqual([event["event_type"] for event in events], ["work_enqueued"])

    def test_distinct_idempotency_runs_coexist(self):
        engine = self.engine()
        first = self.enqueue(engine, key="cycle-1")
        second = self.enqueue(engine, key="cycle-2")
        self.assertNotEqual(first.work_id, second.work_id)
        self.assertEqual(len(engine.work_items), 2)

    def test_current_projection_fences_stale_worker_after_re_lease(self):
        engine = self.engine()
        work = self.enqueue(engine)
        engine.lease_work(
            work.work_id,
            owner="worker-a",
            ttl_seconds=1,
            now=NOW,
            lease_token="token-a",
        )
        engine.release_expired_work(work.work_id, now=NOW + timedelta(seconds=1))
        second = engine.lease_work(
            work.work_id,
            owner="worker-b",
            ttl_seconds=60,
            now=NOW + timedelta(seconds=2),
            lease_token="token-b",
        )
        with self.assertRaises(ValueError):
            engine.complete_work(
                work.work_id,
                owner="worker-a",
                lease_token="token-a",
                proof_receipt="stale.json",
                now=NOW + timedelta(seconds=3),
            )
        done = engine.complete_work(
            work.work_id,
            owner="worker-b",
            lease_token="token-b",
            proof_receipt="fresh.json",
            now=NOW + timedelta(seconds=3),
        )
        self.assertEqual(done.state, "done")
        restarted = self.engine()
        self.assertEqual(restarted.work_items[work.work_id].to_dict(), done.to_dict())
        self.assertEqual(second.lease_generation, 2)

    def test_backdated_transition_and_invalid_selection_are_rejected_without_events(self):
        engine = self.engine()
        work = self.enqueue(engine)
        before = len(engine.ledger.read_events())
        with self.assertRaises(ValueError):
            engine.lease_work(
                work.work_id,
                owner="worker-a",
                ttl_seconds=60,
                now=NOW - timedelta(seconds=1),
                lease_token="token-a",
            )
        self.assertEqual(len(engine.ledger.read_events()), before)
        for bad_budget in (float("nan"), float("inf"), -1.0):
            with self.assertRaises(ValueError):
                engine.select_work(budget=bad_budget, now=NOW)


if __name__ == "__main__":
    unittest.main()
