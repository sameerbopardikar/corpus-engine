from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from defusedxml.common import DefusedXmlException

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_adapters import base as adapter_base
from corpus_adapters.arxiv import _entries
from corpus_adapters.base import AdapterRunner
from corpus_adapters.http import safe_get
from corpus_adapters.rss import _items
from corpus_adapters.youtube import parse_feed
from corpus_engine_models import _normalize_url
from corpus_intake import IntakeStore
from corpus_scholarly_discovery import discover_europepmc
from corpus_ssrf import SSRFError


class SafeAdapterHttpTests(unittest.TestCase):
    def test_default_adapter_transport_rejects_localhost_before_network(self):
        with self.assertRaises(SSRFError):
            safe_get("http://localhost:8080/private", 1.0)

    def test_response_shim_preserves_content_url_type_and_accept_header(self):
        fetched = SimpleNamespace(
            body=b'{"ok": true}',
            final_url="https://api.example.test/final",
            content_type="application/json",
        )
        with mock.patch("corpus_live_fetch.live_fetch", return_value=fetched) as live:
            response = safe_get(
                "https://api.example.test/start",
                2.5,
                headers={"Accept": "application/vnd.example+json"},
            )
        live.assert_called_once_with(
            "https://api.example.test/start",
            timeout=2.5,
            headers={"Accept": "application/vnd.example+json"},
        )
        self.assertEqual(response.url, "https://api.example.test/final")
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.json(), {"ok": True})


class DefusedXmlRegressionTests(unittest.TestCase):
    ENTITY_PAYLOAD = b'<!DOCTYPE root [<!ENTITY injected "boom">]><root>&injected;</root>'

    def test_arxiv_rss_and_youtube_reject_entity_expansion(self):
        for parser in (_entries, _items, parse_feed):
            with self.subTest(parser=parser.__module__):
                with self.assertRaises(DefusedXmlException):
                    parser(self.ENTITY_PAYLOAD)

    def test_docx_entity_expansion_is_rejected_without_publishing_card(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = IntakeStore(root / "intake")
            document = root / "malicious.docx"
            with zipfile.ZipFile(document, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("_rels/.rels", "<Relationships/>")
                archive.writestr("word/document.xml", self.ENTITY_PAYLOAD)
            receipt = store.submit_file(
                title="Malicious XML",
                file_path=document,
                rights_state="owned",
                origin={"platform": "test"},
            )
            corpus = root / "corpus"
            result = store.process_next(corpus_root=corpus)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(store.get(receipt["receipt_id"])["status"], "rejected")
            self.assertFalse(any(corpus.rglob("*.md")) if corpus.exists() else False)

    def test_ncbi_license_entity_expansion_is_rejected(self):
        search = {
            "resultList": {
                "result": [
                    {"pmcid": "PMC123", "title": "Paper", "doi": "10.1/test"}
                ]
            }
        }
        calls = iter(
            [
                SimpleNamespace(content=json.dumps(search).encode()),
                SimpleNamespace(content=self.ENTITY_PAYLOAD),
            ]
        )
        candidates = discover_europepmc(
            domain="training",
            topics=["strength"],
            http_get=lambda *_args, **_kwargs: next(calls),
            fetched_at=lambda: "2026-07-20T00:00:00Z",
            max_candidates=1,
        )
        self.assertEqual(candidates, [])


class FileDescriptorRegressionTests(unittest.TestCase):
    def test_lock_descriptor_closes_when_fchmod_fails(self):
        runner = AdapterRunner(Path(tempfile.mkdtemp()) / "state.json")
        real_open = os.open
        opened_fds = []

        def capture_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened_fds.append(fd)
            return fd

        with (
            mock.patch.object(adapter_base.os, "open", side_effect=capture_open),
            mock.patch.object(adapter_base.os, "fchmod", side_effect=OSError("denied")),
        ):
            with self.assertRaisesRegex(OSError, "denied"):
                runner._commit(object())  # type: ignore[arg-type]
        self.assertEqual(len(opened_fds), 1)
        with self.assertRaises(OSError):
            os.fstat(opened_fds[0])


class IntegrityRegressionTests(unittest.TestCase):
    def test_invalid_idna_host_has_stable_value_error(self):
        malformed = f"https://{'a' * 64}.example/path"
        with self.assertRaisesRegex(ValueError, "canonical_url has an invalid host"):
            _normalize_url(malformed)

    def test_policy_projection_and_seed_lanes_cannot_drift(self):
        policy = json.loads((ROOT / "config" / "agentic-engineering-policy.json").read_text())
        agentic = json.loads((ROOT / "config" / "domains" / "agentic-engineering.json").read_text())
        self.assertEqual(policy["evidence_lane_weights"], agentic["evidence_lanes"])

        for domain_name in ("training", "agentic-engineering"):
            with self.subTest(domain=domain_name):
                domain = json.loads((ROOT / "config" / "domains" / f"{domain_name}.json").read_text())
                profile = json.loads((ROOT / "config" / "profiles" / f"{domain_name}-default.json").read_text())
                seeds = json.loads((ROOT / "docs" / "source-maps" / f"{domain_name}-seed-candidates.json").read_text())
                domain_lanes = set(domain["evidence_lanes"])
                profile_lanes = set(profile["applicability"]["evidence_lanes_allow"])
                for candidate in seeds["candidates"]:
                    self.assertIn(candidate["evidence_lane"], domain_lanes, candidate["id"])
                    self.assertIn(candidate["evidence_lane"], profile_lanes, candidate["id"])

    def test_broad_source_family_identity_never_grants_shared_rights(self):
        domain = json.loads((ROOT / "config" / "domains" / "agentic-engineering.json").read_text())
        families = {item["family"]: item for item in domain["source_families"]}
        for name in ("peer-reviewed-literature", "open-source-repository"):
            with self.subTest(family=name):
                self.assertEqual(families[name]["rights_state"], "rights_unclear")
                self.assertFalse(families[name]["shared_corpus_eligible"])

    def test_post_publication_failure_removes_card_and_evaluation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = IntakeStore(root / "intake")
            receipt = store.submit_suggestion(
                title="Rollback proof",
                text="candidate only",
                origin={"platform": "test"},
            )
            original_atomic_json = store._atomic_json

            def write_then_fail(path, value):
                original_atomic_json(path, value)
                raise OSError("injected post-write failure")

            with mock.patch.object(store, "_atomic_json", side_effect=write_then_fail):
                result = store.process_next(corpus_root=root / "corpus")
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(store.get(receipt["receipt_id"])["status"], "rejected")
            self.assertFalse(any((root / "corpus").rglob("*.md")))
            self.assertFalse(any((root / "intake" / "evaluations").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
