from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_entity_identity import (
    EntityIdentityError,
    canonical_entity_identity,
    evidence_document_identity,
    publisher_identity,
)


class CanonicalEntityIdentityTests(unittest.TestCase):
    def test_tracking_aliases_resolve_to_one_identity(self):
        base = canonical_entity_identity("https://github.com/example/recursive-engine")
        for alias in (
            "https://github.com/example/recursive-engine?utm_source=cycle-b",
            "https://GitHub.com/example/recursive-engine/",
            "https://www.github.com/example/recursive-engine.git",
            "https://github.com/example/recursive-engine#readme",
            "https://github.com/example/recursive-engine?ref=newsletter&utm_medium=x",
        ):
            self.assertEqual(canonical_entity_identity(alias), base, alias)

    def test_arxiv_and_youtube_forms_converge(self):
        self.assertEqual(
            canonical_entity_identity("https://arxiv.org/pdf/2406.13352.pdf"),
            canonical_entity_identity("https://arxiv.org/abs/2406.13352"),
        )
        self.assertEqual(
            canonical_entity_identity("https://youtu.be/090oR--s__8?si=abc"),
            canonical_entity_identity("https://www.youtube.com/watch?v=090oR--s__8&t=90"),
        )

    def test_distinct_entities_do_not_collide(self):
        self.assertNotEqual(
            canonical_entity_identity("https://github.com/example/one"),
            canonical_entity_identity("https://github.com/example/two"),
        )
        self.assertNotEqual(
            canonical_entity_identity("https://example.org/people/maya-chen"),
            canonical_entity_identity("https://attacker.example/people/maya-chen"),
        )

    def test_meaningful_query_is_retained_for_entity_identity(self):
        self.assertNotEqual(
            canonical_entity_identity("https://api.github.com/search/repositories?q=alpha"),
            canonical_entity_identity("https://api.github.com/search/repositories?q=beta"),
        )

    def test_invalid_urls_fail_closed(self):
        for value in ("", "not-a-url", "ftp://example.org/x", "https://user:pw@example.org/x", None):
            with self.assertRaises(EntityIdentityError):
                canonical_entity_identity(value)


class EvidenceDocumentIdentityTests(unittest.TestCase):
    def test_query_aliases_of_one_page_are_one_document(self):
        aliases = [
            "https://api.github.com/search/repositories?q=agent%20security%20benchmark",
            "https://api.github.com/search/repositories?q=llm%20agent%20reliability",
            "https://api.github.com/search/repositories",
        ]
        self.assertEqual(len({evidence_document_identity(value) for value in aliases}), 1)

    def test_span_and_digest_pointers_collapse_to_one_document(self):
        self.assertEqual(
            evidence_document_identity("https://example.com/podcast/12?span=1"),
            evidence_document_identity("https://example.com/podcast/12?artifact_sha256=ab&evidence_span=1-9"),
        )

    def test_distinct_pages_remain_distinct_documents(self):
        self.assertNotEqual(
            evidence_document_identity("https://x.com/builder/status/1"),
            evidence_document_identity("https://x.com/builder/status/2"),
        )

    def test_platform_document_identity_keeps_the_resource_selector(self):
        self.assertNotEqual(
            evidence_document_identity("https://www.youtube.com/watch?v=090oR--s__8"),
            evidence_document_identity("https://www.youtube.com/watch?v=4SnvMieJiuw"),
        )


class PublisherIdentityTests(unittest.TestCase):
    def test_one_owner_is_one_publisher_across_pages(self):
        self.assertEqual(
            publisher_identity("https://x.com/builder/status/1"),
            publisher_identity("https://x.com/builder/status/2"),
        )
        self.assertEqual(
            publisher_identity("https://github.com/example/one"),
            publisher_identity("https://github.com/example/two"),
        )

    def test_distinct_owners_and_hosts_are_distinct_publishers(self):
        self.assertNotEqual(
            publisher_identity("https://github.com/example/one"),
            publisher_identity("https://github.com/other/one"),
        )
        self.assertNotEqual(
            publisher_identity("https://arxiv.org/abs/2406.13352"),
            publisher_identity("https://api.github.com/search/repositories?q=x"),
        )

    def test_host_alias_is_one_publisher(self):
        self.assertEqual(
            publisher_identity("https://www.example.org/a"),
            publisher_identity("https://example.org/b"),
        )


if __name__ == "__main__":
    unittest.main()
