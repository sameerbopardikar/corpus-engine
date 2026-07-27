from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_discovery import DiscoveryEngine
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


if __name__ == "__main__":
    unittest.main()
