import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_acquisition_executor import (
    AcquisitionCandidate,
    AcquisitionExecutor,
    AcquisitionExecutorError,
    FetchResult,
    retrieval_topic_matches,
)
from corpus_engine_models import CandidateObservation
from corpus_rights_resolver import RightsEvidence


def _candidate(**overrides):
    canonical = overrides.pop("canonical_locator", "https://example.org/vbt")
    obs = CandidateObservation.create(
        domain=overrides.pop("domain", "training"),
        entity_type="paper",
        canonical_url=canonical,
        discovery_source="scholarly_discovery:openalex:W1",
        evidence_pointer=canonical,
        evidence_lane="primary-study",
        topics=overrides.pop("topics", ("hypertrophy",)),
        observed_at="2026-07-19T00:00:00Z",
    )
    base = dict(
        candidate_id=obs.candidate_key,
        domain=obs.domain,
        canonical_locator=canonical,
        content_locator=canonical,
        evidence_lane="primary-study",
        source_family="peer-reviewed-literature",
        title="Velocity based training and hypertrophy",
        topics=obs.topics,
        rights_evidence=RightsEvidence(license="cc-by", access_class="open_content", repository="pmc"),
        metadata={"openalex_id": "W1"},
        idempotency_key="global-2026-07-19:acquire:" + obs.candidate_key,
    )
    base.update(overrides)
    return AcquisitionCandidate(**base)


class _FakeFetcher:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []
        self.max_bytes_seen = []

    def __call__(self, url, *, timeout, max_bytes=None):
        self.calls.append((url, timeout))
        self.max_bytes_seen.append(max_bytes)
        if self.error is not None:
            raise self.error
        return self.result


def _html(body_text):
    return f"<html><head><title>VBT</title></head><body><p>{body_text}</p></body></html>".encode("utf-8")


class ExecutorHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.raw_root = root / "archive"
        self.staging_root = root / "staging"
        self.receipts_root = root / "receipts"
        self.quarantine_root = root / "quarantine"
        self.addCleanup(self._tmp.cleanup)

    def _executor(self, fetcher):
        return AcquisitionExecutor(
            raw_root=self.raw_root,
            staging_root=self.staging_root,
            receipts_root=self.receipts_root,
            quarantine_root=self.quarantine_root,
            http_fetch=fetcher,
            now=lambda: "2026-07-19T01:00:00Z",
        )


