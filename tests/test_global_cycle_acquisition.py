import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import corpus_priority
from corpus_acquisition_executor import AcquisitionCandidate, AcquisitionExecutor, FetchResult
from corpus_global_cycle import DomainAcquisitionInputs, run_global_acquisition_cycle
from corpus_priority import CandidateTask, PriorityPolicy
from corpus_rights_resolver import RightsEvidence

# Sibling test modules reload corpus_engine_models under the same name, so bind
# the model classes from the exact instance corpus_priority validates against.
CandidateRecord = corpus_priority.CandidateRecord
CandidateObservation = CandidateRecord.from_observation.__func__.__globals__["CandidateObservation"]


def _pair(domain, key, canonical, rights, topics=("hypertrophy",), evidence_lane="primary-study"):
    obs = CandidateObservation.create(
        domain=domain, entity_type="paper", canonical_url=canonical,
        discovery_source="scholarly_discovery:openalex:" + key, evidence_pointer=canonical,
        evidence_lane=evidence_lane, topics=topics, observed_at="2026-07-19T00:00:00Z",
    )
    from corpus_discovery import score_observation
    scores, rationale = score_observation(obs)
    resolution_state = {
        "cc": "public_rights_clear", "meta": "public_metadata_only",
        "paid": "rights_unclear", "priv": "private_authorized",
    }
    record = CandidateRecord.from_observation(
        obs, scores, rationale=rationale,
        rights_state=resolution_state.get(rights[0], "rights_unclear"),
    )
    task = CandidateTask(record, material_delta=True)
    candidate = AcquisitionCandidate(
        candidate_id=obs.candidate_key, domain=domain, canonical_locator=canonical,
        content_locator=canonical, evidence_lane=evidence_lane,
        source_family="peer-reviewed-literature", title="Study " + key, topics=obs.topics,
        rights_evidence=rights[1], metadata={"openalex_id": key},
        idempotency_key=f"global-2026-07-19:acquire:{obs.candidate_key}",
    )
    return task, candidate


CC = ("cc", RightsEvidence(license="cc-by", access_class="open_content"))
META = ("meta", RightsEvidence(access_class="metadata", repository="openalex"))
PAID = ("paid", RightsEvidence(access_class="paid"))
PRIV = ("priv", RightsEvidence(access_class="private"))


class CycleHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.fetches = []

        def fetch(url, *, timeout, max_bytes=None):
            self.fetches.append(url)
            return FetchResult(
                body=b"<html><body><p>hypertrophy strength adaptation evidence text long enough</p></body></html>",
                final_url=url, content_type="text/html")

        self.executor = AcquisitionExecutor(
            raw_root=self.root / "archive", staging_root=self.root / "staging",
            receipts_root=self.root / "receipts", quarantine_root=self.root / "quarantine",
            http_fetch=fetch, now=lambda: "2026-07-19T01:00:00Z",
        )
        self.now = datetime(2026, 7, 19, tzinfo=timezone.utc)

    def _run(self, loaders, reservation_id="global-2026-07-19-test"):
        return run_global_acquisition_cycle(
            reservation_id=reservation_id, domain_loaders=loaders,
            policy=PriorityPolicy.default(),
            budget_ledger_path=self.root / "budget.json", now=self.now,
            executor=self.executor, corpus_revision="rev-1", cycle_time_seconds=3.0,
        )

    def _loader(self, domain, pairs):
        def load():
            tasks = [p[0] for p in pairs]
            cands = {p[1].candidate_id: p[1] for p in pairs}
            return DomainAcquisitionInputs(domain=domain, tasks=tasks, acquisition_candidates=cands)
        return (domain, load)


