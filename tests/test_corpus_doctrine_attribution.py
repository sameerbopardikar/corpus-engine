from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_doctrine import DoctrineEngine

CITATION = {
    "source_id": "corpora",
    "page_slug": "training/sources/paper-a",
    "locator": "section-3",
    "claim_sha256": "a" * 64,
    "evidence_class": "scientific",
}

RECEIPT = {
    "receipt_id": "adopt-0001",
    "authorized_by": "sameer",
    "granted_at": "2026-07-18T10:00:00+00:00",
    "statement": "Adopted after independent verification.",
}


def _engine(tmp: Path, domain: str | None = None) -> DoctrineEngine:
    return DoctrineEngine(tmp / "doctrine.jsonl", domain=domain)


def _propose(engine: DoctrineEngine, key: str = "force-velocity", command_id: str = "cmd-a") -> dict:
    return engine.propose(
        command_id=command_id,
        concept_key=key,
        title=key.replace("-", " ").title(),
        statement="Force-velocity profiling structures training prescription.",
        citations=[CITATION],
        rationale="Systematic review evidence.",
    )


class GenericAttributionDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)

    def test_new_concept_defaults_to_generic_not_adopted(self):
        concept = _propose(_engine(self.tmp))
        self.assertEqual(concept["adoption_state"], "not_adopted")
        self.assertIsNone(concept["holder_id"])
        self.assertIsNone(concept["adoption_receipt"])
        self.assertEqual(concept["epistemic_layer"], "external_corpus_synthesis")

    def test_sameer_adopted_projection_present_and_false_by_default(self):
        concept = _propose(_engine(self.tmp))
        self.assertIn("sameer_adopted", concept)
        self.assertFalse(concept["sameer_adopted"])

    def test_external_synthesis_is_never_auto_adopted(self):
        engine = _engine(self.tmp)
        _propose(engine)
        snapshot = engine.snapshot()
        for concept in snapshot["concepts"]:
            self.assertEqual(concept["adoption_state"], "not_adopted")


class ExplicitAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)

    def test_adopt_requires_explicit_receipt_and_sets_holder(self):
        engine = _engine(self.tmp)
        _propose(engine)
        adopted = engine.adopt(
            command_id="cmd-adopt",
            concept_key="force-velocity",
            holder_id="sameer",
            adoption_receipt=RECEIPT,
            rationale="Independent verification passed.",
        )
        self.assertEqual(adopted["adoption_state"], "adopted")
        self.assertEqual(adopted["holder_id"], "sameer")
        self.assertEqual(adopted["adoption_receipt"]["receipt_id"], "adopt-0001")
        self.assertTrue(adopted["sameer_adopted"])

    def test_adopt_without_receipt_fails_closed(self):
        engine = _engine(self.tmp)
        _propose(engine)
        with self.assertRaises(ValueError):
            engine.adopt(
                command_id="cmd-adopt",
                concept_key="force-velocity",
                holder_id="sameer",
                adoption_receipt=None,
                rationale="No receipt.",
            )

    def test_adopt_with_malformed_receipt_fails_closed(self):
        engine = _engine(self.tmp)
        _propose(engine)
        with self.assertRaises(ValueError):
            engine.adopt(
                command_id="cmd-adopt",
                concept_key="force-velocity",
                holder_id="sameer",
                adoption_receipt={"receipt_id": "x"},  # missing authorized_by/granted_at
                rationale="Bad receipt.",
            )

    def test_non_sameer_holder_does_not_set_sameer_projection(self):
        engine = _engine(self.tmp)
        _propose(engine)
        adopted = engine.adopt(
            command_id="cmd-adopt",
            concept_key="force-velocity",
            holder_id="lab-x",
            adoption_receipt=RECEIPT,
            rationale="Adopted by another holder.",
        )
        self.assertEqual(adopted["holder_id"], "lab-x")
        self.assertFalse(adopted["sameer_adopted"])


class DomainScopedIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)

    def test_domain_scoped_engine_stamps_domain_on_concepts(self):
        (self.tmp / "training").mkdir(parents=True, exist_ok=True)
        engine = DoctrineEngine(self.tmp / "training" / "doctrine.jsonl", domain="training")
        concept = _propose(engine)
        self.assertEqual(concept["domain"], "training")

    def test_undomained_engine_omits_domain_field_for_compatibility(self):
        concept = _propose(_engine(self.tmp))
        self.assertNotIn("domain", concept)

    def test_same_concept_key_isolated_across_domain_ledgers(self):
        (self.tmp / "a").mkdir()
        (self.tmp / "b").mkdir()
        engine_a = DoctrineEngine(self.tmp / "a" / "doctrine.jsonl", domain="training")
        engine_b = DoctrineEngine(self.tmp / "b" / "doctrine.jsonl", domain="agentic-engineering")
        concept_a = _propose(engine_a)
        concept_b = _propose(engine_b)
        self.assertEqual(concept_a["domain"], "training")
        self.assertEqual(concept_b["domain"], "agentic-engineering")
        self.assertEqual(concept_a["concept_key"], concept_b["concept_key"])


if __name__ == "__main__":
    unittest.main()