class ContentAcquisitionTests(ExecutorHarness):
    def test_open_rights_content_reaches_probationary(self):
        fetcher = _FakeFetcher(
            FetchResult(body=_html("hypertrophy adaptation evidence"), final_url="https://example.org/vbt", content_type="text/html")
        )
        receipt = self._executor(fetcher).acquire(_candidate())
        self.assertEqual(receipt["disposition"], "probationary")
        self.assertEqual(receipt["terminal_state"], "probationary")
        self.assertEqual(receipt["lifecycle"][0], "discovered")
        self.assertEqual(receipt["lifecycle"][-1], "probationary")
        self.assertIn("raw_preserved", receipt["lifecycle"])
        self.assertIn("staged", receipt["lifecycle"])
        self.assertEqual(len(receipt["raw"]["sha256"]), 64)
        self.assertEqual(len(receipt["normalized"]["sha256"]), 64)
        self.assertTrue(receipt["evaluation"]["passed"])
        self.assertFalse(receipt["canonical_mutated"])
        self.assertFalse(receipt["promotion_enabled"])

    def test_title_with_quotes_does_not_break_staged_frontmatter(self):
        fetcher = _FakeFetcher(
            FetchResult(body=_html("hypertrophy adaptation evidence"), final_url="https://example.org/vbt", content_type="text/html")
        )
        candidate = _candidate(title='A "quoted" title: with colons')
        receipt = self._executor(fetcher).acquire(candidate)
        self.assertEqual(receipt["disposition"], "probationary")
        staged = Path(receipt["staged"]["path"]).read_text(encoding="utf-8")
        # Frontmatter block must remain well-formed (opens and closes with ---).
        self.assertTrue(staged.startswith("---\n"))
        self.assertEqual(staged.count("\n---\n"), 1)
        self.assertNotIn('""', staged)

    def test_staged_page_lives_in_staging_not_canonical(self):
        fetcher = _FakeFetcher(
            FetchResult(body=_html("hypertrophy adaptation evidence"), final_url="https://example.org/vbt", content_type="text/html")
        )
        receipt = self._executor(fetcher).acquire(_candidate())
        staged = Path(receipt["staged"]["path"])
        self.assertTrue(staged.is_file())
        self.assertTrue(str(staged).startswith(str(self.staging_root)))
        text = staged.read_text(encoding="utf-8")
        self.assertIn("public_rights_clear", text)

    def test_byte_cap_is_threaded_to_the_fetch_contract(self):
        fetcher = _FakeFetcher(
            FetchResult(body=_html("hypertrophy adaptation evidence"), final_url="https://example.org/vbt", content_type="text/html")
        )
        executor = self._executor(fetcher)
        executor.max_bytes = 4242
        executor.acquire(_candidate())
        # The executor must hand its byte cap to the transport so the live
        # adapter can stop an oversized body before draining it.
        self.assertEqual(fetcher.max_bytes_seen[0], 4242)

    def test_exact_retry_is_idempotent_no_duplicate_files(self):
        fetcher = _FakeFetcher(
            FetchResult(body=_html("hypertrophy adaptation evidence"), final_url="https://example.org/vbt", content_type="text/html")
        )
        executor = self._executor(fetcher)
        candidate = _candidate()
        first = executor.acquire(candidate)
        calls_after_first = len(fetcher.calls)
        second = executor.acquire(candidate)
        self.assertEqual(first, second)
        # Second call short-circuits on the stored receipt: no extra fetch.
        self.assertEqual(len(fetcher.calls), calls_after_first)
        receipts = list(self.receipts_root.glob("*.json"))
        self.assertEqual(len(receipts), 1)
        raw_files = list(self.raw_root.glob("raw-*"))
        self.assertEqual(len(raw_files), 1)
        staged_files = list(self.staging_root.rglob("*.md"))
        self.assertEqual(len(staged_files), 1)


class MetadataAndGateTests(ExecutorHarness):
    def test_metadata_only_preserves_metadata_but_no_content(self):
        fetcher = _FakeFetcher(error=AssertionError("must not fetch content for metadata-only"))
        candidate = _candidate(rights_evidence=RightsEvidence(access_class="metadata", repository="openalex"))
        receipt = self._executor(fetcher).acquire(candidate)
        self.assertEqual(receipt["disposition"], "metadata_only")
        self.assertIsNone(receipt["staged"])
        self.assertIsNotNone(receipt["metadata_artifact"])
        self.assertEqual(len(fetcher.calls), 0)
        self.assertFalse(receipt["canonical_mutated"])
        # Lifecycle stops truthfully at rights_resolved (content never entered).
        self.assertEqual(receipt["terminal_state"], "rights_resolved")
        self.assertEqual(receipt["lifecycle"][-1], "rights_resolved")
        self.assertNotIn("rejected", receipt["lifecycle"])

    def test_private_source_is_human_gated_no_fetch(self):
        fetcher = _FakeFetcher(error=AssertionError("must not fetch a private source"))
        candidate = _candidate(rights_evidence=RightsEvidence(access_class="private"))
        receipt = self._executor(fetcher).acquire(candidate)
        self.assertEqual(receipt["disposition"], "human_gate")
        self.assertIsNone(receipt["staged"])
        self.assertEqual(len(fetcher.calls), 0)

    def test_paid_source_is_human_gated(self):
        fetcher = _FakeFetcher(error=AssertionError("must not fetch a paid source"))
        candidate = _candidate(rights_evidence=RightsEvidence(access_class="paid"))
        receipt = self._executor(fetcher).acquire(candidate)
        self.assertEqual(receipt["disposition"], "human_gate")
        self.assertEqual(len(fetcher.calls), 0)

    def test_rights_unclear_fails_closed(self):
        fetcher = _FakeFetcher(error=AssertionError("must not fetch an unclear source"))
        candidate = _candidate(rights_evidence=RightsEvidence(access_class="unknown", is_publicly_visible=True))
        receipt = self._executor(fetcher).acquire(candidate)
        self.assertEqual(receipt["disposition"], "rights_unclear")
        self.assertEqual(len(fetcher.calls), 0)


