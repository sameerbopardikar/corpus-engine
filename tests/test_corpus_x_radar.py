from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_x_radar import (
    XRadarStateError,
    commit_failure,
    commit_success,
    lease_next,
)


class XRadarRotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "x-radar-state.json"
        self.queries = ("query zero", "query one", "query two", "query three")

    def lease(self, cycle: str):
        return lease_next(
            self.queries,
            self.state,
            cycle_id=cycle,
            leased_at=f"2026-07-27T{int(cycle[-1]):02d}:00:00Z",
        )

    def test_four_successes_rotate_zero_one_two_three_zero(self):
        indexes = []
        for index in range(4):
            lease = self.lease(f"cycle-{index}")
            indexes.append(lease.index)
            commit_success(
                self.queries,
                self.state,
                lease=lease,
                result_sha256=f"{index + 1:064x}",
                committed_at=f"2026-07-27T{index + 4:02d}:00:00Z",
            )
        self.assertEqual(indexes, [0, 1, 2, 3])
        self.assertEqual(self.lease("cycle-4").index, 0)

    def test_failure_does_not_advance_and_next_cycle_retries_same_query(self):
        first = self.lease("cycle-0")
        commit_failure(
            self.queries,
            self.state,
            lease=first,
            error="provider unavailable",
            committed_at="2026-07-27T05:00:00Z",
        )
        retry = self.lease("cycle-1")
        self.assertEqual(retry.index, first.index)
        self.assertEqual(retry.query, first.query)

    def test_restart_reads_persisted_next_index(self):
        first = self.lease("cycle-0")
        commit_success(
            self.queries,
            self.state,
            lease=first,
            result_sha256="a" * 64,
            committed_at="2026-07-27T05:00:00Z",
        )
        restarted = lease_next(
            tuple(self.queries), self.state,
            cycle_id="cycle-1", leased_at="2026-07-27T06:00:00Z",
        )
        self.assertEqual(restarted.index, 1)

    def test_exact_lease_and_success_retries_are_physical_noops(self):
        lease = self.lease("cycle-0")
        leased_bytes = self.state.read_bytes()
        replayed = self.lease("cycle-0")
        self.assertEqual(replayed, lease)
        self.assertEqual(self.state.read_bytes(), leased_bytes)

        commit_success(
            self.queries,
            self.state,
            lease=lease,
            result_sha256="b" * 64,
            committed_at="2026-07-27T05:00:00Z",
        )
        completed_bytes = self.state.read_bytes()
        commit_success(
            self.queries,
            self.state,
            lease=lease,
            result_sha256="b" * 64,
            committed_at="2026-07-27T05:00:00Z",
        )
        self.assertEqual(self.state.read_bytes(), completed_bytes)

    def test_conflicting_result_for_completed_lease_fails_closed(self):
        lease = self.lease("cycle-0")
        commit_success(
            self.queries,
            self.state,
            lease=lease,
            result_sha256="c" * 64,
            committed_at="2026-07-27T05:00:00Z",
        )
        before = self.state.read_bytes()
        with self.assertRaisesRegex(XRadarStateError, "conflicting result"):
            commit_success(
                self.queries,
                self.state,
                lease=lease,
                result_sha256="d" * 64,
                committed_at="2026-07-27T05:00:00Z",
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
            cycle_id=lease.cycle_id,
            lease_id="lease_" + "0" * 24,
            index=lease.index,
            query=lease.query,
            queries_sha256=lease.queries_sha256,
            leased_at=lease.leased_at,
        )
        before = self.state.read_bytes()
        with self.assertRaisesRegex(XRadarStateError, "lease does not match"):
            commit_success(
                self.queries,
                self.state,
                lease=forged,
                result_sha256="e" * 64,
                committed_at="2026-07-27T05:00:00Z",
            )
        self.assertEqual(self.state.read_bytes(), before)

    def test_query_configuration_drift_fails_closed(self):
        lease = self.lease("cycle-0")
        before = self.state.read_bytes()
        changed = (*self.queries[:-1], "replacement query")
        with self.assertRaisesRegex(XRadarStateError, "query configuration changed"):
            commit_failure(
                changed,
                self.state,
                lease=lease,
                error="test",
                committed_at="2026-07-27T05:00:00Z",
            )
        self.assertEqual(self.state.read_bytes(), before)

    def test_state_is_strict_json_and_contains_no_query_results(self):
        lease = self.lease("cycle-0")
        commit_success(
            self.queries,
            self.state,
            lease=lease,
            result_sha256="f" * 64,
            committed_at="2026-07-27T05:00:00Z",
        )
        value = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(value["next_index"], 1)
        self.assertNotIn("result_text", self.state.read_text(encoding="utf-8"))
        self.assertEqual(value["history"][0]["result_sha256"], "f" * 64)


if __name__ == "__main__":
    unittest.main()
