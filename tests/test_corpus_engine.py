from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "corpus_engine.py"
spec = importlib.util.spec_from_file_location("corpus_engine", MODULE_PATH)
assert spec and spec.loader
corpus_engine = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = corpus_engine
spec.loader.exec_module(corpus_engine)


class CorpusEngineTests(unittest.TestCase):
    def test_main_text_parser_removes_scripts_and_normalizes_blocks(self):
        parser = corpus_engine.MainTextParser()
        parser.feed("<html><title>Useful</title><script>bad()</script><main><h1>Header</h1><p>Body text</p></main></html>")
        text = parser.text()
        self.assertIn("Header", text)
        self.assertIn("Body text", text)
        self.assertNotIn("bad()", text)
        self.assertEqual(parser.title, "Useful")

    def test_vtt_to_text_deduplicates_caption_rollups(self):
        raw = """WEBVTT

00:00:00.000 --> 00:00:01.000
Hello

00:00:01.000 --> 00:00:02.000
Hello

00:00:02.000 --> 00:00:03.000
<b>world</b>
"""
        self.assertEqual(corpus_engine.vtt_to_text(raw), "Hello\nworld\n")

    def test_slugify_is_stable(self):
        self.assertEqual(corpus_engine.slugify("Agentic Engineering / Hooks"), "agentic-engineering-hooks")

    def test_refresh_window_uses_shared_classes(self):
        entry = {"refresh_class": "weekly"}
        fresh = {"last_checked_at": corpus_engine.iso(datetime.now(timezone.utc) - timedelta(days=2))}
        stale = {"last_checked_at": corpus_engine.iso(datetime.now(timezone.utc) - timedelta(days=8))}
        self.assertFalse(corpus_engine.is_due(entry, fresh, False))
        self.assertTrue(corpus_engine.is_due(entry, stale, False))
        self.assertTrue(corpus_engine.is_due(entry, fresh, True))

    def test_frozen_sources_are_never_swept_by_refresh(self):
        entry = {"refresh_class": "frozen"}
        self.assertFalse(corpus_engine.is_due(entry, {}, False))
        self.assertFalse(corpus_engine.is_due(entry, {}, True))

    def test_all_not_due_results_are_a_read_only_noop(self):
        results = [
            corpus_engine.RefreshResult("one", "not_due", [], "refresh window not reached"),
            corpus_engine.RefreshResult("two", "not_due", [], "refresh window not reached"),
        ]
        self.assertFalse(corpus_engine.has_material_refresh_results(results))
        results.append(corpus_engine.RefreshResult("three", "refreshed", ["page"], "changed"))
        self.assertTrue(corpus_engine.has_material_refresh_results(results))


if __name__ == "__main__":
    unittest.main()