class AcquisitionFailureTests(ExecutorHarness):
    def test_bad_locator_is_rejected_before_fetch(self):
        fetcher = _FakeFetcher(error=AssertionError("must not fetch a bad locator"))
        candidate = _candidate(canonical_locator="ftp://example.org/x", content_locator="ftp://example.org/x")
        receipt = self._executor(fetcher).acquire(candidate)
        self.assertEqual(receipt["disposition"], "rejected")
        self.assertEqual(receipt["terminal_state"], "rejected")
        self.assertEqual(len(fetcher.calls), 0)

    def test_size_cap_exceeded_is_failed(self):
        fetcher = _FakeFetcher(
            FetchResult(body=b"x" * 10_000, final_url="https://example.org/vbt", content_type="text/plain")
        )
        executor = self._executor(fetcher)
        executor.max_bytes = 1000
        receipt = executor.acquire(_candidate())
        self.assertEqual(receipt["disposition"], "failed")
        self.assertIn("size", receipt["failure"]["reason"])
        self.assertIsNone(receipt["staged"])

    def test_unsupported_binary_mime_is_failed(self):
        fetcher = _FakeFetcher(
            FetchResult(body=b"\x89PNG binary", final_url="https://example.org/vbt.png", content_type="image/png")
        )
        receipt = self._executor(fetcher).acquire(_candidate())
        self.assertEqual(receipt["disposition"], "failed")
        self.assertIn("mime", receipt["failure"]["reason"])

    def test_pdf_is_human_gated_pending_extractor_not_acquired(self):
        # A PDF is real full text but there is no packaged extractor: truthfully
        # route it to a human/extractor gate rather than reporting it acquired or
        # fabricating text from a landing shell.
        fetcher = _FakeFetcher(
            FetchResult(body=b"%PDF-1.7 binary content", final_url="https://example.org/vbt.pdf", content_type="application/pdf")
        )
        receipt = self._executor(fetcher).acquire(_candidate())
        self.assertEqual(receipt["disposition"], "human_gate")
        self.assertNotEqual(receipt["terminal_state"], "probationary")
        self.assertIsNone(receipt["staged"])
        self.assertEqual(receipt["gate"]["content_type"], "application/pdf")

    def test_redirect_to_credentialed_url_is_failed(self):
        fetcher = _FakeFetcher(
            FetchResult(
                body=_html("hypertrophy"),
                final_url="https://user:pass@example.org/vbt",
                content_type="text/html",
                redirect_chain=("https://example.org/vbt",),
            )
        )
        receipt = self._executor(fetcher).acquire(_candidate())
        self.assertEqual(receipt["disposition"], "failed")
        self.assertIn("redirect", receipt["failure"]["reason"])
        self.assertIsNone(receipt["staged"])

    def test_transport_error_is_failed_receipt(self):
        fetcher = _FakeFetcher(error=TimeoutError("boom"))
        receipt = self._executor(fetcher).acquire(_candidate())
        self.assertEqual(receipt["disposition"], "failed")
        self.assertIsNone(receipt["staged"])

    def test_locator_to_private_ip_is_rejected_before_fetch(self):
        fetcher = _FakeFetcher(error=AssertionError("must not fetch a private target"))
        candidate = _candidate(canonical_locator="http://169.254.169.254/latest/",
                               content_locator="http://169.254.169.254/latest/")
        receipt = self._executor(fetcher).acquire(candidate)
        self.assertEqual(receipt["disposition"], "rejected")
        self.assertEqual(len(fetcher.calls), 0)

    def test_locator_to_localhost_name_is_rejected(self):
        fetcher = _FakeFetcher(error=AssertionError("must not fetch localhost"))
        candidate = _candidate(canonical_locator="http://localhost/x", content_locator="http://localhost/x")
        receipt = self._executor(fetcher).acquire(candidate)
        self.assertEqual(receipt["disposition"], "rejected")
        self.assertEqual(len(fetcher.calls), 0)

    def test_redirect_to_private_ip_is_failed(self):
        fetcher = _FakeFetcher(
            FetchResult(
                body=_html("hypertrophy"),
                final_url="http://10.0.0.5/internal",
                content_type="text/html",
                redirect_chain=("https://example.org/vbt",),
            )
        )
        receipt = self._executor(fetcher).acquire(_candidate())
        self.assertEqual(receipt["disposition"], "failed")
        self.assertIn("redirect", receipt["failure"]["reason"])
        self.assertIsNone(receipt["staged"])

    def test_redirect_hop_to_metadata_ip_is_failed(self):
        fetcher = _FakeFetcher(
            FetchResult(
                body=_html("hypertrophy"),
                final_url="https://example.org/vbt",
                content_type="text/html",
                redirect_chain=("http://169.254.169.254/",),
            )
        )
        receipt = self._executor(fetcher).acquire(_candidate())
        self.assertEqual(receipt["disposition"], "failed")
        self.assertIn("redirect", receipt["failure"]["reason"])


