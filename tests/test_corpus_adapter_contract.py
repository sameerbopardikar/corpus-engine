from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_adapters.base import AdapterFailure, AdapterRunner, SourceAdapter
from corpus_adapters.types import (
    FailureKind,
    InventoryRequest,
    NormalizedObservation,
    RightsState,
    SourceSpec,
    TransportPayload,
)


class FixtureAdapter(SourceAdapter):
    family = "fixture"

    def __init__(self, raw_path: Path, *, malformed: bool = False, fail_transport: bool = False):
        self.raw_path = raw_path
        self.malformed = malformed
        self.fail_transport = fail_transport
        self.fetch_count = 0

    def fetch(self, spec: SourceSpec, request: InventoryRequest) -> TransportPayload:
        self.fetch_count += 1
        if self.fail_transport:
            raise TimeoutError("fixture timeout")
        payload = b"not-json" if self.malformed else json.dumps(
            {"items": [{"url": "https://example.com/item/1", "title": "Item 1"}], "next": "cursor-1"}
        ).encode("utf-8")
        self.raw_path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_path.write_bytes(payload)
        return TransportPayload(
            body=payload,
            final_url="https://example.com/feed",
            fetched_at="2026-07-17T06:00:00Z",
            source_revision="rev-1",
            raw_pointer=str(self.raw_path),
        )

    def parse(self, spec: SourceSpec, request: InventoryRequest, payload: TransportPayload):
        document = json.loads(payload.body)
        observations = tuple(
            NormalizedObservation(
                canonical_locator=item["url"],
                title=item["title"],
                evidence_pointer=payload.final_url,
                raw_pointer=payload.raw_pointer,
                raw_sha256=hashlib.sha256(payload.body).hexdigest(),
                normalized_pointer=payload.raw_pointer,
                normalized_sha256=hashlib.sha256(payload.body).hexdigest(),
                content_kind="fixture_json",
                fetched_at=payload.fetched_at,
                source_revision=payload.source_revision,
                rights_state=spec.rights_state,
            )
            for item in document["items"][: request.max_items]
        )
        return observations, document["next"]


class AdapterContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw_path = self.root / "raw" / "fixture.json"
        self.state_path = self.root / "adapter-state.json"
        self.registry_path = self.root / "registry" / "sources.json"
        self.registry_path.parent.mkdir(parents=True)
        self.registry_path.write_text('{"schema_version": 1, "sources": []}\n', encoding="utf-8")
        self.registry_before = self.registry_path.read_bytes()
        self.spec = SourceSpec(
            source_id="fixture-source",
            domain="agentic-engineering",
            source_family="fixture",
            canonical_locator="https://example.com/feed",
            evidence_lane="scientific-mechanisms",
            rights_state=RightsState.PUBLIC_RIGHTS_CLEAR,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_contract_values_are_immutable_and_inventory_is_bounded(self):
        with self.assertRaises(FrozenInstanceError):
            self.spec.source_id = "changed"  # type: ignore[misc]
        with self.assertRaises(ValueError):
            InventoryRequest(max_items=0)
        with self.assertRaises(ValueError):
            InventoryRequest(max_items=101)
        with self.assertRaises(ValueError):
            SourceSpec(
                source_id="bad",
                domain="agentic-engineering",
                source_family="fixture",
                canonical_locator="file:///private/source",
                evidence_lane="scientific-mechanisms",
                rights_state=RightsState.PUBLIC_RIGHTS_CLEAR,
            )

    def test_success_commits_observations_and_cursor_together_without_registry_promotion(self):
        runner = AdapterRunner(self.state_path)
        batch = runner.run(FixtureAdapter(self.raw_path), self.spec, InventoryRequest(max_items=3))

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        source_state = state["sources"][self.spec.source_id]
        self.assertEqual(source_state["cursor"], "cursor-1")
        self.assertEqual(source_state["batches"][0]["batch_id"], batch.batch_id)
        self.assertEqual(source_state["batches"][0]["observations"][0]["title"], "Item 1")
        self.assertEqual(self.registry_path.read_bytes(), self.registry_before)
        self.assertEqual(os.stat(self.state_path).st_mode & 0o777, 0o600)

    def test_retry_is_physically_idempotent(self):
        runner = AdapterRunner(self.state_path)
        adapter = FixtureAdapter(self.raw_path)
        first = runner.run(adapter, self.spec, InventoryRequest(max_items=3))
        before = self.state_path.read_bytes()
        stat_before = self.state_path.stat()
        second = runner.run(adapter, self.spec, InventoryRequest(max_items=3, cursor=None))
        stat_after = self.state_path.stat()

        self.assertEqual(first.batch_id, second.batch_id)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(stat_after.st_ino, stat_before.st_ino)
        self.assertEqual(stat_after.st_mtime_ns, stat_before.st_mtime_ns)

    def test_parse_failure_is_classified_and_does_not_advance_cursor(self):
        runner = AdapterRunner(self.state_path)
        with self.assertRaises(AdapterFailure) as caught:
            runner.run(FixtureAdapter(self.raw_path, malformed=True), self.spec, InventoryRequest(max_items=3))
        self.assertEqual(caught.exception.kind, FailureKind.MALFORMED_RESPONSE)
        self.assertFalse(self.state_path.exists())

    def test_transport_failure_is_classified_retryable(self):
        runner = AdapterRunner(self.state_path)
        with self.assertRaises(AdapterFailure) as caught:
            runner.run(FixtureAdapter(self.raw_path, fail_transport=True), self.spec, InventoryRequest(max_items=3))
        self.assertEqual(caught.exception.kind, FailureKind.TRANSPORT)
        self.assertTrue(caught.exception.retryable)
        self.assertFalse(self.state_path.exists())

    def test_raw_pointer_hash_mismatch_fails_before_commit(self):
        class CorruptingAdapter(FixtureAdapter):
            def parse(self, spec, request, payload):
                observations, cursor = super().parse(spec, request, payload)
                self.raw_path.write_bytes(b"tampered-after-fetch")
                return observations, cursor

        runner = AdapterRunner(self.state_path)
        with self.assertRaises(AdapterFailure) as caught:
            runner.run(CorruptingAdapter(self.raw_path), self.spec, InventoryRequest(max_items=3))
        self.assertEqual(caught.exception.kind, FailureKind.RAW_INTEGRITY)
        self.assertFalse(self.state_path.exists())

    def test_normalized_pointer_hash_mismatch_fails_before_commit(self):
        normalized_path = self.root / "normalized.txt"

        class CorruptingNormalizedAdapter(FixtureAdapter):
            def parse(inner_self, spec, request, payload):
                observations, cursor = super().parse(spec, request, payload)
                normalized_path.write_bytes(b"normalized-good")
                observation = replace(
                    observations[0],
                    normalized_pointer=str(normalized_path),
                    normalized_sha256=hashlib.sha256(b"normalized-good").hexdigest(),
                )
                normalized_path.write_bytes(b"normalized-tampered")
                return (observation,), cursor

        with self.assertRaises(AdapterFailure) as caught:
            AdapterRunner(self.state_path).run(CorruptingNormalizedAdapter(self.raw_path), self.spec, InventoryRequest(max_items=3))
        self.assertEqual(caught.exception.kind, FailureKind.RAW_INTEGRITY)
        self.assertFalse(self.state_path.exists())

    def test_parser_cannot_upgrade_source_rights_state(self):
        restricted_spec = SourceSpec(
            source_id="fixture-source",
            domain="agentic-engineering",
            source_family="fixture",
            canonical_locator="https://example.com/feed",
            evidence_lane="scientific-mechanisms",
            rights_state=RightsState.RIGHTS_UNCLEAR,
        )

        class EscalatingAdapter(FixtureAdapter):
            def parse(self, spec, request, payload):
                observations, cursor = super().parse(spec, request, payload)
                observation = observations[0]
                return (
                    NormalizedObservation(
                        canonical_locator=observation.canonical_locator,
                        title=observation.title,
                        evidence_pointer=observation.evidence_pointer,
                        raw_pointer=observation.raw_pointer,
                        raw_sha256=observation.raw_sha256,
                        normalized_pointer=observation.normalized_pointer,
                        normalized_sha256=observation.normalized_sha256,
                        content_kind=observation.content_kind,
                        fetched_at=observation.fetched_at,
                        source_revision=observation.source_revision,
                        rights_state=RightsState.PUBLIC_RIGHTS_CLEAR,
                    ),
                ), cursor

        runner = AdapterRunner(self.state_path)
        with self.assertRaises(AdapterFailure) as caught:
            runner.run(EscalatingAdapter(self.raw_path), restricted_spec, InventoryRequest(max_items=3))
        self.assertEqual(caught.exception.kind, FailureKind.CONTRACT)
        self.assertFalse(self.state_path.exists())

    def test_cursor_conflict_fails_closed_without_mutation(self):
        runner = AdapterRunner(self.state_path)
        runner.run(FixtureAdapter(self.raw_path), self.spec, InventoryRequest(max_items=3))
        before = self.state_path.read_bytes()
        with self.assertRaises(AdapterFailure) as caught:
            runner.run(FixtureAdapter(self.raw_path), self.spec, InventoryRequest(max_items=3, cursor="stale"))
        self.assertEqual(caught.exception.kind, FailureKind.CURSOR_CONFLICT)
        self.assertEqual(self.state_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