class HappyPathTests(CycleHarness):
    def test_unseeded_clear_candidate_is_acquired_to_probationary(self):
        pairs = [_pair("training", "W1", "https://example.org/oa", CC)]
        result = self._run([self._loader("training", pairs)])
        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["report"]["acquired"], 1)
        self.assertIn("training", result["report"]["changed_domains"])
        receipt = result["receipts"][0]
        self.assertEqual(receipt["disposition"], "probationary")
        self.assertEqual(len(self.fetches), 1)

    def test_metadata_and_gate_candidates_are_not_fetched(self):
        pairs = [
            _pair("training", "W1", "https://example.org/oa", CC),
            _pair("training", "W2", "https://example.org/meta", META),
            _pair("training", "W3", "https://example.org/paid", PAID),
            _pair("training", "W4", "https://example.org/priv", PRIV),
        ]
        result = self._run([self._loader("training", pairs)])
        self.assertEqual(len(self.fetches), 1)  # only the CC candidate fetched content
        report = result["report"]
        self.assertEqual(report["acquired"], 1)
        self.assertEqual(report["gated"], 2)  # paid + private are human gates
        self.assertEqual(report["metadata_only"], 1)

    def test_report_exposes_required_counts(self):
        pairs = [_pair("training", "W1", "https://example.org/oa", CC)]
        result = self._run([self._loader("training", pairs)])
        for key in ("discovered", "rights_resolved", "acquired", "gated", "failed", "changed_domains"):
            self.assertIn(key, result["report"])


class BudgetAndIsolationTests(CycleHarness):
    def test_content_fetch_is_bounded_by_the_single_budget_plan(self):
        # Five clear candidates but the default policy caps deep acquisitions at 3.
        pairs = [
            _pair("training", f"W{i}", f"https://example.org/oa{i}", CC, topics=("hypertrophy",))
            for i in range(5)
        ]
        result = self._run([self._loader("training", pairs)])
        selected = result["selected_acquire_ids"]
        self.assertLessEqual(len(self.fetches), 3)
        self.assertEqual(len(self.fetches), len(selected))

    def test_one_reservation_and_one_budget(self):
        pairs = [_pair("training", "W1", "https://example.org/oa", CC)]
        result = self._run([self._loader("training", pairs)])
        self.assertEqual(result["reservation_id"], "global-2026-07-19-test")
        budget_files = list(self.root.glob("budget.json"))
        self.assertEqual(len(budget_files), 1)

    def test_one_domain_loader_failure_does_not_corrupt_another(self):
        def bad_loader():
            raise RuntimeError("domain A intake exploded")

        pairs_b = [_pair("nutrition", "N1", "https://example.org/oa", CC, topics=("hypertrophy",))]
        result = self._run([("agentic", bad_loader), self._loader("nutrition", pairs_b)])
        self.assertTrue(any(f["domain"] == "agentic" for f in result["failures"]))
        self.assertEqual(result["report"]["acquired"], 1)
        self.assertIn("nutrition", result["report"]["changed_domains"])

    def test_duplicate_domain_label_is_not_executed_twice(self):
        first = [_pair("training", "W1", "https://example.org/oa", CC)]
        duplicate = [_pair("training", "W2", "https://example.org/oa2", CC)]
        result = self._run([
            self._loader("training", first),
            self._loader("training", duplicate),
        ])
        self.assertEqual(result["domains_planned"], ["training"])
        self.assertEqual(result["report"]["discovered"], 1)
        self.assertEqual(result["report"]["acquired"], 1)
        self.assertEqual(len(self.fetches), 1)
        self.assertTrue(any(f["error"] == "duplicate domain loader" for f in result["failures"]))

    def test_executor_factory_failure_is_isolated_to_its_domain(self):
        training = [_pair("training", "W1", "https://example.org/oa", CC)]
        nutrition = [_pair("nutrition", "N1", "https://example.org/oa2", CC)]

        def factory(domain):
            if domain == "training":
                raise RuntimeError("executor unavailable")
            return self.executor

        result = run_global_acquisition_cycle(
            reservation_id="global-executor-isolation",
            domain_loaders=[
                self._loader("training", training),
                self._loader("nutrition", nutrition),
            ],
            policy=PriorityPolicy.default(),
            budget_ledger_path=self.root / "factory-budget.json",
            now=self.now,
            executor_factory=factory,
            corpus_revision="rev-1",
            cycle_time_seconds=3.0,
        )
        self.assertEqual(result["report"]["acquired"], 1)
        self.assertEqual(len(self.fetches), 1)
        self.assertIn("nutrition", result["report"]["changed_domains"])
        self.assertTrue(any(
            f["domain"] == "training" and "executor unavailable" in f["error"]
            for f in result["failures"]
        ))

    def test_non_candidate_task_is_rejected_without_aborting_healthy_domain(self):
        nutrition = [_pair("nutrition", "N1", "https://example.org/oa2", CC)]
        malformed = DomainAcquisitionInputs(
            domain="training", tasks=[object()], acquisition_candidates={}  # type: ignore[list-item]
        )
        result = self._run([
            ("training", lambda: malformed),
            self._loader("nutrition", nutrition),
        ], reservation_id="global-invalid-task")
        self.assertEqual(result["report"]["acquired"], 1)
        self.assertEqual(result["domains_planned"], ["nutrition"])
        self.assertTrue(any(f["error"] == "loader returned a non-CandidateTask" for f in result["failures"]))

    def test_mismatched_execution_candidate_is_rejected(self):
        task, candidate = _pair("training", "W1", "https://example.org/oa", CC)
        mismatched = replace(candidate, candidate_id="different-candidate")
        malformed = DomainAcquisitionInputs(
            domain="training",
            tasks=[task],
            acquisition_candidates={task.candidate.candidate_id: mismatched},
        )
        result = self._run(
            [("training", lambda: malformed)],
            reservation_id="global-mismatched-candidate",
        )
        self.assertEqual(result["status"], "no_candidates")
        self.assertFalse(self.fetches)
        self.assertTrue(any("mismatched acquisition candidate" in f["error"] for f in result["failures"]))

    def test_cross_domain_candidates_stage_into_separate_domain_dirs(self):
        pairs_a = [_pair("training", "W1", "https://example.org/oa", CC)]
        pairs_b = [_pair("nutrition", "N1", "https://example.org/oa2", CC)]
        self._run([self._loader("training", pairs_a), self._loader("nutrition", pairs_b)])
        self.assertTrue((self.root / "staging" / "training").exists())
        self.assertTrue((self.root / "staging" / "nutrition").exists())