class EvaluationRollbackTests(ExecutorHarness):
    def test_generated_composite_topic_matches_domain_and_title_semantics(self):
        candidate = _candidate(
            domain="nutrition",
            topics=("nutrition-foundations",),
            title="Dietary intakes and nutrition status among young children",
        )
        self.assertTrue(
            retrieval_topic_matches(
                candidate,
                "Food security and dietary intake were measured before harvest.",
            )
        )

    def test_generated_composite_topic_rejects_unrelated_title_and_content(self):
        candidate = _candidate(
            domain="nutrition",
            topics=("nutrition-foundations",),
            title="Distributed database replication under network partitions",
        )
        self.assertFalse(
            retrieval_topic_matches(
                candidate,
                "This systems paper evaluates consensus latency and storage throughput.",
            )
        )

    def test_retrieval_failure_quarantines_staged_page(self):
        # Body contains none of the candidate topics -> retrieval eval fails.
        fetcher = _FakeFetcher(
            FetchResult(
                body=_html("this is a long stretch of unrelated boilerplate text mentioning nothing relevant to the query at all"),
                final_url="https://example.org/vbt",
                content_type="text/html",
            )
        )
        candidate = _candidate(
            topics=("hypertrophy",),
            title="Distributed database replication under network partitions",
        )
        receipt = self._executor(fetcher).acquire(candidate)
        self.assertEqual(receipt["disposition"], "quarantined")
        self.assertEqual(receipt["terminal_state"], "quarantined")
        self.assertIsNotNone(receipt["quarantine"])
        # The staged page must be moved OUT of staging (canonical never touched).
        staged_files = list(self.staging_root.rglob("*.md"))
        self.assertEqual(staged_files, [])
        quarantined = Path(receipt["quarantine"]["path"])
        self.assertTrue(quarantined.is_file())
        self.assertFalse(receipt["canonical_mutated"])


