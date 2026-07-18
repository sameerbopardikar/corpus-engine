from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import corpus_priority
from corpus_global_cycle import GlobalCycleError, run_global_cycle
from corpus_priority import CandidateTask, PriorityPolicy

# Some sibling test modules reload corpus_engine_models under the same name into
# sys.modules, so a plain `from corpus_engine_models import ...` here can bind a
# DIFFERENT class object than the one corpus_priority validates against. Source
# every model symbol from the exact module instance corpus_priority uses.
CandidateRecord = corpus_priority.CandidateRecord
SCORE_COMPONENTS = corpus_priority.SCORE_COMPONENTS
CandidateObservation = CandidateRecord.from_observation.__func__.__globals__["CandidateObservation"]

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


def _task(domain, name, *, score=0.7, first_seen="2026-07-17T10:00:00Z", rights="public_rights_clear", lane="scientific-evaluation"):
    values = {key: score for key in SCORE_COMPONENTS}
    values["cost"] = 0.2
    observation = CandidateObservation.create(
        domain=domain, entity_type="document",
        canonical_url=f"https://example.com/{domain}/{name}",
        discovery_source="test", evidence_pointer=f"fixture:{domain}:{name}",
        evidence_lane=lane, topics=("verification",), observed_at=first_seen,
    )
    record = CandidateRecord.from_observation(observation, values, rights_state=rights)
    return CandidateTask(record, material_delta=True)


class GlobalRankingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.budget = Path(self.tempdir.name) / "_engine" / "budget.json"
        self.policy = PriorityPolicy.default()

    def test_candidates_from_both_domains_compete_in_one_ranking(self):
        loaders = [
            ("agentic-engineering", lambda: [_task("agentic-engineering", "low", score=0.2)]),
            ("training", lambda: [_task("training", "high", score=0.95)]),
        ]
        result = run_global_cycle(
            reservation_id="global-1", domain_loaders=loaders, policy=self.policy,
            budget_ledger_path=self.budget, now=NOW,
        )
        decisions = result["plan"].decisions
        # The higher-scored training candidate outranks the agentic one in ONE plan.
        top = decisions[0].candidate_id
        self.assertEqual(result["candidate_domains"][top], "training")

    def test_one_global_budget_cannot_be_spent_once_per_vertical(self):
        import dataclasses
        policy = dataclasses.replace(self.policy, max_deep_acquisitions_per_utc_day=1)
        loaders = [
            ("agentic-engineering", lambda: [_task("agentic-engineering", "a", score=0.9)]),
            ("training", lambda: [_task("training", "t", score=0.9)]),
        ]
        result = run_global_cycle(
            reservation_id="global-2", domain_loaders=loaders, policy=policy,
            budget_ledger_path=self.budget, now=NOW,
        )
        self.assertEqual(result["plan"].deep_acquisitions_scheduled, 1)

    def test_starvation_forces_old_candidate_across_domains(self):
        loaders = [
            ("agentic-engineering", lambda: [_task("agentic-engineering", "fresh", score=0.9)]),
            ("training", lambda: [_task("training", "starved", score=0.1, first_seen="2026-01-01T00:00:00Z")]),
        ]
        result = run_global_cycle(
            reservation_id="global-3", domain_loaders=loaders, policy=self.policy,
            budget_ledger_path=self.budget, now=NOW,
        )
        top = result["plan"].decisions[0].candidate_id
        self.assertEqual(result["candidate_domains"][top], "training")

    def test_one_domain_failure_does_not_corrupt_another(self):
        def boom():
            raise RuntimeError("agentic seed corrupt")
        loaders = [
            ("agentic-engineering", boom),
            ("training", lambda: [_task("training", "t", score=0.9)]),
        ]
        result = run_global_cycle(
            reservation_id="global-4", domain_loaders=loaders, policy=self.policy,
            budget_ledger_path=self.budget, now=NOW,
        )
        self.assertEqual(result["domains_planned"], ["training"])
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(result["failures"][0]["domain"], "agentic-engineering")
        self.assertEqual(set(result["candidate_domains"].values()), {"training"})

    def test_single_shared_budget_ledger_no_per_domain_budget(self):
        loaders = [
            ("agentic-engineering", lambda: [_task("agentic-engineering", "a", score=0.9)]),
            ("training", lambda: [_task("training", "t", score=0.9)]),
        ]
        run_global_cycle(
            reservation_id="global-5", domain_loaders=loaders, policy=self.policy,
            budget_ledger_path=self.budget, now=NOW,
        )
        self.assertTrue(self.budget.is_file())
        budget_files = list(Path(self.tempdir.name).rglob("budget.json"))
        self.assertEqual(len(budget_files), 1)  # exactly one global budget authority

    def test_zero_candidates_is_no_work_no_llm(self):
        loaders = [
            ("agentic-engineering", lambda: []),
            ("training", lambda: []),
        ]
        result = run_global_cycle(
            reservation_id="global-6", domain_loaders=loaders, policy=self.policy,
            budget_ledger_path=self.budget, now=NOW,
        )
        self.assertEqual(result["status"], "no_candidates")
        self.assertEqual(result["llm_calls"], 0)
        self.assertFalse(self.budget.exists())  # no budget spent when there is no work


class ManifestEnumerationEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.budget = Path(self.tempdir.name) / "_engine" / "budget.json"

    def test_entrypoint_enumerates_all_checked_in_domains_in_one_cycle(self):
        # Run the real scheduler entrypoint as a clean subprocess (as the
        # deployed scheduler does), enumerating every checked-in domain spec.
        import subprocess
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "corpus_global_cycle.py"),
             "--config-dir", str(ROOT / "config" / "domains"),
             "--budget-path", str(self.budget),
             "--reservation-id", "manifest-1"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["status"], "planned")
        self.assertIn("agentic-engineering", summary["domains_planned"])
        self.assertIn("training", summary["domains_planned"])
        self.assertEqual(summary["failures"], [])

    def test_exactly_one_scheduler_cycle_script(self):
        cycle_scripts = list((ROOT / "scripts").glob("*cycle*.sh"))
        self.assertEqual(len(cycle_scripts), 1, f"expected one scheduler script, found {cycle_scripts}")


if __name__ == "__main__":
    unittest.main()
