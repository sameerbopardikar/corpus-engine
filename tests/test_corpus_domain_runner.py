from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import corpus_domain_spec as spec_mod
import corpus_domain_runner as runner_mod

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_corpus_domain_spec import valid_spec  # noqa: E402

NOW = "2026-07-18T12:00:00Z"


def training_seed() -> dict:
    return {
        "schema_version": 1,
        "domain": "training",
        "status": "candidate-seed-not-promoted",
        "generated_at": "2026-07-18T00:00:00Z",
        "policy": "Candidate metadata only; rights verified before acquisition.",
        "candidates": [
            {
                "id": "fv-profiling-review",
                "entity_type": "paper",
                "canonical_url": "https://pubmed.ncbi.nlm.nih.gov/30000001/",
                "source_type": "web_document",
                "evidence_lane": "scientific-evaluation",
                "authority_tier": "A2",
                "refresh_class": "monthly",
                "topics": ["force-velocity", "power-output"],
            },
            {
                "id": "hypertrophy-meta",
                "entity_type": "paper",
                "canonical_url": "https://pubmed.ncbi.nlm.nih.gov/30000002/",
                "source_type": "web_document",
                "evidence_lane": "scientific-evaluation",
                "authority_tier": "A2",
                "refresh_class": "monthly",
                "topics": ["hypertrophy", "strength"],
            },
        ],
    }


def _source_card(*, title: str, topics: list[str], rights_state: str, evidence_lane: str) -> str:
    body = " ".join(topics)
    return (
        "---\n"
        'type: "source_card"\n'
        f'title: "{title}"\n'
        'privacy: "private"\n'
        "source_url: https://pubmed.ncbi.nlm.nih.gov/30000001/\n"
        "source_revision: rev-1\n"
        f"rights_state: {rights_state}\n"
        f"evidence_lane: {evidence_lane}\n"
        "candidate_status: acquired\n"
        "---\n\n"
        f"# {title}\n\n"
        f"This source covers {body}. It discusses {body} in depth.\n"
    )


class DomainRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        spec_path = self.root / "training.json"
        spec_path.write_text(json.dumps(valid_spec()), encoding="utf-8")
        self.spec = spec_mod.load_domain_spec(spec_path)
        self.seed_path = self.root / "training-seed.json"
        self.seed_path.write_text(json.dumps(training_seed()), encoding="utf-8")
        self.corpus_root = self.root / "corpus"
        self.state_root = self.root / "state"
        self.output_root = self.root / "corpus" / "discovery" / "domain-v1"

    def _write_page(self, relpath: str, content: str) -> None:
        path = self.corpus_root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _run(self, cycle_id="2026-07-18-training"):
        return runner_mod.run_domain_cycle(
            spec=self.spec,
            seed_path=self.seed_path,
            corpus_root=self.corpus_root,
            state_root=self.state_root,
            output_root=self.output_root,
            cycle_id=cycle_id,
            now=NOW,
        )

    def test_cycle_produces_receipt_with_all_stages(self):
        self._write_page(
            "sources/fv.md",
            _source_card(
                title="Force-velocity profiling",
                topics=["force-velocity", "power-output", "load-velocity"],
                rights_state="public_rights_clear",
                evidence_lane="scientific-evaluation",
            ),
        )
        receipt = self._run()
        self.assertEqual(receipt["domain"], "training")
        self.assertTrue(receipt["validate"]["ok"])
        self.assertTrue(receipt["rank"]["ranked_candidates"])
        self.assertIn("mechanical-force-velocity", receipt["project"]["field_map"])
        self.assertFalse(receipt["promotion_enabled"])
        self.assertFalse(receipt["production_mutation"])
        self.assertIn(receipt["doctrine"]["decision"], {"proposed", "declined"})
        self.assertIn("passed", receipt["evaluate"])
        self.assertIsNotNone(receipt["next_gap"])

    def test_doctrine_proposes_concept_for_covered_axis_with_rights_clear_citation(self):
        self._write_page(
            "sources/fv.md",
            _source_card(
                title="Force-velocity profiling",
                topics=["force-velocity", "power-output", "load-velocity"],
                rights_state="public_rights_clear",
                evidence_lane="scientific-evaluation",
            ),
        )
        receipt = self._run()
        self.assertEqual(receipt["doctrine"]["decision"], "proposed")
        self.assertEqual(receipt["doctrine"]["concept_key"], "mechanical-force-velocity")
        self.assertFalse(receipt["doctrine"]["sameer_adopted"])
        self.assertEqual(receipt["doctrine"]["epistemic_layer"], "external_corpus_synthesis")

    def test_doctrine_declines_with_reason_when_no_rights_clear_page(self):
        # Page covers the axis but is not rights-cleared, so nothing is citeable.
        self._write_page(
            "sources/fv.md",
            _source_card(
                title="Force-velocity profiling",
                topics=["force-velocity", "power-output"],
                rights_state="rights_unclear",
                evidence_lane="scientific-evaluation",
            ),
        )
        receipt = self._run()
        self.assertEqual(receipt["doctrine"]["decision"], "declined")
        self.assertTrue(receipt["doctrine"]["reason"])

    def test_next_gap_selects_uncovered_axis(self):
        self._write_page(
            "sources/fv.md",
            _source_card(
                title="Force-velocity profiling",
                topics=["force-velocity", "power-output"],
                rights_state="public_rights_clear",
                evidence_lane="scientific-evaluation",
            ),
        )
        receipt = self._run()
        # adaptation axis (hypertrophy/strength/endurance) is uncovered.
        self.assertEqual(receipt["next_gap"]["axis"], "adaptation")

    def test_writes_private_projection_files(self):
        self._write_page(
            "sources/fv.md",
            _source_card(
                title="Force-velocity profiling",
                topics=["force-velocity"],
                rights_state="public_rights_clear",
                evidence_lane="scientific-evaluation",
            ),
        )
        self._run()
        latest = self.output_root / "latest.json"
        self.assertTrue(latest.exists())
        self.assertEqual(oct(latest.stat().st_mode)[-3:], "600")
        self.assertTrue((self.output_root / "latest.md").exists())

    def test_cycle_is_idempotent_on_replay(self):
        self._write_page(
            "sources/fv.md",
            _source_card(
                title="Force-velocity profiling",
                topics=["force-velocity", "power-output"],
                rights_state="public_rights_clear",
                evidence_lane="scientific-evaluation",
            ),
        )
        first = self._run()
        second = self._run()
        self.assertEqual(first["receipt_sha256"], second["receipt_sha256"])
        ledger = self.state_root / "doctrine-ledger.jsonl"
        events_after_first = ledger.read_text().count("\n")
        self._run()
        self.assertEqual(ledger.read_text().count("\n"), events_after_first)

    def test_dry_run_writes_nothing(self):
        self._write_page(
            "sources/fv.md",
            _source_card(
                title="Force-velocity profiling",
                topics=["force-velocity"],
                rights_state="public_rights_clear",
                evidence_lane="scientific-evaluation",
            ),
        )
        runner_mod.run_domain_cycle(
            spec=self.spec,
            seed_path=self.seed_path,
            corpus_root=self.corpus_root,
            state_root=self.state_root,
            output_root=self.output_root,
            cycle_id="dry",
            now=NOW,
            dry_run=True,
        )
        self.assertFalse(self.output_root.exists())
        self.assertFalse(self.state_root.exists())

    def test_rejects_seed_domain_mismatch(self):
        bad = training_seed()
        bad["domain"] = "agentic-engineering"
        bad_path = self.root / "bad-seed.json"
        bad_path.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaises(runner_mod.DomainCycleError):
            runner_mod.run_domain_cycle(
                spec=self.spec,
                seed_path=bad_path,
                corpus_root=self.corpus_root,
                state_root=self.state_root,
                output_root=self.output_root,
                cycle_id="x",
                now=NOW,
            )


