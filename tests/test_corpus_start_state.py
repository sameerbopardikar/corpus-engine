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

from corpus_start_state import (  # noqa: E402
    PHASES,
    REQUIRED_RECEIPT_PHASES,
    StartRunConflictError,
    StartRunError,
    StartRunState,
    derive_slug,
    receipt_sha256,
)

NOW = "2026-07-19T12:00:00Z"


def _later(seconds: int) -> str:
    return f"2026-07-19T12:00:{seconds:02d}Z"


class PhaseVocabularyTest(unittest.TestCase):
    def test_exact_ordered_phases(self):
        self.assertEqual(
            PHASES,
            (
                "initialized",
                "bootstrap_packet_validated",
                "domain_bootstrapped",
                "initial_acquisition_complete",
                "gbrain_verified",
                "atlas_verified",
                "scheduler_verified",
                "complete",
            ),
        )

    def test_required_receipt_phases_exclude_base_and_terminal(self):
        self.assertNotIn("initialized", REQUIRED_RECEIPT_PHASES)
        self.assertNotIn("complete", REQUIRED_RECEIPT_PHASES)
        self.assertEqual(set(REQUIRED_RECEIPT_PHASES), set(PHASES[1:-1]))


class SafeIdTest(unittest.TestCase):
    def test_derive_slug_normalizes(self):
        self.assertEqual(derive_slug("Nutrition"), "nutrition")
        self.assertEqual(derive_slug("Sports  Medicine"), "sports-medicine")

    def test_derive_slug_rejects_traversal(self):
        for bad in ["../etc", "a/b", "", "   ", "..", "/"]:
            with self.assertRaises(StartRunError):
                derive_slug(bad)


class LedgerLifecycleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_begin_creates_only_state_at_initialized(self):
        state = StartRunState.begin(self.run_root, topic="Nutrition", now=NOW)
        self.assertEqual(state.phase, "initialized")
        self.assertEqual(state.topic_input, "Nutrition")
        self.assertTrue(state.clean_root)
        self.assertIsNone(state.domain)
        self.assertIsNone(state.packet_sha256)
        marker = self.run_root / ".corpus-start" / "run.json"
        self.assertTrue(marker.exists())
        # begin creates nothing else in the run root.
        entries = {p.name for p in self.run_root.iterdir()}
        self.assertEqual(entries, {".corpus-start"})

    def test_begin_is_idempotent_for_same_topic(self):
        StartRunState.begin(self.run_root, topic="Nutrition", now=NOW)
        again = StartRunState.begin(self.run_root, topic="Nutrition", now=_later(5))
        self.assertEqual(again.phase, "initialized")

    def test_begin_rejects_conflicting_topic(self):
        StartRunState.begin(self.run_root, topic="Nutrition", now=NOW)
        with self.assertRaises(StartRunConflictError):
            StartRunState.begin(self.run_root, topic="Training", now=_later(5))

    def test_load_round_trips(self):
        StartRunState.begin(self.run_root, topic="Nutrition", now=NOW)
        loaded = StartRunState.load(self.run_root)
        self.assertEqual(loaded.topic_input, "Nutrition")

    def _advance_to(self, state, target, *, now, **kw):
        receipt = {"phase": target, "detail": target}
        return state.record_transition(target, receipt=receipt, now=now, **kw)

    def test_ordered_advance_and_receipt_binding(self):
        state = StartRunState.begin(self.run_root, topic="Nutrition", now=NOW)
        state.record_transition(
            "bootstrap_packet_validated",
            receipt={"packet": "ok"},
            now=_later(1),
            domain="nutrition",
            packet_sha256="a" * 64,
        )
        self.assertEqual(state.phase, "bootstrap_packet_validated")
        self.assertEqual(state.domain, "nutrition")
        self.assertEqual(state.packet_sha256, "a" * 64)
        self.assertIn("bootstrap_packet_validated", state.receipts)

    def test_skipping_a_phase_is_rejected(self):
        state = StartRunState.begin(self.run_root, topic="Nutrition", now=NOW)
        with self.assertRaises(StartRunError):
            self._advance_to(state, "domain_bootstrapped", now=_later(1))

    def test_backwards_transition_is_rejected(self):
        state = StartRunState.begin(self.run_root, topic="Nutrition", now=NOW)
        self._advance_to(state, "bootstrap_packet_validated", now=_later(1))
        with self.assertRaises(StartRunError):
            self._advance_to(state, "initialized", now=_later(2))

    def test_retry_same_receipt_is_physical_noop(self):
        state = StartRunState.begin(self.run_root, topic="Nutrition", now=NOW)
        receipt = {"packet": "ok"}
        state.record_transition("bootstrap_packet_validated", receipt=receipt, now=_later(1))
        marker = self.run_root / ".corpus-start" / "run.json"
        before = marker.read_bytes()
        state.record_transition("bootstrap_packet_validated", receipt=receipt, now=_later(9))
        after = marker.read_bytes()
        self.assertEqual(before, after)  # byte-level no-op

    def test_retry_with_different_receipt_conflicts(self):
        state = StartRunState.begin(self.run_root, topic="Nutrition", now=NOW)
        state.record_transition("bootstrap_packet_validated", receipt={"a": 1}, now=_later(1))
        with self.assertRaises(StartRunConflictError):
            state.record_transition("bootstrap_packet_validated", receipt={"a": 2}, now=_later(2))

    def test_conflicting_packet_sha_is_rejected(self):
        state = StartRunState.begin(self.run_root, topic="Nutrition", now=NOW)
        state.record_transition(
            "bootstrap_packet_validated", receipt={"p": 1}, now=_later(1),
            domain="nutrition", packet_sha256="a" * 64,
        )
        # advancing further while asserting a different packet sha must fail.
        with self.assertRaises(StartRunConflictError):
            state.record_transition(
                "domain_bootstrapped", receipt={"d": 1}, now=_later(2),
                packet_sha256="b" * 64,
            )

    def test_complete_forbidden_without_all_receipts(self):
        state = StartRunState.begin(self.run_root, topic="Nutrition", now=NOW)
        self._advance_to(state, "bootstrap_packet_validated", now=_later(1))
        # jump attempt straight to complete is rejected as a skip anyway;
        # but even reaching scheduler with a missing prior receipt must not
        # allow complete. Force the guard directly:
        with self.assertRaises(StartRunError):
            state.assert_completable()

    def test_full_walk_to_complete(self):
        state = StartRunState.begin(self.run_root, topic="Nutrition", now=NOW)
        now = 1
        for phase in REQUIRED_RECEIPT_PHASES:
            self._advance_to(state, phase, now=_later(now))
            now += 1
        state.assert_completable()
        state.record_transition("complete", receipt={"done": True}, now=_later(now))
        self.assertEqual(state.phase, "complete")

    def test_atomic_write_leaves_no_temp(self):
        state = StartRunState.begin(self.run_root, topic="Nutrition", now=NOW)
        self._advance_to(state, "bootstrap_packet_validated", now=_later(1))
        temps = list((self.run_root / ".corpus-start").glob("*.tmp"))
        self.assertEqual(temps, [])

    def test_state_file_is_valid_json(self):
        StartRunState.begin(self.run_root, topic="Nutrition", now=NOW)
        marker = self.run_root / ".corpus-start" / "run.json"
        data = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(data["phase"], "initialized")


class ReceiptDigestTest(unittest.TestCase):
    def test_receipt_sha256_is_stable_and_order_independent(self):
        a = receipt_sha256({"x": 1, "y": 2})
        b = receipt_sha256({"y": 2, "x": 1})
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)


if __name__ == "__main__":
    unittest.main()
