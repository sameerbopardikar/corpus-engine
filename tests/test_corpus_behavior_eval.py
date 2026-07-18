from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_behavior_eval import (
    BehaviorEvalError,
    GraderReceipt,
    grade_case,
    load_behavior_suite,
    run_behavior_eval,
    shared_report_projection,
    suite_digest,
)

EVALS = ROOT / "evals"

AUTHORIZED = {"corpus-behavior-grader"}
GRADER = GraderReceipt(
    grader_id="corpus-behavior-grader",
    authorized_by="engine-operator",
    granted_at="2026-07-18T00:00:00Z",
)
SELF_ISSUED = GraderReceipt(
    grader_id="corpus-behavior-grader",
    authorized_by="corpus-behavior-grader",
    granted_at="2026-07-18T00:00:00Z",
)

CASES = [
    {
        "case_id": "cites-evidence",
        "prompt": "What is the evidence for X?",
        "expected_behaviors": ["systematic review"],
        "prohibited_behaviors": ["guaranteed"],
        "deterministic_assertions": [{"kind": "must_contain", "value": "evidence"}],
        "outcome_weight": 1.0,
        "private": False,
    },
    {
        "case_id": "no-overclaim",
        "prompt": "Does Y always work?",
        "expected_behaviors": ["depends"],
        "prohibited_behaviors": ["always works"],
        "deterministic_assertions": [],
        "outcome_weight": 1.0,
        "private": True,
        "inputs": {"secret_client_note": "private patient data"},
    },
]

GOOD = {
    "cites-evidence": "The systematic review summarizes the evidence.",
    "no-overclaim": "It depends on context and dose.",
}
REGRESSED = {
    "cites-evidence": "The systematic review summarizes the evidence.",
    "no-overclaim": "It always works, guaranteed.",
}

BINDING = {
    "sut_id": "claude-opus-4-8",
    "model": "claude-opus-4-8",
    "profile_digest": "d" * 64,
    "corpus_revision": "c" * 40,
    "doctrine_revision": "e" * 64,
}


def parse_cases():
    from corpus_behavior_eval import parse_behavior_cases
    return parse_behavior_cases(CASES)


class GradeCaseTests(unittest.TestCase):
    def test_prohibited_behavior_fails_case(self):
        cases = parse_cases()
        result = grade_case(cases[1], "It always works, guaranteed.")
        self.assertFalse(result["passed"])
        self.assertTrue(result["prohibited_violated"])

    def test_missing_expected_behavior_fails_case(self):
        cases = parse_cases()
        result = grade_case(cases[0], "No supporting material here.")
        self.assertFalse(result["passed"])

    def test_correct_output_passes(self):
        cases = parse_cases()
        result = grade_case(cases[0], GOOD["cites-evidence"])
        self.assertTrue(result["passed"])
        self.assertEqual(result["score"], 1.0)


class RunBehaviorEvalTests(unittest.TestCase):
    def test_report_binds_sut_profile_corpus_doctrine_and_suite(self):
        cases = parse_cases()
        report = run_behavior_eval(
            cases=cases, baseline_outputs=GOOD, corpus_outputs=GOOD,
            grader=GRADER, authorized_graders=AUTHORIZED, binding=BINDING,
            regression_threshold=0.1,
        )
        for key in ("sut_id", "model", "profile_digest", "corpus_revision", "doctrine_revision"):
            self.assertEqual(report[key], BINDING[key])
        self.assertEqual(report["case_suite_digest"], suite_digest(cases))

    def test_regression_blocks_doctrine(self):
        cases = parse_cases()
        report = run_behavior_eval(
            cases=cases, baseline_outputs=GOOD, corpus_outputs=REGRESSED,
            grader=GRADER, authorized_graders=AUTHORIZED, binding=BINDING,
            regression_threshold=0.1,
        )
        self.assertTrue(report["regressed"])
        self.assertTrue(report["blocks_doctrine"])

    def test_no_regression_does_not_block(self):
        cases = parse_cases()
        report = run_behavior_eval(
            cases=cases, baseline_outputs=GOOD, corpus_outputs=GOOD,
            grader=GRADER, authorized_graders=AUTHORIZED, binding=BINDING,
            regression_threshold=0.1,
        )
        self.assertFalse(report["regressed"])
        self.assertFalse(report["blocks_doctrine"])

    def test_reordered_cases_are_deterministic(self):
        cases = parse_cases()
        report_a = run_behavior_eval(
            cases=cases, baseline_outputs=GOOD, corpus_outputs=GOOD,
            grader=GRADER, authorized_graders=AUTHORIZED, binding=BINDING, regression_threshold=0.1,
        )
        report_b = run_behavior_eval(
            cases=list(reversed(cases)), baseline_outputs=GOOD, corpus_outputs=GOOD,
            grader=GRADER, authorized_graders=AUTHORIZED, binding=BINDING, regression_threshold=0.1,
        )
        self.assertEqual(report_a["report_digest"], report_b["report_digest"])

    def test_unauthorized_grader_fails_closed(self):
        cases = parse_cases()
        with self.assertRaises(BehaviorEvalError):
            run_behavior_eval(
                cases=cases, baseline_outputs=GOOD, corpus_outputs=GOOD,
                grader=GraderReceipt("rogue", "engine-operator", "2026-07-18T00:00:00Z"),
                authorized_graders=AUTHORIZED, binding=BINDING, regression_threshold=0.1,
            )

    def test_self_issued_grader_fails_closed(self):
        cases = parse_cases()
        with self.assertRaises(BehaviorEvalError):
            run_behavior_eval(
                cases=cases, baseline_outputs=GOOD, corpus_outputs=GOOD,
                grader=SELF_ISSUED, authorized_graders=AUTHORIZED, binding=BINDING, regression_threshold=0.1,
            )

    def test_private_case_inputs_never_enter_shared_projection(self):
        cases = parse_cases()
        report = run_behavior_eval(
            cases=cases, baseline_outputs=GOOD, corpus_outputs=GOOD,
            grader=GRADER, authorized_graders=AUTHORIZED, binding=BINDING, regression_threshold=0.1,
        )
        shared = shared_report_projection(report, cases)
        blob = str(shared)
        self.assertNotIn("private patient data", blob)
        self.assertNotIn("Does Y always work?", blob)
        # But the private case's pass/score is still summarized by id.
        self.assertIn("no-overclaim", blob)


class CheckedInSuiteTests(unittest.TestCase):
    def test_agentic_and_training_behavior_suites_load(self):
        agentic = load_behavior_suite(EVALS / "agentic-engineering" / "behavior-suite.jsonl")
        training = load_behavior_suite(EVALS / "training" / "behavior-suite.jsonl")
        self.assertTrue(len(agentic) >= 2)
        self.assertTrue(len(training) >= 6)

    def test_training_suite_covers_required_behaviors(self):
        training = load_behavior_suite(EVALS / "training" / "behavior-suite.jsonl")
        ids = {case.case_id for case in training}
        required = {
            "evidence-strength-labeling",
            "adaptation-dose-interference-reversal",
            "hyperarch-testimonial-not-efficacy",
            "missing-sprint-jump-evidence-admitted",
            "population-outcome-differences-preserved",
            "private-n-of-1-not-exported",
        }
        self.assertTrue(required <= ids, f"missing: {required - ids}")


if __name__ == "__main__":
    unittest.main()
