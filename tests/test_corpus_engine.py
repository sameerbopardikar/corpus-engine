from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


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
        self.assertFalse(
            corpus_engine.has_material_refresh_results(
                [corpus_engine.RefreshResult("four", "unchanged", ["page"], "same revision")]
            )
        )

    def test_stable_youtube_inventory_retries_pending_caption_and_preserves_it_content_addressed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            corpus_root = root / "corpora"
            raw_root = root / "raw"
            page_slug = "agentic-engineering/sources/practitioner/channel-video-1"
            page_path = corpus_root / f"{page_slug}.md"
            page_path.parent.mkdir(parents=True)
            page_path.write_text('---\nvideo_id: "video-1"\ntranscript_status: "pending"\n---\n', encoding="utf-8")
            metadata_path = root / "video-1.json"
            metadata_path.write_text(json.dumps({"video_id": "video-1", "title": "Video", "published": "2026-07-16", "url": "https://www.youtube.com/watch?v=video-1"}), encoding="utf-8")
            raw_feed = root / "feed.xml"
            raw_feed.write_bytes(b"feed")
            observation = SimpleNamespace(
                normalized_pointer=str(metadata_path),
                raw_pointer=str(raw_feed),
                raw_sha256=corpus_engine.sha256_bytes(b"feed"),
                normalized_sha256=corpus_engine.sha256_bytes(metadata_path.read_bytes()),
                canonical_locator="https://www.youtube.com/watch?v=video-1",
            )
            batch = SimpleNamespace(source_revision="a" * 64, observations=(observation,))
            transcript = root / "captions" / "normalized" / "video-1-deadbeef.txt"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("content addressed transcript\n", encoding="utf-8")

            engine = object.__new__(corpus_engine.CorpusEngine)
            engine.source_dir = Mock(return_value=raw_root)
            engine.source_spec = Mock(return_value=SimpleNamespace(rights_state=corpus_engine.RightsState.PUBLIC_RIGHTS_CLEAR))
            engine.run_adapter = Mock(return_value=batch)
            engine.prior_youtube_revision = Mock(return_value=batch.source_revision)
            engine.acquire_youtube_transcript = Mock(return_value=(transcript, None))
            engine.write_card = Mock(return_value=page_slug)
            entry = {"id": "channel", "channel_id": "channel", "source_type": "youtube_channel", "evidence_lane": "practitioner", "acquire_transcripts": True}

            with patch.object(corpus_engine, "CORPUS_REPO", corpus_root):
                result = engine.refresh_youtube(entry, {"content_hash": batch.source_revision, "pages": [page_slug]})

            engine.acquire_youtube_transcript.assert_called_once_with("video-1", raw_root / "transcripts")
            self.assertEqual(result.status, "refreshed")
            self.assertIn(page_slug, result.pages)

    def test_agentic_engineering_baseline_fixture_preserves_registry_shape(self):
        fixture_path = Path(__file__).parent / "fixtures" / "agentic_engineering_registry.json"
        registry = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(registry["schema_version"], 1)
        self.assertEqual(registry["domain"], "agentic-engineering")
        self.assertEqual(len(registry["sources"]), 19)
        self.assertTrue(all(source.get("id") for source in registry["sources"]))
        self.assertTrue(all(source.get("source_type") for source in registry["sources"]))


if __name__ == "__main__":
    unittest.main()
