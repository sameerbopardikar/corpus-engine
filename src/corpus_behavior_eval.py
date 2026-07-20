#!/usr/bin/env python3
"""Deterministic behavioral evaluation for the generalized Corpus Engine.

Where :mod:`corpus_eval` measures corpus integrity and retrieval recall, this
module measures *behavior*: does an evidence-assisted system-under-test produce
the expected behavior, avoid prohibited behavior, and not regress against a
baseline? A behavior suite is a set of :class:`BehaviorCase`s with expected and
prohibited behaviors plus deterministic assertions. A report binds the exact
SUT/model, feedback-profile digest, corpus revision, doctrine revision, and case
suite digest, so a doctrine event can be hash-bound to the behavior evidence
that justified it.

Everything here is deterministic and LLM-free: outputs are graded by string-
level assertions, results are order-independent, only an authorized (never self-
issued) grader may sign a report, and private case inputs never enter a shared
projection.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CASE_FIELDS = {
    "case_id", "prompt", "expected_behaviors", "prohibited_behaviors",
    "deterministic_assertions", "outcome_weight", "private",
}
_OPTIONAL_CASE_FIELDS = {"inputs"}
_ASSERTION_KINDS = {"must_contain", "must_not_contain"}


class BehaviorEvalError(ValueError):
    """Raised when a behavior suite, grader, or run is malformed or unsafe."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BehaviorEvalError(f"{field} must be a non-empty string")
    return value.strip()


def _str_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise BehaviorEvalError(f"{field} must be a list")
    return tuple(_text(item, f"{field} item") for item in value)


@dataclass(frozen=True)
class BehaviorCase:
    case_id: str
    prompt: str
    expected_behaviors: tuple[str, ...]
    prohibited_behaviors: tuple[str, ...]
    deterministic_assertions: tuple[dict[str, str], ...]
    outcome_weight: float
    private: bool
    inputs: dict[str, Any]


@dataclass(frozen=True)
class GraderReceipt:
    grader_id: str
    authorized_by: str
    granted_at: str


def parse_behavior_cases(raw_cases: list[dict[str, Any]]) -> list[BehaviorCase]:
    if not isinstance(raw_cases, list) or not raw_cases:
        raise BehaviorEvalError("behavior suite must be a non-empty list")
    cases: list[BehaviorCase] = []
    seen: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise BehaviorEvalError("behavior case must be an object")
        unknown = set(raw) - _CASE_FIELDS - _OPTIONAL_CASE_FIELDS
        missing = _CASE_FIELDS - set(raw)
        if unknown or missing:
            raise BehaviorEvalError(f"behavior case fields mismatch: missing={sorted(missing)} unknown={sorted(unknown)}")
        assertions: list[dict[str, str]] = []
        for entry in raw["deterministic_assertions"]:
            if not isinstance(entry, dict) or set(entry) != {"kind", "value"}:
                raise BehaviorEvalError("deterministic assertion must be {kind, value}")
            kind = _text(entry["kind"], "assertion kind")
            if kind not in _ASSERTION_KINDS:
                raise BehaviorEvalError(f"unknown assertion kind: {kind!r}")
            assertions.append({"kind": kind, "value": _text(entry["value"], "assertion value")})
        weight = raw["outcome_weight"]
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or float(weight) <= 0:
            raise BehaviorEvalError("outcome_weight must be a positive number")
        private = raw["private"]
        if not isinstance(private, bool):
            raise BehaviorEvalError("private must be a boolean")
        inputs = raw.get("inputs", {})
        if not isinstance(inputs, dict):
            raise BehaviorEvalError("inputs must be an object")
        case_id = _text(raw["case_id"], "case_id")
        if case_id in seen:
            raise BehaviorEvalError(f"duplicate case_id: {case_id}")
        seen.add(case_id)
        cases.append(
            BehaviorCase(
                case_id=case_id,
                prompt=_text(raw["prompt"], "prompt"),
                expected_behaviors=_str_tuple(raw["expected_behaviors"], "expected_behaviors"),
                prohibited_behaviors=_str_tuple(raw["prohibited_behaviors"], "prohibited_behaviors"),
                deterministic_assertions=tuple(assertions),
                outcome_weight=float(weight),
                private=private,
                inputs=inputs,
            )
        )
    return cases


def load_behavior_suite(path: Path | str) -> list[BehaviorCase]:
    suite_path = Path(path)
    raw_cases: list[dict[str, Any]] = []
    try:
        for line in suite_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                raw_cases.append(json.loads(line))
    except (OSError, json.JSONDecodeError) as exc:
        raise BehaviorEvalError(f"cannot read behavior suite {suite_path}: {exc}") from exc
    return parse_behavior_cases(raw_cases)


def _case_identity(case: BehaviorCase) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "prompt": case.prompt,
        "expected_behaviors": list(case.expected_behaviors),
        "prohibited_behaviors": list(case.prohibited_behaviors),
        "deterministic_assertions": [dict(a) for a in case.deterministic_assertions],
        "outcome_weight": case.outcome_weight,
        "private": case.private,
    }