class ReceiptIntegrityTests(ExecutorHarness):
    def _ok_fetcher(self):
        return _FakeFetcher(
            FetchResult(body=_html("hypertrophy adaptation evidence"), final_url="https://example.org/vbt", content_type="text/html")
        )

    def test_receipt_records_preimage_digest(self):
        receipt = self._executor(self._ok_fetcher()).acquire(_candidate())
        self.assertEqual(len(receipt["preimage_digest"]), 64)

    def test_same_key_different_candidate_fails_closed(self):
        executor = self._executor(self._ok_fetcher())
        executor.acquire(_candidate(idempotency_key="shared-key", title="First"))
        # Reuse the SAME idempotency key with a materially different candidate.
        with self.assertRaises(AcquisitionExecutorError):
            executor.acquire(_candidate(
                idempotency_key="shared-key", title="Different",
                canonical_locator="https://example.org/other", content_locator="https://example.org/other",
            ))

    def test_exact_same_preimage_reuses_receipt(self):
        executor = self._executor(self._ok_fetcher())
        first = executor.acquire(_candidate(idempotency_key="stable-key"))
        second = executor.acquire(_candidate(idempotency_key="stable-key"))
        self.assertEqual(first, second)

    def test_tampered_stored_receipt_is_rejected(self):
        executor = self._executor(self._ok_fetcher())
        candidate = _candidate()
        executor.acquire(candidate)
        receipt_path = next(self.receipts_root.glob("*.json"))
        stored = json.loads(receipt_path.read_text())
        stored["disposition"] = "promoted"  # tamper without fixing the digest
        receipt_path.write_text(json.dumps(stored))
        with self.assertRaises(AcquisitionExecutorError):
            executor.acquire(candidate)

    def test_preplanted_lock_symlink_fails_closed_without_fetch(self):
        fetcher = self._ok_fetcher()
        executor = self._executor(fetcher)
        candidate = _candidate(idempotency_key="symlink-lock")
        lock_dir = self.receipts_root / ".locks"
        lock_dir.mkdir(parents=True)
        target = Path(self._tmp.name) / "outside-lock-target"
        target.write_text("do not touch", encoding="utf-8")
        (lock_dir / "symlink-lock.lock").symlink_to(target)
        with self.assertRaises(AcquisitionExecutorError):
            executor.acquire(candidate)
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(target.read_text(encoding="utf-8"), "do not touch")
        self.assertEqual(list(self.receipts_root.glob("*.json")), [])

    def test_preplanted_receipt_symlink_fails_closed_without_reading_target(self):
        fetcher = self._ok_fetcher()
        executor = self._executor(fetcher)
        candidate = _candidate(idempotency_key="symlink-receipt")
        self.receipts_root.mkdir(parents=True)
        target = Path(self._tmp.name) / "outside-receipt.json"
        target.write_text('{"secret":"outside"}', encoding="utf-8")
        (self.receipts_root / "symlink-receipt.json").symlink_to(target)
        with self.assertRaisesRegex(AcquisitionExecutorError, "symlink"):
            executor.acquire(candidate)
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(target.read_text(encoding="utf-8"), '{"secret":"outside"}')

    def test_concurrent_same_key_converges_to_one_fetch_and_receipt(self):
        import threading
        import time

        calls = []

        def slow_fetch(url, *, timeout, max_bytes=None):
            calls.append(url)
            time.sleep(0.15)
            return FetchResult(body=_html("hypertrophy adaptation evidence"),
                               final_url=url, content_type="text/html")

        executor = AcquisitionExecutor(
            raw_root=self.raw_root, staging_root=self.staging_root,
            receipts_root=self.receipts_root, quarantine_root=self.quarantine_root,
            http_fetch=slow_fetch, now=lambda: "2026-07-19T01:00:00Z",
        )
        candidate = _candidate()
        results = []

        def worker():
            results.append(executor.acquire(candidate))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # The per-key lock serializes both attempts: exactly one fetch, one raw
        # artifact, and one receipt, and both callers see the same receipt.
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(list(self.receipts_root.glob("*.json"))), 1)
        self.assertEqual(len(list(self.raw_root.glob("raw-*"))), 1)
        self.assertEqual(results[0], results[1])


