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

from corpus_doctrine import DoctrineCorruptionError, DoctrineEngine


CITATION_A = {
    "source_id": "corpora",
    "page_slug": "agentic-engineering/sources/paper-a",
    "locator": "section-3",
    "claim_sha256": "a" * 64,
    "evidence_class": "scientific",
}
CITATION_B = {
    "source_id": "corpora",
    "page_slug": "agentic-engineering/sources/postmortem-b",
    "locator": "incident-timeline",
    "claim_sha256": "b" * 64,
    "evidence_class": "production-incident",
}


class DoctrineEngineTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "doctrine.jsonl"
        self.engine = DoctrineEngine(self.path)

    def propose(self, key="verification-loops", *, command_id="cmd-propose"):
        return self.engine.propose(
            command_id=command_id,
            concept_key=key,
            title=key.replace("-", " ").title(),
            statement="Verification must be independent of the executor.",
            citations=[CITATION_A],
            rationale="Emergent mechanism in scientific evidence.",
        )

    def test_proposal_is_probationary_external_evidence_not_sameer_doctrine(self):
        concept = self.propose()
        self.assertEqual(concept["status"], "probationary")
        self.assertEqual(concept["version"], 1)
        self.assertEqual(concept["epistemic_layer"], "external_corpus_synthesis")
        self.assertFalse(concept["sameer_adopted"])
        self.assertEqual(concept["citations"], [CITATION_A])

    def test_alias_safe_merge_keeps_all_prior_keys_resolvable_and_citations(self):
        self.propose("verification-loops", command_id="cmd-a")
        self.propose("independent-checks", command_id="cmd-b")
        merged = self.engine.merge(
            command_id="cmd-merge",
            source_keys=["verification-loops", "independent-checks"],
            target_key="independent-verification",
            title="Independent Verification",
            statement="Executors require independent verification loops.",
            citations=[CITATION_B],
            rationale="Two concepts describe one mechanism.",
        )
        self.assertEqual(self.engine.resolve("verification-loops")["concept_key"], "independent-verification")
        self.assertEqual(self.engine.resolve("independent-checks")["concept_key"], "independent-verification")
        self.assertEqual(self.engine.resolve("independent-verification")["concept_key"], "independent-verification")
        self.assertEqual(merged["status"], "probationary")
        self.assertEqual(merged["citations"], [CITATION_A, CITATION_B])

    def test_split_requires_alias_routes_and_preserves_parent_resolution(self):
        self.propose("verification-loops")
        children = self.engine.split(
            command_id="cmd-split",
            source_key="verification-loops",
            children=[
                {
                    "concept_key": "artifact-verification",
                    "title": "Artifact Verification",
                    "statement": "Verify artifact bytes.",
                    "aliases": ["byte-checks"],
                },
                {
                    "concept_key": "behavior-verification",
                    "title": "Behavior Verification",
                    "statement": "Verify observed behavior.",
                    "aliases": ["runtime-checks"],
                },
            ],
            primary_key="artifact-verification",
            citations=[CITATION_B],
            rationale="Evidence distinguishes byte and runtime proof.",
        )
        self.assertEqual([row["concept_key"] for row in children], ["artifact-verification", "behavior-verification"])
        self.assertEqual(self.engine.resolve("verification-loops")["concept_key"], "artifact-verification")
        self.assertEqual(self.engine.resolve("byte-checks")["concept_key"], "artifact-verification")
        self.assertEqual(self.engine.resolve("runtime-checks")["concept_key"], "behavior-verification")
        self.assertEqual(self.engine.resolve("behavior-verification")["citations"], [CITATION_A, CITATION_B])

    def test_supersession_preserves_prior_version_and_citation_union(self):
        original = self.propose()
        revised = self.engine.supersede(
            command_id="cmd-supersede",
            source_key="verification-loops",
            target_key="evidence-bound-verification",
            title="Evidence-Bound Verification",
            statement="Verification must bind exact evidence bytes and executor identity.",
            citations=[CITATION_B],
            rationale="Production evidence adds artifact binding.",
        )
        self.assertEqual(revised["citations"], [CITATION_A, CITATION_B])
        history = self.engine.lineage("verification-loops")
        self.assertEqual(history["versions"][0], original)
        self.assertIn("evidence-bound-verification", history["descendants"])
        self.assertEqual(self.engine.resolve("verification-loops")["concept_key"], "evidence-bound-verification")

    def test_contradictory_evidence_must_bound_or_revise_not_overwrite(self):
        original = self.propose()
        with self.assertRaisesRegex(ValueError, "contradictory evidence requires bound or revise"):
            self.engine.revise(
                command_id="cmd-bad-revise",
                concept_key="verification-loops",
                statement="Executor self-verification is sufficient.",
                citations=[CITATION_B],
                rationale="Contradiction.",
                contradictory=True,
            )
        bounded = self.engine.bound(
            command_id="cmd-bound",
            concept_key="verification-loops",
            statement="Independent verification is required for high-impact outputs; low-impact deterministic transforms may self-check.",
            citations=[CITATION_B],
            rationale="Production evidence bounds the mechanism.",
        )
        self.assertEqual(bounded["status"], "bounded")
        self.assertEqual(bounded["version"], 2)
        self.assertEqual(bounded["citations"], [CITATION_A, CITATION_B])
        self.assertEqual(self.engine.lineage("verification-loops")["versions"][0], original)

    def test_rejected_proposal_records_event_but_is_projection_noop(self):
        self.propose()
        before = copy.deepcopy(self.engine.snapshot())
        event_count = len(self.engine.events)
        result = self.engine.reject(
            command_id="cmd-reject",
            proposal_key="agent-self-certification",
            citations=[CITATION_B],
            rationale="Conflicts with independent-verifier boundary.",
        )
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(self.engine.snapshot(), before)
        self.assertEqual(len(self.engine.events), event_count + 1)
        self.assertIsNone(self.engine.resolve("agent-self-certification"))

    def test_restore_replays_prior_version_without_losing_later_history(self):
        first = self.propose()
        self.engine.bound(
            command_id="cmd-bound",
            concept_key="verification-loops",
            statement="Bounded statement.",
            citations=[CITATION_B],
            rationale="New boundary.",
        )
        restored = self.engine.restore(
            command_id="cmd-restore",
            concept_key="verification-loops",
            version=1,
            rationale="Boundary evidence was retracted.",
            citations=[CITATION_A],
        )
        self.assertEqual(restored["statement"], first["statement"])
        self.assertEqual(restored["version"], 3)
        self.assertEqual(len(self.engine.lineage("verification-loops")["versions"]), 3)

    def test_restore_reconciles_alias_routing_to_restored_version(self):
        self.propose()
        self.engine.add_alias(command_id="cmd-alias", concept_key="verification-loops", alias="proof-loops")
        self.assertIsNotNone(self.engine.resolve("proof-loops"))
        self.engine.restore(
            command_id="cmd-restore",
            concept_key="verification-loops",
            version=1,
            rationale="Restore the pre-alias version.",
            citations=[CITATION_A],
        )
        self.assertIsNone(self.engine.resolve("proof-loops"))
        self.assertNotIn("proof-loops", self.engine.snapshot()["aliases"])

    def test_exact_command_retry_is_physical_noop_and_conflict_fails_closed(self):
        first = self.propose()
        before = self.path.read_bytes()
        retried = self.propose()
        self.assertEqual(retried, first)
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "command_id conflict"):
            self.engine.propose(
                command_id="cmd-propose",
                concept_key="different",
                title="Different",
                statement="Different statement.",
                citations=[CITATION_A],
                rationale="Different payload.",
            )
        self.assertEqual(self.path.read_bytes(), before)

    def test_replay_is_deterministic_and_rejects_digest_tampering(self):
        self.propose()
        self.engine.add_alias(command_id="cmd-alias", concept_key="verification-loops", alias="proof-loops")
        expected = self.engine.snapshot()
        replayed = DoctrineEngine(self.path)
        self.assertEqual(replayed.snapshot(), expected)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        forged = json.loads(lines[0])
        forged["payload"]["statement"] = "Forged doctrine."
        lines[0] = json.dumps(forged, sort_keys=True, separators=(",", ":"))
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(DoctrineCorruptionError, "event digest"):
            DoctrineEngine(self.path)

    def test_invalid_citation_and_alias_collision_fail_before_append(self):
        with self.assertRaisesRegex(ValueError, "claim_sha256"):
            self.engine.propose(
                command_id="cmd-invalid",
                concept_key="bad",
                title="Bad",
                statement="Bad.",
                citations=[{**CITATION_A, "claim_sha256": "nope"}],
                rationale="Bad citation.",
            )
        self.assertEqual(self.path.read_bytes(), b"")
        self.propose("verification-loops", command_id="cmd-a")
        self.propose("leases", command_id="cmd-b")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "already resolves"):
            self.engine.add_alias(command_id="cmd-collision", concept_key="leases", alias="verification-loops")
        self.assertEqual(self.path.read_bytes(), before)

    def test_unterminated_tail_blocks_append_without_mutating_bytes(self):
        self.propose()
        self.path.write_bytes(self.path.read_bytes().rstrip(b"\n"))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(DoctrineCorruptionError, "unterminated tail"):
            self.engine.propose(
                command_id="cmd-second",
                concept_key="leases",
                title="Leases",
                statement="Leases fence ownership.",
                citations=[CITATION_A],
                rationale="Second concept.",
            )
        self.assertEqual(self.path.read_bytes(), before)

    def test_self_consistent_unknown_payload_field_is_rejected_on_replay(self):
        self.propose()
        event = json.loads(self.path.read_text(encoding="utf-8"))
        event["payload"]["unvalidated_authority"] = "sameer_doctrine"
        body = {"event_type": event["event_type"], "payload": event["payload"]}
        import hashlib
        encode = lambda value: json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        event["command_digest"] = hashlib.sha256(encode(body)).hexdigest()
        unsigned = {key: value for key, value in event.items() if key != "event_digest"}
        event["event_digest"] = hashlib.sha256(encode(unsigned)).hexdigest()
        self.path.write_bytes(encode(event) + b"\n")
        with self.assertRaisesRegex(DoctrineCorruptionError, "payload fields"):
            DoctrineEngine(self.path)


if __name__ == "__main__":
    unittest.main()
