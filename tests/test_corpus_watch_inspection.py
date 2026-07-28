from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = Path(__file__).parent / "fixtures" / "self_expansion"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_candidate_policy import evaluate_candidates
from corpus_discovery import DiscoveryEngine
from corpus_source_graph import (
    SourceRelationship,
    ingest_relationships,
    relationship_to_observation,
)
from corpus_watch_inspection import WatchInspectionError, run_watch_inspection

DOMAIN = "agentic-engineering"
WATCH_SOURCE = "https://github.com/example/agent-runtime"
EVALUATED_AT = "2026-07-27T20:00:00Z"
NOW = datetime(2026, 7, 27, 21, 0, 0, tzinfo=timezone.utc)


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class WatchInspectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.graph = self.root / "source-graph.jsonl"
        self.ledger = self.root / "discovery-ledger.jsonl"
        self.watch = self.root / "watch-projection.json"
        self.preserve = self.root / "artifacts"
        self.receipts = self.root / "receipts"
        self.repository_bytes = (FIXTURES / "github-repository.json").read_bytes()

    # --- Cycle A: promote the watch source through the real policy path ---

    def promote_watch_source(
        self,
        candidate: str = WATCH_SOURCE,
        *,
        rights_state: str = "private_authorized",
    ) -> dict:
        common = {
            "domain": DOMAIN,
            "entity_type": "repository",
            "canonical_url": candidate,
            "title": candidate,
            "topics": ("reliability", "tool-use", "verification"),
            "observed_at": "2026-07-27T12:00:00Z",
            "relationship_type": "linked_primary_source",
        }
        relationships = [
            SourceRelationship(
                source_family="paper",
                discovered_from_url="https://arxiv.org/abs/2607.12345",
                evidence_pointer="https://arxiv.org/abs/2607.12345?section=4",
                evidence_lane="scientific",
                **common,
            ),
            SourceRelationship(
                source_family="conference",
                discovered_from_url="https://conf.example/talks/agent-runtime",
                evidence_pointer="https://conf.example/talks/agent-runtime",
                evidence_lane="security-evaluation",
                **common,
            ),
        ]
        if rights_state != "rights_unclear":
            DiscoveryEngine(self.ledger).observe(
                relationship_to_observation(relationships[0]),
                rights_state=rights_state,
            )
        ingest_relationships(
            relationships,
            graph_path=self.graph,
            discovery_ledger_path=self.ledger,
        )
        result = evaluate_candidates(
            ledger_path=self.ledger,
            graph_path=self.graph,
            watch_projection_path=self.watch,
            evaluated_at=EVALUATED_AT,
        )
        self.assertEqual(result["counts"]["promoted"], 1)
        return result

    def artifact(self, **overrides) -> dict:
        request = {
            "artifact_url": WATCH_SOURCE,
            "source_family": "github",
            "content_bytes": self.repository_bytes,
            "content_sha256": sha256(self.repository_bytes),
            "fetched_at": "2026-07-27T21:00:00Z",
            "source_revision": "fixture-rev-1",
            "evidence_lane": "production-reliability",
            "topics": ["reliability", "tool-use"],
            "observed_at": "2026-07-27T20:30:00Z",
            "extraction": {"kind": "github"},
        }
        request.update(overrides)
        return request

    def inspect(self, inspections, **overrides):
        kwargs = {
            "domain": DOMAIN,
            "watch_projection_path": self.watch,
            "ledger_path": self.ledger,
            "graph_path": self.graph,
            "inspections": inspections,
            "preserve_root": self.preserve,
            "receipts_root": self.receipts,
            "now": NOW,
        }
        kwargs.update(overrides)
        return run_watch_inspection(**kwargs)

    # --- the causal path that must work ---

    def test_promoted_watch_source_is_inspected_through_the_durable_work_item(self):
        policy = self.promote_watch_source()
        work_id = policy["watch_work_ids"][0]

        result = self.inspect([{"watch_source_url": WATCH_SOURCE, "artifacts": [self.artifact()]}])

        self.assertEqual(result["processed_inspections"], 1)
        self.assertEqual(result["consumed_work_ids"], [work_id])
        self.assertGreater(result["relationships_derived"], 0)

        receipt = result["artifact_receipts"][0]
        self.assertEqual(receipt["sha256"], sha256(self.repository_bytes))
        self.assertEqual(receipt["binding"], "watch_source_identity")
        self.assertEqual(receipt["work_id"], work_id)
        preserved = Path(receipt["preserved_path"])
        self.assertTrue(preserved.is_file())
        self.assertEqual(sha256(preserved.read_bytes()), receipt["sha256"])

        engine = DiscoveryEngine(self.ledger)
        self.assertEqual(engine.work_items[work_id].state, "done")
        second_order = {record.canonical_url for record in engine.candidates.values()}
        self.assertIn("https://github.com/example/replay-core", second_order)
        self.assertIn("https://github.com/other/state-store", second_order)

    def test_completion_proof_binds_the_preserved_artifact_bytes(self):
        policy = self.promote_watch_source()
        work_id = policy["watch_work_ids"][0]
        self.inspect([{"watch_source_url": WATCH_SOURCE, "artifacts": [self.artifact()]}])

        engine = DiscoveryEngine(self.ledger)
        proof_path = Path(engine.work_items[work_id].proof_receipts[0])
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        self.assertEqual(proof["work_id"], work_id)
        self.assertTrue(proof["verified"])
        manifest_path = Path(proof["artifact_path"])
        self.assertEqual(sha256(manifest_path.read_bytes()), proof["artifact_sha256"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [item["sha256"] for item in manifest["artifact_receipts"]],
            [sha256(self.repository_bytes)],
        )

    def test_child_artifact_linked_from_preserved_parent_bytes_is_admitted(self):
        self.promote_watch_source()
        child_url = "https://docs.example.org/agent-runtime/recovery-notes"
        parent_bytes = json.dumps(
            {
                "repository_url": WATCH_SOURCE,
                "owner": {"login": "example", "html_url": "https://github.com/example"},
                "contributors": [],
                "dependencies": [],
                "documentation": [child_url],
            }
        ).encode("utf-8")
        child_html = (
            '<!doctype html><html><body>'
            '<article class="talk" data-speaker-url="https://example.org/people/nora-lee"'
            ' data-project-url="https://github.com/example/recovery-graphs">'
            '<span class="speaker">Nora Lee</span></article></body></html>'
        ).encode("utf-8")

        result = self.inspect(
            [
                {
                    "watch_source_url": WATCH_SOURCE,
                    "artifacts": [
                        self.artifact(content_bytes=parent_bytes, content_sha256=sha256(parent_bytes)),
                        self.artifact(
                            artifact_url=child_url,
                            source_family="conference",
                            content_bytes=child_html,
                            content_sha256=sha256(child_html),
                            parent_artifact_url=WATCH_SOURCE,
                            extraction={"kind": "conference"},
                        ),
                    ],
                }
            ]
        )
        bindings = {item["artifact_url"]: item["binding"] for item in result["artifact_receipts"]}
        self.assertEqual(bindings[child_url], "parent_artifact_link")
        candidates = {record.canonical_url for record in DiscoveryEngine(self.ledger).candidates.values()}
        self.assertIn("https://example.org/people/nora-lee", candidates)

    # --- adversarial: nothing may enter without a causal parent binding ---

    def test_promoted_source_does_not_unlock_an_unrelated_attacker_artifact(self):
        self.promote_watch_source()
        attacker = "https://attacker.example/predeclared-artifact"
        payload = json.dumps(
            {
                "repository_url": attacker,
                "owner": {"login": "attacker", "html_url": "https://github.com/attacker"},
                "contributors": [],
                "dependencies": ["https://github.com/attacker/backdoor"],
            }
        ).encode("utf-8")
        with self.assertRaisesRegex(WatchInspectionError, "not causally bound"):
            self.inspect(
                [
                    {
                        "watch_source_url": WATCH_SOURCE,
                        "artifacts": [
                            self.artifact(
                                artifact_url=attacker,
                                content_bytes=payload,
                                content_sha256=sha256(payload),
                            )
                        ],
                    }
                ]
            )
        candidates = {record.canonical_url for record in DiscoveryEngine(self.ledger).candidates.values()}
        self.assertNotIn("https://github.com/attacker/backdoor", candidates)

    def test_child_artifact_absent_from_parent_bytes_is_rejected(self):
        self.promote_watch_source()
        unlinked = "https://docs.example.org/never-referenced"
        html = b'<!doctype html><html><body><article class="talk" data-speaker-url="https://example.org/people/x" data-project-url="https://github.com/example/y"><span class="speaker">X</span></article></body></html>'
        with self.assertRaisesRegex(WatchInspectionError, "not causally bound"):
            self.inspect(
                [
                    {
                        "watch_source_url": WATCH_SOURCE,
                        "artifacts": [
                            self.artifact(),
                            self.artifact(
                                artifact_url=unlinked,
                                source_family="conference",
                                content_bytes=html,
                                content_sha256=sha256(html),
                                parent_artifact_url=WATCH_SOURCE,
                                extraction={"kind": "conference"},
                            ),
                        ],
                    }
                ]
            )

    def test_unpromoted_watch_source_is_never_fetched(self):
        self.promote_watch_source()
        result = self.inspect(
            [
                {
                    "watch_source_url": "https://github.com/example/not-promoted",
                    "artifacts": [self.artifact(artifact_url="https://github.com/example/not-promoted")],
                }
            ]
        )
        self.assertEqual(result["processed_inspections"], 0)
        self.assertEqual(result["skipped_unpromoted_sources"], ["https://github.com/example/not-promoted"])
        self.assertEqual(result["artifact_receipts"], [])
        self.assertFalse(self.preserve.exists() and any(self.preserve.iterdir()))

    def test_tracking_alias_of_the_promoted_source_resolves_to_the_same_watch_entry(self):
        policy = self.promote_watch_source()
        result = self.inspect(
            [
                {
                    "watch_source_url": f"{WATCH_SOURCE}?utm_source=cycle-b",
                    "artifacts": [self.artifact(artifact_url=f"{WATCH_SOURCE}?utm_source=cycle-b")],
                }
            ]
        )
        self.assertEqual(result["consumed_work_ids"], policy["watch_work_ids"])

    def test_modified_artifact_bytes_fail_the_fetch_receipt(self):
        self.promote_watch_source()
        with self.assertRaisesRegex(WatchInspectionError, "digest"):
            self.inspect(
                [
                    {
                        "watch_source_url": WATCH_SOURCE,
                        "artifacts": [self.artifact(content_sha256=sha256(b"different bytes"))],
                    }
                ]
            )

    def test_watch_entry_without_a_live_queue_item_fails_closed(self):
        self.promote_watch_source()
        projection = json.loads(self.watch.read_text(encoding="utf-8"))
        projection["entries"][0]["work_id"] = "work_" + "0" * 24
        self.watch.write_text(json.dumps(projection), encoding="utf-8")
        with self.assertRaisesRegex(WatchInspectionError, "work item"):
            self.inspect([{"watch_source_url": WATCH_SOURCE, "artifacts": [self.artifact()]}])

    def test_projection_entry_must_match_the_ledger_candidate_it_names(self):
        """A rewritten projection cannot point a real work item at another URL."""
        self.promote_watch_source()
        attacker = "https://attacker.example/pretend-source"
        projection = json.loads(self.watch.read_text(encoding="utf-8"))
        projection["entries"][0]["canonical_url"] = attacker
        self.watch.write_text(json.dumps(projection), encoding="utf-8")
        payload = json.dumps(
            {
                "repository_url": attacker,
                "owner": {"login": "attacker", "html_url": "https://github.com/attacker"},
                "contributors": [],
                "dependencies": ["https://github.com/attacker/backdoor"],
            }
        ).encode("utf-8")
        with self.assertRaisesRegex(WatchInspectionError, "does not match the promoted candidate"):
            self.inspect(
                [
                    {
                        "watch_source_url": attacker,
                        "artifacts": [
                            self.artifact(
                                artifact_url=attacker,
                                content_bytes=payload,
                                content_sha256=sha256(payload),
                            )
                        ],
                    }
                ]
            )
        candidates = {record.canonical_url for record in DiscoveryEngine(self.ledger).candidates.values()}
        self.assertNotIn("https://github.com/attacker/backdoor", candidates)

    def test_projection_entry_for_a_candidate_that_is_no_longer_promoted_fails_closed(self):
        self.promote_watch_source()
        engine = DiscoveryEngine(self.ledger)
        candidate_id = next(iter(engine.candidates))
        engine.transition_candidate(candidate_id, "blocked")
        with self.assertRaisesRegex(WatchInspectionError, "no longer promoted"):
            self.inspect([{"watch_source_url": WATCH_SOURCE, "artifacts": [self.artifact()]}])

    def test_receipt_records_how_the_artifact_bytes_were_obtained(self):
        self.promote_watch_source()
        result = self.inspect([{"watch_source_url": WATCH_SOURCE, "artifacts": [self.artifact()]}])
        self.assertEqual(result["artifact_receipts"][0]["transport"], "fixture-inline")

    def test_watch_projection_must_exist_before_any_inspection(self):
        with self.assertRaisesRegex(WatchInspectionError, "watch projection"):
            self.inspect([{"watch_source_url": WATCH_SOURCE, "artifacts": [self.artifact()]}])

    def test_replayed_inspection_neither_duplicates_edges_nor_reopens_completed_work(self):
        self.promote_watch_source()
        inspections = [{"watch_source_url": WATCH_SOURCE, "artifacts": [self.artifact()]}]
        first = self.inspect(inspections)
        graph_bytes = self.graph.read_bytes()

        second = self.inspect(
            inspections, now=NOW + timedelta(minutes=5)
        )
        self.assertEqual(second["processed_inspections"], 0)
        self.assertEqual(second["already_completed_work_ids"], first["consumed_work_ids"])
        self.assertEqual(self.graph.read_bytes(), graph_bytes)

    def test_inspection_preserves_existing_rights_and_grants_none_to_discoveries(self):
        self.promote_watch_source()
        self.inspect([{"watch_source_url": WATCH_SOURCE, "artifacts": [self.artifact()]}])
        engine = DiscoveryEngine(self.ledger)
        by_url = {record.canonical_url: record for record in engine.candidates.values()}
        self.assertEqual(by_url[WATCH_SOURCE].rights_state, "private_authorized")
        self.assertTrue(
            all(
                record.rights_state == "rights_unclear"
                for url, record in by_url.items()
                if url != WATCH_SOURCE
            )
        )
        self.assertEqual({item.action for item in engine.work_items.values()}, {"inspect"})

    def test_rights_unclear_source_cannot_preserve_or_extract_full_artifact_body(self):
        self.promote_watch_source(rights_state="rights_unclear")
        with self.assertRaisesRegex(WatchInspectionError, "rights"):
            self.inspect([{"watch_source_url": WATCH_SOURCE, "artifacts": [self.artifact()]}])
        self.assertFalse(self.preserve.exists() and any(self.preserve.iterdir()))

    def test_expired_inspection_lease_is_released_and_retried(self):
        policy = self.promote_watch_source()
        work_id = policy["watch_work_ids"][0]
        engine = DiscoveryEngine(self.ledger)
        engine.lease_work(work_id, owner="crashed-worker", ttl_seconds=1, now=NOW)

        result = self.inspect(
            [{"watch_source_url": WATCH_SOURCE, "artifacts": [self.artifact()]}],
            now=NOW + timedelta(seconds=2),
        )

        self.assertEqual(result["consumed_work_ids"], [work_id])
        self.assertEqual(DiscoveryEngine(self.ledger).work_items[work_id].state, "done")

    def test_derived_relationships_must_come_from_the_fetched_artifact(self):
        self.promote_watch_source()
        foreign = json.dumps(
            {
                "repository_url": "https://github.com/other/unrelated",
                "owner": {"login": "other", "html_url": "https://github.com/other"},
                "contributors": [],
                "dependencies": [],
            }
        ).encode("utf-8")
        with self.assertRaisesRegex(WatchInspectionError, "derived relationship"):
            self.inspect(
                [
                    {
                        "watch_source_url": WATCH_SOURCE,
                        "artifacts": [
                            self.artifact(content_bytes=foreign, content_sha256=sha256(foreign))
                        ],
                    }
                ]
            )


if __name__ == "__main__":
    unittest.main()