class CanonicalWriteTests(ExecutorHarness):
    def setUp(self):
        super().setUp()
        self.canonical_root = Path(self._tmp.name) / "corpora" / "training"

    def _canonical_executor(self, fetcher):
        return AcquisitionExecutor(
            raw_root=self.raw_root, staging_root=self.staging_root,
            receipts_root=self.receipts_root, quarantine_root=self.quarantine_root,
            canonical_root=self.canonical_root,
            http_fetch=fetcher, now=lambda: "2026-07-19T01:00:00Z",
        )

    def _ok_fetcher(self):
        return _FakeFetcher(
            FetchResult(body=_html("hypertrophy adaptation evidence"), final_url="https://example.org/vbt", content_type="text/html")
        )

    def test_probationary_installs_canonical_evidence_page(self):
        receipt = self._canonical_executor(self._ok_fetcher()).acquire(_candidate())
        self.assertEqual(receipt["disposition"], "probationary")
        self.assertTrue(receipt["canonical_mutated"])
        # Evidence admission, NOT doctrine promotion.
        self.assertFalse(receipt["promotion_enabled"])
        self.assertFalse(receipt["production_mutation"])
        canonical_path = Path(receipt["canonical"]["path"])
        self.assertTrue(canonical_path.is_file())
        self.assertEqual(
            canonical_path.parent,
            self.canonical_root / "training" / "sources" / "acquired",
        )
        self.assertEqual(len(receipt["canonical"]["sha256"]), 64)

    def test_canonical_bytes_match_staged_bytes(self):
        receipt = self._canonical_executor(self._ok_fetcher()).acquire(_candidate())
        self.assertEqual(receipt["canonical"]["sha256"], receipt["staged"]["sha256"])
        canonical_text = Path(receipt["canonical"]["path"]).read_text(encoding="utf-8")
        self.assertIn("public_rights_clear", canonical_text)

    def test_rollback_receipt_records_prior_absence(self):
        receipt = self._canonical_executor(self._ok_fetcher()).acquire(_candidate())
        self.assertFalse(receipt["canonical"]["rollback"]["existed"])
        self.assertIsNone(receipt["canonical"]["rollback"]["previous_sha256"])

    def test_eval_failure_leaves_canonical_untouched(self):
        fetcher = _FakeFetcher(
            FetchResult(
                body=_html("unrelated boilerplate text mentioning nothing relevant at all here"),
                final_url="https://example.org/vbt", content_type="text/html",
            )
        )
        receipt = self._canonical_executor(fetcher).acquire(
            _candidate(
                topics=("hypertrophy",),
                title="Distributed database replication under network partitions",
            )
        )
        self.assertEqual(receipt["disposition"], "quarantined")
        self.assertFalse(receipt["canonical_mutated"])
        self.assertIsNone(receipt["canonical"])
        acquired_dir = self.canonical_root / "training" / "sources" / "acquired"
        self.assertFalse(acquired_dir.exists() and any(acquired_dir.iterdir()))

    def test_exact_retry_does_not_rewrite_canonical(self):
        executor = self._canonical_executor(self._ok_fetcher())
        candidate = _candidate()
        first = executor.acquire(candidate)
        canonical_path = Path(first["canonical"]["path"])
        mtime_before = canonical_path.stat().st_mtime_ns
        second = executor.acquire(candidate)
        self.assertEqual(first, second)
        self.assertEqual(canonical_path.stat().st_mtime_ns, mtime_before)
        acquired_dir = self.canonical_root / "training" / "sources" / "acquired"
        self.assertEqual(len(list(acquired_dir.glob("*.md"))), 1)


