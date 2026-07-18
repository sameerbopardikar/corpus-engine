from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_doctrine import DoctrineEngine
from corpus_eval_gate import EvalGateError, GatePaths, run_eval_gated_cycle

CITATION = {
    "source_id": "corpora",
    "page_slug": "training/sources/scientific/paper-a",
    "locator": "section-1",
    "claim_sha256": "a" * 64,
    "evidence_class": "scientific",
}


class GateHarness:
    """A minimal domain-agnostic harness of injected callables for the gate."""

    def __init__(self, base: Path, *, domain: str = "training", concept_key: str = "force-velocity",
                 retrieval_ok: bool = True, behavior_blocks: bool = False):
        self.base = base
        self.domain = domain
        self.concept_key = concept_key
        self.retrieval_ok = retrieval_ok
        self.behavior_blocks = behavior_blocks
        self.corpus_root = base / "corpus"
        self.corpus_root.mkdir(parents=True, exist_ok=True)
        self.doctrine = DoctrineEngine(base / "doctrine.jsonl", domain=domain)
        self.behavior_report_digest = "b" * 64

    def paths(self):
        return GatePaths(
            state_root=self.base / "state",
            corpus_root=self.corpus_root,
            quarantine_root=self.base / "quarantine",
        )

    def acquire(self):
        page = self.corpus_root / "paper-a.md"
        page.write_text("staged evidence page\n", encoding="utf-8")
        sha = hashlib.sha256(page.read_bytes()).hexdigest()
        return {
            "record": {"page_slug": CITATION["page_slug"], "source_id": "corpora"},
            "doctrine_proposal": {
                "concept_key": self.concept_key,
                "title": "Force Velocity",
                "statement": "Force-velocity profiling structures prescription.",
                "rationale": "Systematic review evidence.",
                "citation": CITATION,
            },
            "page_slug": CITATION["page_slug"],
            "staged_path": str(page),
            "staged_sha256": sha,
        }

    def sync_corpus(self, package):
        return {"source_id": "corpora", "changed_pages": [package["page_slug"]]}

    def retrieve(self, package):
        if not self.retrieval_ok:
            return [{"page_slug": "training/sources/other/unrelated"}]
        return [{"page_slug": package["page_slug"]}]

    def integrity_eval(self, package):
        return {"passed": True, "failures": []}

    def behavior_eval(self, package):
        return {
            "passed": not self.behavior_blocks,
            "blocks_doctrine": self.behavior_blocks,
            "regressed": self.behavior_blocks,
            "report_digest": self.behavior_report_digest,
        }

    def commit_doctrine(self, package, behavior_report_digest):
        proposal = package["doctrine_proposal"]
        concept = self.doctrine.propose(
            command_id=f"gate:{self.domain}:{proposal['concept_key']}",
            concept_key=proposal["concept_key"],
            title=proposal["title"],
            statement=proposal["statement"],
            citations=[proposal["citation"]],
            rationale=f"{proposal['rationale']} behavior_report={behavior_report_digest}",
        )
        event_digest = self.doctrine.events[-1]["event_digest"]
        return {"concept_key": concept["concept_key"], "event_digest": event_digest,
                "behavior_report_digest": behavior_report_digest}

    def run(self, **kw):
        return run_eval_gated_cycle(
            cycle_id="c1", domain=self.domain, paths=self.paths(),
            acquire=self.acquire, sync_corpus=self.sync_corpus, retrieve=self.retrieve,
            integrity_eval=self.integrity_eval, behavior_eval=self.behavior_eval,
            commit_doctrine=self.commit_doctrine, now="2026-07-18T00:00:00Z", **kw,
        )


class EvalGateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.base = Path(self.tempdir.name)

    def test_success_commits_doctrine_after_eval_and_binds_behavior(self):
        h = GateHarness(self.base)
        receipt = h.run()
        self.assertEqual(receipt["status"], "committed")
        self.assertEqual(receipt["doctrine"]["behavior_report_digest"], h.behavior_report_digest)
        self.assertTrue(receipt["behavior_bound"])
        self.assertEqual(len(h.doctrine.events), 1)

    def test_retrieval_failure_leaves_doctrine_bytes_unchanged(self):
        h = GateHarness(self.base, retrieval_ok=False)
        before = (self.base / "doctrine.jsonl").read_bytes()
        with self.assertRaises(EvalGateError):
            h.run()
        after = (self.base / "doctrine.jsonl").read_bytes()
        self.assertEqual(before, after)
        self.assertEqual(len(h.doctrine.events), 0)

    def test_behavior_failure_quarantines_staging_with_byte_evidence(self):
        h = GateHarness(self.base, behavior_blocks=True)
        staged = self.base / "corpus" / "paper-a.md"
        with self.assertRaises(EvalGateError):
            h.run()
        # Doctrine untouched.
        self.assertEqual(len(h.doctrine.events), 0)
        # Staged page moved out of the corpus into quarantine, sha recorded.
        state = _read_state(self.base)
        quarantine = state["phases"].get("quarantine")
        self.assertIsNotNone(quarantine)
        self.assertEqual(len(quarantine["items"]), 1)
        moved = Path(quarantine["items"][0]["quarantined_path"])
        self.assertTrue(moved.is_file())
        self.assertEqual(
            hashlib.sha256(moved.read_bytes()).hexdigest(),
            quarantine["items"][0]["sha256"],
        )

    def test_crash_after_eval_before_commit_resumes_idempotently(self):
        h = GateHarness(self.base)
        with self.assertRaises(RuntimeError):
            h.run(interrupt_after=_raise_on("evaluated"))
        # Doctrine not yet committed.
        self.assertEqual(len(h.doctrine.events), 0)
        receipt = h.run()  # resume
        self.assertEqual(receipt["status"], "committed")
        self.assertEqual(len(h.doctrine.events), 1)  # committed exactly once

    def test_cross_domain_concept_keys_do_not_collide(self):
        h1 = GateHarness(self.base / "a", domain="training", concept_key="shared-key")
        h2 = GateHarness(self.base / "b", domain="agentic-engineering", concept_key="shared-key")
        r1 = h1.run()
        r2 = h2.run()
        self.assertEqual(r1["doctrine"]["concept_key"], r2["doctrine"]["concept_key"])
        self.assertNotEqual(r1["doctrine"]["event_digest"], r2["doctrine"]["event_digest"])


def _read_state(base: Path):
    import json
    return json.loads((base / "state" / "gate" / "c1.json").read_text(encoding="utf-8"))


def _raise_on(phase_name):
    def _hook(phase):
        if phase == phase_name:
            raise RuntimeError(f"simulated crash after {phase}")
    return _hook


if __name__ == "__main__":
    unittest.main()
