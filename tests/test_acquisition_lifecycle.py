import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_acquisition_lifecycle import (
    ACQUISITION_STATES,
    TERMINAL_STATES,
    AcquisitionLifecycle,
    AcquisitionState,
    LifecycleError,
    validate_transition,
)


class StateVocabularyTests(unittest.TestCase):
    def test_canonical_states_present(self):
        expected = {
            "discovered", "locator_verified", "rights_resolved", "queued",
            "acquiring", "raw_preserved", "normalized", "staged", "evaluated",
            "probationary", "rejected", "quarantined",
        }
        self.assertEqual({s.value for s in ACQUISITION_STATES}, expected)

    def test_terminal_states(self):
        self.assertEqual(
            {s.value for s in TERMINAL_STATES},
            {"probationary", "rejected", "quarantined"},
        )


class TransitionTests(unittest.TestCase):
    def test_full_happy_path_is_valid(self):
        path = [
            AcquisitionState.DISCOVERED,
            AcquisitionState.LOCATOR_VERIFIED,
            AcquisitionState.RIGHTS_RESOLVED,
            AcquisitionState.QUEUED,
            AcquisitionState.ACQUIRING,
            AcquisitionState.RAW_PRESERVED,
            AcquisitionState.NORMALIZED,
            AcquisitionState.STAGED,
            AcquisitionState.EVALUATED,
            AcquisitionState.PROBATIONARY,
        ]
        for current, new in zip(path, path[1:]):
            validate_transition(current, new)  # must not raise

    def test_cannot_skip_locator_verification(self):
        with self.assertRaises(LifecycleError):
            validate_transition(AcquisitionState.DISCOVERED, AcquisitionState.RIGHTS_RESOLVED)

    def test_cannot_queue_without_rights_resolution(self):
        with self.assertRaises(LifecycleError):
            validate_transition(AcquisitionState.LOCATOR_VERIFIED, AcquisitionState.QUEUED)

    def test_cannot_acquire_content_without_queue(self):
        with self.assertRaises(LifecycleError):
            validate_transition(AcquisitionState.RIGHTS_RESOLVED, AcquisitionState.ACQUIRING)

    def test_terminal_state_cannot_transition(self):
        for terminal in TERMINAL_STATES:
            with self.assertRaises(LifecycleError):
                validate_transition(terminal, AcquisitionState.DISCOVERED)

    def test_staged_can_only_become_evaluated_or_quarantined(self):
        validate_transition(AcquisitionState.STAGED, AcquisitionState.EVALUATED)
        validate_transition(AcquisitionState.STAGED, AcquisitionState.QUARANTINED)
        with self.assertRaises(LifecycleError):
            validate_transition(AcquisitionState.STAGED, AcquisitionState.PROBATIONARY)

    def test_rights_resolved_may_reject_for_non_acquirable(self):
        validate_transition(AcquisitionState.RIGHTS_RESOLVED, AcquisitionState.REJECTED)

    def test_evaluation_failure_quarantines(self):
        validate_transition(AcquisitionState.EVALUATED, AcquisitionState.QUARANTINED)


class LifecycleTrackerTests(unittest.TestCase):
    def test_advance_records_history(self):
        lifecycle = AcquisitionLifecycle()
        self.assertEqual(lifecycle.state, AcquisitionState.DISCOVERED)
        lifecycle.advance(AcquisitionState.LOCATOR_VERIFIED)
        lifecycle.advance(AcquisitionState.RIGHTS_RESOLVED)
        self.assertEqual(
            lifecycle.history,
            ("discovered", "locator_verified", "rights_resolved"),
        )

    def test_illegal_advance_raises_and_does_not_mutate(self):
        lifecycle = AcquisitionLifecycle()
        with self.assertRaises(LifecycleError):
            lifecycle.advance(AcquisitionState.STAGED)
        self.assertEqual(lifecycle.state, AcquisitionState.DISCOVERED)
        self.assertEqual(lifecycle.history, ("discovered",))

    def test_cannot_advance_from_terminal(self):
        lifecycle = AcquisitionLifecycle()
        lifecycle.advance(AcquisitionState.LOCATOR_VERIFIED)
        lifecycle.advance(AcquisitionState.RIGHTS_RESOLVED)
        lifecycle.advance(AcquisitionState.REJECTED)
        self.assertTrue(lifecycle.is_terminal)
        with self.assertRaises(LifecycleError):
            lifecycle.advance(AcquisitionState.QUEUED)


if __name__ == "__main__":
    unittest.main()
