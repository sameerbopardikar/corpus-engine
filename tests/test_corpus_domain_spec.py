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


def valid_spec() -> dict:
    return {
        "schema_version": 1,
        "domain": "training",
        "title": "Training Operating Corpus",
        "objective": "Understand and improve human physical training across adaptations.",
        "epistemic_policy": "capture-all-promote-by-evidence-v1",
        "seed_ref": "docs/source-maps/training-seed-candidates.json",
        "roots": {
            "corpus_root": "training",
            "state_root": "training/self-expansion-v1",
            "archive_root": "training/archive",
            "output_root": "training/discovery/domain-v1",
        },
        "ontology": {
            "axes": [
                {
                    "key": "mechanical-force-velocity",
                    "title": "Force-Velocity Mechanical Axis",
                    "topics": ["force-velocity", "load-velocity", "power-output"],
                },
                {
                    "key": "adaptation",
                    "title": "Adaptation Axis",
                    "topics": ["hypertrophy", "strength", "endurance"],
                },
            ]
        },
        "evidence_lanes": {
            "scientific-evaluation": 1.15,
            "practitioner-implementation": 0.95,
            "unverified-discovery-signal": 0.55,
            "default": 0.85,
        },
        "source_families": [
            {
                "family": "peer-reviewed-literature",
                "acquisition_mode": "public_rights_clear",
                "rights_state": "public_rights_clear",
                "shared_corpus_eligible": True,
            },
            {
                "family": "personal-training-logs",
                "acquisition_mode": "authenticated_private_ingest",
                "rights_state": "private_authorized",
                "shared_corpus_eligible": False,
            },
        ],
        "feedback_profile": {
            "corroboration_increment": 0.1,
            "starvation_age_boost_per_day": 0.03,
            "starvation_max_boost": 0.3,
            "yield_target_per_cycle": 1,
        },
        "acquisition_policy": {
            "max_suggestions": 5,
            "family_priority": ["peer-reviewed-literature", "personal-training-logs"],
        },
        "bootstrap": {
            "max_items_per_cycle": 3,
            "max_deep_acquisitions_per_utc_day": 3,
            "max_llm_tasks_per_cycle": 1,
            "max_cost_usd_per_utc_day": 1.0,
        },
        "promotion": {
            "automatic_promotion_enabled": False,
            "probationary_threshold": 0.35,
            "promotion_threshold": 0.65,
            "rejection_threshold": 0.1,
        },
        "eval_requirements": {
            "min_corpus_pages_scanned": 0,
            "require_axes_in_field_map": True,
            "forbid_private_export_to_shared": True,
            "require_next_gap": True,
        },
    }


class DomainSpecLoadTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _write(self, data: dict) -> Path:
        path = Path(self.tmpdir.name) / "spec.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_valid_spec_loads_identity_and_ontology(self):
        spec = spec_mod.load_domain_spec(self._write(valid_spec()))
        self.assertEqual(spec.domain, "training")
        self.assertEqual(spec.title, "Training Operating Corpus")
        self.assertEqual([axis.key for axis in spec.ontology_axes], ["mechanical-force-velocity", "adaptation"])
        self.assertEqual(spec.seed_ref, "docs/source-maps/training-seed-candidates.json")

    def test_rejects_unknown_top_level_field(self):
        data = valid_spec()
        data["surprise"] = True
        with self.assertRaises(spec_mod.DomainSpecError):
            spec_mod.load_domain_spec(self._write(data))

    def test_rejects_unknown_nested_field(self):
        data = valid_spec()
        data["promotion"]["surprise"] = 1
        with self.assertRaises(spec_mod.DomainSpecError):
            spec_mod.load_domain_spec(self._write(data))

    def test_rejects_duplicate_json_keys_and_nonfinite_numbers(self):
        dup = Path(self.tmpdir.name) / "dup.json"
        dup.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(spec_mod.DomainSpecError):
            spec_mod.load_domain_spec(dup)
        nonfinite = Path(self.tmpdir.name) / "nonfinite.json"
        nonfinite.write_text('{"schema_version": Infinity}')
        with self.assertRaises(spec_mod.DomainSpecError):
            spec_mod.load_domain_spec(nonfinite)

    def test_rejects_bad_schema_version_and_non_kebab_domain(self):
        bad_version = valid_spec()
        bad_version["schema_version"] = 2
        with self.assertRaises(spec_mod.DomainSpecError):
            spec_mod.load_domain_spec(self._write(bad_version))
        bad_domain = valid_spec()
        bad_domain["domain"] = "Training_Domain"
        with self.assertRaises(spec_mod.DomainSpecError):
            spec_mod.load_domain_spec(self._write(bad_domain))

    def test_rejects_duplicate_axis_keys_and_topics(self):
        dup_axis = valid_spec()
        dup_axis["ontology"]["axes"].append(copy.deepcopy(dup_axis["ontology"]["axes"][0]))
        with self.assertRaises(spec_mod.DomainSpecError):
            spec_mod.load_domain_spec(self._write(dup_axis))
        dup_topic = valid_spec()
        dup_topic["ontology"]["axes"][0]["topics"].append(dup_topic["ontology"]["axes"][0]["topics"][0])
        with self.assertRaises(spec_mod.DomainSpecError):
            spec_mod.load_domain_spec(self._write(dup_topic))

    def test_rejects_duplicate_source_family_names(self):
        data = valid_spec()
        data["source_families"].append(copy.deepcopy(data["source_families"][0]))
        with self.assertRaises(spec_mod.DomainSpecError):
            spec_mod.load_domain_spec(self._write(data))

    def test_rejects_automatic_promotion_enabled(self):
        data = valid_spec()
        data["promotion"]["automatic_promotion_enabled"] = True
        with self.assertRaises(spec_mod.DomainSpecError):
            spec_mod.load_domain_spec(self._write(data))

    def test_rejects_threshold_misordering(self):
        data = valid_spec()
        data["promotion"]["probationary_threshold"] = 0.9  # exceeds promotion threshold
        with self.assertRaises(spec_mod.DomainSpecError):
            spec_mod.load_domain_spec(self._write(data))

    def test_rejects_negative_and_nonfinite_budgets(self):
        data = valid_spec()
        data["bootstrap"]["max_items_per_cycle"] = -1
        with self.assertRaises(spec_mod.DomainSpecError):
            spec_mod.load_domain_spec(self._write(data))

    def test_rejects_absolute_and_traversal_roots(self):
        absolute = valid_spec()
        absolute["roots"]["output_root"] = "/root/corpora/training/discovery"
        with self.assertRaises(spec_mod.DomainSpecError):
            spec_mod.load_domain_spec(self._write(absolute))
        traversal = valid_spec()
        traversal["roots"]["output_root"] = "training/../../etc"
        with self.assertRaises(spec_mod.DomainSpecError):
            spec_mod.load_domain_spec(self._write(traversal))

    def test_rejects_absolute_and_traversal_seed_ref(self):
        data = valid_spec()
        data["seed_ref"] = "../../etc/passwd"
        with self.assertRaises(spec_mod.DomainSpecError):
            spec_mod.load_domain_spec(self._write(data))

    def test_rejects_private_family_marked_shared_corpus_eligible(self):
        data = valid_spec()
        data["source_families"][1]["shared_corpus_eligible"] = True  # private_authorized family
        with self.assertRaises(spec_mod.DomainSpecError):
            spec_mod.load_domain_spec(self._write(data))

    def test_rejects_unknown_rights_state(self):
        data = valid_spec()
        data["source_families"][0]["rights_state"] = "totally_open"
        with self.assertRaises(spec_mod.DomainSpecError):
            spec_mod.load_domain_spec(self._write(data))

    def test_requires_default_evidence_lane(self):
        data = valid_spec()
        del data["evidence_lanes"]["default"]
        with self.assertRaises(spec_mod.DomainSpecError):
            spec_mod.load_domain_spec(self._write(data))


if __name__ == "__main__":
    unittest.main()
