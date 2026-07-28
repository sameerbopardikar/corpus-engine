from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_x_radar import (
    XRadarCycleFinalized,
    XRadarStateError,
    commit_failure,
    commit_success,
    lease_next,
)


def _lease_worker(queries, state, cycle_id, start, queue):
    start.wait()
    try:
        lease = lease_next(
            tuple(queries), Path(state), cycle_id=cycle_id,
            leased_at="2026-07-27T00:00:00Z",
        )
        queue.put(("ok", lease.to_dict()))
    except Exception as exc:  # pragma: no cover - asserted in parent process
        queue.put(("error", type(exc).__name__, str(exc)))


class XRadarRotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "x-radar-state.json"
        self.queries = ("query zero", "query one", "query two", "query three")

    def lease(self, cycle: str):
        suffix = int(cycle.rsplit("-", 1)[-1]) % 24
        return lease_next(
            self.queries,
            self.state,
            cycle_id=cycle,
            leased_at=f"2026-07-27T{suffix:02d}:00:00Z",
        )

    def receipt(self, lease, name: str, *, result="material_signal_ingested", artifact=True):
        artifacts = []
        if artifact:
            artifact_path = self.root / f"{name}.artifact.json"
            artifact_path.write_text(json.dumps({"name": name}) + "\n", encoding="utf-8")
            artifacts.append({
                "path": str(artifact_path.resolve()),
                "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            })
        receipt = {
            "schema_version": 1,
            "cycle_id": lease.cycle_id,
            "lease_id": lease.lease_id,
            "queries_sha256": lease.queries_sha256,
            "query_index": lease.index,
            "query_sha256": lease.query_sha256,
            "result": result,
            "checked_at": "2026-07-27T05:00:00Z",
            "artifacts": artifacts,
        }
        path = self.root / f"{name}.receipt.json"
        path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def succeed(self, lease, name: str):
        receipt = self.receipt(lease, name)
        return commit_success(
            self.queries, self.state, lease=lease,
            result_receipt=receipt, committed_at="2026-07-27T06:00:00Z",
        ), receipt

    def test_four_successes_rotate_zero_one_two_three_zero(self):
        indexes = []
        for index in range(4):
            lease = self.lease(f"cycle-{index}")
            indexes.append(lease.index)
            self.succeed(lease, f"result-{index}")
        self.assertEqual(indexes, [0, 1, 2, 3])
        self.assertEqual(self.lease("cycle-4").index, 0)

    def test_failure_does_not_advance_and_next_cycle_retries_same_query(self):
        first = self.lease("cycle-0")
        commit_failure(
            self.queries, self.state, lease=first, error="provider unavailable",
            committed_at="2026-07-27T05:00:00Z",
        )
        retry = self.lease("cycle-1")
        self.assertEqual(retry.index, first.index)
        self.assertEqual(retry.query, first.query)

    def test_restart_reads_persisted_next_index(self):
        first = self.lease("cycle-0")
        self.succeed(first, "restart")
        restarted = lease_next(
            tuple(self.queries), self.state, cycle_id="cycle-1",
            leased_at="2026-07-27T06:00:00Z",
        )
        self.assertEqual(restarted.index, 1)

    def test_exact_active_lease_and_success_retries_are_physical_noops(self):
        lease = self.lease("cycle-0")
        state_before = (self.state.read_bytes(), self.state.stat())
        lock_path = self.state.with_suffix(self.state.suffix + ".lock")
        lock_before = (lock_path.read_bytes(), lock_path.stat())
        replayed = self.lease("cycle-0")
        self.assertEqual(replayed, lease)
        self.assertEqual(self.state.read_bytes(), state_before[0])
        self.assertEqual(self.state.stat().st_mtime_ns, state_before[1].st_mtime_ns)
        self.assertEqual(lock_path.read_bytes(), lock_before[0])
        self.assertEqual(lock_path.stat().st_mtime_ns, lock_before[1].st_mtime_ns)

        _, receipt = self.succeed(lease, "exact")
        state_after = (self.state.read_bytes(), self.state.stat())
        lock_after = (lock_path.read_bytes(), lock_path.stat())
        commit_success(
            self.queries, self.state, lease=lease,
            result_receipt=receipt, committed_at="2026-07-27T23:00:00Z",
        )
        self.assertEqual(self.state.read_bytes(), state_after[0])
        self.assertEqual(self.state.stat().st_mtime_ns, state_after[1].st_mtime_ns)
        self.assertEqual(lock_path.read_bytes(), lock_after[0])
        self.assertEqual(lock_path.stat().st_mtime_ns, lock_after[1].st_mtime_ns)

    def test_finalized_cycle_cannot_be_leased_again(self):
        lease = self.lease("cycle-0")
        self.succeed(lease, "finalized")
        before = self.state.read_bytes()
        with self.assertRaisesRegex(XRadarCycleFinalized, "already succeeded"):
            self.lease("cycle-0")
        self.assertEqual(self.state.read_bytes(), before)

    def test_same_receipt_digest_at_different_path_is_not_exact(self):
        lease = self.lease("cycle-0")
        _, receipt = self.succeed(lease, "original")
        copy = self.root / "copied.receipt.json"
        copy.write_bytes(receipt.read_bytes())
        before = self.state.read_bytes()
        with self.assertRaisesRegex(XRadarStateError, "conflicting result receipt"):
            commit_success(
                self.queries, self.state, lease=lease,
                result_receipt=copy, committed_at="2026-07-27T07:00:00Z",
            )
        self.assertEqual(self.state.read_bytes(), before)

    def test_second_cycle_cannot_lease_while_first_is_active(self):
        self.lease("cycle-0")
        before = self.state.read_bytes()
        with self.assertRaisesRegex(XRadarStateError, "active lease"):
            self.lease("cycle-1")
        self.assertEqual(self.state.read_bytes(), before)

    def test_stale_or_forged_lease_cannot_commit(self):
        lease = self.lease("cycle-0")
        forged = type(lease)(
            cycle_id=lease.cycle_id, lease_id="lease_" + "0" * 64,
            index=lease.index, query=lease.query,
            query_sha256=lease.query_sha256,
            queries_sha256=lease.queries_sha256,
            leased_at=lease.leased_at,
        )
        receipt = self.receipt(forged, "forged")
        before = self.state.read_bytes()
        with self.assertRaisesRegex(XRadarStateError, "lease does not match"):
            commit_success(
                self.queries, self.state, lease=forged,
                result_receipt=receipt, committed_at="2026-07-27T05:00:00Z",
            )
        self.assertEqual(self.state.read_bytes(), before)

    def test_query_configuration_drift_fails_closed(self):
        lease = self.lease("cycle-0")
        before = self.state.read_bytes()
        changed = (*self.queries[:-1], "replacement query")
        with self.assertRaisesRegex(XRadarStateError, "query configuration changed"):
            commit_failure(
                changed, self.state, lease=lease, error="test",
                committed_at="2026-07-27T05:00:00Z",
            )
        self.assertEqual(self.state.read_bytes(), before)

    def test_receipt_must_exist_be_strict_json_and_bind_lease(self):
        lease = self.lease("cycle-0")
        before = self.state.read_bytes()
        missing = self.root / "missing.json"
        with self.assertRaisesRegex(XRadarStateError, "receipt"):
            commit_success(
                self.queries, self.state, lease=lease,
                result_receipt=missing, committed_at="2026-07-27T05:00:00Z",
            )
        malformed = self.root / "malformed.json"
        malformed.write_text('{"schema_version": NaN}', encoding="utf-8")
        with self.assertRaisesRegex(XRadarStateError, "receipt"):
            commit_success(
                self.queries, self.state, lease=lease,
                result_receipt=malformed, committed_at="2026-07-27T05:00:00Z",
            )
        wrong = self.receipt(lease, "wrong")
        value = json.loads(wrong.read_text())
        value["query_index"] = 3
        wrong.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(XRadarStateError, "receipt binding"):
            commit_success(
                self.queries, self.state, lease=lease,
                result_receipt=wrong, committed_at="2026-07-27T05:00:00Z",
            )
        self.assertEqual(self.state.read_bytes(), before)

    def test_changed_declared_artifact_does_not_advance(self):
        lease = self.lease("cycle-0")
        receipt = self.receipt(lease, "artifact-change")
        value = json.loads(receipt.read_text())
        Path(value["artifacts"][0]["path"]).write_text("changed\n", encoding="utf-8")
        before = self.state.read_bytes()
        with self.assertRaisesRegex(XRadarStateError, "artifact digest"):
            commit_success(
                self.queries, self.state, lease=lease,
                result_receipt=receipt, committed_at="2026-07-27T05:00:00Z",
            )
        self.assertEqual(self.state.read_bytes(), before)

    def test_state_and_lock_symlinks_fail_closed(self):
        target = self.root / "target.json"
        target.write_text("{}", encoding="utf-8")
        symlink_state = self.root / "symlink-state.json"
        symlink_state.symlink_to(target)
        with self.assertRaisesRegex(XRadarStateError, "symlink|regular"):
            lease_next(
                self.queries, symlink_state, cycle_id="cycle-0",
                leased_at="2026-07-27T00:00:00Z",
            )
        state = self.root / "lock-test.json"
        lock = state.with_suffix(state.suffix + ".lock")
        lock.symlink_to(target)
        with self.assertRaisesRegex(XRadarStateError, "lock|symlink|regular"):
            lease_next(
                self.queries, state, cycle_id="cycle-0",
                leased_at="2026-07-27T00:00:00Z",
            )

    def test_delayed_success_retry_after_more_than_256_cycles_is_noop(self):
        queries = ("only query",)
        state = self.root / "long-history.json"
        first_lease = None
        first_receipt = None
        for index in range(260):
            lease = lease_next(
                queries, state, cycle_id=f"long-{index}",
                leased_at="2026-07-27T00:00:00Z",
            )
            receipt = self.receipt(lease, f"long-{index}")
            commit_success(
                queries, state, lease=lease, result_receipt=receipt,
                committed_at="2026-07-27T01:00:00Z",
            )
            if index == 0:
                first_lease, first_receipt = lease, receipt
        assert first_lease is not None and first_receipt is not None
        before = self.state.read_bytes() if self.state.exists() else b""
        long_before = state.read_bytes()
        commit_success(
            queries, state, lease=first_lease, result_receipt=first_receipt,
            committed_at="2026-07-28T01:00:00Z",
        )
        self.assertEqual(state.read_bytes(), long_before)
        self.assertEqual(self.state.read_bytes() if self.state.exists() else b"", before)

    def test_two_processes_with_different_cycles_yield_one_lease(self):
        context = multiprocessing.get_context("fork")
        start = context.Event()
        queue = context.Queue()
        processes = [
            context.Process(
                target=_lease_worker,
                args=(self.queries, str(self.state), f"concurrent-{index}", start, queue),
            )
            for index in range(2)
        ]
        for process in processes:
            process.start()
        start.set()
        results = [queue.get(timeout=5) for _ in processes]
        for process in processes:
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sum(result[0] == "ok" for result in results), 1)
        self.assertEqual(sum(result[0] == "error" for result in results), 1)

    def test_state_is_strict_json_and_contains_receipt_binding_not_result_text(self):
        lease = self.lease("cycle-0")
        self.succeed(lease, "strict")
        value = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(value["next_index"], 1)
        self.assertNotIn("result_text", self.state.read_text(encoding="utf-8"))
        self.assertTrue(value["history"][0]["result_receipt_path"].endswith("strict.receipt.json"))
        self.assertEqual(len(value["history"]), 1)


if __name__ == "__main__":
    unittest.main()
