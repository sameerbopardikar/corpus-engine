from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_adapters.base import AdapterRunner
from corpus_adapters.rss import RSSAdapter
from corpus_adapters.types import InventoryRequest, RightsState, SourceSpec


RSS = b'''<rss version="2.0"><channel><title>Agent Platform Changelog</title>
<item><guid isPermaLink="false">release-2.4.1</guid><title>Agent runtime 2.4.1</title>
<link>https://vendor.example/changelog/2.4.1</link><pubDate>Wed, 15 Jul 2026 12:00:00 GMT</pubDate>
<description>Fixed tool retry accounting.</description></item></channel></rss>'''


class Response:
    content = RSS
    url = "https://vendor.example/changelog/feed.xml"


class RSSAdapterTests(unittest.TestCase):
    def test_official_changelog_entry_has_stable_guid_revision_and_class(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            adapter = RSSAdapter(root / "raw", feed_kind="official_changelog", http_get=lambda url, timeout: Response(), fetched_at=lambda: "2026-07-17T07:00:00Z")
            spec = SourceSpec("vendor-changelog", "agentic-engineering", "rss", Response.url, "official-changelog", RightsState.PUBLIC_RIGHTS_CLEAR)
            batch = AdapterRunner(root / "state.json").run(adapter, spec, InventoryRequest(max_items=1))
            observation = batch.observations[0]
            self.assertEqual(observation.source_revision, "release-2.4.1")
            self.assertEqual(observation.content_kind, "official_changelog_entry")
            self.assertEqual(observation.evidence_pointer, "https://vendor.example/changelog/2.4.1")

    def test_unknown_feed_kind_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, "feed_kind"):
                RSSAdapter(Path(td), feed_kind="doctrine", http_get=lambda url, timeout: Response(), fetched_at=lambda: "2026-07-17T07:00:00Z")


if __name__ == "__main__":
    unittest.main()
