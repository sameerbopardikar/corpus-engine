import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_adapters.types import RightsState
from corpus_scholarly_discovery import ScholarlyCandidate, discover_scholarly


class _FakeResponse:
    def __init__(self, document, url):
        self.content = json.dumps(document).encode("utf-8")
        self.url = url


def _openalex_fixture():
    return {
        "meta": {"count": 2, "next_cursor": None},
        "results": [
            {
                "id": "https://openalex.org/W4000000001",
                "title": "Velocity based training and hypertrophy adaptation",
                "publication_date": "2024-01-01",
                "updated_date": "2024-02-01",
                "doi": "https://doi.org/10.1/vbt",
                "ids": {"openalex": "https://openalex.org/W4000000001"},
                "cited_by_count": 42,
                "open_access": {"is_oa": True, "oa_status": "gold"},
                "best_oa_location": {
                    "license": "cc-by",
                    "landing_page_url": "https://example.org/vbt",
                    "pdf_url": "https://example.org/vbt.pdf",
                    "source": {"host_organization_name": "Open Journal"},
                },
                "primary_location": {"landing_page_url": "https://example.org/vbt"},
            },
            {
                "id": "https://openalex.org/W4000000002",
                "title": "A closed access study of sprint speed",
                "publication_date": "2023-06-01",
                "updated_date": "2023-07-01",
                "doi": "https://doi.org/10.2/sprint",
                "ids": {"openalex": "https://openalex.org/W4000000002"},
                "cited_by_count": 7,
                "open_access": {"is_oa": False, "oa_status": "closed"},
                "best_oa_location": None,
                "primary_location": {"landing_page_url": "https://paywall.example/sprint"},
            },
        ],
    }


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_get(url, timeout):
            self.calls.append((url, timeout))
            return _FakeResponse(_openalex_fixture(), url)

        self.fake_get = fake_get

    def test_discovers_candidates_from_topics_without_api_key(self):
        candidates = discover_scholarly(
            domain="training",
            topics=["hypertrophy", "sprint-speed"],
            http_get=self.fake_get,
            fetched_at=lambda: "2026-07-19T00:00:00Z",
        )
        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(isinstance(c, ScholarlyCandidate) for c in candidates))
        # Query must be topic-driven (no credentials in the URL).
        url = self.calls[0][0]
        self.assertIn("hypertrophy", url)
        self.assertNotIn("api_key", url)

    def test_large_domain_ontology_fans_out_into_bounded_single_topic_queries(self):
        topics = [f"topic-{index}" for index in range(26)]
        calls = []

        def rejects_conjunctions(url, timeout):
            calls.append(url)
            search = url.split("search=", 1)[1].split("&", 1)[0]
            if "+" in search or "%20" in search:
                return _FakeResponse({"meta": {"count": 0}, "results": []}, url)
            return _FakeResponse(_openalex_fixture(), url)

        candidates = discover_scholarly(
            domain="training", topics=topics, http_get=rejects_conjunctions,
            fetched_at=lambda: "2026-07-19T00:00:00Z", max_topic_queries=8,
        )
        self.assertEqual(len(candidates), 2)
        self.assertLessEqual(len(calls), 8)
        self.assertTrue(calls)
        self.assertTrue(all("+" not in url.split("search=", 1)[1].split("&", 1)[0] for url in calls))

    def test_open_licensed_work_with_generic_landing_surfaces_pdf(self):
        candidates = discover_scholarly(
            domain="training",
            topics=["hypertrophy"],
            http_get=self.fake_get,
            fetched_at=lambda: "2026-07-19T00:00:00Z",
        )
        oa = next(c for c in candidates if c.openalex_id == "W4000000001")
        self.assertEqual(oa.rights_evidence.license, "cc-by")
        self.assertEqual(oa.observation.entity_type, "paper")
        # The fixture's landing_page_url is a generic publisher landing shell —
        # NOT full-text HTML. Discovery must surface the PDF (which the executor
        # routes to an extractor/human gate), never claim the landing as text.
        self.assertEqual(oa.content_locator, "https://example.org/vbt.pdf")
        self.assertEqual(oa.observation.domain, "training")

    def test_pmc_html_landing_is_used_as_fulltext(self):
        def fixture_pmc(url, timeout):
            doc = _openalex_fixture()
            doc["results"] = [doc["results"][0]]
            doc["results"][0]["best_oa_location"] = {
                "license": "cc-by",
                "landing_page_url": "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7654321/",
                "pdf_url": "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7654321/pdf/",
            }
            return _FakeResponse(doc, url)

        candidates = discover_scholarly(
            domain="training", topics=["hypertrophy"], http_get=fixture_pmc,
            fetched_at=lambda: "2026-07-19T00:00:00Z",
        )
        oa = candidates[0]
        # An allowlisted PMC full-text HTML landing IS directly acquirable text.
        self.assertEqual(
            oa.content_locator, "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7654321/"
        )
        self.assertEqual(oa.rights_evidence.access_class, "open_content")

    def test_licensed_alternate_europepmc_html_beats_generic_best_landing(self):
        def fixture_alternate(url, timeout):
            doc = _openalex_fixture()
            doc["results"] = [doc["results"][0]]
            doc["results"][0]["best_oa_location"] = {
                "license": "cc-by",
                "landing_page_url": "https://doi.org/10.1/vbt",
                "pdf_url": "https://publisher.example/vbt.pdf",
            }
            doc["results"][0]["locations"] = [{
                "license": "cc-by",
                "landing_page_url": "https://europepmc.org/pmc/articles/PMC7654321",
            }]
            return _FakeResponse(doc, url)

        candidate = discover_scholarly(
            domain="training", topics=["hypertrophy"], http_get=fixture_alternate,
            fetched_at=lambda: "2026-07-19T00:00:00Z",
        )[0]
        # Europe PMC's public article shell redirects to a navigation-only page.
        # Rewrite the PMCID to PMC's stable full-text HTML route instead.
        self.assertEqual(candidate.content_locator, "https://pmc.ncbi.nlm.nih.gov/articles/PMC7654321/")
        self.assertEqual(candidate.rights_evidence.license, "cc-by")

    def test_numeric_europepmc_article_route_normalizes_to_pmc_fulltext(self):
        def fixture_numeric(url, timeout):
            doc = _openalex_fixture()
            doc["results"] = [doc["results"][0]]
            doc["results"][0]["best_oa_location"] = {
                "license": "cc-by",
                "landing_page_url": "https://doi.org/10.1/vbt",
                "pdf_url": "https://publisher.example/vbt.pdf",
            }
            doc["results"][0]["locations"] = [{
                "license": "cc-by",
                "landing_page_url": "https://europepmc.org/articles/6238764",
            }]
            return _FakeResponse(doc, url)

        candidate = discover_scholarly(
            domain="training", topics=["hypertrophy"], http_get=fixture_numeric,
            fetched_at=lambda: "2026-07-19T00:00:00Z",
        )[0]
        self.assertEqual(candidate.content_locator, "https://pmc.ncbi.nlm.nih.gov/articles/PMC6238764/")
        self.assertEqual(candidate.rights_evidence.license, "cc-by")

    def test_unlicensed_alternate_repository_html_does_not_upgrade_rights(self):
        def fixture_unlicensed(url, timeout):
            doc = _openalex_fixture()
            doc["results"] = [doc["results"][0]]
            doc["results"][0]["best_oa_location"] = None
            doc["results"][0]["locations"] = [{
                "license": None,
                "landing_page_url": "https://europepmc.org/pmc/articles/PMC7654321",
            }]
            return _FakeResponse(doc, url)

        candidate = discover_scholarly(
            domain="training", topics=["hypertrophy"], http_get=fixture_unlicensed,
            fetched_at=lambda: "2026-07-19T00:00:00Z",
        )[0]
        self.assertIsNone(candidate.content_locator)
        self.assertEqual(candidate.rights_evidence.access_class, "metadata")

    def test_arxiv_and_europepmc_html_are_used_as_fulltext(self):
        for landing in (
            "https://arxiv.org/html/2401.01234v1",
            "https://europepmc.org/article/MED/38000000",
        ):
            def fixture(url, timeout, landing=landing):
                doc = _openalex_fixture()
                doc["results"] = [doc["results"][0]]
                doc["results"][0]["best_oa_location"] = {
                    "license": "cc-by", "landing_page_url": landing, "pdf_url": None,
                }
                return _FakeResponse(doc, url)

            candidates = discover_scholarly(
                domain="training", topics=["hypertrophy"], http_get=fixture,
                fetched_at=lambda: "2026-07-19T00:00:00Z",
            )
            self.assertEqual(candidates[0].content_locator, landing)

    def test_generic_landing_only_is_metadata_only(self):
        def fixture_landing_only(url, timeout):
            doc = _openalex_fixture()
            doc["results"] = [doc["results"][0]]
            doc["results"][0]["best_oa_location"] = {
                "license": "cc-by",
                "landing_page_url": "https://journal.example.com/article/123",
                "pdf_url": None,
            }
            return _FakeResponse(doc, url)

        candidates = discover_scholarly(
            domain="training", topics=["hypertrophy"], http_get=fixture_landing_only,
            fetched_at=lambda: "2026-07-19T00:00:00Z",
        )
        oa = candidates[0]
        # A generic landing shell with no PDF is never claimed as full text: it
        # degrades to metadata-only so the executor never fetches the shell.
        self.assertIsNone(oa.content_locator)
        self.assertEqual(oa.rights_evidence.access_class, "metadata")

    def test_pdf_only_open_work_surfaces_pdf_locator(self):
        def fixture_pdf_only(url, timeout):
            doc = _openalex_fixture()
            doc["results"] = [doc["results"][0]]
            doc["results"][0]["best_oa_location"] = {
                "license": "cc-by",
                "pdf_url": "https://example.org/only.pdf",
                "landing_page_url": None,
            }
            return _FakeResponse(doc, url)

        candidates = discover_scholarly(
            domain="training", topics=["hypertrophy"], http_get=fixture_pdf_only,
            fetched_at=lambda: "2026-07-19T00:00:00Z",
        )
        oa = candidates[0]
        # When only a PDF exists it is still surfaced (the executor gates it);
        # discovery never fabricates an HTML locator.
        self.assertEqual(oa.content_locator, "https://example.org/only.pdf")

    def test_closed_work_is_metadata_only_evidence(self):
        candidates = discover_scholarly(
            domain="training",
            topics=["sprint-speed"],
            http_get=self.fake_get,
            fetched_at=lambda: "2026-07-19T00:00:00Z",
        )
        closed = next(c for c in candidates if c.openalex_id == "W4000000002")
        self.assertIsNone(closed.content_locator)
        self.assertEqual(closed.rights_evidence.access_class, "metadata")

    def test_discovered_candidates_are_not_checked_in_seeds(self):
        # The seed bundle uses pubmed.ncbi.nlm.nih.gov locators; discovery uses
        # openalex.org locators, so candidate identities cannot collide.
        candidates = discover_scholarly(
            domain="training",
            topics=["hypertrophy"],
            http_get=self.fake_get,
            fetched_at=lambda: "2026-07-19T00:00:00Z",
        )
        seed_path = ROOT / "docs" / "source-maps" / "training-seed-candidates.json"
        seed_ids = set()
        if seed_path.exists():
            from corpus_seed_loader import load_candidate_seed
            bundle = load_candidate_seed(seed_path)
            seed_ids = {
                obs.candidate_key
                for obs in bundle.observations(source_ref=str(seed_path))
            }
        discovered_ids = {c.observation.candidate_key for c in candidates}
        self.assertTrue(discovered_ids)
        self.assertEqual(discovered_ids & seed_ids, set())

    def test_bounded_by_max_candidates(self):
        candidates = discover_scholarly(
            domain="training",
            topics=["hypertrophy"],
            http_get=self.fake_get,
            fetched_at=lambda: "2026-07-19T00:00:00Z",
            max_candidates=1,
        )
        self.assertEqual(len(candidates), 1)


if __name__ == "__main__":
    unittest.main()
