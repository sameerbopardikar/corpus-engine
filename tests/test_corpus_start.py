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

from corpus_start import (  # noqa: E402
    CorpusStartRun,
    StartBoundaries,
    StartOrchestrationError,
)

FIXTURE = ROOT / "tests" / "fixtures" / "nutrition-bootstrap-packet.json"
NOW = "2026-07-19T12:00:00Z"


class _Recorder:
    """Deterministic, network-free boundary fakes that record invocations."""

    def __init__(self, *, sources_acquired=1, atlas_ready=True, owner_count=1, extra_source=False):
        self.calls = {"acquire": 0, "gbrain": 0, "atlas": 0, "scheduler": 0}
        self.sources_acquired = sources_acquired
        self.atlas_ready = atlas_ready
        self.owner_count = owner_count
        self.extra_source = extra_source

    def acquire(self, ctx):
        self.calls["acquire"] += 1
        owned = []
        sources_dir = ctx.corpus_root / "sources"
        sources_dir.mkdir(parents=True, exist_ok=True)
        for i in range(self.sources_acquired):
            rel = f"sources/acquired-{i}.md"
            (ctx.corpus_root / rel).write_text(f"raw source {i}\n", encoding="utf-8")
            owned.append(rel)
        if self.extra_source:
            # A rogue, non-orchestrator-owned page appears in the corpus.
            (sources_dir / "manual-card.md").write_text("hand written\n", encoding="utf-8")
        return {
            "sources_discovered": max(self.sources_acquired, 1),
            "sources_acquired": self.sources_acquired,
            "owned_sources": owned,
        }

    def gbrain(self, ctx):
        self.calls["gbrain"] += 1
        return {"verified": True, "source": "corpora", "query": ctx.domain}

    def atlas(self, ctx):
        self.calls["atlas"] += 1
        return {
            "atlas_ready": self.atlas_ready,
            "healthz": self.atlas_ready,
            "readyz": self.atlas_ready,
            "product_name": f"{ctx.title} Research Atlas",
        }

    def scheduler(self, ctx):
        self.calls["scheduler"] += 1
        return {"owner_count": self.owner_count}

    def boundaries(self):
        return StartBoundaries(
            acquire=self.acquire,
            verify_gbrain=self.gbrain,
            verify_atlas=self.atlas,
            verify_scheduler=self.scheduler,
        )


class CorpusStartTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, rec):
        return CorpusStartRun.begin(
            self.run_root, topic="Nutrition", now=NOW, boundaries=rec.boundaries()
        )

    def test_begin_only_creates_ledger(self):
        rec = _Recorder()
        run = self._run(rec)
        self.assertEqual(run.status()["status"], "initialized")
        self.assertEqual(run.next_action(), "produce_bootstrap_packet")
        # No corpus, no acquisition until packet + continue.
        self.assertFalse((self.run_root / "corpora").exists())
        self.assertEqual(rec.calls["acquire"], 0)

    def test_apply_packet_compiles_inputs(self):
        rec = _Recorder()
        run = self._run(rec)
        run.apply_packet(FIXTURE, now=NOW)
        self.assertEqual(run.status()["status"], "bootstrap_packet_validated")
        spec_path = self.run_root / "config" / "domains" / "nutrition.json"
        seed_path = self.run_root / "config" / "seeds" / "nutrition-seed-candidates.json"
        self.assertTrue(spec_path.exists())
        self.assertTrue(seed_path.exists())
        self.assertEqual(run.next_action(), "continue")

    def test_full_run_reaches_complete(self):
        rec = _Recorder()
        run = self._run(rec)
        run.apply_packet(FIXTURE, now=NOW)
        run.continue_run(now=NOW)
        status = run.status()
        self.assertEqual(
            status,
            {
                "status": "complete",
                "topic_input": "Nutrition",
                "clean_root": True,
                "field_map_ready": True,
                "sources_discovered": 1,
                "sources_acquired": 1,
                "gbrain_sync_verified": True,
                "atlas_ready": True,
                "scheduler_owner_count": 1,
                "manual_seed_substitution": False,
            },
        )

    def test_continue_is_idempotent_noop_on_replay(self):
        rec = _Recorder()
        run = self._run(rec)
        run.apply_packet(FIXTURE, now=NOW)
        run.continue_run(now=NOW)
        calls_after_first = dict(rec.calls)
        marker = self.run_root / ".corpus-start" / "run.json"
        before = marker.read_bytes()
        run.continue_run(now="2026-07-19T13:00:00Z")  # a physical retry
        self.assertEqual(rec.calls, calls_after_first)  # boundaries not re-invoked
        self.assertEqual(marker.read_bytes(), before)  # ledger unchanged

    def test_zero_acquisition_blocks_completion(self):
        rec = _Recorder(sources_acquired=0)
        run = self._run(rec)
        run.apply_packet(FIXTURE, now=NOW)
        with self.assertRaises(StartOrchestrationError):
            run.continue_run(now=NOW)
        self.assertNotEqual(run.status()["status"], "complete")

    def test_atlas_not_ready_blocks_completion(self):
        rec = _Recorder(atlas_ready=False)
        run = self._run(rec)
        run.apply_packet(FIXTURE, now=NOW)
        with self.assertRaises(StartOrchestrationError):
            run.continue_run(now=NOW)
        self.assertNotEqual(run.status()["status"], "complete")

    def test_multiple_scheduler_owners_blocks_completion(self):
        rec = _Recorder(owner_count=2)
        run = self._run(rec)
        run.apply_packet(FIXTURE, now=NOW)
        with self.assertRaises(StartOrchestrationError):
            run.continue_run(now=NOW)
        self.assertNotEqual(run.status()["status"], "complete")

    def test_manual_seed_substitution_blocks_completion(self):
        rec = _Recorder(extra_source=True)
        run = self._run(rec)
        run.apply_packet(FIXTURE, now=NOW)
        with self.assertRaises(StartOrchestrationError):
            run.continue_run(now=NOW)
        self.assertTrue(run.status()["manual_seed_substitution"])
        self.assertNotEqual(run.status()["status"], "complete")

    def test_status_of_partial_run(self):
        rec = _Recorder()
        run = self._run(rec)
        run.apply_packet(FIXTURE, now=NOW)
        status = run.status()
        self.assertEqual(status["status"], "bootstrap_packet_validated")
        self.assertFalse(status["gbrain_sync_verified"])
        self.assertFalse(status["atlas_ready"])

    def test_reload_resumes_from_ledger(self):
        rec = _Recorder()
        run = self._run(rec)
        run.apply_packet(FIXTURE, now=NOW)
        # Fresh handle over the same root resumes without redoing packet.
        resumed = CorpusStartRun.load(self.run_root, boundaries=rec.boundaries())
        resumed.continue_run(now=NOW)
        self.assertEqual(resumed.status()["status"], "complete")

    def test_clean_mode_rejects_existing_context_refs(self):
        rec = _Recorder()
        run = self._run(rec)
        contaminated = json.loads(FIXTURE.read_text(encoding="utf-8"))
        contaminated["existing_context_refs"] = ["/root/corpora/nutrition/x.md"]
        packet_path = self.run_root / "packet.json"
        packet_path.write_text(json.dumps(contaminated), encoding="utf-8")
        with self.assertRaises(StartOrchestrationError):
            run.apply_packet(packet_path, now=NOW)


import importlib.util  # noqa: E402

_CLI = ROOT / "scripts" / "corpus_start.py"
_cli_spec = importlib.util.spec_from_file_location("corpus_start_cli", _CLI)
cli = importlib.util.module_from_spec(_cli_spec)
_cli_spec.loader.exec_module(cli)


class CorpusStartCliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_root = str(Path(self._tmp.name) / "proof")

    def tearDown(self):
        self._tmp.cleanup()

    def test_cli_end_to_end(self):
        rec = _Recorder()
        b = rec.boundaries()
        self.assertEqual(
            cli.main(["begin", "--topic", "Nutrition", "--run-root", self.run_root], boundaries=b, now=NOW),
            0,
        )
        self.assertEqual(
            cli.main(["apply-packet", "--run", self.run_root, "--packet", str(FIXTURE)], boundaries=b, now=NOW),
            0,
        )
        self.assertEqual(cli.main(["continue", "--run", self.run_root], boundaries=b, now=NOW), 0)
        run = CorpusStartRun.load(self.run_root, boundaries=b)
        self.assertEqual(run.status()["status"], "complete")

    def test_cli_begin_only_creates_ledger(self):
        rec = _Recorder()
        cli.main(["begin", "--topic", "Nutrition", "--run-root", self.run_root], boundaries=rec.boundaries(), now=NOW)
        self.assertFalse((Path(self.run_root) / "corpora").exists())
        self.assertTrue((Path(self.run_root) / ".corpus-start" / "run.json").exists())

    def test_cli_resume_live_root_marks_unclean(self):
        rec = _Recorder()
        cli.main(
            ["begin", "--topic", "Nutrition", "--run-root", self.run_root, "--resume-live-root", "/root/corpora"],
            boundaries=rec.boundaries(), now=NOW,
        )
        run = CorpusStartRun.load(self.run_root, boundaries=rec.boundaries())
        self.assertFalse(run.status()["clean_root"])

    def test_cli_status_reports_incomplete(self):
        rec = _Recorder()
        cli.main(["begin", "--topic", "Nutrition", "--run-root", self.run_root], boundaries=rec.boundaries(), now=NOW)
        self.assertEqual(cli.main(["status", "--run", self.run_root, "--json"], boundaries=rec.boundaries(), now=NOW), 0)


if __name__ == "__main__":
    unittest.main()