def suite_digest(cases: list[BehaviorCase]) -> str:
    """Order-independent digest of a case suite's content."""
    identities = sorted((_case_identity(case) for case in cases), key=lambda item: item["case_id"])
    blob = json.dumps(identities, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def grade_case(case: BehaviorCase, output_text: str) -> dict[str, Any]:
    """Grade one output against a case's expected/prohibited/assertion rules."""
    if not isinstance(output_text, str):
        raise BehaviorEvalError("output_text must be a string")
    lowered = output_text.lower()
    expected_hits = [b for b in case.expected_behaviors if b.lower() in lowered]
    expected_fraction = len(expected_hits) / len(case.expected_behaviors) if case.expected_behaviors else 1.0
    prohibited_violated = any(b.lower() in lowered for b in case.prohibited_behaviors)
    assertions_passed = True
    for assertion in case.deterministic_assertions:
        present = assertion["value"].lower() in lowered
        if assertion["kind"] == "must_contain" and not present:
            assertions_passed = False
        if assertion["kind"] == "must_not_contain" and present:
            assertions_passed = False
    passed = expected_fraction == 1.0 and not prohibited_violated and assertions_passed
    score = expected_fraction if (not prohibited_violated and assertions_passed) else 0.0
    return {
        "case_id": case.case_id,
        "passed": passed,
        "score": round(score, 8),
        "expected_fraction": round(expected_fraction, 8),
        "prohibited_violated": prohibited_violated,
        "assertions_passed": assertions_passed,
    }


def verify_grader(grader: GraderReceipt, authorized_graders: set[str]) -> None:
    """Fail closed unless the grader is authorized and not self-issued."""
    if not isinstance(grader, GraderReceipt):
        raise BehaviorEvalError("grader must be a GraderReceipt")
    _text(grader.grader_id, "grader_id")
    _text(grader.authorized_by, "authorized_by")
    _text(grader.granted_at, "granted_at")
    if grader.grader_id not in authorized_graders:
        raise BehaviorEvalError(f"unauthorized grader: {grader.grader_id!r}")
    if grader.authorized_by == grader.grader_id:
        raise BehaviorEvalError("grader may not self-issue its own authorization")


def _weighted_mean(cases: list[BehaviorCase], graded: dict[str, dict[str, Any]]) -> float:
    total_weight = sum(case.outcome_weight for case in cases)
    if total_weight == 0:
        return 0.0
    return sum(case.outcome_weight * graded[case.case_id]["score"] for case in cases) / total_weight


def run_behavior_eval(
    *,
    cases: list[BehaviorCase],
    baseline_outputs: dict[str, str],
    corpus_outputs: dict[str, str],
    grader: GraderReceipt,
    authorized_graders: set[str],
    binding: dict[str, Any],
    regression_threshold: float,
) -> dict[str, Any]:
    """Compare baseline vs corpus-assisted behavior and gate on regression."""
    verify_grader(grader, authorized_graders)
    for key in ("sut_id", "model", "profile_digest", "corpus_revision", "doctrine_revision"):
        _text(binding.get(key), f"binding.{key}")
    ordered = sorted(cases, key=lambda case: case.case_id)

    baseline_graded: dict[str, dict[str, Any]] = {}
    corpus_graded: dict[str, dict[str, Any]] = {}
    comparisons: list[dict[str, Any]] = []
    for case in ordered:
        if case.case_id not in baseline_outputs or case.case_id not in corpus_outputs:
            raise BehaviorEvalError(f"missing run output for case {case.case_id!r}")
        baseline = grade_case(case, baseline_outputs[case.case_id])
        corpus = grade_case(case, corpus_outputs[case.case_id])
        baseline_graded[case.case_id] = baseline
        corpus_graded[case.case_id] = corpus
        comparisons.append(
            {
                "case_id": case.case_id,
                "private": case.private,
                "baseline_score": baseline["score"],
                "corpus_score": corpus["score"],
                "baseline_passed": baseline["passed"],
                "corpus_passed": corpus["passed"],
                "prohibited_violated": corpus["prohibited_violated"],
            }
        )

    baseline_mean = _weighted_mean(ordered, baseline_graded)
    corpus_mean = _weighted_mean(ordered, corpus_graded)
    regressed = corpus_mean < baseline_mean - float(regression_threshold)

    report = {
        "schema_version": 1,
        "sut_id": binding["sut_id"],
        "model": binding["model"],
        "profile_digest": binding["profile_digest"],
        "corpus_revision": binding["corpus_revision"],
        "doctrine_revision": binding["doctrine_revision"],
        "case_suite_digest": suite_digest(ordered),
        "regression_threshold": float(regression_threshold),
        "baseline_score": round(baseline_mean, 8),
        "corpus_score": round(corpus_mean, 8),
        "regressed": regressed,
        "blocks_doctrine": regressed,
        "grader": {
            "grader_id": grader.grader_id,
            "authorized_by": grader.authorized_by,
            "granted_at": grader.granted_at,
        },
        "comparisons": comparisons,
    }
    report["report_digest"] = _report_digest(report)
    return report


def _report_digest(report: dict[str, Any]) -> str:
    payload = {key: value for key, value in report.items() if key != "report_digest"}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def shared_report_projection(report: dict[str, Any], cases: list[BehaviorCase]) -> dict[str, Any]:
    """Strip private case prompts/inputs; keep only id + pass/score summary."""
    private_ids = {case.case_id for case in cases if case.private}
    projected = dict(report)
    projected["comparisons"] = [
        {
            "case_id": comp["case_id"],
            "corpus_passed": comp["corpus_passed"],
            "corpus_score": comp["corpus_score"],
        }
        if comp["case_id"] in private_ids
        else comp
        for comp in report["comparisons"]
    ]
    return projected
