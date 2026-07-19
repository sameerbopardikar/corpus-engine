from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_bootstrap_packet import (  # noqa: E402
    BootstrapPacketError,
    MIN_AXES,
    MIN_SOURCE_FAMILIES,
    MIN_TOPICS_TOTAL,
    compile_domain_inputs,
    validate_bootstrap_packet,
)
from corpus_domain_spec import load_domain_spec  # noqa: E402
from corpus_seed_loader import load_candidate_seed  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "nutrition-bootstrap-packet.json"
NOW = "2026-07-19T12:00:00Z"


def _packet() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class ValidatePacketTest(unittest.TestCase):
    def test_fixture_is_valid(self):
        packet = validate_bootstrap_packet(_packet(), clean=True)
        self.assertEqual(packet["domain"], "nutrition")
        self.assertEqual(packet["topic_input"], "Nutrition")

    def test_rejects_unknown_key(self):
        data = _packet()
        data["surprise"] = 1
        with self.assertRaises(BootstrapPacketError):
            validate_bootstrap_packet(data, clean=True)

    def test_rejects_missing_key(self):
        data = _packet()
        del data["axes"]
        with self.assertRaises(BootstrapPacketError):
            validate_bootstrap_packet(data, clean=True)

    def test_rejects_bad_schema_version(self):
        data = _packet()
        data["schema_version"] = 2
        with self.assertRaises(BootstrapPacketError):
            validate_bootstrap_packet(data, clean=True)

    def test_rejects_unsafe_domain(self):
        for bad in ["../etc", "Nutrition", "a/b", ""]:
            data = _packet()
            data["domain"] = bad
            with self.assertRaises(BootstrapPacketError):
                validate_bootstrap_packet(data, clean=True)

    def test_rejects_non_finite_evidence_weight(self):
        data = _packet()
        data["evidence_lanes"] = {"default": float("inf")}
        with self.assertRaises(BootstrapPacketError):
            validate_bootstrap_packet(data, clean=True)

    def test_rejects_negative_evidence_weight(self):
        data = _packet()
        data["evidence_lanes"] = {"default": 0.85, "systematic-review": -1.0}
        with self.assertRaises(BootstrapPacketError):
            validate_bootstrap_packet(data, clean=True)

    def test_evidence_lanes_require_default(self):
        data = _packet()
        data["evidence_lanes"] = {"systematic-review": 1.25}
        with self.assertRaises(BootstrapPacketError):
            validate_bootstrap_packet(data, clean=True)

    def test_min_axis_diversity(self):
        data = _packet()
        data["axes"] = data["axes"][:1]
        self.assertLess(len(data["axes"]), MIN_AXES)
        with self.assertRaises(BootstrapPacketError):
            validate_bootstrap_packet(data, clean=True)

    def test_min_total_topics(self):
        data = _packet()
        data["axes"] = [{"key": "only", "title": "Only", "topics": ["a"]}, {"key": "two", "title": "Two", "topics": ["b"]}]
        self.assertLess(2, MIN_TOPICS_TOTAL)
        with self.assertRaises(BootstrapPacketError):
            validate_bootstrap_packet(data, clean=True)

    def test_min_source_families(self):
        data = _packet()
        data["source_families"] = data["source_families"][:1]
        self.assertLess(len(data["source_families"]), MIN_SOURCE_FAMILIES)
        with self.assertRaises(BootstrapPacketError):
            validate_bootstrap_packet(data, clean=True)

    def test_rejects_non_https_locator(self):
        data = _packet()
        data["candidate_locators"][0]["url"] = "http://insecure.example/x"
        with self.assertRaises(BootstrapPacketError):
            validate_bootstrap_packet(data, clean=True)

    def test_rejects_duplicate_locator(self):
        data = _packet()
        data["candidate_locators"][1]["url"] = data["candidate_locators"][0]["url"]
        with self.assertRaises(BootstrapPacketError):
            validate_bootstrap_packet(data, clean=True)

    def test_rejects_duplicate_axis_key(self):
        data = _packet()
        data["axes"][1]["key"] = data["axes"][0]["key"]
        with self.assertRaises(BootstrapPacketError):
            validate_bootstrap_packet(data, clean=True)

    def test_rejects_duplicate_family(self):
        data = _packet()
        data["source_families"][1]["family"] = data["source_families"][0]["family"]
        with self.assertRaises(BootstrapPacketError):
            validate_bootstrap_packet(data, clean=True)

    def test_rejects_unknown_acquisition_mode(self):
        data = _packet()
        data["source_families"][0]["acquisition_mode"] = "login_scrape"
        with self.assertRaises(BootstrapPacketError):
            validate_bootstrap_packet(data, clean=True)

    def test_scout_provenance_https_only(self):
        data = _packet()
        data["scout_provenance"][0]["result_url"] = "http://insecure.example/x"
        with self.assertRaises(BootstrapPacketError):
            validate_bootstrap_packet(data, clean=True)

    def test_clean_mode_rejects_existing_context_refs(self):
        data = _packet()
        data["existing_context_refs"] = ["/root/corpora/nutrition/sources/x.md"]
        with self.assertRaises(BootstrapPacketError):
            validate_bootstrap_packet(data, clean=True)

    def test_non_clean_mode_allows_existing_context_refs(self):
        data = _packet()
        data["existing_context_refs"] = ["/root/corpora/nutrition/sources/x.md"]
        packet = validate_bootstrap_packet(data, clean=False)
        self.assertEqual(len(packet["existing_context_refs"]), 1)


class CompileDomainInputsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_compiles_loadable_domain_spec_and_seed(self):
        inputs = compile_domain_inputs(_packet(), now=NOW)
        self.assertEqual(set(inputs), {"domain_spec", "seed_bundle"})

        spec_path = self.tmp / "spec.json"
        seed_path = self.tmp / "seed.json"
        spec_path.write_text(json.dumps(inputs["domain_spec"]), encoding="utf-8")
        seed_path.write_text(json.dumps(inputs["seed_bundle"]), encoding="utf-8")

        spec = load_domain_spec(spec_path)
        self.assertEqual(spec.domain, "nutrition")
        self.assertEqual(spec.title, "Nutrition Research Corpus")
        self.assertGreaterEqual(len(spec.ontology_axes), MIN_AXES)

        seed = load_candidate_seed(seed_path)
        self.assertEqual(seed.domain, "nutrition")
        self.assertEqual(len(seed.candidates), 5)

    def test_rights_derived_from_acquisition_mode(self):
        spec = compile_domain_inputs(_packet(), now=NOW)["domain_spec"]
        by_family = {f["family"]: f for f in spec["source_families"]}
        fetch = by_family["systematic-review-database"]
        self.assertEqual(fetch["rights_state"], "public_rights_clear")
        self.assertTrue(fetch["shared_corpus_eligible"])
        meta = by_family["government-dietary-guideline"]
        self.assertEqual(meta["rights_state"], "public_metadata_only")
        self.assertFalse(meta["shared_corpus_eligible"])

    def test_no_training_specific_values(self):
        blob = json.dumps(compile_domain_inputs(_packet(), now=NOW))
        self.assertNotIn("training", blob.lower())
        self.assertNotIn("agentic-engineering", blob.lower())

    def test_cannot_write_corpus_pages(self):
        # The compiler only emits declarative inputs — never authored corpus
        # markdown pages. No key may carry page/markdown body content.
        inputs = compile_domain_inputs(_packet(), now=NOW)
        blob = json.dumps(inputs)
        self.assertNotIn("---\n", blob)  # no frontmatter markdown page bodies
        for forbidden in ("pages", "page_body", "markdown", "body_md"):
            self.assertNotIn(forbidden, inputs["domain_spec"])
            self.assertNotIn(forbidden, inputs["seed_bundle"])

    def test_compile_is_deterministic(self):
        a = compile_domain_inputs(_packet(), now=NOW)
        b = compile_domain_inputs(_packet(), now=NOW)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_family_priority_matches_packet_order(self):
        spec = compile_domain_inputs(_packet(), now=NOW)["domain_spec"]
        self.assertEqual(
            spec["acquisition_policy"]["family_priority"],
            ["systematic-review-database", "peer-reviewed-journal", "government-dietary-guideline"],
        )


if __name__ == "__main__":
    unittest.main()