class TransactionalAdmissionTests(ExecutorHarness):
    """Canonical admission must be transactionally safe: if the receipt cannot be
    persisted after canonical bytes are written, the canonical file is restored
    to its EXACT prior state (bytes + mtime) or deleted if it was newly created.
    """

    def setUp(self):
        super().setUp()
        self.canonical_root = Path(self._tmp.name) / "corpora" / "training"
        self.acquired_dir = self.canonical_root / "training" / "sources" / "acquired"

    def _canonical_executor(self, fetcher):
        return AcquisitionExecutor(
            raw_root=self.raw_root, staging_root=self.staging_root,
            receipts_root=self.receipts_root, quarantine_root=self.quarantine_root,
            canonical_root=self.canonical_root,
            http_fetch=fetcher, now=lambda: "2026-07-19T01:00:00Z",
        )

    def _ok_fetcher(self):
        return _FakeFetcher(
            FetchResult(body=_html("hypertrophy adaptation evidence"), final_url="https://example.org/vbt", content_type="text/html")
        )

    def test_final_receipt_failure_deletes_newly_created_canonical(self):
        executor = self._canonical_executor(self._ok_fetcher())

        def boom(receipt, candidate):
            raise OSError("receipt store unavailable")

        executor._finalize = boom
        with self.assertRaises(AcquisitionExecutorError):
            executor.acquire(_candidate())
        # The canonical file was newly created this attempt: it must be gone.
        self.assertFalse(any(self.acquired_dir.glob("*.md")))

    def test_final_receipt_failure_restores_exact_prior_bytes_and_mtime(self):
        from corpus_acquisition_executor import _slug
        import os

        candidate = _candidate()
        self.acquired_dir.mkdir(parents=True, exist_ok=True)
        canonical_path = self.acquired_dir / f"{_slug(candidate.candidate_id)}.md"
        prior_bytes = b"PRIOR CANONICAL EVIDENCE BYTES\n"
        canonical_path.write_bytes(prior_bytes)
        prior_ns = 1_600_000_000_000_000_000
        os.utime(canonical_path, ns=(prior_ns, prior_ns))
        prior_mtime_ns = canonical_path.stat().st_mtime_ns

        executor = self._canonical_executor(self._ok_fetcher())

        def boom(receipt, candidate):
            raise OSError("receipt store unavailable")

        executor._finalize = boom
        with self.assertRaises(AcquisitionExecutorError):
            executor.acquire(candidate)
        # Exact prior bytes and mtime are restored — the admission never happened.
        self.assertEqual(canonical_path.read_bytes(), prior_bytes)
        self.assertEqual(canonical_path.stat().st_mtime_ns, prior_mtime_ns)

    def test_rollback_receipt_exposes_only_hashes_not_prior_text(self):
        # Sanity: the rollback record on a *successful* admission carries only a
        # hash of prior bytes, never the prior text itself.
        from corpus_acquisition_executor import _slug

        candidate = _candidate()
        self.acquired_dir.mkdir(parents=True, exist_ok=True)
        canonical_path = self.acquired_dir / f"{_slug(candidate.candidate_id)}.md"
        secret_prior = "TOP-SECRET-PRIOR-CANONICAL-PLAINTEXT\n"
        canonical_path.write_text(secret_prior, encoding="utf-8")

        receipt = self._canonical_executor(self._ok_fetcher()).acquire(candidate)
        rollback = receipt["canonical"]["rollback"]
        self.assertTrue(rollback["existed"])
        self.assertEqual(len(rollback["previous_sha256"]), 64)
        self.assertNotIn("TOP-SECRET", json.dumps(receipt))

    def test_restore_failure_raises_compound_error_and_writes_emergency_marker(self):
        executor = self._canonical_executor(self._ok_fetcher())

        def boom_finalize(receipt, candidate):
            raise OSError("receipt store unavailable")

        def boom_restore(*args, **kwargs):
            raise OSError("canonical volume vanished")

        executor._finalize = boom_finalize
        executor._restore_canonical = boom_restore
        with self.assertRaises(AcquisitionExecutorError) as ctx:
            executor.acquire(_candidate())
        # The compound error must NOT falsely claim a clean rollback.
        message = str(ctx.exception).lower()
        self.assertIn("rollback", message)
        self.assertIn("fail", message)
        # A local emergency marker is preserved for operator recovery.
        markers = list((self.receipts_root / ".emergency").glob("*"))
        self.assertTrue(markers, "expected an emergency marker after a failed rollback")
        # The marker must not carry prior private text (hashes only).
        marker_blob = markers[0].read_text(encoding="utf-8")
        self.assertNotIn("hypertrophy adaptation evidence", marker_blob)


class PathEscapeTests(ExecutorHarness):
    def test_domain_path_traversal_is_rejected(self):
        fetcher = _FakeFetcher(
            FetchResult(body=_html("hypertrophy adaptation evidence"), final_url="https://example.org/vbt", content_type="text/html")
        )
        # A candidate whose domain tries to escape the staging root must never
        # write outside it.
        candidate = _candidate(domain="../../escape")
        receipt = self._executor(fetcher).acquire(candidate)
        self.assertEqual(receipt["disposition"], "failed")
        escaped = (self.staging_root.parent.parent / "escape")
        self.assertFalse(escaped.exists())


if __name__ == "__main__":
    unittest.main()