class ProjectionTests(CycleHarness):
    def test_cycle_emits_redacted_projection(self):
        import json
        pairs = [
            _pair("training", "W1", "https://example.org/oa", CC),
            _pair("training", "W3", "https://example.org/paid", PAID),
        ]
        result = self._run([self._loader("training", pairs)])
        projection = result["projection"]
        self.assertEqual(projection["corpus_revision"], "rev-1")
        self.assertEqual(len(projection["human_gates"]), 1)
        blob = json.dumps(projection)
        self.assertNotIn(str(self.root / "archive"), blob)
        self.assertNotIn(str(self.root / "staging"), blob)

    def test_budget_deferred_candidates_are_queued_in_projection(self):
        # Five clear candidates, default policy funds at most 3: the remainder
        # must surface as queued/deferred candidates in the projection, and the
        # next best must be a queued candidate, never a completed probationary.
        pairs = [
            _pair("training", f"W{i}", f"https://example.org/oa{i}", CC)
            for i in range(5)
        ]
        result = self._run([self._loader("training", pairs)])
        projection = result["projection"]
        self.assertGreaterEqual(projection["totals"]["queued"], 1)
        queued_ids = {c["candidate_id"] for c in projection["queued_candidates"]}
        self.assertTrue(queued_ids)
        self.assertIsNotNone(projection["next_best"])
        self.assertEqual(projection["next_best"]["status"], "queued")
        # Every deferred candidate is visible in the ranked view too.
        ranked_ids = {c["candidate_id"] for c in projection["ranked_candidates"]}
        self.assertTrue(queued_ids <= ranked_ids)

    def test_no_candidates_is_readonly_noop(self):
        def empty():
            return DomainAcquisitionInputs(domain="training", tasks=[], acquisition_candidates={})
        result = self._run([("training", empty)])
        self.assertEqual(result["status"], "no_candidates")
        self.assertEqual(len(self.fetches), 0)
        self.assertFalse((self.root / "budget.json").exists())


if __name__ == "__main__":
    unittest.main()