class CrossDomainIsolationTests(unittest.TestCase):
    """The same runner drives a second domain with zero domain-specific code."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _cooking_spec(self) -> spec_mod.DomainSpec:
        data = valid_spec()
        data["domain"] = "cooking"
        data["title"] = "Cooking Corpus"
        data["objective"] = "Understand and improve cooking technique across methods."
        data["seed_ref"] = "docs/source-maps/cooking-seed-candidates.json"
        data["roots"] = {
            "corpus_root": "cooking",
            "state_root": "cooking/self-expansion-v1",
            "archive_root": "cooking/archive",
            "output_root": "cooking/discovery/domain-v1",
        }
        data["ontology"] = {
            "axes": [
                {"key": "heat-transfer", "title": "Heat Transfer", "topics": ["maillard", "braising"]},
                {"key": "fermentation", "title": "Fermentation", "topics": ["sourdough", "koji"]},
            ]
        }
        path = self.root / "cooking.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return spec_mod.load_domain_spec(path)

    def _cooking_seed(self) -> Path:
        seed = training_seed()
        seed["domain"] = "cooking"
        seed["candidates"] = [
            {
                "id": "maillard-review",
                "entity_type": "paper",
                "canonical_url": "https://example.org/maillard",
                "source_type": "web_document",
                "evidence_lane": "scientific-evaluation",
                "authority_tier": "A2",
                "refresh_class": "monthly",
                "topics": ["maillard", "braising"],
            }
        ]
        path = self.root / "cooking-seed.json"
        path.write_text(json.dumps(seed), encoding="utf-8")
        return path

    def test_second_domain_runs_through_same_runner(self):
        spec = self._cooking_spec()
        seed = self._cooking_seed()
        corpus = self.root / "cooking-corpus"
        page = corpus / "sources" / "maillard.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            _source_card(
                title="Maillard reaction",
                topics=["maillard", "braising"],
                rights_state="public_rights_clear",
                evidence_lane="scientific-evaluation",
            ),
            encoding="utf-8",
        )
        receipt = runner_mod.run_domain_cycle(
            spec=spec,
            seed_path=seed,
            corpus_root=corpus,
            state_root=self.root / "cooking-state",
            output_root=corpus / "discovery" / "domain-v1",
            cycle_id="2026-07-18-cooking",
            now=NOW,
        )
        self.assertEqual(receipt["domain"], "cooking")
        self.assertIn("heat-transfer", receipt["project"]["field_map"])
        self.assertTrue(receipt["validate"]["ok"])
        # Isolation: nothing written under a training path.
        self.assertFalse((self.root / "training-state").exists())


if __name__ == "__main__":
    unittest.main()
