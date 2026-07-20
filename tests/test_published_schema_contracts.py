from __future__ import annotations

import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft7Validator, Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_doctrine import PAYLOAD_FIELDS


CITATION = {
    "source_id": "corpora",
    "page_slug": "agentic-engineering/sources/example",
    "locator": "section-1",
    "claim_sha256": "a" * 64,
    "evidence_class": "scientific",
}


def _payloads():
    rationale = "Evidence-bound decision."
    citations = [CITATION]
    return {
        "propose": {"concept_key": "proof-loop", "title": "Proof Loop", "statement": "Verify independently.", "citations": citations, "rationale": rationale},
        "add_alias": {"concept_key": "proof-loop", "alias": "verification-loop", "rationale": rationale},
        "merge": {"source_keys": ["proof-loop", "check-loop"], "target_key": "verification-loop", "title": "Verification Loop", "statement": "Verify independently.", "citations": citations, "rationale": rationale},
        "split": {"source_key": "verification-loop", "children": [{"concept_key": "artifact-proof", "title": "Artifact Proof", "statement": "Verify bytes.", "aliases": []}, {"concept_key": "behavior-proof", "title": "Behavior Proof", "statement": "Verify behavior.", "aliases": []}], "primary_key": "artifact-proof", "citations": citations, "rationale": rationale},
        "revise": {"concept_key": "proof-loop", "statement": "Revise with evidence.", "citations": citations, "rationale": rationale, "contradictory": False},
        "bound": {"concept_key": "proof-loop", "statement": "Bound the claim.", "citations": citations, "rationale": rationale},
        "supersede": {"source_key": "proof-loop", "target_key": "proof-system", "title": "Proof System", "statement": "Superseded claim.", "citations": citations, "rationale": rationale},
        "deprecate": {"concept_key": "proof-loop", "citations": citations, "rationale": rationale},
        "reject": {"proposal_key": "self-certification", "citations": citations, "rationale": rationale},
        "restore": {"concept_key": "proof-loop", "version": 1, "citations": citations, "rationale": rationale},
        "adopt": {"concept_key": "proof-loop", "holder_id": "sameer", "adoption_receipt": {"receipt_id": "receipt-1", "authorized_by": "sameer", "granted_at": "2026-07-20T00:00:00Z"}, "citations": citations, "rationale": rationale},
    }


def _event(event_type: str, payload: dict):
    return {
        "schema_version": 1,
        "sequence": 1,
        "event_id": "doctrine_evt_" + "b" * 24,
        "command_id": "command-1",
        "command_digest": "c" * 64,
        "event_type": event_type,
        "recorded_at": "2026-07-20T00:00:00Z",
        "previous_digest": "0" * 64,
        "payload": payload,
        "event_digest": "d" * 64,
    }


class PublishedSchemaRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doctrine_schema = json.loads((ROOT / "schemas" / "doctrine-event-v1.json").read_text())
        cls.doctrine = Draft202012Validator(cls.doctrine_schema)
        cls.feedback_schema = json.loads((ROOT / "schemas" / "feedback-profile-v1.json").read_text())
        cls.feedback = Draft7Validator(cls.feedback_schema)

    def test_doctrine_schema_matches_every_runtime_event_payload(self):
        payloads = _payloads()
        self.assertEqual(set(payloads), set(PAYLOAD_FIELDS))
        for event_type, payload in payloads.items():
            with self.subTest(event_type=event_type):
                self.assertEqual(set(payload), PAYLOAD_FIELDS[event_type])
                self.assertEqual(list(self.doctrine.iter_errors(_event(event_type, payload))), [])

    def test_doctrine_schema_rejects_bad_or_missing_citations_and_extra_fields(self):
        for event_type, payload in _payloads().items():
            if event_type == "add_alias":
                continue
            with self.subTest(event_type=event_type):
                bad = deepcopy(payload)
                bad["citations"] = [{"garbage": True}]
                self.assertTrue(list(self.doctrine.iter_errors(_event(event_type, bad))))
                missing = deepcopy(payload)
                del missing["citations"]
                self.assertTrue(list(self.doctrine.iter_errors(_event(event_type, missing))))
        extra = _payloads()["propose"] | {"unvalidated_authority": "sameer_doctrine"}
        self.assertTrue(list(self.doctrine.iter_errors(_event("propose", extra))))

    def test_feedback_schema_rejects_shared_export_without_private_guard(self):
        profile = json.loads((ROOT / "config" / "profiles" / "training-default.json").read_text())
        profile["privacy"]["forbid_private_outcome_export"] = False
        self.assertTrue(list(self.feedback.iter_errors(profile)))
        profile["privacy"]["forbid_private_outcome_export"] = True
        self.assertEqual(list(self.feedback.iter_errors(profile)), [])


if __name__ == "__main__":
    unittest.main()
