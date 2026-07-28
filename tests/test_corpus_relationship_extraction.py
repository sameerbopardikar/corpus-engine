from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = Path(__file__).parent / "fixtures" / "self_expansion"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_discovery import DiscoveryEngine
from corpus_relationship_extraction import (
    RelationshipExtractionError,
    extract_conference_relationships,
    extract_github_relationships,
    extract_openalex_relationships,
    extract_semantic_relationships,
    extract_x_relationships,
)
from corpus_source_graph import ingest_relationships


class CrossFamilyRelationshipExtractionTests(unittest.TestCase):
    def common(self):
        return {
            "domain": "agentic-engineering",
            "observed_at": "2026-07-27T12:00:00Z",
            "evidence_lane": "unverified-discovery-signal",
            "topics": (),
        }

    def test_x_projection_extracts_author_and_linked_repository(self):
        projection = json.loads((FIXTURES / "x-thread.json").read_text())
        relationships = extract_x_relationships(projection, **self.common())
        self.assertEqual(
            {(item.entity_type, item.canonical_url) for item in relationships},
            {
                ("creator", "https://x.com/novel_builder"),
                ("repository", "https://github.com/example/novel-runtime"),
            },
        )

    def test_youtube_semantic_guest_requires_exact_grounding_quote(self):
        text = (FIXTURES / "youtube-transcript.md").read_text()
        relationships = extract_semantic_relationships(
            source_family="youtube",
            artifact_url="https://www.youtube.com/watch?v=episode100",
            artifact_text=text,
            claims=[
                {
                    "relationship_type": "guest_of",
                    "entity_type": "creator",
                    "canonical_url": "https://example.org/people/maya-chen",
                    "title": "Maya Chen",
                    "evidence_quote": "Today I am joined by Maya Chen, who built the recovery controller used by her team.",
                }
            ],
            **self.common(),
        )
        self.assertEqual(len(relationships), 1)
        self.assertIn("evidence_sha256=", relationships[0].evidence_pointer)

    def test_podcast_semantic_guest_is_grounded(self):
        text = (FIXTURES / "podcast-transcript.md").read_text()
        relationships = extract_semantic_relationships(
            source_family="podcast",
            artifact_url="https://example.com/podcast/episode-12",
            artifact_text=text,
            claims=[
                {
                    "relationship_type": "guest_of",
                    "entity_type": "creator",
                    "canonical_url": "https://example.org/people/ravi-shah",
                    "title": "Ravi Shah",
                    "evidence_quote": "Our guest Ravi Shah maintains the replay debugger.",
                }
            ],
            **self.common(),
        )
        self.assertEqual(relationships[0].title, "Ravi Shah")

    def test_hallucinated_semantic_guest_fails_closed(self):
        text = (FIXTURES / "youtube-transcript.md").read_text()
        with self.assertRaisesRegex(RelationshipExtractionError, "quote is not present"):
            extract_semantic_relationships(
                source_family="youtube",
                artifact_url="https://www.youtube.com/watch?v=episode100",
                artifact_text=text,
                claims=[
                    {
                        "relationship_type": "guest_of",
                        "entity_type": "creator",
                        "canonical_url": "https://example.org/people/invented-person",
                        "title": "Invented Person",
                        "evidence_quote": "Invented Person was the featured guest.",
                    }
                ],
                **self.common(),
            )

    def test_openalex_extracts_authors_and_references(self):
        work = json.loads((FIXTURES / "openalex-work.json").read_text())
        relationships = extract_openalex_relationships(work, **self.common())
        self.assertEqual(len(relationships), 4)
        self.assertEqual(
            {item.relationship_type for item in relationships}, {"authored", "cited"}
        )

    def test_github_extracts_owner_contributors_and_dependencies(self):
        repository = json.loads((FIXTURES / "github-repository.json").read_text())
        relationships = extract_github_relationships(repository, **self.common())
        self.assertEqual(len(relationships), 5)
        self.assertEqual(
            {item.relationship_type for item in relationships},
            {"maintains", "contributed_to", "depends_on"},
        )

    def test_conference_extracts_speaker_and_related_project(self):
        html = (FIXTURES / "conference-schedule.html").read_text()
        relationships = extract_conference_relationships(
            html,
            artifact_url="https://example.org/events/agent-systems-2026",
            **self.common(),
        )
        self.assertEqual(len(relationships), 2)
        self.assertEqual(
            {item.relationship_type for item in relationships},
            {"speaker_at", "related_project"},
        )

    def test_all_families_converge_into_one_rights_unclear_candidate_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            common = self.common()
            relationships = []
            relationships.extend(
                extract_x_relationships(
                    json.loads((FIXTURES / "x-thread.json").read_text()), **common
                )
            )
            relationships.extend(
                extract_semantic_relationships(
                    source_family="youtube",
                    artifact_url="https://www.youtube.com/watch?v=episode100",
                    artifact_text=(FIXTURES / "youtube-transcript.md").read_text(),
                    claims=[{
                        "relationship_type": "guest_of", "entity_type": "creator",
                        "canonical_url": "https://example.org/people/maya-chen", "title": "Maya Chen",
                        "evidence_quote": "Today I am joined by Maya Chen, who built the recovery controller used by her team.",
                    }],
                    **common,
                )
            )
            relationships.extend(
                extract_semantic_relationships(
                    source_family="podcast",
                    artifact_url="https://example.com/podcast/episode-12",
                    artifact_text=(FIXTURES / "podcast-transcript.md").read_text(),
                    claims=[{
                        "relationship_type": "guest_of", "entity_type": "creator",
                        "canonical_url": "https://example.org/people/ravi-shah", "title": "Ravi Shah",
                        "evidence_quote": "Our guest Ravi Shah maintains the replay debugger.",
                    }],
                    **common,
                )
            )
            relationships.extend(
                extract_openalex_relationships(
                    json.loads((FIXTURES / "openalex-work.json").read_text()), **common
                )
            )
            relationships.extend(
                extract_github_relationships(
                    json.loads((FIXTURES / "github-repository.json").read_text()), **common
                )
            )
            relationships.extend(
                extract_conference_relationships(
                    (FIXTURES / "conference-schedule.html").read_text(),
                    artifact_url="https://example.org/events/agent-systems-2026",
                    **common,
                )
            )
            result = ingest_relationships(
                relationships,
                graph_path=root / "source-graph.jsonl",
                discovery_ledger_path=root / "discovery-ledger.jsonl",
            )
            engine = DiscoveryEngine(root / "discovery-ledger.jsonl")
            self.assertGreaterEqual(result["relationships"], 15)
            self.assertTrue(engine.candidates)
            self.assertTrue(
                all(candidate.rights_state == "rights_unclear" for candidate in engine.candidates.values())
            )


if __name__ == "__main__":
    unittest.main()
