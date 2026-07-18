from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import corpus_domain_spec as spec_mod
import corpus_scout_import as scout_mod

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_corpus_domain_spec import valid_spec  # noqa: E402


def valid_scout() -> dict:
    return {
        "schema_version": 1,
        "domain": "training",
        "scout_id": "training-terrain-scout-2026-07-18",
        "model": "claude-opus-4-8",
        "generated_at": "2026-07-18T00:00:00Z",
        "bounds": {"max_fields": 8, "max_sources": 6},
        "fields": [
            {
                "axis": "adaptation",
                "field": "myofibrillar-hypertrophy",
                "confidence": 0.7,
                "evidence_hint": "distinguish myofibrillar from sarcoplasmic",
                "topics": ["hypertrophy", "muscle-protein"],
            },
            {
                "axis": "mechanical-force-velocity",
                "field": "load-velocity-profiling",
                "confidence": 0.55,
                "evidence_hint": "velocity-based training zones",
                "topics": ["force-velocity", "velocity-based-training"],
            },
        ],
        "source_suggestions": [
            {
                "family": "peer-reviewed-literature",
                "locator": "https://pubmed.ncbi.nlm.nih.gov/12345678/",
                "title": "Force-velocity profiling meta-analysis",
                "confidence": 0.8,
                "rationale": "high-authority synthesis",
                "rights_hint": "public_rights_clear",
            },
            {
                "family": "personal-training-logs",
                "locator": "https://private.example.com/logs/sameer",
                "title": "Personal training logs",
                "confidence": 0.9,
                "rationale": "direct personal evidence",
                "rights_hint": "private_authorized",
            },
        ],
    }


class ScoutImportTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        spec_path = Path(self.tmpdir.name) / "spec.json"
        spec_path.write_text(json.dumps(valid_spec()), encoding="utf-8")
        self.spec = spec_mod.load_domain_spec(spec_path)

    def _write_scout(self, data: dict) -> Path:
        path = Path(self.tmpdir.name) / "scout.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_field_map_groups_by_axis_with_provenance_and_uncertainty(self):
        projection = scout_mod.import_scout_result(self.spec, self._write_scout(valid_scout()))
        self.assertEqual(projection.provenance["scout_id"], "training-terrain-scout-2026-07-18")
        self.assertEqual(projection.provenance["model"], "claude-opus-4-8")
        self.assertIn("adaptation", projection.field_map)
        adaptation = projection.field_map["adaptation"]
        self.assertEqual(adaptation[0].field, "myofibrillar-hypertrophy")
        self.assertEqual(adaptation[0].confidence, 0.7)
        self.assertEqual(adaptation[0].provenance["scout_id"], "training-terrain-scout-2026-07-18")

    def test_acquisitions_ranked_capped_and_rights_gated(self):
        projection = scout_mod.import_scout_result(self.spec, self._write_scout(valid_scout()))
        acquisitions = projection.ranked_acquisitions
        # Higher-priority, rights-clear source outranks the private log despite lower confidence.
        self.assertEqual(acquisitions[0].locator, "https://pubmed.ncbi.nlm.nih.gov/12345678/")
        self.assertFalse(acquisitions[0].requires_human_gate)
        private = [a for a in acquisitions if a.rights_hint == "private_authorized"][0]
        self.assertTrue(private.requires_human_gate)
        self.assertFalse(private.shared_corpus_eligible)
        self.assertLessEqual(len(acquisitions), self.spec.acquisition_policy["max_suggestions"])

    def test_unknown_axis_fields_are_preserved_as_provisional_not_dropped(self):
        data = valid_scout()
        data["fields"].append({
            "axis": "recovery-modalities",
            "field": "contrast-therapy",
            "confidence": 0.4,
            "evidence_hint": "provisional new axis",
            "topics": ["recovery"],
        })
        projection = scout_mod.import_scout_result(self.spec, self._write_scout(data))
        unmapped_axes = {entry.axis for entry in projection.unmapped_fields}
        self.assertIn("recovery-modalities", unmapped_axes)
        self.assertNotIn("recovery-modalities", projection.field_map)

    def test_rejects_domain_mismatch(self):
        data = valid_scout()
        data["domain"] = "agentic-engineering"
        with self.assertRaises(scout_mod.ScoutImportError):
            scout_mod.import_scout_result(self.spec, self._write_scout(data))

    def test_rejects_out_of_bounds_field_count(self):
        data = valid_scout()
        data["bounds"]["max_fields"] = 1
        with self.assertRaises(scout_mod.ScoutImportError):
            scout_mod.import_scout_result(self.spec, self._write_scout(data))

    def test_rejects_unknown_source_family(self):
        data = valid_scout()
        data["source_suggestions"][0]["family"] = "undeclared-family"
        with self.assertRaises(scout_mod.ScoutImportError):
            scout_mod.import_scout_result(self.spec, self._write_scout(data))

    def test_rejects_out_of_range_confidence_and_nonfinite(self):
        data = valid_scout()
        data["fields"][0]["confidence"] = 1.5
        with self.assertRaises(scout_mod.ScoutImportError):
            scout_mod.import_scout_result(self.spec, self._write_scout(data))
        nan_scout = Path(self.tmpdir.name) / "nan.json"
        nan_scout.write_text('{"schema_version": 1, "confidence": NaN}')
        with self.assertRaises(scout_mod.ScoutImportError):
            scout_mod.import_scout_result(self.spec, nan_scout)

    def test_rejects_unknown_rights_hint_and_credential_locator(self):
        bad_rights = valid_scout()
        bad_rights["source_suggestions"][0]["rights_hint"] = "totally_open"
        with self.assertRaises(scout_mod.ScoutImportError):
            scout_mod.import_scout_result(self.spec, self._write_scout(bad_rights))
        creds = valid_scout()
        creds["source_suggestions"][0]["locator"] = "https://user:pass@example.com/x"
        with self.assertRaises(scout_mod.ScoutImportError):
            scout_mod.import_scout_result(self.spec, self._write_scout(creds))

    def test_to_dict_is_json_serializable_and_preserves_provenance(self):
        projection = scout_mod.import_scout_result(self.spec, self._write_scout(valid_scout()))
        blob = json.dumps(projection.to_dict(), allow_nan=False)
        restored = json.loads(blob)
        self.assertEqual(restored["provenance"]["model"], "claude-opus-4-8")
        self.assertEqual(restored["domain"], "training")


if __name__ == "__main__":
    unittest.main()
