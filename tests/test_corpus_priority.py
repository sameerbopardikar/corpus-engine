from __future__ import annotations

import json
import multiprocessing
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_engine_models import CandidateObservation, CandidateRecord, SCORE_COMPONENTS
from corpus_priority import BudgetLedger, CandidateTask, PriorityPolicy, UsageSnapshot, plan_cycle

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)


def candidate(
    name: str,
    *,
    rights_state: str = "public_rights_clear",
    lane: str = "scientific-evaluation",
    first_seen_at: str = "2026-07-17T10:00:00Z",
    scores: dict[str, float] | None = None,
) -> CandidateRecord:
    values = {key: 0.7 for key in SCORE_COMPONENTS}
    values["cost"] = 0.2
    if scores:
        values.update(scores)
    observation = CandidateObservation.create(
        domain="agentic-engineering",
        entity_type="document",
        canonical_url=f"https://example.com/{name}",
        discovery_source="test",
        evidence_pointer=f"fixture:{name}",
        evidence_lane=lane,
        topics=("verification",),
        observed_at=first_seen_at,
    )
    return CandidateRecord.from_observation(
        observation,
        values,
        rights_state=rights_state,
    )


def reserve_full_budget(args: tuple[str, str]) -> int:
    ledger_path, reservation_id = args
    task = CandidateTask(
        candidate(reservation_id),
        requires_llm=True,
        estimated_llm_tokens=50_000,
        estimated_cost_usd=1.0,
    )
    plan = BudgetLedger(Path(ledger_path)).reserve_cycle(
        reservation_id,
        [task],
        PriorityPolicy.default(),
        now=NOW,
    )
    return len(plan.selected)


class CorpusPriorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PriorityPolicy.default()

    def test_policy_round_trip_is_strict_and_complete(self):
        checked_in = PriorityPolicy.from_file(ROOT / "config" / "agentic-engineering-policy.json")
        self.assertEqual(checked_in, self.policy)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "policy.json"
            path.write_text(json.dumps(self.policy.to_dict()), encoding="utf-8")
            self.assertEqual(PriorityPolicy.from_file(path), self.policy)
            document = self.policy.to_dict()
            document["unexpected"] = True
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown policy fields"):
                PriorityPolicy.from_file(path)

            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                PriorityPolicy.from_file(path)

            document = self.policy.to_dict()
            document["score_weights"]["relevence"] = 1.0
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown score_weights"):
                PriorityPolicy.from_file(path)

        missing_default = self.policy.to_dict()
        del missing_default["evidence_lane_weights"]["default"]
        with self.assertRaisesRegex(ValueError, "default"):
            PriorityPolicy(**missing_default)
        zero_weights = self.policy.to_dict()
        zero_weights["score_weights"] = {key: 0.0 for key in zero_weights["score_weights"]}
        with self.assertRaisesRegex(ValueError, "positive total"):
            PriorityPolicy(**zero_weights)

    def test_llm_task_requires_a_positive_token_estimate(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            CandidateTask(candidate("llm-unbounded"), requires_llm=True, estimated_llm_tokens=0)

    def test_ranking_is_deterministic_under_reordered_input(self):
        tasks = [CandidateTask(candidate("zeta")), CandidateTask(candidate("alpha")), CandidateTask(candidate("mu"))]
        forward = plan_cycle(tasks, self.policy, UsageSnapshot(), now=NOW)
        reverse = plan_cycle(reversed(tasks), self.policy, UsageSnapshot(), now=NOW)
        self.assertEqual(
            [item.candidate_id for item in forward.decisions],
            [item.candidate_id for item in reverse.decisions],
        )
        self.assertEqual(forward.to_dict(), reverse.to_dict())

    def test_rights_unclear_and_private_are_never_automatically_eligible(self):
        tasks = [
            CandidateTask(candidate("clear", rights_state="public_rights_clear")),
            CandidateTask(candidate("unclear", rights_state="rights_unclear")),
            CandidateTask(candidate("private", rights_state="private_authorized")),
            CandidateTask(candidate("metadata", rights_state="public_metadata_only")),
        ]
        plan = plan_cycle(tasks, self.policy, UsageSnapshot(), now=NOW)
        by_url = {item.canonical_url: item for item in plan.decisions}
        self.assertTrue(by_url["https://example.com/clear"].auto_acquire_eligible)
        self.assertFalse(by_url["https://example.com/unclear"].scheduled)
        self.assertFalse(by_url["https://example.com/private"].scheduled)
        self.assertIn("rights_not_publicly_acquirable", by_url["https://example.com/unclear"].reason_codes)
        self.assertIn("private_source_human_gate", by_url["https://example.com/private"].reason_codes)
        self.assertEqual(by_url["https://example.com/metadata"].action, "inspect")
        self.assertFalse(by_url["https://example.com/metadata"].auto_acquire_eligible)

    def test_no_material_delta_schedules_no_llm_bearing_work(self):
        task = CandidateTask(candidate("same"), material_delta=False, requires_llm=True, estimated_llm_tokens=500)
        plan = plan_cycle([task], self.policy, UsageSnapshot(), now=NOW)
        self.assertEqual(plan.selected, ())
        self.assertEqual(plan.llm_tasks_scheduled, 0)
        self.assertIn("no_material_delta", plan.decisions[0].reason_codes)

    def test_explicit_daily_and_cycle_caps_fail_closed(self):
        tasks = [
            CandidateTask(candidate(f"c{index}"), requires_llm=True, estimated_llm_tokens=20_000, estimated_cost_usd=0.40)
            for index in range(5)
        ]
        usage = UsageSnapshot(deep_acquisitions=2, llm_tasks=0, llm_tokens=30_000, estimated_cost_usd=0.50)
        plan = plan_cycle(tasks, self.policy, usage, now=NOW)
        self.assertLessEqual(len(plan.selected), self.policy.max_items_per_cycle)
        self.assertLessEqual(plan.deep_acquisitions_scheduled, 1)
        self.assertLessEqual(plan.llm_tasks_scheduled, self.policy.max_llm_tasks_per_cycle)
        self.assertLessEqual(plan.llm_tokens_scheduled + usage.llm_tokens, self.policy.max_llm_tokens_per_utc_day)
        self.assertLessEqual(plan.estimated_cost_usd + usage.estimated_cost_usd, self.policy.max_cost_usd_per_utc_day)
        self.assertTrue(any("daily_deep_acquisition_cap" in item.reason_codes for item in plan.decisions))

    def test_retry_backoff_defers_failures_and_then_releases_them(self):
        task = CandidateTask(
            candidate("retry"),
            failure_count=3,
            last_attempted_at="2026-07-17T11:50:00Z",
        )
        deferred = plan_cycle([task], self.policy, UsageSnapshot(), now=NOW)
        self.assertFalse(deferred.decisions[0].scheduled)
        self.assertEqual(deferred.decisions[0].retry_after, "2026-07-17T12:10:00Z")
        released = plan_cycle([task], self.policy, UsageSnapshot(), now=datetime(2026, 7, 17, 12, 11, tzinfo=timezone.utc))
        self.assertTrue(released.decisions[0].scheduled)

    def test_starvation_age_boost_can_overtake_a_slightly_higher_new_candidate(self):
        old = CandidateTask(candidate("old", first_seen_at="2026-07-07T12:00:00Z", scores={"novelty": 0.66}))
        new = CandidateTask(candidate("new", scores={"novelty": 0.70}))
        plan = plan_cycle([new, old], self.policy, UsageSnapshot(), now=NOW)
        self.assertEqual(plan.selected[0].canonical_url, "https://example.com/old")
        self.assertGreater(plan.selected[0].starvation_boost, 0)

    def test_starvation_and_low_cost_cannot_manufacture_promotion_quality(self):
        low_scores = {key: 0.40 for key in SCORE_COMPONENTS}
        low_scores["cost"] = 0.01
        old = CandidateTask(candidate("old-low-quality", first_seen_at="2025-07-17T12:00:00Z", scores=low_scores))
        decision = plan_cycle([old], self.policy, UsageSnapshot(), now=NOW).decisions[0]
        self.assertGreater(decision.priority_score, self.policy.promotion_threshold)
        self.assertLess(decision.evidence_score, self.policy.promotion_threshold)
        self.assertEqual(decision.suggested_disposition, "probationary")

    def test_expensive_candidate_does_not_starve_lower_cost_high_yield_work(self):
        expensive = CandidateTask(candidate("expensive"), estimated_cost_usd=0.95)
        efficient = CandidateTask(candidate("efficient"), estimated_cost_usd=0.05)
        plan = plan_cycle([expensive, efficient], self.policy, UsageSnapshot(), now=NOW)
        self.assertEqual(plan.selected[0].canonical_url, "https://example.com/efficient")

    def test_reserved_aging_slot_guarantees_eventual_service(self):
        policy_data = self.policy.to_dict()
        policy_data["max_items_per_cycle"] = 2
        policy = PriorityPolicy(**policy_data)
        old_scores = {key: 0.01 for key in SCORE_COMPONENTS}
        old_scores["cost"] = 0.01
        old = CandidateTask(candidate("old-forced", first_seen_at="2026-06-01T00:00:00Z", scores=old_scores))
        fresh = [
            CandidateTask(candidate(f"fresh-{index}", scores={"novelty": 0.99}))
            for index in range(4)
        ]
        plan = plan_cycle(fresh + [old], policy, UsageSnapshot(), now=NOW)
        self.assertIn(old.candidate.candidate_id, {item.candidate_id for item in plan.selected})
        by_id = {item.candidate_id: item for item in plan.decisions}
        self.assertIn("reserved_aging_slot", by_id[old.candidate.candidate_id].reason_codes)

    def test_daily_budget_is_atomically_reserved_across_processes(self):
        with tempfile.TemporaryDirectory() as td:
            ledger_path = str(Path(td) / "budget.json")
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(2) as pool:
                admitted = pool.map(
                    reserve_full_budget,
                    [(ledger_path, "cycle-a"), (ledger_path, "cycle-b")],
                )
            self.assertEqual(sum(admitted), 1)
            usage = BudgetLedger(Path(ledger_path)).usage_for_day(NOW)
            self.assertEqual(usage.deep_acquisitions, 1)
            self.assertEqual(usage.llm_tasks, 1)
            self.assertEqual(usage.llm_tokens, 50_000)
            self.assertEqual(usage.estimated_cost_usd, 1.0)

    def test_budget_reservation_is_idempotent_and_conflicts_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "budget.json"
            ledger = BudgetLedger(path)
            task = CandidateTask(candidate("same"), estimated_cost_usd=0.25)
            first = ledger.reserve_cycle("cycle-one", [task], self.policy, now=NOW)
            before = path.read_bytes()
            before_stat = path.stat()
            second = ledger.reserve_cycle("cycle-one", [task], self.policy, now=NOW)
            self.assertEqual(first.to_dict(), second.to_dict())
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(
                (path.stat().st_ino, path.stat().st_mtime_ns, path.stat().st_size),
                (before_stat.st_ino, before_stat.st_mtime_ns, before_stat.st_size),
            )
            conflicting = CandidateTask(candidate("different"), estimated_cost_usd=0.25)
            with self.assertRaisesRegex(ValueError, "conflicting reservation_id"):
                ledger.reserve_cycle("cycle-one", [conflicting], self.policy, now=NOW)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(
                (path.stat().st_ino, path.stat().st_mtime_ns, path.stat().st_size),
                (before_stat.st_ino, before_stat.st_mtime_ns, before_stat.st_size),
            )

    def test_budget_ledger_rejects_forged_accounting(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "budget.json"
            ledger = BudgetLedger(path)
            task = CandidateTask(
                candidate("metered"),
                requires_llm=True,
                estimated_llm_tokens=50_000,
                estimated_cost_usd=1.0,
            )
            ledger.reserve_cycle("cycle-one", [task], self.policy, now=NOW)
            document = json.loads(path.read_text(encoding="utf-8"))
            document["reservations"]["cycle-one"]["plan"]["llm_tokens_scheduled"] = 0
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "inconsistent stored cycle plan"):
                ledger.usage_for_day(NOW)


if __name__ == "__main__":
    unittest.main()
