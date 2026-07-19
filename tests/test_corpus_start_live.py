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

from corpus_start import PhaseContext, StartOrchestrationError, default_boundaries
from corpus_start_live import CommandResult, acquire_live, verify_gbrain_live, verify_scheduler_live


class LiveBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        run = Path(self.tmp.name)
        config = run / "config" / "domains"
        config.mkdir(parents=True)
        spec = config / "nutrition.json"
        spec.write_text("{}\n", encoding="utf-8")
        seed = run / "config" / "seeds" / "nutrition.json"
        seed.parent.mkdir(parents=True)
        seed.write_text("[]\n", encoding="utf-8")
        corpus = run / "corpora" / "nutrition"
        corpus.mkdir(parents=True)
        self.ctx = PhaseContext(
            run_root=run, domain="nutrition", title="Nutrition Research Corpus",
            topic_input="Nutrition", clean_root=True, config_dir=config,
            spec_path=spec, seed_path=seed, corpora_base=run / "corpora",
            corpus_root=corpus, state_root=run / "state", output_root=run / "output",
            archive_root=run / "archive", now="2026-07-19T00:00:00Z",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_acquisition_invokes_existing_execute_cli_and_owns_real_page(self):
        calls = []
        def runner(argv, env, cwd):
            calls.append((list(argv), env, cwd))
            page = self.ctx.corpus_root / "sources" / "acquired" / "paper.md"
            page.parent.mkdir(parents=True)
            page.write_text("# Nutrition evidence\n", encoding="utf-8")
            summary = {
                "status": "executed", "domains_planned": ["nutrition"], "failures": {},
                "report": {"discovered": 3, "acquired": 1, "changed_domains": ["nutrition"]},
            }
            return CommandResult(0, json.dumps(summary), "")
        receipt = acquire_live(self.ctx, runner=runner)
        argv = calls[0][0]
        self.assertIn("--execute", argv)
        self.assertEqual(argv[argv.index("--config-dir") + 1], str(self.ctx.config_dir))
        self.assertEqual(receipt["sources_acquired"], 1)
        self.assertEqual(receipt["owned_sources"], ["sources/acquired/paper.md"])

    def test_acquisition_fails_closed_on_zero_or_malformed(self):
        zero = CommandResult(0, json.dumps({
            "domains_planned": ["nutrition"], "failures": {},
            "report": {"discovered": 1, "acquired": 0},
        }))
        with self.assertRaises(StartOrchestrationError):
            acquire_live(self.ctx, runner=lambda *_: zero)
        with self.assertRaises(StartOrchestrationError):
            acquire_live(self.ctx, runner=lambda *_: CommandResult(0, "not-json"))

    def test_gbrain_uses_isolated_source_and_cleans_it(self):
        calls = []
        def runner(argv, env, cwd):
            calls.append((list(argv), dict(env or {})))
            if list(argv)[:3] == ["gbrain", "sources", "list"]:
                return CommandResult(0, "corpora isolated\n")
            if list(argv)[:2] == ["gbrain", "search"]:
                return CommandResult(0, "[1.0] nutrition evidence\n")
            return CommandResult(0, "ok\n")
        receipt = verify_gbrain_live(self.ctx, runner=runner)
        self.assertTrue(receipt["verified"])
        source = receipt["source_id"]
        self.assertNotEqual(source, "corpora")
        import_call = next(call for call in calls if call[0][:2] == ["gbrain", "import"])
        search_call = next(call for call in calls if call[0][:2] == ["gbrain", "search"])
        self.assertEqual(import_call[1]["GBRAIN_SOURCE"], source)
        self.assertEqual(search_call[1]["GBRAIN_SOURCE"], source)
        self.assertTrue(any(call[0][:3] == ["gbrain", "sources", "remove"] for call in calls))

    def test_scheduler_requires_exactly_one_active_owner(self):
        one = "8dd [active]\n    Name:      corpus-engine-cycle\n    Schedule: daily\n"
        receipt = verify_scheduler_live(self.ctx, runner=lambda *_: CommandResult(0, one))
        self.assertEqual(receipt["owner_count"], 1)
        for output in ("", one + "\n\n99 [active]\n    Name:      corpus-engine-cycle\n"):
            with self.assertRaises(StartOrchestrationError):
                verify_scheduler_live(self.ctx, runner=lambda *_, value=output: CommandResult(0, value))

    def test_default_boundaries_are_live(self):
        boundaries = default_boundaries()
        for boundary in (boundaries.acquire, boundaries.verify_gbrain, boundaries.verify_atlas, boundaries.verify_scheduler):
            self.assertNotIn("_unconfigured", getattr(boundary, "__name__", ""))


if __name__ == "__main__":
    unittest.main()
