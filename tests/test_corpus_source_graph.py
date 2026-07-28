from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import corpus_source_graph as source_graph_module
from corpus_discovery import DiscoveryEngine
from corpus_engine_models import CandidateObservation
from corpus_source_graph import (
    SourceGraphContractError,
    ingest_relationships,
    relationships_from_x_projection,
)


class SourceGraphExpansionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.graph = self.root / "source-graph.jsonl"
        self.discovery = self.root / "discovery-ledger.jsonl"

    def relation(self, **overrides):
        value = {
            "domain": "agentic-engineering",
            "source_family": "podcast",
            "relationship_type": "guest_of",
            "entity_type": "creator",
            "canonical_url": "https://example.com/people/new-operator",
            "title": "New Operator",
            "discovered_from_url": "https://example.com/podcast/episode-7",
            "evidence_pointer": "https://example.com/podcast/episode-7#transcript",
            "evidence_lane": "practitioner",
            "topics": ["agentic-systems"],
            "observed_at": "2026-07-27T12:00:00Z",
        }
        value.update(overrides)
        return value

    def test_interrupted_append_restores_the_exact_valid_graph_prefix(self):
        ingest_relationships(
            [self.relation()], graph_path=self.graph, discovery_ledger_path=self.discovery
        )
        before = self.graph.read_bytes()
        real_write = source_graph_module.os.write
        calls = 0

        def interrupted_write(descriptor, payload):
            nonlocal calls
            calls += 1
            if calls == 1:
                real_write(descriptor, payload[: max(1, len(payload) // 2)])
                raise TimeoutError("injected SIGALRM-equivalent interruption")
            return real_write(descriptor, payload)

        with patch("corpus_source_graph.os.write", side_effect=interrupted_write):
            with self.assertRaisesRegex(TimeoutError, "SIGALRM-equivalent"):
                ingest_relationships(
                    [
                        self.relation(
                            canonical_url="https://example.com/people/second-operator",
                            title="Second Operator",
                        )
                    ],
                    graph_path=self.graph,
                    discovery_ledger_path=self.discovery,
                )
        self.assertEqual(self.graph.read_bytes(), before)
        replay = ingest_relationships(
            [self.relation()], graph_path=self.graph, discovery_ledger_path=self.discovery
        )
        self.assertEqual(replay["relationships"], 1)

    def test_unseen_entities_from_multiple_source_families_enter_one_candidate_graph(self):
        relationships = [
            self.relation(),
            self.relation(
                source_family="youtube",
                canonical_url="https://x.com/unknown_builder",
                discovered_from_url="https://www.youtube.com/watch?v=opaque-video",
                evidence_pointer="https://www.youtube.com/watch?v=opaque-video",
                title="Unknown Builder",
            ),
            self.relation(
                source_family="paper",
                relationship_type="authored",
                canonical_url="https://orcid.org/0000-0002-1825-0097",
                discovered_from_url="https://arxiv.org/abs/2607.12345",
                evidence_pointer="https://arxiv.org/abs/2607.12345",
                title="A. Researcher",
                evidence_lane="scientific",
            ),
            self.relation(
                source_family="github",
                relationship_type="contributed_to",
                canonical_url="https://github.com/unknown-maintainer",
                discovered_from_url="https://github.com/example/agent-runtime",
                evidence_pointer="https://github.com/example/agent-runtime/commits/main",
                title="unknown-maintainer",
                evidence_lane="canonical",
            ),
            self.relation(
                source_family="conference",
                relationship_type="speaker_at",
                canonical_url="https://example.org/speakers/emerging-engineer",
                discovered_from_url="https://example.org/events/agent-systems-2026",
                evidence_pointer="https://example.org/events/agent-systems-2026#schedule",
                title="Emerging Engineer",
            ),
        ]

        result = ingest_relationships(
            relationships,
            graph_path=self.graph,
            discovery_ledger_path=self.discovery,
        )

        self.assertEqual(result["relationships_appended"], 5)
        self.assertEqual(result["candidates_added"], 5)
        engine = DiscoveryEngine(self.discovery)
        self.assertEqual(len(engine.candidates), 5)
        discovery_sources = {record.discovery_source for record in engine.candidates.values()}
        self.assertTrue(any(":podcast:guest_of:" in source for source in discovery_sources))
        self.assertTrue(any(":youtube:guest_of:" in source for source in discovery_sources))
        self.assertTrue(any(":paper:authored:" in source for source in discovery_sources))
        self.assertTrue(any(":github:contributed_to:" in source for source in discovery_sources))
        self.assertTrue(any(":conference:speaker_at:" in source for source in discovery_sources))

    def test_replay_is_physically_idempotent_and_reconciles_projection(self):
        relationship = self.relation()
        first = ingest_relationships(
            [relationship], graph_path=self.graph, discovery_ledger_path=self.discovery
        )
        graph_before = self.graph.read_bytes()
        ledger_before = self.discovery.read_bytes()
        second = ingest_relationships(
            [relationship], graph_path=self.graph, discovery_ledger_path=self.discovery
        )
        self.assertEqual(first["relationships_appended"], 1)
        self.assertEqual(second["relationships_appended"], 0)
        self.assertEqual(self.graph.read_bytes(), graph_before)
        self.assertEqual(self.discovery.read_bytes(), ledger_before)

    def test_x_relationship_expansion_does_not_require_knowing_the_emerging_term(self):
        projection = {
            "schema_version": 1,
            "signals": [
                {
                    "author": "@new_builder",
                    "published_at": "2026-07-27T12:00:00Z",
                    "url": "https://x.com/new_builder/status/123",
                    "text": "We changed how the runtime is structured.",
                    "thread_id": "123",
                    "linked_primary_sources": [
                        "https://github.com/newco/runtime/tree/abcdef",
                        "https://arxiv.org/pdf/2607.12345.pdf",
                    ],
                    "evidence_class": "unverified_discovery_signal",
                    "doctrine_eligible": False,
                }
            ],
        }
        relationships = relationships_from_x_projection(
            projection, domain="agentic-engineering", topics=()
        )
        result = ingest_relationships(
            relationships, graph_path=self.graph, discovery_ledger_path=self.discovery
        )

        self.assertEqual(result["candidates_after"], 3)
        engine = DiscoveryEngine(self.discovery)
        urls = {record.canonical_url for record in engine.candidates.values()}
        self.assertIn("https://x.com/new_builder", urls)
        self.assertIn("https://github.com/newco/runtime", urls)
        self.assertIn("https://arxiv.org/abs/2607.12345", urls)
        self.assertNotIn("graph-engineering", self.graph.read_text(encoding="utf-8"))

    def test_invalid_relationship_fails_before_mutating_either_ledger(self):
        invalid = self.relation(relationship_type="trust_me")
        with self.assertRaises(SourceGraphContractError):
            ingest_relationships(
                [invalid], graph_path=self.graph, discovery_ledger_path=self.discovery
            )
        self.assertFalse(self.graph.exists())
        self.assertFalse(self.discovery.exists())

    def test_same_batch_duplicate_relationships_cannot_corrupt_the_graph(self):
        relationship = self.relation()
        result = ingest_relationships(
            [relationship, dict(relationship), relationship],
            graph_path=self.graph,
            discovery_ledger_path=self.discovery,
        )
        self.assertEqual(result["relationships_appended"], 1)
        lines = [line for line in self.graph.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        relation_ids = [json.loads(line)["relation_id"] for line in lines]
        self.assertEqual(len(relation_ids), len(set(relation_ids)))
        # The durable bytes stay readable, so a follow-up cycle still converges.
        again = ingest_relationships(
            [relationship], graph_path=self.graph, discovery_ledger_path=self.discovery
        )
        self.assertEqual(again["relationships_appended"], 0)
        self.assertEqual(again["relationships"], 1)

    def test_duplicate_batch_dedup_preserves_every_distinct_relationship(self):
        first = self.relation()
        second = self.relation(
            source_family="conference",
            relationship_type="speaker_at",
            discovered_from_url="https://example.org/events/agent-systems-2026",
            evidence_pointer="https://example.org/events/agent-systems-2026#schedule",
        )
        result = ingest_relationships(
            [first, second, dict(first), dict(second)],
            graph_path=self.graph,
            discovery_ledger_path=self.discovery,
        )
        self.assertEqual(result["relationships_appended"], 2)
        self.assertEqual(result["relationships"], 2)

    def test_tracking_aliases_of_one_entity_converge_on_one_durable_candidate(self):
        """False novelty must be impossible in the ledger, not just in reports."""
        canonical = "https://github.com/example/recursive-engine"
        result = ingest_relationships(
            [
                self.relation(
                    canonical_url=canonical,
                    discovered_from_url="https://x.com/builder/status/1",
                    evidence_pointer="https://x.com/builder/status/1",
                ),
                self.relation(
                    canonical_url=f"{canonical}?utm_source=cycle-b",
                    discovered_from_url="https://x.com/builder/status/2",
                    evidence_pointer="https://x.com/builder/status/2",
                ),
                self.relation(
                    canonical_url=f"{canonical}/",
                    source_family="conference",
                    discovered_from_url="https://example.org/events/agent-systems-2026",
                    evidence_pointer="https://example.org/events/agent-systems-2026#schedule",
                ),
            ],
            graph_path=self.graph,
            discovery_ledger_path=self.discovery,
        )
        self.assertEqual(result["candidates_added"], 1)
        engine = DiscoveryEngine(self.discovery)
        self.assertEqual(
            [record.canonical_url for record in engine.candidates.values()], [canonical]
        )
        # Aliases corroborate the one entity instead of splitting its evidence.
        self.assertEqual(next(iter(engine.candidates.values())).occurrences, 3)

    def test_distinct_entities_still_remain_distinct_candidates(self):
        result = ingest_relationships(
            [
                self.relation(canonical_url="https://github.com/example/one"),
                self.relation(
                    canonical_url="https://github.com/example/two",
                    discovered_from_url="https://example.com/podcast/episode-8",
                    evidence_pointer="https://example.com/podcast/episode-8#transcript",
                ),
            ],
            graph_path=self.graph,
            discovery_ledger_path=self.discovery,
        )
        self.assertEqual(result["candidates_added"], 2)

    def test_observation_of_an_existing_candidate_preserves_upgraded_rights(self):
        relationship = self.relation()
        ingest_relationships(
            [relationship], graph_path=self.graph, discovery_ledger_path=self.discovery
        )
        engine = DiscoveryEngine(self.discovery)
        candidate_id = next(iter(engine.candidates))
        upgraded = _upgrade_rights(self.discovery, candidate_id, "public_rights_clear")
        self.assertEqual(upgraded, "public_rights_clear")

        # A second, genuinely new observation of the same candidate must not
        # attempt to reset the rights an authorized resolver already granted.
        second = self.relation(
            source_family="conference",
            relationship_type="speaker_at",
            discovered_from_url="https://example.org/events/agent-systems-2026",
            evidence_pointer="https://example.org/events/agent-systems-2026#schedule",
        )
        result = ingest_relationships(
            [second], graph_path=self.graph, discovery_ledger_path=self.discovery
        )
        self.assertEqual(result["relationships_appended"], 1)
        engine = DiscoveryEngine(self.discovery)
        self.assertEqual(engine.candidates[candidate_id].rights_state, "public_rights_clear")

    def test_observation_still_cannot_grant_rights_to_a_new_candidate(self):
        ingest_relationships(
            [self.relation()], graph_path=self.graph, discovery_ledger_path=self.discovery
        )
        engine = DiscoveryEngine(self.discovery)
        self.assertTrue(
            all(record.rights_state == "rights_unclear" for record in engine.candidates.values())
        )

    def test_graph_to_ledger_projection_recovers_after_an_interrupted_run(self):
        relationship = self.relation()
        # Simulate a crash after the durable graph append but before projection:
        # only the graph bytes exist.
        from corpus_source_graph import SourceRelationship, _append_new, reconcile_source_graph

        appended = _append_new(self.graph, [SourceRelationship.from_dict(relationship)])
        self.assertEqual(appended, 1)
        self.assertFalse(self.discovery.exists())

        recovered = reconcile_source_graph(self.graph, self.discovery)
        self.assertEqual(recovered["candidates_added"], 1)
        # Reconciliation is replayable: a second pass adds nothing and does not raise.
        ledger_before = self.discovery.read_bytes()
        again = reconcile_source_graph(self.graph, self.discovery)
        self.assertEqual(again["candidates_added"], 0)
        self.assertEqual(self.discovery.read_bytes(), ledger_before)

    def test_rights_upgraded_candidate_survives_graph_to_ledger_reconciliation(self):
        from corpus_source_graph import reconcile_source_graph

        ingest_relationships(
            [self.relation()], graph_path=self.graph, discovery_ledger_path=self.discovery
        )
        candidate_id = next(iter(DiscoveryEngine(self.discovery).candidates))
        _upgrade_rights(self.discovery, candidate_id, "public_rights_clear")
        result = reconcile_source_graph(self.graph, self.discovery)
        self.assertEqual(result["candidates_added"], 0)
        engine = DiscoveryEngine(self.discovery)
        self.assertEqual(engine.candidates[candidate_id].rights_state, "public_rights_clear")


def _upgrade_rights(ledger_path: Path, candidate_id: str, rights_state: str) -> str:
    """Record an authorized rights upgrade the way a rights resolver would."""
    engine = DiscoveryEngine(ledger_path)
    record = engine.candidates[candidate_id]
    engine.ledger.append(
        "candidate_observed",
        {
            "observation": {
                **CandidateObservation.create(
                    domain=record.domain,
                    entity_type=record.entity_type,
                    canonical_url=record.canonical_url,
                    discovery_source="rights-resolver:test",
                    evidence_pointer=record.evidence_pointer,
                    evidence_lane=record.evidence_lane,
                    topics=record.topics,
                    observed_at=record.last_seen_at,
                ).to_dict()
            },
            "score_components": dict(record.score_components),
            "rationale": record.rationale,
            "rights_state": rights_state,
        },
    )
    # The first observation event for a candidate carries its rights state, so
    # rebuild the ledger with the upgraded value replayed from the start.
    events = engine.ledger.read_events()
    rewritten = []
    for event in events:
        if (
            event["event_type"] == "candidate_observed"
            and event["payload"]["observation"]["canonical_url"] == record.canonical_url
        ):
            event = {**event, "payload": {**event["payload"], "rights_state": rights_state}}
        rewritten.append(event)
    ledger_path.write_text(
        "".join(
            json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for event in rewritten
        ),
        encoding="utf-8",
    )
    return DiscoveryEngine(ledger_path).candidates[candidate_id].rights_state


if __name__ == "__main__":
    unittest.main()
