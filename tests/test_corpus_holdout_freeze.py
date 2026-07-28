from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
EVALS = ROOT / "evals" / "self-expansion" / "fixtures"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_holdout_freeze import HoldoutFreezeError, freeze_worker_packet, hidden_terms


class HoldoutFreezeTests(unittest.TestCase):
    def evaluator(self, **overrides) -> dict:
        value = {
            "target_label": "Scratch-pad active memory pattern",
            "accepted_close_labels": ["externalized active memory"],
            "forbidden_seed_terms": ["scratchpad", "active memory"],
        }
        value.update(overrides)
        return value

    def packet(self, excerpt: str = "The operator keeps a plan file between runs.") -> dict:
        return {
            "schema_version": 1,
            "seed_packet": {"registry": ["a channel"], "queries": ["agent reliability"]},
            "cycle_a_artifacts": [
                {
                    "url": "https://www.youtube.com/watch?v=090oR--s__8",
                    "published_at": "2024-10-14",
                    "source_sha256": "0" * 64,
                    "transcript_excerpt": excerpt,
                }
            ],
        }

    def test_freeze_hashes_the_packet_and_every_artifact_body(self):
        packet = self.packet()
        receipt = freeze_worker_packet(
            packet, hidden_evaluator=self.evaluator(), packet_label="scratchpad"
        )
        self.assertEqual(receipt["artifact_count"], 1)
        artifact = receipt["artifacts"][0]
        self.assertEqual(artifact["url"], "https://www.youtube.com/watch?v=090oR--s__8")
        self.assertEqual(
            artifact["sha256"],
            hashlib.sha256(packet["cycle_a_artifacts"][0]["transcript_excerpt"].encode()).hexdigest(),
        )
        self.assertEqual(artifact["text_field"], "transcript_excerpt")
        self.assertTrue(receipt["leakage_absent"])
        self.assertEqual(receipt["leaked_terms"], [])

    def test_freeze_is_deterministic_for_identical_bytes(self):
        first = freeze_worker_packet(
            self.packet(), hidden_evaluator=self.evaluator(), packet_label="scratchpad"
        )
        second = freeze_worker_packet(
            self.packet(), hidden_evaluator=self.evaluator(), packet_label="scratchpad"
        )
        self.assertEqual(first, second)

    def test_any_changed_worker_visible_byte_changes_the_freeze_digest(self):
        first = freeze_worker_packet(
            self.packet(), hidden_evaluator=self.evaluator(), packet_label="scratchpad"
        )
        second = freeze_worker_packet(
            self.packet("The operator keeps a plan file between runs and reloads it."),
            hidden_evaluator=self.evaluator(),
            packet_label="scratchpad",
        )
        self.assertNotEqual(first["packet_sha256"], second["packet_sha256"])

    def test_target_label_inside_an_artifact_body_is_detected(self):
        receipt = freeze_worker_packet(
            self.packet("This is the scratch-pad active memory pattern in practice."),
            hidden_evaluator=self.evaluator(),
            packet_label="scratchpad",
        )
        self.assertFalse(receipt["leakage_absent"])
        self.assertIn("Scratch-pad active memory pattern", receipt["leaked_terms"])
        finding = next(
            item for item in receipt["leakage_findings"]
            if item["term"] == "Scratch-pad active memory pattern"
        )
        self.assertIn("cycle_a_artifacts[0]", finding["locations"])

    def test_forbidden_term_anywhere_in_the_packet_is_detected(self):
        packet = self.packet()
        packet["seed_packet"]["queries"].append("scratchpad workflows")
        receipt = freeze_worker_packet(
            packet, hidden_evaluator=self.evaluator(), packet_label="scratchpad"
        )
        self.assertFalse(receipt["leakage_absent"])
        self.assertIn("scratchpad", receipt["leaked_terms"])

    def test_hidden_evaluator_terms_are_never_folded_into_the_scanned_surface(self):
        receipt = freeze_worker_packet(
            self.packet(), hidden_evaluator=self.evaluator(), packet_label="scratchpad"
        )
        self.assertIn("scratchpad", receipt["scanned_terms"])
        self.assertTrue(receipt["leakage_absent"])

    def test_evaluator_without_terms_fails_closed(self):
        with self.assertRaises(HoldoutFreezeError):
            hidden_terms({"required_mechanism_dimensions": ["a"]})

    def test_frozen_scratchpad_holdout_fixture_is_not_actually_blinded(self):
        """The frozen holdout fails its own blinding contract, by its own bytes.

        The Cycle-B transcript names the pattern outright ("the scratchpad ACT
        memory pattern") and every artifact repeats "active memory", both of
        which the frozen hidden evaluator lists as forbidden. Scanning only the
        seed packet hid this; scanning every worker-visible byte surfaces it.
        This test locks the finding so the packet cannot be quietly reused as a
        blinded holdout without new source material.
        """
        packet = json.loads((EVALS / "scratchpad-worker-packet.json").read_text(encoding="utf-8"))
        evaluator = json.loads((EVALS / "scratchpad-hidden-evaluator.json").read_text(encoding="utf-8"))
        receipt = freeze_worker_packet(
            packet, hidden_evaluator=evaluator, packet_label="scratchpad-holdout"
        )
        self.assertEqual(receipt["artifact_count"], 3)
        self.assertFalse(receipt["leakage_absent"])
        self.assertEqual(sorted(receipt["leaked_terms"]), ["active memory", "scratchpad"])
        by_term = {item["term"]: item["locations"] for item in receipt["leakage_findings"]}
        self.assertEqual(by_term["scratchpad"], ["cycle_b_artifacts[0]"])
        self.assertEqual(
            by_term["active memory"],
            ["cycle_a_artifacts[0]", "cycle_a_artifacts[1]", "cycle_b_artifacts[0]"],
        )

    def test_seed_packet_alone_would_have_looked_blinded(self):
        """Scope matters: the seed packet is clean while the artifacts are not."""
        packet = json.loads((EVALS / "scratchpad-worker-packet.json").read_text(encoding="utf-8"))
        evaluator = json.loads((EVALS / "scratchpad-hidden-evaluator.json").read_text(encoding="utf-8"))
        seed_only = freeze_worker_packet(
            {"seed_packet": packet["seed_packet"]},
            hidden_evaluator=evaluator,
            packet_label="scratchpad-seed-only",
        )
        self.assertTrue(seed_only["leakage_absent"])


if __name__ == "__main__":
    unittest.main()
