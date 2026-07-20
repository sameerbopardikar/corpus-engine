from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock
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

    def observe_candidate(self, engine, *, domain="agentic-engineering", rights_state="public_rights_clear"):
        observation = models.CandidateObservation.create(
            domain=domain,
            entity_type="repository",
            canonical_url=f"https://example.com/{domain}",
            discovery_source="adversarial-test",
            evidence_pointer=f"evidence/{domain}.json",
            evidence_lane="implementation-research",
            observed_at=models.iso(NOW),
        )
        return engine.observe(observation, rights_state=rights_state)

    def proof_receipt(self, work, *, verifier="corpus-engine-verifier", artifact_body=b"verified artifact\n"):
        artifact = Path(self.tmpdir.name) / f"{work.work_id}.artifact"
        artifact.write_bytes(artifact_body)
        receipt = Path(self.tmpdir.name) / f"{work.work_id}.proof.json"
        receipt.write_text(json.dumps({
            "schema_version": 1,
            "work_id": work.work_id,
            "candidate_id": work.candidate_id,
            "action": work.action,
            "verifier": verifier,
            "verified": True,
            "artifact_path": str(artifact.resolve()),
            "artifact_sha256": hashlib.sha256(artifact_body).hexdigest(),
        }), encoding="utf-8")
        return str(receipt.resolve())

    def enqueue(self, engine, *, key="cycle-1"):
        candidate = next(iter(engine.candidates.values()), None) or self.observe_candidate(engine)
        return engine.enqueue_work(
            domain="agentic-engineering",
            candidate_id=candidate.candidate_id,
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

    def test_append_failure_rolls_back_exact_bytes_and_readback_failure_is_not_committed(self):
        ledger = self.engine().ledger
        ledger.append("work_enqueued", {"work": self.pending_record().to_dict()}, recorded_at=NOW)
        before = ledger.path.read_bytes()

        with mock.patch.object(discovery.os, "fsync", side_effect=OSError("injected fsync failure")):
            with self.assertRaises(OSError):
                ledger.append("candidate_observed", {"score": 1}, recorded_at=NOW)
        self.assertEqual(ledger.path.read_bytes(), before)
        self.assertEqual(len(ledger.read_events()), 1)

        ledger._readback_appended_event = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            discovery.LedgerCorruptionError("injected readback failure")
        )
        with self.assertRaises(discovery.LedgerCorruptionError):
            ledger.append("candidate_observed", {"score": 1}, recorded_at=NOW)
        self.assertEqual(ledger.path.read_bytes(), before)
        self.assertEqual(len(ledger.read_events()), 1)

    def test_enqueue_requires_matching_known_candidate_and_rights_clear_material_acquisition(self):
        engine = self.engine()
        with self.assertRaises(ValueError):
            engine.enqueue_work(
                domain="agentic-engineering", candidate_id="cand_unknown", action="inspect",
                score_components={"priority_score": 0.5}, budget_estimate=0.0, now=NOW,
            )
        candidate = self.observe_candidate(engine, rights_state="public_metadata_only")
        before = engine.ledger.path.read_bytes()
        with self.assertRaises(ValueError):
            engine.enqueue_work(
                domain="wrong-domain", candidate_id=candidate.candidate_id, action="inspect",
                score_components={"priority_score": 0.5}, budget_estimate=0.0, now=NOW,
            )
        with self.assertRaises(ValueError):
            engine.enqueue_work(
                domain=candidate.domain, candidate_id=candidate.candidate_id, action="acquire",
                score_components={"priority_score": 0.5}, budget_estimate=0.0, now=NOW,
            )
        self.assertEqual(engine.ledger.path.read_bytes(), before)
        inspected = engine.enqueue_work(
            domain=candidate.domain, candidate_id=candidate.candidate_id, action="inspect",
            score_components={"priority_score": 0.5}, budget_estimate=0.0, now=NOW,
        )
        self.assertEqual(inspected.candidate_id, candidate.candidate_id)

    def test_completion_requires_artifact_backed_authorized_verifier_receipt(self):
        engine = self.engine()
        work = self.enqueue(engine, key="proof-contract")
        leased = engine.lease_work(
            work.work_id, owner="worker", ttl_seconds=60, now=NOW, lease_token="proof-token"
        )
        before = engine.ledger.path.read_bytes()
        unauthorized = self.proof_receipt(work, verifier="self-declared-worker")
        with self.assertRaises(ValueError):
            engine.complete_work(
                work.work_id, owner="worker", lease_token="proof-token",
                lease_generation=leased.lease_generation, proof_receipt=unauthorized, now=NOW,
            )
        self.assertEqual(engine.ledger.path.read_bytes(), before)

        authorized = self.proof_receipt(work)
        receipt_data = json.loads(Path(authorized).read_text(encoding="utf-8"))
        Path(receipt_data["artifact_path"]).write_bytes(b"tampered\n")
        with self.assertRaises(ValueError):
            engine.complete_work(
                work.work_id, owner="worker", lease_token="proof-token",
                lease_generation=leased.lease_generation, proof_receipt=authorized, now=NOW,
            )
        self.assertEqual(engine.ledger.path.read_bytes(), before)

        authorized = self.proof_receipt(work)
        receipt_data = json.loads(Path(authorized).read_text(encoding="utf-8"))
        done = engine.complete_work(
            work.work_id, owner="worker", lease_token="proof-token",
            lease_generation=leased.lease_generation, proof_receipt=authorized, now=NOW,
        )
        self.assertEqual(done.state, "done")
        completion_event = engine.ledger.read_events()[-1]
        self.assertEqual(completion_event["payload"]["proof"]["verifier"], "corpus-engine-verifier")
        self.assertEqual(completion_event["payload"]["proof"]["artifact_sha256"], receipt_data["artifact_sha256"])

    def test_replay_rejects_completion_from_verifier_outside_authorized_set(self):
        engine = self.engine()
        work = self.enqueue(engine, key="replay-verifier")
        leased = engine.lease_work(
            work.work_id, owner="worker", ttl_seconds=60, now=NOW, lease_token="proof-token"
        )
        engine.complete_work(
            work.work_id, owner="worker", lease_token="proof-token",
            lease_generation=leased.lease_generation,
            proof_receipt=self.proof_receipt(work), now=NOW,
        )
        lines = engine.ledger.path.read_text(encoding="utf-8").splitlines()
        forged = json.loads(lines[-1])
        forged["payload"]["proof"]["verifier"] = "unauthorized-verifier"
        lines[-1] = json.dumps(forged, sort_keys=True, separators=(",", ":"))
        engine.ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(discovery.LedgerCorruptionError):
            self.engine()

    def test_replay_revalidates_receipt_and_artifact_bytes(self):
        engine = self.engine()
        work = self.enqueue(engine, key="replay-artifact")
        leased = engine.lease_work(
            work.work_id, owner="worker", ttl_seconds=60, now=NOW, lease_token="proof-token"
        )
        proof_receipt = self.proof_receipt(work)
        engine.complete_work(
            work.work_id, owner="worker", lease_token="proof-token",
            lease_generation=leased.lease_generation,
            proof_receipt=proof_receipt, now=NOW,
        )
        receipt = json.loads(Path(proof_receipt).read_text(encoding="utf-8"))
        Path(receipt["artifact_path"]).write_bytes(b"post-completion tamper\n")
        with self.assertRaises(discovery.LedgerCorruptionError):
            self.engine()

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
        self.assertEqual(
            [event["event_type"] for event in events],
            ["candidate_observed", "work_enqueued"],
        )

    def test_distinct_idempotency_runs_coexist(self):
        engine = self.engine()
        first = self.enqueue(engine, key="cycle-1")
        second = self.enqueue(engine, key="cycle-2")
        self.assertNotEqual(first.work_id, second.work_id)
        self.assertEqual(len(engine.work_items), 2)

    def test_current_projection_fences_stale_worker_after_re_lease(self):
        engine = self.engine()
        work = self.enqueue(engine)
        first = engine.lease_work(
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
                lease_generation=first.lease_generation,
                proof_receipt="stale.json",
                now=NOW + timedelta(seconds=3),
            )
        done = engine.complete_work(
            work.work_id,
            owner="worker-b",
            lease_token="token-b",
            lease_generation=second.lease_generation,
            proof_receipt=self.proof_receipt(second),
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

    def test_replay_rejects_impossible_work_transition_even_when_snapshot_is_valid(self):
        engine = self.engine()
        work = self.enqueue(engine)
        forged_lease = work.lease(
            owner="forged-worker",
            ttl_seconds=60,
            now=NOW,
            lease_token="forged-token",
        )
        engine.ledger.append(
            "work_state_changed",
            {"work": forged_lease.to_dict(), "transition": "completed"},
            recorded_at=NOW,
        )

        with self.assertRaises(discovery.LedgerCorruptionError):
            self.engine()

    def test_select_work_durably_recovers_expired_leases_before_ranking(self):
        engine = self.engine()
        work = self.enqueue(engine)
        engine.lease_work(
            work.work_id,
            owner="abandoned-worker",
            ttl_seconds=1,
            now=NOW,
            lease_token="abandoned-token",
        )

        selected = engine.select_work(now=NOW + timedelta(seconds=1))

        self.assertEqual([item.work_id for item in selected], [work.work_id])
        self.assertEqual(selected[0].state, "pending")
        self.assertEqual(selected[0].attempts, 1)
        restarted = self.engine()
        self.assertEqual(restarted.work_items[work.work_id].state, "pending")
        self.assertEqual(restarted.work_items[work.work_id].attempts, 1)
        self.assertEqual(
            [event["payload"].get("transition") for event in restarted.ledger.read_events()],
            [None, None, "leased", "lease_expired"],
        )


if __name__ == "__main__":
    unittest.main()
