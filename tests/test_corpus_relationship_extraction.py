from __future__ import annotations

import hashlib
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

YOUTUBE_URL = "https://www.youtube.com/watch?v=episode100"
PODCAST_URL = "https://example.com/podcast/episode-12"
MAYA_QUOTE = "Today I am joined by Maya Chen, who built the recovery controller used by her team."
RAVI_QUOTE = "Our guest Ravi Shah maintains the replay debugger."


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def span_of(text: str, quote: str) -> list[int]:
    start = text.index(quote)
    return [start, start + len(quote)]


def maya_claim(text: str, **overrides) -> dict:
    claim = {
        "relationship_type": "guest_of",
        "entity_type": "creator",
        "canonical_url": "https://example.org/people/maya-chen",
        "title": "Maya Chen",
        "entity_mention": "Maya Chen",
        "relation_mention": "joined by",
        "evidence_quote": MAYA_QUOTE,
        "evidence_span": span_of(text, MAYA_QUOTE),
    }
    claim.update(overrides)
    return claim


def ravi_claim(text: str, **overrides) -> dict:
    claim = {
        "relationship_type": "guest_of",
        "entity_type": "creator",
        "canonical_url": "https://example.org/people/ravi-shah",
        "title": "Ravi Shah",
        "entity_mention": "Ravi Shah",
        "relation_mention": "Our guest",
        "evidence_quote": RAVI_QUOTE,
        "evidence_span": span_of(text, RAVI_QUOTE),
    }
    claim.update(overrides)
    return claim


class CrossFamilyRelationshipExtractionTests(unittest.TestCase):
    def common(self):
        return {
            "domain": "agentic-engineering",
            "observed_at": "2026-07-27T12:00:00Z",
            "evidence_lane": "unverified-discovery-signal",
            "topics": (),
        }

    def youtube(self):
        return (FIXTURES / "youtube-transcript.md").read_text()

    def podcast(self):
        return (FIXTURES / "podcast-transcript.md").read_text()

    def semantic(self, *, text: str, url: str, claims: list[dict], artifact_sha256: str | None = None):
        return extract_semantic_relationships(
            source_family="youtube" if "youtube" in url else "podcast",
            artifact_url=url,
            artifact_text=text,
            artifact_sha256=digest(text) if artifact_sha256 is None else artifact_sha256,
            claims=claims,
            **self.common(),
        )

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
        text = self.youtube()
        relationships = self.semantic(text=text, url=YOUTUBE_URL, claims=[maya_claim(text)])
        self.assertEqual(len(relationships), 1)
        self.assertIn(f"artifact_sha256={digest(text)}", relationships[0].evidence_pointer)
        self.assertIn("evidence_span=", relationships[0].evidence_pointer)

    def test_podcast_semantic_guest_is_grounded(self):
        text = self.podcast()
        relationships = self.semantic(text=text, url=PODCAST_URL, claims=[ravi_claim(text)])
        self.assertEqual(relationships[0].title, "Ravi Shah")

    def test_hallucinated_semantic_guest_fails_closed(self):
        text = self.youtube()
        with self.assertRaisesRegex(RelationshipExtractionError, "quote is not present"):
            self.semantic(
                text=text,
                url=YOUTUBE_URL,
                claims=[
                    maya_claim(
                        text,
                        canonical_url="https://example.org/people/invented-person",
                        title="Invented Person",
                        entity_mention="Invented Person",
                        evidence_quote="Invented Person was the featured guest.",
                    )
                ],
            )

    # --- P1: a real quote must ground the named entity and the relation ---

    def test_real_quote_cannot_ground_an_entity_it_does_not_name(self):
        text = self.youtube()
        with self.assertRaisesRegex(RelationshipExtractionError, "entity mention"):
            self.semantic(
                text=text,
                url=YOUTUBE_URL,
                claims=[
                    maya_claim(
                        text,
                        canonical_url="https://example.org/people/invented-guest",
                        title="Invented Guest",
                        entity_mention="Invented Guest",
                    )
                ],
            )

    def test_entity_mention_must_match_the_claimed_title(self):
        text = self.youtube()
        with self.assertRaisesRegex(RelationshipExtractionError, "entity mention"):
            self.semantic(
                text=text,
                url=YOUTUBE_URL,
                claims=[maya_claim(text, title="Somebody Else Entirely")],
            )

    def test_canonical_target_must_match_locator_named_in_the_span(self):
        quote = (
            "The repository https://github.com/example/good depends on the replay controller."
        )
        text = f"Intro. {quote} End."
        claim = {
            "relationship_type": "depends_on",
            "entity_type": "repository",
            "canonical_url": "https://github.com/attacker/backdoor",
            "title": "https://github.com/example/good",
            "entity_mention": "https://github.com/example/good",
            "relation_mention": "depends on",
            "evidence_quote": quote,
            "evidence_span": span_of(text, quote),
        }
        with self.assertRaisesRegex(RelationshipExtractionError, "canonical target"):
            self.semantic(text=text, url=YOUTUBE_URL, claims=[claim])

    def test_relation_must_be_expressed_inside_the_validated_span(self):
        text = self.youtube()
        with self.assertRaisesRegex(RelationshipExtractionError, "relation mention"):
            self.semantic(
                text=text,
                url=YOUTUBE_URL,
                claims=[maya_claim(text, relation_mention="was the keynote speaker at")],
            )

    def test_relation_mention_cannot_simply_repeat_the_entity(self):
        text = self.youtube()
        with self.assertRaisesRegex(RelationshipExtractionError, "relation mention"):
            self.semantic(
                text=text,
                url=YOUTUBE_URL,
                claims=[maya_claim(text, relation_mention="Maya Chen")],
            )

    def test_span_must_actually_contain_the_quoted_bytes(self):
        text = self.youtube()
        start, end = span_of(text, MAYA_QUOTE)
        with self.assertRaisesRegex(RelationshipExtractionError, "span"):
            self.semantic(
                text=text,
                url=YOUTUBE_URL,
                claims=[maya_claim(text, evidence_span=[start + 5, end + 5])],
            )

    def test_out_of_range_or_malformed_spans_fail_closed(self):
        text = self.youtube()
        for span in ([0, len(text) + 10], [-1, 20], [30, 10], ["0", "10"], [0], "0-10"):
            with self.assertRaises(RelationshipExtractionError):
                self.semantic(text=text, url=YOUTUBE_URL, claims=[maya_claim(text, evidence_span=span)])

    def test_modified_artifact_bytes_invalidate_every_claim(self):
        text = self.youtube()
        with self.assertRaisesRegex(RelationshipExtractionError, "digest"):
            self.semantic(
                text=text,
                url=YOUTUBE_URL,
                claims=[maya_claim(text)],
                artifact_sha256=digest(text + " tampered"),
            )

    def test_claims_must_declare_the_full_grounding_contract(self):
        text = self.youtube()
        incomplete = maya_claim(text)
        incomplete.pop("evidence_span")
        with self.assertRaisesRegex(RelationshipExtractionError, "fields invalid"):
            self.semantic(text=text, url=YOUTUBE_URL, claims=[incomplete])

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
            youtube_text = self.youtube()
            podcast_text = self.podcast()
            relationships = []
            relationships.extend(
                extract_x_relationships(
                    json.loads((FIXTURES / "x-thread.json").read_text()), **common
                )
            )
            relationships.extend(
                self.semantic(text=youtube_text, url=YOUTUBE_URL, claims=[maya_claim(youtube_text)])
            )
            relationships.extend(
                self.semantic(text=podcast_text, url=PODCAST_URL, claims=[ravi_claim(podcast_text)])
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
