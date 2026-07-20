from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_x_signal_ingest import XSignalContractError, ingest_x_signals


class XSignalIngestTests(unittest.TestCase):
    def signal(self, **overrides):
        value = {
            "author": "@builder",
            "published_at": "2026-07-16T20:15:00Z",
            "url": "https://x.com/builder/status/101",
            "text": "Retry storms disappeared after fencing every lease generation.",
            "thread_id": "x-thread-77",
            "linked_primary_sources": [
                "https://github.com/example/agent-runtime/commit/abc123",
                "https://example.com/postmortems/retry-storm",
            ],
        }
        value.update(overrides)
        return value

    def test_preserves_exact_signal_and_primary_source_links_with_hard_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "x-signals.json"
            result = ingest_x_signals([self.signal()], output, max_items=10)

            self.assertEqual(result["signals"][0]["author"], "@builder")
            self.assertEqual(result["signals"][0]["published_at"], "2026-07-16T20:15:00Z")
            self.assertEqual(result["signals"][0]["url"], "https://x.com/builder/status/101")
            self.assertEqual(
                result["signals"][0]["text"],
                "Retry storms disappeared after fencing every lease generation.",
            )
            self.assertEqual(
                result["threads"][0]["linked_primary_sources"],
                [
                    "https://example.com/postmortems/retry-storm",
                    "https://github.com/example/agent-runtime/commit/abc123",
                ],
            )
            self.assertEqual(result["evidence_class"], "unverified_discovery_signal")
            self.assertFalse(result["doctrine_eligible"])
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), result)

    def test_duplicate_thread_converges_and_exact_retry_is_byte_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "x-signals.json"
            first = self.signal()
            reply = self.signal(
                author="@maintainer",
                published_at="2026-07-16T20:20:00Z",
                url="https://x.com/maintainer/status/102",
                text="Primary fix and incident notes are linked above.",
                linked_primary_sources=[
                    "https://github.com/example/agent-runtime/commit/abc123",
                ],
            )
            result = ingest_x_signals([reply, first, first], output, max_items=10)
            before = output.read_bytes()
            replay = ingest_x_signals([first, reply], output, max_items=10)

            self.assertEqual(len(result["threads"]), 1)
            self.assertEqual(result["threads"][0]["signal_urls"], [first["url"], reply["url"]])
            self.assertEqual(len(result["signals"]), 2)
            self.assertEqual(replay, result)
            self.assertEqual(output.read_bytes(), before)

    def test_conflicting_duplicate_url_fails_closed_without_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "x-signals.json"
            ingest_x_signals([self.signal()], output, max_items=10)
            before = output.read_bytes()
            conflicting = self.signal(text="A conflicting rendering of the same post")

            with self.assertRaisesRegex(XSignalContractError, "conflicting duplicate URL"):
                ingest_x_signals([self.signal(), conflicting], output, max_items=10)
            self.assertEqual(output.read_bytes(), before)

    def test_concurrent_batches_merge_without_losing_a_thread_member(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "x-signals.json"
            barrier = threading.Barrier(2)
            errors: list[BaseException] = []

            def worker(signal):
                try:
                    barrier.wait(timeout=5)
                    ingest_x_signals([signal], output, max_items=10)
                except BaseException as exc:
                    errors.append(exc)

            first = self.signal()
            reply = self.signal(
                author="@maintainer",
                url="https://x.com/maintainer/status/102",
                published_at="2026-07-16T20:20:00Z",
                text="The immutable revision is in the linked commit.",
                linked_primary_sources=["https://github.com/example/agent-runtime/commit/abc123"],
            )
            workers = [threading.Thread(target=worker, args=(item,)) for item in (first, reply)]
            for worker_thread in workers:
                worker_thread.start()
            for worker_thread in workers:
                worker_thread.join(timeout=10)

            self.assertFalse(errors)
            self.assertTrue(all(not worker_thread.is_alive() for worker_thread in workers))
            projection = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(projection["signals"]), 2)
            self.assertEqual(len(projection["threads"]), 1)

    def test_rejects_missing_exact_fields_x_links_as_primary_and_unbounded_batches(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "x-signals.json"
            for field in ("author", "published_at", "url", "text", "thread_id"):
                value = self.signal()
                value.pop(field)
                with self.subTest(field=field), self.assertRaises(XSignalContractError):
                    ingest_x_signals([value], output, max_items=10)

            with self.assertRaisesRegex(XSignalContractError, "primary-source"):
                ingest_x_signals(
                    [self.signal(linked_primary_sources=["https://x.com/other/status/999"])],
                    output,
                    max_items=10,
                )
            with self.assertRaisesRegex(XSignalContractError, "max_items"):
                ingest_x_signals([self.signal()], output, max_items=0)
            with self.assertRaisesRegex(XSignalContractError, "exceeds max_items"):
                ingest_x_signals([self.signal(), self.signal(url="https://x.com/builder/status/103")], output, max_items=1)


if __name__ == "__main__":
    unittest.main()
