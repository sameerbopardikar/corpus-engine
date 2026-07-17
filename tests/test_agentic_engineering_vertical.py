from __future__ import annotations

import hashlib
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

from corpus_engine_models import CandidateObservation, CandidateRecord
from corpus_priority import PriorityPolicy
from corpus_shadow import ShadowCycleError, ShadowCyclePaths, run_shadow_cycle

# The legacy model test intentionally reloads corpus_engine_models under its
# canonical module name during unittest discovery. Do not leave this test's
# transitive priority module cached across that reload: CandidateTask's strict
# class-identity guard must bind the same model generation as its callers.
sys.modules.pop("corpus_shadow", None)
sys.modules.pop("corpus_priority", None)

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
SCORES = {
    "authority": 0.95,
    "demonstrated_practice": 0.9,
    "novelty": 0.8,
    "relevance": 0.95,
    "corroboration": 0.7,
    "production_or_scientific_value": 0.9,
    "cost": 0.1,
}


def candidate(url: str, *, topic: str, rights: str = "public_rights_clear", score: float = 1.0) -> CandidateRecord:
    observation = CandidateObservation.create(
        domain="agentic-engineering",
        entity_type="document",
        canonical_url=url,
        discovery_source="public-test-feed",
        evidence_pointer=url,
        evidence_lane="production-reliability",
        topics=(topic,),
        observed_at="2026-07-16T00:00:00Z",
    )
    scores = {key: min(value * score, 1.0) for key, value in SCORES.items()}
    return CandidateRecord.from_observation(
        observation, scores, rationale="Public production evidence.", rights_state=rights
    )


class AgenticEngineeringShadowVerticalTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.paths = ShadowCyclePaths(
            state_root=self.root / "state",
            corpus_root=self.root / "corpora" / "agentic-engineering",
            archive_root=self.root / "archive" / "agentic-engineering",
        )
        self.calls = {"acquire": 0, "sync": 0, "retrieve": 0}
        self.primary = candidate("https://example.com/public-postmortem", topic="lease-fencing")
        self.secondary = candidate("https://example.com/second", topic="tool-telemetry", score=0.8)

    def acquire(self, selected, paths):
        self.calls["acquire"] += 1
        raw = paths.archive_root / "raw" / f"{selected.candidate_id}.html"
        normalized = paths.archive_root / "normalized" / f"{selected.candidate_id}.md"
        page = paths.corpus_root / "sources" / "production-reliability" / "public-postmortem.md"
        raw.parent.mkdir(parents=True, exist_ok=True)
        normalized.parent.mkdir(parents=True, exist_ok=True)
        page.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"<article>Lease fencing prevents stale agents from committing.</article>")
        normalized.write_text("Lease fencing prevents stale agents from committing.\n", encoding="utf-8")
        page.write_text("---\ntype: source\nsource_url: https://example.com/public-postmortem\n---\n\nLease fencing prevents stale agents from committing.\n", encoding="utf-8")
        claim = hashlib.sha256(b"lease-fencing-prevents-stale-commits").hexdigest()
        record = {
            "record_id": selected.candidate_id,
            "source_id": "corpora",
            "page_slug": "agentic-engineering/sources/production-reliability/public-postmortem",
            "locator": "article",
            "claim_sha256": claim,
            "raw_pointer": str(raw),
            "raw_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
            "normalized_pointer": str(normalized),
            "normalized_sha256": hashlib.sha256(normalized.read_bytes()).hexdigest(),
            "canonical_url": selected.canonical_url,
            "source_revision": hashlib.sha256(raw.read_bytes()).hexdigest(),
            "retrieved_at": "2026-07-17T12:00:00Z",
            "evidence_lane": "production-reliability",
            "evidence_class": "production-incident",
            "epistemic_layer": "primary_source",
            "rights_state": selected.rights_state,
            "candidate_status": "probationary",
            "contradiction_group": "lease-fencing-scope",
            "contradiction_stance": "supports",
        }
        return {
            "record": record,
            "doctrine": {
                "concept_key": "lease-fencing",
                "title": "Lease Fencing",
                "statement": "Durable agents must fence stale owners before commit.",
                "rationale": "The public incident evidence exposes a concrete stale-owner failure mode.",
            },
            "page_path": str(page),
        }

    def sync(self, package):
        self.calls["sync"] += 1
        page = Path(package["page_path"])
        return {
            "source_id": "corpora",
            "changed_pages": [package["record"]["page_slug"]],
            "page_sha256": hashlib.sha256(page.read_bytes()).hexdigest(),
        }

    def retrieve(self, package):
        self.calls["retrieve"] += 1
        return [{"source_id": "corpora", "page_slug": package["record"]["page_slug"]}]

    def execute_cycle(self, **overrides):
        values = dict(
            cycle_id="cycle-2026-07-17",
            candidates=[self.primary, self.secondary],
            policy=PriorityPolicy.default(),
            paths=self.paths,
            now=NOW,
            acquire=self.acquire,
            sync_corpus=self.sync,
            retrieve=self.retrieve,
        )
        values.update(overrides)
        return run_shadow_cycle(**values)

    def test_real_vertical_is_stable_restart_safe_and_selects_next_gap(self):
        receipt = self.execute_cycle()
        receipt_path = self.paths.state_root / "receipts" / "cycle-2026-07-17.json"
        before = receipt_path.read_bytes()
        doctrine_before = (self.paths.state_root / "doctrine.jsonl").read_bytes()
        budget_before = (self.paths.state_root / "budget.json").read_bytes()

        self.assertEqual(receipt["status"], "verified_shadow_complete")
        self.assertTrue(receipt["evaluation"]["passed"])
        self.assertEqual(receipt["selected"]["candidate_id"], self.primary.candidate_id)
        self.assertFalse(receipt["selected"]["auto_promote"])
        self.assertEqual(receipt["doctrine_decision"]["decision"], "proposed")
        self.assertEqual(receipt["doctrine_decision"]["epistemic_layer"], "external_corpus_synthesis")
        self.assertFalse(receipt["doctrine_decision"]["sameer_adopted"])
        self.assertEqual(receipt["next_gap"]["candidate_id"], self.secondary.candidate_id)
        self.assertEqual(receipt["llm_calls"], 0)
        self.assertEqual(self.calls, {"acquire": 1, "sync": 1, "retrieve": 1})

        replayed = self.execute_cycle()
        self.assertEqual(replayed, receipt)
        self.assertEqual(receipt_path.read_bytes(), before)
        self.assertEqual((self.paths.state_root / "doctrine.jsonl").read_bytes(), doctrine_before)
        self.assertEqual((self.paths.state_root / "budget.json").read_bytes(), budget_before)
        self.assertEqual(self.calls, {"acquire": 1, "sync": 1, "retrieve": 1})

    def test_restart_after_doctrine_append_reuses_acquisition_and_command(self):
        fired = {"value": False}

        def interrupt(phase):
            if phase == "doctrine_applied" and not fired["value"]:
                fired["value"] = True
                raise RuntimeError("simulated process death")

        with self.assertRaisesRegex(RuntimeError, "simulated process death"):
            self.execute_cycle(interrupt_after=interrupt)
        doctrine = self.paths.state_root / "doctrine.jsonl"
        self.assertEqual(len(doctrine.read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(self.calls["acquire"], 1)

        receipt = self.execute_cycle()
        self.assertEqual(receipt["status"], "verified_shadow_complete")
        self.assertEqual(len(doctrine.read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(self.calls["acquire"], 1)

    def test_cycle_id_conflict_fails_closed_without_mutating_current_bytes(self):
        self.execute_cycle()
        files = [path for path in self.paths.state_root.rglob("*") if path.is_file()]
        before = {str(path): path.read_bytes() for path in files}
        conflicting = candidate("https://example.com/conflict", topic="different")
        with self.assertRaisesRegex(ShadowCycleError, "cycle_id conflict"):
            self.execute_cycle(candidates=[conflicting])
        self.assertEqual({str(path): path.read_bytes() for path in files}, before)

    def test_rights_unclear_candidate_cannot_reach_acquirer(self):
        unclear = candidate(
            "https://example.com/rights-unclear", topic="unclear", rights="rights_unclear"
        )
        with self.assertRaisesRegex(ShadowCycleError, "no public rights-clear acquisition"):
            self.execute_cycle(candidates=[unclear])
        self.assertEqual(self.calls["acquire"], 0)

    def test_live_candidate_identity_is_stable_across_process_restart_time(self):
        import importlib.util
        script_path = ROOT / "scripts" / "run_agentic_engineering_shadow.py"
        spec = importlib.util.spec_from_file_location("agentic_shadow_script_test", script_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        first = [item.to_dict() for item in module.build_candidates(datetime(2026, 7, 17, 1, tzinfo=timezone.utc))]
        second = [item.to_dict() for item in module.build_candidates(datetime(2026, 7, 17, 2, tzinfo=timezone.utc))]
        self.assertEqual(first, second)

    def test_live_sync_commits_only_intentional_corpus_page(self):
        import importlib.util
        import subprocess
        script_path = ROOT / "scripts" / "run_agentic_engineering_shadow.py"
        spec = importlib.util.spec_from_file_location("agentic_shadow_script_git_test", script_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        repo = self.root / "corpus-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Shadow Test"], check=True)
        (repo / "baseline.md").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "baseline.md"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)
        target = repo / "agentic-engineering" / "sources" / "agentdojo.md"
        target.parent.mkdir(parents=True)
        target.write_text("agentdojo\n", encoding="utf-8")
        unrelated = repo / "unrelated.md"
        unrelated.write_text("do not commit\n", encoding="utf-8")
        commit = module.commit_corpus_page(repo, target, cycle_id="cycle-one")
        changed = subprocess.check_output(
            ["git", "-C", str(repo), "show", "--pretty=format:", "--name-only", commit], text=True
        ).splitlines()
        self.assertEqual([item for item in changed if item], ["agentic-engineering/sources/agentdojo.md"])
        self.assertIn("?? unrelated.md", subprocess.check_output(["git", "-C", str(repo), "status", "--short"], text=True))

    def test_live_retrieval_uses_broad_high_detail_source_scoped_probe(self):
        import importlib.util
        from unittest.mock import patch
        script_path = ROOT / "scripts" / "run_agentic_engineering_shadow.py"
        spec = importlib.util.spec_from_file_location("agentic_shadow_script_retrieval_test", script_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        package = {"record": {"page_slug": "agentic-engineering/sources/security-evaluation/agentdojo"}}
        completed = type("Completed", (), {"returncode": 0, "stdout": "[1.0000] agentic-engineering/sources/security-evaluation/agentdojo -- hit\n", "stderr": ""})()
        with patch.object(module.subprocess, "run", return_value=completed) as run:
            results = module.retrieve_corpora(package)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--source-id") + 1], "corpora")
        self.assertEqual(command[command.index("--limit") + 1], "20")
        self.assertEqual(command[command.index("--detail") + 1], "high")
        self.assertEqual(results, [{"source_id": "corpora", "page_slug": package["record"]["page_slug"]}])

    def test_failed_evaluation_never_writes_final_receipt(self):
        def wrong_scope(package):
            return [{"source_id": "default", "page_slug": package["record"]["page_slug"]}]

        with self.assertRaisesRegex(ShadowCycleError, "evaluation failed"):
            self.execute_cycle(retrieve=wrong_scope)
        self.assertFalse((self.paths.state_root / "receipts" / "cycle-2026-07-17.json").exists())

    def test_evaluation_time_cannot_predate_the_acquired_evidence(self):
        receipt = self.execute_cycle(now=datetime(2026, 7, 17, 11, 59, tzinfo=timezone.utc))
        self.assertTrue(receipt["evaluation"]["passed"])
        self.assertEqual(receipt["evaluation"]["evaluated_at"], "2026-07-17T12:00:00Z")

    def test_acquisition_package_paths_must_stay_inside_owned_roots(self):
        outside = self.root / "outside"

        def escaped_acquire(selected, paths):
            package = dict(self.acquire(selected, paths))
            record = dict(package["record"])
            escaped = outside / "escaped.md"
            escaped.parent.mkdir(parents=True)
            escaped.write_text("escaped\n", encoding="utf-8")
            record["normalized_pointer"] = str(escaped)
            record["normalized_sha256"] = hashlib.sha256(escaped.read_bytes()).hexdigest()
            package["record"] = record
            return package

        with self.assertRaisesRegex(ShadowCycleError, "outside the owned root"):
            self.execute_cycle(acquire=escaped_acquire)
        self.assertFalse((self.paths.state_root / "receipts" / "cycle-2026-07-17.json").exists())


if __name__ == "__main__":
    unittest.main()
