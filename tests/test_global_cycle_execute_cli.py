import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_SCRIPT = ROOT / "scripts" / "corpus_global_cycle.py"
_spec = importlib.util.spec_from_file_location("corpus_global_cycle_script", _SCRIPT)
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

from corpus_acquisition_executor import FetchResult
from corpus_scholarly_discovery import ScholarlyCandidate
from corpus_rights_resolver import RightsEvidence


def _fake_discover(domain, topics):
    CandidateObservation = script.CandidateRecord.from_observation.__func__.__globals__["CandidateObservation"]
    obs = CandidateObservation.create(
        domain=domain, entity_type="paper", canonical_url="https://openalex.org/W9999999999",
        discovery_source="scholarly_discovery:openalex:W9999999999",
        evidence_pointer="https://example.org/oa", evidence_lane="primary-study",
        topics=("hypertrophy",), observed_at="2026-07-19T00:00:00Z",
    )
    return [ScholarlyCandidate(
        observation=obs,
        rights_evidence=RightsEvidence(license="cc-by", access_class="open_content"),
        openalex_id="W9999999999", title="Discovered open work",
        content_locator="https://example.org/oa",
        metadata={"openalex_id": "W9999999999"},
    )]


def _html_fetch(url, *, timeout, max_bytes=None):
    return FetchResult(
        body=b"<html><body><p>hypertrophy strength adaptation evidence text long enough here</p></body></html>",
        final_url=url, content_type="text/html")


class DefaultDiscoveryFallbackTests(unittest.TestCase):
    def _candidate(self, *, locator):
        CandidateObservation = script.CandidateRecord.from_observation.__func__.__globals__["CandidateObservation"]
        obs = CandidateObservation.create(
            domain="neuroscience", entity_type="paper",
            canonical_url=f"https://openalex.org/W{abs(hash(locator))}",
            discovery_source="scholarly_discovery:test", evidence_pointer=locator,
            evidence_lane="primary-study", topics=("neuroscience",),
            observed_at="2026-07-20T00:00:00Z",
        )
        return ScholarlyCandidate(
            observation=obs,
            rights_evidence=RightsEvidence(license="cc-by", access_class="open_content"),
            openalex_id="test", title="Neuroscience evidence", content_locator=locator,
            metadata={},
        )

    def test_openalex_pdf_only_triggers_europepmc_html_fallback(self):
        pdf = self._candidate(locator="https://journal.example/paper.pdf")
        html = self._candidate(locator="https://pmc.ncbi.nlm.nih.gov/articles/PMC123/")
        calls = []
        with patch.object(script, "discover_scholarly", lambda **_kwargs: [pdf]), \
             patch.object(script, "discover_europepmc", lambda **_kwargs: calls.append("europepmc") or [html]):
            discover = script._default_discover(lambda: "2026-07-20T00:00:00Z")
            result = discover("neuroscience", ["neuroscience-foundations"])
        self.assertEqual(calls, ["europepmc"])
        self.assertEqual(result[0].content_locator, html.content_locator)
        self.assertIn(pdf, result)

    def test_openalex_html_candidate_avoids_fallback(self):
        html = self._candidate(locator="https://pmc.ncbi.nlm.nih.gov/articles/PMC456/")
        with patch.object(script, "discover_scholarly", lambda **_kwargs: [html]), \
             patch.object(script, "discover_europepmc", lambda **_kwargs: self.fail("fallback should not run")):
            discover = script._default_discover(lambda: "2026-07-20T00:00:00Z")
            result = discover("neuroscience", ["neuroscience-foundations"])
        self.assertEqual(result, [html])


class MultiDomainExecuteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.corpora = Path(self._tmp.name) / "corpora"

    def _run(self):
        return script.run_execute(
            config_dir=ROOT / "config" / "domains",
            budget_ledger_path=self.corpora / "_engine" / "budget.json",
            reservation_id="global-2026-07-19-multi",
            now=datetime(2026, 7, 19, tzinfo=timezone.utc),
            corpora_root=self.corpora,
            discover=_fake_discover, http_fetch=_html_fetch,
            fetched_at=lambda: "2026-07-19T01:00:00Z", corpus_revision="rev-multi",
        )

    def test_projections_are_keyed_by_domain(self):
        result = self._run()
        self.assertEqual(result["status"], "executed")
        self.assertIn("training", result["projections"])
        self.assertIn("agentic-engineering", result["projections"])

    def test_each_domain_has_isolated_roots_and_projection(self):
        self._run()
        for domain in ("training", "agentic-engineering"):
            proj = self.corpora / domain / "acquisition" / "latest.json"
            self.assertTrue(proj.is_file(), f"missing projection for {domain}")
            data = json.loads(proj.read_text())
            self.assertEqual(data["corpus_revision"], "rev-multi")
            # Self-describing per-domain contract: the Atlas reads it without guessing.
            self.assertEqual(data["domain"], domain)
            self.assertEqual(data["cycle_id"], "global-2026-07-19-multi")
            self.assertIsInstance(data["schema_version"], int)
            # Domain-local receipts live under the domain's own acquisition root.
            receipts = self.corpora / domain / "acquisition" / "receipts"
            self.assertTrue(receipts.is_dir())

    def test_canonical_pages_admitted_under_each_domain_namespace(self):
        result = self._run()
        for domain, projection in result["projections"].items():
            for card in projection["recent_receipts"]:
                if card["disposition"] == "probationary":
                    acquired = self.corpora / domain / "sources" / "acquired"
                    self.assertTrue(any(acquired.glob("*.md")), domain)

    def test_no_cross_domain_leakage_and_no_paths_in_projection(self):
        self._run()
        training_receipts = list((self.corpora / "training" / "acquisition" / "receipts").glob("*.json"))
        agentic_ids = {
            r.stem for r in (self.corpora / "agentic-engineering" / "acquisition" / "receipts").glob("*.json")
        }
        training_ids = {r.stem for r in training_receipts}
        self.assertTrue(training_ids)
        self.assertEqual(training_ids & agentic_ids, set())
        blob = (self.corpora / "training" / "acquisition" / "latest.json").read_text()
        self.assertNotIn(str(self.corpora), blob)
        self.assertNotIn("pointer", blob)


class ExecuteCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_execute_mode_acquires_discovered_open_source(self):
        result = script.run_execute(
            config_dir=ROOT / "config" / "domains",
            budget_ledger_path=self.base / "budget.json",
            reservation_id="global-2026-07-19-exec",
            now=datetime(2026, 7, 19, tzinfo=timezone.utc),
            corpora_root=self.base,
            discover=_fake_discover,
            http_fetch=_html_fetch,
            fetched_at=lambda: "2026-07-19T01:00:00Z",
            corpus_revision="rev-exec",
        )
        self.assertEqual(result["status"], "executed")
        self.assertGreaterEqual(result["report"]["acquired"], 1)

    def test_execute_writes_redacted_projection_file(self):
        script.run_execute(
            config_dir=ROOT / "config" / "domains",
            budget_ledger_path=self.base / "budget.json",
            reservation_id="global-2026-07-19-exec",
            now=datetime(2026, 7, 19, tzinfo=timezone.utc),
            corpora_root=self.base,
            discover=_fake_discover,
            http_fetch=_html_fetch,
            fetched_at=lambda: "2026-07-19T01:00:00Z",
            corpus_revision="rev-exec",
        )
        projection_paths = list(self.base.glob("*/acquisition/latest.json"))
        self.assertTrue(projection_paths)
        blob = projection_paths[0].read_text(encoding="utf-8")
        self.assertNotIn(str(self.base), blob)
        projection = json.loads(blob)
        self.assertEqual(projection["corpus_revision"], "rev-exec")

    def _first_candidate(self, reservation_id):
        loaders = script.build_acquisition_loaders(
            ROOT / "config" / "domains", discover=_fake_discover, reservation_id=reservation_id
        )
        inputs = loaders[0][1]()
        return next(iter(inputs.acquisition_candidates.values()))

    def test_idempotency_key_is_independent_of_reservation(self):
        # The same source discovered under two different daily reservations must
        # map to the SAME idempotency key, so it is never re-acquired per day.
        cand_a = self._first_candidate("global-2026-07-19-AAAA")
        cand_b = self._first_candidate("global-2026-08-30-BBBB")
        self.assertEqual(cand_a.idempotency_key, cand_b.idempotency_key)
        self.assertNotIn("2026-07-19", cand_a.idempotency_key)
        self.assertNotIn("AAAA", cand_a.idempotency_key)

    def test_idempotency_key_binds_domain_and_candidate_identity(self):
        cand = self._first_candidate("global-2026-07-19-AAAA")
        self.assertIn(cand.domain, cand.idempotency_key)
        self.assertIn(cand.candidate_id, cand.idempotency_key)
        # Source revision is recorded so a changed source produces a new key.
        self.assertIn("source_revision", cand.metadata)

    def test_unchanged_next_day_run_does_not_refetch_or_rewrite(self):
        fetches = []

        def counting_fetch(url, *, timeout, max_bytes=None):
            fetches.append(url)
            return FetchResult(
                body=b"<html><body><p>hypertrophy strength adaptation evidence text long enough here</p></body></html>",
                final_url=url, content_type="text/html")

        common = dict(
            config_dir=ROOT / "config" / "domains",
            budget_ledger_path=self.base / "budget.json",
            now=datetime(2026, 7, 19, tzinfo=timezone.utc),
            corpora_root=self.base, discover=_fake_discover,
            http_fetch=counting_fetch, fetched_at=lambda: "2026-07-19T01:00:00Z",
            corpus_revision="rev-exec",
        )
        def evidence_snapshot():
            # Receipts and preserved raw/canonical bytes must be stable; the
            # shared budget ledger and the regenerated projection legitimately
            # record the new daily reservation and are excluded.
            snap = {}
            for p in sorted(self.base.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(self.base).as_posix()
                if p.name in ("budget.json", "latest.json"):
                    continue
                snap[rel] = p.read_bytes()
            return snap

        script.run_execute(reservation_id="global-2026-07-19-day1", **common)
        first_fetches = len(fetches)
        before = evidence_snapshot()
        # A second run on another day with unchanged discovery.
        script.run_execute(reservation_id="global-2026-07-20-day2", **common)
        self.assertEqual(len(fetches), first_fetches, "unchanged source must not refetch")
        self.assertEqual(before, evidence_snapshot(), "receipts/canonical must be byte-identical")

    def test_planning_dry_run_still_works(self):
        result = script.run(
            config_dir=ROOT / "config" / "domains",
            budget_ledger_path=self.base / "plan-budget.json",
            reservation_id="global-2026-07-19-plan",
            now=datetime(2026, 7, 19, tzinfo=timezone.utc),
        )
        self.assertIn(result["status"], {"planned", "no_candidates"})


if __name__ == "__main__":
    unittest.main()
