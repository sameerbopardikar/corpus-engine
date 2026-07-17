from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_adapters.base import AdapterRunner
from corpus_adapters.types import InventoryRequest, RightsState, SourceSpec
from corpus_adapters.youtube import (
    YouTubeFeedAdapter,
    decode_inventory_cursor,
    inventory_revision,
    normalize_caption_vtt,
    parse_feed,
    parse_inventory,
    preserve_caption_artifacts,
    write_revision_guarded,
)


FEED = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry><yt:videoId>new-video</yt:videoId><title>New video</title><published>2026-07-17T01:00:00Z</published><link href="https://www.youtube.com/watch?v=new-video"/></entry>
  <entry><yt:videoId>older-video</yt:videoId><title>Older video</title><published>2026-07-16T01:00:00Z</published><link href="https://www.youtube.com/watch?v=older-video"/></entry>
</feed>'''


class Response:
    content = FEED
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=channel"
    headers = {"content-type": "application/atom+xml"}
    encoding = "utf-8"


class YouTubeFeedAdapterTests(unittest.TestCase):
    def test_inventory_revision_is_stable_across_feed_and_fallback_transport(self):
        fallback = json.dumps(
            {
                "entries": [
                    {"id": "new-video", "title": "New video", "url": "https://www.youtube.com/watch?v=new-video"},
                    {"id": "older-video", "title": "Older video", "url": "https://www.youtube.com/watch?v=older-video"},
                ]
            }
        ).encode("utf-8")

        self.assertEqual(inventory_revision(parse_feed(FEED)), inventory_revision(parse_inventory(fallback)))

    def test_fallback_transport_metadata_does_not_create_no_delta_batch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            calls = iter(["first-run", "second-run"])

            def inventory(channel_id, max_items, timeout):
                return json.dumps(
                    {
                        "extractor_run": next(calls),
                        "entries": [{"id": "same-video", "title": "Same video", "url": "https://www.youtube.com/watch?v=same-video"}],
                    },
                    sort_keys=True,
                ).encode("utf-8")

            times = iter(["2026-07-17T06:00:00Z", "2026-07-17T06:01:00Z"])
            adapter = YouTubeFeedAdapter(
                root / "raw",
                channel_id="channel",
                http_get=lambda url, timeout: (_ for _ in ()).throw(RuntimeError("feed unavailable")),
                inventory_fetch=inventory,
                fetched_at=lambda: next(times),
            )
            spec = SourceSpec("channel", "agentic-engineering", "youtube", "https://www.youtube.com/channel/channel", "practitioner", RightsState.PUBLIC_METADATA_ONLY)
            runner = AdapterRunner(root / "state.json")
            first = runner.run(adapter, spec, InventoryRequest(max_items=1))
            before = (root / "state.json").read_bytes()
            second = runner.run(adapter, spec, InventoryRequest(max_items=1, cursor=first.cursor_after))

            self.assertEqual(second.observations, ())
            self.assertEqual(second.cursor_before, second.cursor_after)
            self.assertEqual((root / "state.json").read_bytes(), before)
            self.assertEqual(len(json.loads(before)["sources"]["channel"]["batches"]), 1)

    def test_feed_failure_falls_back_to_bounded_public_inventory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            calls = []

            def failed_feed(url, timeout):
                raise RuntimeError("feed unavailable")

            def inventory(channel_id, max_items, timeout):
                calls.append((channel_id, max_items, timeout))
                return json.dumps(
                    {
                        "id": channel_id,
                        "entries": [
                            {"id": "new-video", "title": "New video", "url": "https://www.youtube.com/watch?v=new-video"},
                            {"id": "older-video", "title": "Older video", "url": "https://www.youtube.com/watch?v=older-video"},
                        ],
                    },
                    sort_keys=True,
                ).encode("utf-8")

            adapter = YouTubeFeedAdapter(
                root / "raw",
                channel_id="channel",
                http_get=failed_feed,
                inventory_fetch=inventory,
                fetched_at=lambda: "2026-07-17T06:00:00Z",
            )
            spec = SourceSpec("channel", "agentic-engineering", "youtube", "https://www.youtube.com/channel/channel", "practitioner", RightsState.PUBLIC_METADATA_ONLY)
            batch = AdapterRunner(root / "state.json").run(adapter, spec, InventoryRequest(max_items=1, timeout_seconds=17))

            self.assertEqual(calls, [("channel", 100, 17.0)])
            self.assertEqual([item.title for item in batch.observations], ["New video"])
            self.assertTrue(Path(batch.observations[0].raw_pointer).name.endswith(".json"))

    def test_feed_inventory_is_bounded_and_metadata_only_before_transcription(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            adapter = YouTubeFeedAdapter(root / "raw", channel_id="channel", http_get=lambda url, timeout: Response(), fetched_at=lambda: "2026-07-17T06:00:00Z")
            spec = SourceSpec("channel", "agentic-engineering", "youtube", "https://www.youtube.com/channel/channel", "practitioner", RightsState.PUBLIC_METADATA_ONLY)
            batch = AdapterRunner(root / "state.json").run(adapter, spec, InventoryRequest(max_items=1))

            self.assertEqual(len(batch.observations), 1)
            self.assertEqual(batch.observations[0].title, "New video")
            self.assertEqual(batch.observations[0].content_kind, "video_metadata")
            self.assertNotIn("transcript", Path(batch.observations[0].normalized_pointer).read_text(encoding="utf-8").lower())
            cursor = decode_inventory_cursor(batch.cursor_after)
            self.assertEqual(cursor["watermark"], "new-video")
            self.assertEqual(cursor["pending_inventory"], ["older-video"])

    def test_inventory_cursor_emits_initial_backlog_once_then_only_new_items(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            adapter = YouTubeFeedAdapter(root / "raw", channel_id="channel", http_get=lambda url, timeout: Response(), fetched_at=lambda: "2026-07-17T06:00:00Z")
            spec = SourceSpec("channel", "agentic-engineering", "youtube", "https://www.youtube.com/channel/channel", "practitioner", RightsState.PUBLIC_METADATA_ONLY)
            runner = AdapterRunner(root / "state.json")

            first = runner.run(adapter, spec, InventoryRequest(max_items=1))
            second = runner.run(adapter, spec, InventoryRequest(max_items=1, cursor=first.cursor_after))
            before = (root / "state.json").read_bytes()
            third = runner.run(adapter, spec, InventoryRequest(max_items=1, cursor=second.cursor_after))

            self.assertEqual([item.title for item in first.observations], ["New video"])
            self.assertEqual([item.title for item in second.observations], ["Older video"])
            self.assertEqual(third.observations, ())
            self.assertEqual(third.cursor_before, third.cursor_after)
            self.assertEqual((root / "state.json").read_bytes(), before)
            self.assertNotEqual(first.source_revision, first.cursor_after)

    def test_caption_artifacts_are_content_addressed_and_historical_revisions_survive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first_raw = b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nFirst revision\n"
            second_raw = b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nCorrected revision\n"

            first = preserve_caption_artifacts(root, "video-1", first_raw)
            second = preserve_caption_artifacts(root, "video-1", second_raw)

            self.assertNotEqual(first.raw_path, second.raw_path)
            self.assertNotEqual(first.normalized_path, second.normalized_path)
            self.assertEqual(first.raw_sha256, hashlib.sha256(first_raw).hexdigest())
            self.assertEqual(second.raw_sha256, hashlib.sha256(second_raw).hexdigest())
            self.assertEqual(first.raw_path.read_bytes(), first_raw)
            self.assertEqual(second.raw_path.read_bytes(), second_raw)
            self.assertEqual(first.normalized_path.read_text(encoding="utf-8"), "First revision\n")
            self.assertEqual(second.normalized_path.read_text(encoding="utf-8"), "Corrected revision\n")

    def test_vtt_rollups_are_deduplicated_but_page_summary_cannot_be_used_as_transcript(self):
        vtt = """WEBVTT

00:00:00.000 --> 00:00:01.000
Build durable agents

00:00:01.000 --> 00:00:02.000
Build durable agents

00:00:02.000 --> 00:00:03.000
<b>Verify receipts</b>
"""
        self.assertEqual(normalize_caption_vtt(vtt), "Build durable agents\nVerify receipts\n")
        with self.assertRaisesRegex(ValueError, "WEBVTT"):
            normalize_caption_vtt("A page summary of the video")

    def test_same_source_revision_preserves_copied_live_projection_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "live"
            source.mkdir()
            for index in range(205):
                (source / f"video-{index:03d}.md").write_text(f"card {index}\n", encoding="utf-8")
            copied = root / "copied-live"
            shutil.copytree(source, copied)

            def manifest(path: Path):
                return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.iterdir())}

            before = manifest(copied)
            changed = write_revision_guarded(copied / "video-000.md", b"would overwrite\n", source_revision="rev-1", prior_revision="rev-1")
            self.assertFalse(changed)
            self.assertEqual(manifest(copied), before)
            self.assertEqual(len(before), 205)


if __name__ == "__main__":
    unittest.main()
