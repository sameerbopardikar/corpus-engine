from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import stat
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from corpus_intake import IntakeError, IntakeStore


class IntakeStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "intake"
        self.corpus = Path(self.tmp.name) / "corpus"
        self.store = IntakeStore(self.root, max_upload_bytes=1024 * 1024)

    def tearDown(self):
        self.tmp.cleanup()

    def test_suggestion_preserves_exact_text_and_origin(self):
        text = "  Keep exact spacing\nSecond line  "
        receipt = self.store.submit_suggestion(
            title="Lease fencing source",
            text=text,
            source_url="https://example.com/paper",
            origin={"platform": "slack", "channel_id": "C1", "thread_ts": "1.2", "message_ts": "1.3"},
        )
        self.assertEqual(receipt["status"], "queued")
        row = self.store.get(receipt["receipt_id"])
        self.assertEqual(row["submitted_text"], text)
        self.assertEqual(row["origin"]["platform"], "slack")
        self.assertEqual(row["sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(row["history"][0]["status"], "queued")

    def test_exact_duplicate_has_new_origin_receipt_same_submission(self):
        first = self.store.submit_suggestion(title="A", text="same", origin={"platform": "web"})
        second = self.store.submit_suggestion(title="A", text="same", origin={"platform": "slack", "message_ts": "2"})
        self.assertNotEqual(first["receipt_id"], second["receipt_id"])
        self.assertEqual(first["submission_id"], second["submission_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.store.list_recent()), 2)

    def test_concurrent_duplicates_converge(self):
        def submit(index):
            return self.store.submit_suggestion(title="Concurrent", text="identical", origin={"platform": "test", "message_ts": str(index)})
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            rows = list(pool.map(submit, range(30)))
        self.assertEqual(len({row["submission_id"] for row in rows}), 1)
        self.assertEqual(len({row["receipt_id"] for row in rows}), 30)

    def test_file_bytes_are_immutable_and_unknown_rights_stays_pending(self):
        source = Path(self.tmp.name) / "note.md"
        payload = b"# Exact\r\nbytes\x00 are rejected"
        source.write_bytes(payload)
        with self.assertRaisesRegex(IntakeError, "NUL"):
            self.store.submit_file(title="Bad", file_path=source, rights_state="owned", origin={"platform": "cli"})
        source.write_bytes(b"# Exact\r\nbytes\n")
        receipt = self.store.submit_file(title="Exact", file_path=source, rights_state="unknown", origin={"platform": "web"})
        self.assertEqual(receipt["status"], "pending_review")
        blob = self.root / receipt["blob_path"]
        self.assertEqual(blob.read_bytes(), source.read_bytes())
        self.assertFalse(blob.stat().st_mode & stat.S_IWOTH)

    def test_rejects_symlink_executable_archive_and_oversize(self):
        target = Path(self.tmp.name) / "target.md"
        target.write_text("safe")
        link = Path(self.tmp.name) / "link.md"
        link.symlink_to(target)
        with self.assertRaisesRegex(IntakeError, "symlink"):
            self.store.submit_file(title="link", file_path=link, rights_state="owned", origin={})
        script = Path(self.tmp.name) / "run.sh"
        script.write_text("#!/bin/sh\necho bad")
        with self.assertRaisesRegex(IntakeError, "unsupported"):
            self.store.submit_file(title="script", file_path=script, rights_state="owned", origin={})
        archive = Path(self.tmp.name) / "bad.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("../escape.txt", "bad")
        with self.assertRaisesRegex(IntakeError, "unsupported"):
            self.store.submit_file(title="archive", file_path=archive, rights_state="owned", origin={})
        huge = Path(self.tmp.name) / "huge.txt"
        huge.write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaisesRegex(IntakeError, "too large"):
            self.store.submit_file(title="huge", file_path=huge, rights_state="owned", origin={})

    def test_malformed_metadata_rejected(self):
        with self.assertRaisesRegex(IntakeError, "title"):
            self.store.submit_suggestion(title="../", text="x", origin={})
        with self.assertRaisesRegex(IntakeError, "origin"):
            self.store.submit_suggestion(title="Ok", text="x", origin={"platform": ["bad"]})
        with self.assertRaisesRegex(IntakeError, "source_url"):
            self.store.submit_suggestion(title="Ok", text="x", source_url="file:///etc/passwd", origin={})

    def test_processing_writes_provenance_card_and_is_idempotent(self):
        source = Path(self.tmp.name) / "proof.txt"
        source.write_text("External source text.\n", encoding="utf-8")
        receipt = self.store.submit_file(
            title="Owned proof",
            file_path=source,
            note="Sameer's framing, not external evidence.",
            source_url="https://example.com/proof",
            rights_state="owned",
            origin={"platform": "slack", "file_id": "F1"},
        )
        result = self.store.process_next(corpus_root=self.corpus)
        self.assertEqual(result["status"], "processed")
        card = self.corpus / result["corpus_path"]
        body = card.read_text(encoding="utf-8")
        self.assertIn("receipt_id:", body)
        self.assertIn("Sameer suggestion", body)
        self.assertIn("External source text.", body)
        self.assertIn("adopted_doctrine: false", body)
        before = card.read_bytes()
        self.assertIsNone(self.store.process_next(corpus_root=self.corpus))
        self.assertEqual(card.read_bytes(), before)
        self.assertTrue(os.access(card, os.R_OK))
        evaluation = self.root / result["evaluation_receipt"]
        evaluation_data = json.loads(evaluation.read_text(encoding="utf-8"))
        self.assertEqual(evaluation_data["submission_id"], receipt["submission_id"])
        self.assertTrue(evaluation_data["passed"])
        self.assertTrue(all(evaluation_data["checks"].values()))

    def test_created_card_is_readable_by_dashboard_service_account(self):
        if not shutil.which("setfacl") or not shutil.which("runuser"):
            self.skipTest("ACL verification tools unavailable")
        if subprocess.run(["id", "agentic-dashboard"], capture_output=True).returncode != 0:
            self.skipTest("agentic-dashboard account unavailable")
        Path(self.tmp.name).chmod(0o755)
        self.corpus.mkdir(mode=0o755)
        source = Path(self.tmp.name) / "readable.md"
        source.write_text("dashboard must read this\n", encoding="utf-8")
        self.store.submit_file(title="ACL proof", file_path=source, rights_state="owned", origin={"platform": "test"})
        result = self.store.process_next(corpus_root=self.corpus)
        card = self.corpus / result["corpus_path"]
        subprocess.run(["runuser", "-u", "agentic-dashboard", "--", "test", "-r", str(card)], check=True)

    def test_invalid_queued_content_is_rejected_without_aborting_processor(self):
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text('{"not": valid}', encoding="utf-8")
        bad_receipt = self.store.submit_file(title="Bad JSON", file_path=bad, rights_state="owned", origin={})
        good_receipt = self.store.submit_suggestion(title="Good next item", text="keep processing", origin={})
        rejected = self.store.process_next(corpus_root=self.corpus)
        processed = self.store.process_next(corpus_root=self.corpus)
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["receipt_id"], bad_receipt["receipt_id"])
        self.assertEqual(processed["status"], "processed")
        self.assertEqual(processed["receipt_id"], good_receipt["receipt_id"])

    def test_suggestion_processes_as_candidate_not_evidence(self):
        receipt = self.store.submit_suggestion(title="Look at this", text="Maybe useful", source_url="https://example.com", origin={"platform": "web"})
        result = self.store.process_next(corpus_root=self.corpus)
        body = (self.corpus / result["corpus_path"]).read_text()
        self.assertIn('intake_kind: "candidate_suggestion"', body)
        self.assertIn("not external evidence", body)
        self.assertEqual(self.store.get(receipt["receipt_id"])["status"], "processed")


if __name__ == "__main__":
    unittest.main()
