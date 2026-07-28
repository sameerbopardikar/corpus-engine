from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import corpus_self_expansion_cycle as cycle_module
from corpus_discovery import DiscoveryEngine
from corpus_self_expansion_cycle import run_self_expansion_cycle

DOMAIN = "agentic-engineering"
WATCH_URL = "https://example.org/people/maya-chen"
SECOND_ORDER = "https://github.com/example/recovery-controller"
NOW_A = datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc)
NOW_B = datetime(2026, 7, 28, 10, 5, tzinfo=timezone.utc)


def _semantic_artifact(url, family, target, *, rights="private_authorized", other_url=None):
    relation = f"profiles {target} as a production operator"
    if other_url:
        relation = f"mentions {other_url} but profiles {target} as a production operator"
    text = f"Source note: this artifact {relation}."
    quote = text
    return {
        "artifact_url": url,
        "content": text,
        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "fetched_at": "2026-07-28T09:00:00Z",
        "source_revision": "fixture-v1",
        "source_family": family,
        "rights_state": rights,
        "observed_at": "2026-07-28T09:00:00Z",
        "evidence_lane": "scientific-evaluation",
        "topics": ["reliability", "verification"],
        "extraction": {
            "kind": "semantic",
            "claims": [{
                "relationship_type": "mentioned",
                "entity_type": "creator",
                "canonical_url": target,
                "title": target,
                "entity_mention": target,
                "relation_mention": "profiles",
                "evidence_quote": quote,
                "evidence_span": [0, len(quote)],
            }],
        },
    }


def _inspection():
    text = f"The project lives at {SECOND_ORDER} and has deterministic readback tests."
    return {
        "watch_source_url": WATCH_URL,
        "artifacts": [{
            "artifact_url": WATCH_URL,
            "content": text,
            "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "fetched_at": "2026-07-28T10:05:00Z",
            "source_revision": "profile-v2",
            "source_family": "engineering-notes",
            "observed_at": "2026-07-28T10:05:00Z",
            "evidence_lane": "scientific-evaluation",
            "topics": ["reliability", "verification"],
            "extraction": {
                "kind": "semantic",
                "claims": [{
                    "relationship_type": "linked_primary_source",
                    "entity_type": "repository",
                    "canonical_url": SECOND_ORDER,
                    "title": SECOND_ORDER,
                    "entity_mention": SECOND_ORDER,
                    "relation_mention": "lives at",
                    "evidence_quote": text,
                    "evidence_span": [0, len(text)],
                }],
            },
        }],
    }


def _config(state_root: Path):
    return {
        "schema_version": 1,
        "enabled": True,
        "state_root": str(state_root),
        "max_inspections_per_cycle": 2,
        "max_relationships_per_artifact": 8,
        "max_candidates_per_cycle": 20,
        "max_artifact_bytes": 100_000,
        "max_wall_seconds": 20,
        "promotion_mode": "live",
        "domains": [{
            "domain": DOMAIN,
            "source_artifacts": [
                _semantic_artifact("https://podcast.example.com/episode/1", "podcast", WATCH_URL),
                _semantic_artifact("https://events.example.net/talk/2", "conference", WATCH_URL),
            ],
            "rights_assertions": [{
                "canonical_url": WATCH_URL,
                "rights_state": "private_authorized",
            }],
            "inspections": [_inspection()],
        }],
    }


class GlobalCycleSelfExpansionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_disabled_is_backward_compatible_and_has_no_state_writes(self):
        receipt = run_self_expansion_cycle(
            {"schema_version": 1, "enabled": False}, now=NOW_A
        )
        self.assertEqual(receipt["status"], "disabled")
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_optional_config_failure_is_fail_soft_and_test_clock_is_phase_local(self):
        config_path = self.tmp / "malformed-self-expansion.json"
        config_path.write_text("{not-json", encoding="utf-8")
        budget_path = self.tmp / "budget.json"
        command = [
            sys.executable,
            str(ROOT / "scripts" / "corpus_global_cycle.py"),
            "--config-dir",
            str(ROOT / "config" / "domains"),
            "--budget-path",
            str(budget_path),
            "--self-expansion-config",
            str(config_path),
            "--self-expansion-now",
            "2000-01-02T03:04:05Z",
        ]
        process = subprocess.run(command, capture_output=True, text=True, timeout=60)
        self.assertEqual(process.returncode, 0, process.stderr)
        summary = json.loads(process.stdout)
        self.assertEqual(summary["self_expansion"]["status"], "partial_failure")
        self.assertFalse(summary["self_expansion"]["enabled"])
        self.assertEqual(
            summary["self_expansion"]["failures"][0]["code"],
            "self_expansion_config_unavailable",
        )
        self.assertNotIn("20000102", summary["reservation_id"])
        budget = json.loads(budget_path.read_text(encoding="utf-8"))
        reservation = next(iter(budget["reservations"].values()))
        self.assertFalse(reservation["request"]["now"].startswith("2000-01-02"))

    def test_state_path_uses_canonical_domain_subroot(self):
        state = self.tmp / "corpora"
        config = _config(state)
        config["domains"][0]["state_path"] = f"{DOMAIN}/self-expansion-v1"
        receipt = run_self_expansion_cycle(config, now=NOW_A)
        self.assertEqual(receipt["status"], "completed")
        self.assertTrue(
            (state / DOMAIN / "self-expansion-v1" / "discovery-ledger.jsonl").is_file()
        )
        self.assertFalse((state / DOMAIN / "discovery-ledger.jsonl").exists())

    def test_state_path_cannot_escape_its_domain(self):
        state = self.tmp / "corpora"
        sentinel = state / "training" / "sentinel.json"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text('{"authority":"unchanged"}\n')
        before = sentinel.read_bytes()
        config = _config(state)
        config["domains"][0]["state_path"] = f"{DOMAIN}/../../training"
        receipt = run_self_expansion_cycle(config, now=NOW_A)
        self.assertEqual(receipt["status"], "partial_failure")
        self.assertTrue(any("state_path" in item["error"] for item in receipt["failures"]))
        self.assertEqual(sentinel.read_bytes(), before)
        self.assertEqual(list((state / "training").iterdir()), [sentinel])

    def test_domain_root_symlink_cannot_redirect_into_sibling_domain(self):
        state = self.tmp / "corpora"
        training = state / "training"
        training.mkdir(parents=True)
        sentinel = training / "sentinel.json"
        sentinel.write_text('{"authority":"unchanged"}\n')
        (state / DOMAIN).symlink_to(training, target_is_directory=True)
        config = _config(state)
        config["domains"][0]["state_path"] = f"{DOMAIN}/self-expansion-v1"
        receipt = run_self_expansion_cycle(config, now=NOW_A)
        self.assertEqual(receipt["status"], "partial_failure")
        self.assertTrue(
            any(
                "no-follow" in item["error"] or "state authority" in item["error"]
                for item in receipt["failures"]
            )
        )
        self.assertEqual(list(training.iterdir()), [sentinel])

    def test_dangling_nested_state_symlink_fails_closed(self):
        state = self.tmp / "corpora"
        domain_root = state / DOMAIN
        domain_root.mkdir(parents=True)
        nested = domain_root / "self-expansion-v1"
        nested.symlink_to(state / "missing-target", target_is_directory=True)
        config = _config(state)
        config["domains"][0]["state_path"] = f"{DOMAIN}/self-expansion-v1"
        receipt = run_self_expansion_cycle(config, now=NOW_A)
        self.assertEqual(receipt["status"], "partial_failure")
        self.assertTrue(
            any(
                "no-follow" in item["error"] or "state authority" in item["error"]
                for item in receipt["failures"]
            )
        )
        self.assertFalse((state / "missing-target").exists())

    def test_artifact_and_receipt_leaf_symlinks_fail_without_external_writes(self):
        for leaf in ("artifacts", "receipts"):
            with self.subTest(leaf=leaf):
                state = self.tmp / f"corpora-{leaf}"
                root = state / DOMAIN / "self-expansion-v1"
                root.mkdir(parents=True)
                outside = self.tmp / f"outside-{leaf}"
                outside.mkdir()
                sentinel = outside / "sentinel"
                sentinel.write_text("unchanged\n", encoding="utf-8")
                (root / leaf).symlink_to(outside, target_is_directory=True)
                config = _config(state)
                config["domains"][0]["state_path"] = f"{DOMAIN}/self-expansion-v1"
                receipt = run_self_expansion_cycle(config, now=NOW_A)
                self.assertEqual(receipt["status"], "partial_failure")
                self.assertEqual(list(outside.iterdir()), [sentinel])
                self.assertTrue(
                    any("no-follow" in item["error"] for item in receipt["failures"]),
                    receipt["failures"],
                )

    def test_authority_replacement_after_open_fails_before_ledger_write(self):
        state = self.tmp / "race-state"
        config = _config(state)
        config["domains"][0]["state_path"] = f"{DOMAIN}/self-expansion-v1"
        original = cycle_module._run_domain_bound
        moved = state / "training"

        def replace_then_run(domain_config, **kwargs):
            root = state / DOMAIN / "self-expansion-v1"
            root.rename(moved)
            root.symlink_to(moved, target_is_directory=True)
            return original(domain_config, **kwargs)

        with patch(
            "corpus_self_expansion_cycle._run_domain_bound",
            side_effect=replace_then_run,
        ):
            receipt = run_self_expansion_cycle(config, now=NOW_A)
        self.assertEqual(receipt["status"], "partial_failure")
        self.assertFalse((moved / "discovery-ledger.jsonl").exists())
        self.assertFalse((moved / "source-graph.jsonl").exists())
        self.assertTrue(
            any("state authority changed" in item["error"] for item in receipt["failures"]),
            receipt["failures"],
        )

    def test_injected_monotonic_clock_enforces_wall_cap(self):
        config = _config(self.tmp / "state")
        ticks = iter([0.0, 21.0])
        receipt = run_self_expansion_cycle(
            config, now=NOW_A, monotonic=lambda: next(ticks, 21.0)
        )
        self.assertEqual(receipt["status"], "partial_failure")
        self.assertEqual(receipt["domains"], [])
        self.assertTrue(
            any("max_wall_seconds" in item["error"] for item in receipt["failures"]),
            receipt["failures"],
        )

    def test_hard_wall_clock_preempts_stalled_artifact_operation(self):
        config = _config(self.tmp / "state")
        config["max_wall_seconds"] = 0.05

        def stalled(*_args, **_kwargs):
            time.sleep(0.5)
            raise AssertionError("hard wall clock failed to preempt operation")

        started = time.monotonic()
        with patch(
            "corpus_self_expansion_cycle.project_preserved_source_artifact",
            side_effect=stalled,
        ):
            receipt = run_self_expansion_cycle(config, now=NOW_A)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.25)
        self.assertEqual(receipt["status"], "partial_failure")
        self.assertTrue(
            any("max_wall_seconds" in item["error"] for item in receipt["failures"]),
            receipt["failures"],
        )

    def test_two_cycles_persist_then_consume_exact_work_and_derive_second_order(self):
        config = _config(self.tmp / "state")
        cycle_a = run_self_expansion_cycle(config, now=NOW_A)
        domain_a = cycle_a["domains"][0]
        self.assertEqual(domain_a["inspections_consumed"], 0)
        work_ids = domain_a["policy"]["watch_work_ids"]
        self.assertEqual(len(work_ids), 1)

        cycle_b = run_self_expansion_cycle(config, now=NOW_B)
        domain_b = cycle_b["domains"][0]
        self.assertEqual(domain_b["consumed_work_ids"], work_ids)
        self.assertEqual(domain_b["supplied_inspection_relationships"], 0)
        engine = DiscoveryEngine(self.tmp / "state" / DOMAIN / "discovery-ledger.jsonl")
        second = next(item for item in engine.candidates.values() if item.canonical_url == SECOND_ORDER)
        self.assertEqual(second.rights_state, "rights_unclear")
        self.assertEqual(engine.work_items[work_ids[0]].state, "done")

        replay = run_self_expansion_cycle(config, now=NOW_B)
        self.assertEqual(replay["domains"][0]["inspections_consumed"], 0)
        self.assertEqual(len(DiscoveryEngine(self.tmp / "state" / DOMAIN / "discovery-ledger.jsonl").work_items), 1)

    def test_expired_lease_is_recovered_and_consumed(self):
        config = _config(self.tmp / "state")
        first = run_self_expansion_cycle(config, now=NOW_A)
        work_id = first["domains"][0]["policy"]["watch_work_ids"][0]
        ledger = self.tmp / "state" / DOMAIN / "discovery-ledger.jsonl"
        DiscoveryEngine(ledger).lease_work(
            work_id, owner="crashed-worker", ttl_seconds=1, now=NOW_A
        )
        second = run_self_expansion_cycle(config, now=NOW_A + timedelta(seconds=2))
        self.assertEqual(second["domains"][0]["expired_leases_recovered"], 1)
        self.assertEqual(second["domains"][0]["consumed_work_ids"], [work_id])

    def test_rights_rejection_and_artifact_mismatch_are_partial_failures(self):
        config = _config(self.tmp / "state")
        config["domains"][0]["source_artifacts"][0]["rights_state"] = "rights_unclear"
        config["domains"][0]["source_artifacts"][1]["content_sha256"] = "0" * 64
        receipt = run_self_expansion_cycle(config, now=NOW_A)
        failures = " ".join(item["error"] for item in receipt["domains"][0]["failures"])
        self.assertIn("explicit public_rights_clear or private_authorized", failures)
        self.assertIn("digest mismatch", failures)
        self.assertEqual(receipt["status"], "partial_failure")

    def test_inspection_cap_counts_failed_attempts(self):
        config = _config(self.tmp / "state")
        first = run_self_expansion_cycle(config, now=NOW_A)
        self.assertEqual(len(first["domains"][0]["policy"]["watch_work_ids"]), 1)
        bad = _inspection()
        bad["artifacts"][0]["content_sha256"] = "0" * 64
        config["max_inspections_per_cycle"] = 1
        config["domains"][0]["inspections"] = [bad, json.loads(json.dumps(bad))]
        second = run_self_expansion_cycle(config, now=NOW_B)
        domain = second["domains"][0]
        self.assertEqual(domain["inspections_attempted"], 1)
        self.assertEqual(domain["inspections_consumed"], 0)
        inspection_failures = [
            item for item in domain["failures"] if item["stage"].startswith("inspection[")
        ]
        self.assertEqual(len(inspection_failures), 1)

    def test_multiple_url_substitution_is_rejected(self):
        config = _config(self.tmp / "state")
        artifact = _semantic_artifact(
            "https://podcast.example.com/episode/1", "podcast", WATCH_URL,
            other_url="https://attacker.example/bait",
        )
        artifact["extraction"]["claims"][0]["canonical_url"] = "https://attacker.example/bait"
        # Keep the declared entity mention bound to the legitimate URL. Merely
        # co-locating the attacker URL in the exact span cannot ground it.
        config["domains"][0]["source_artifacts"] = [artifact]
        receipt = run_self_expansion_cycle(config, now=NOW_A)
        failures = receipt["domains"][0]["failures"]
        self.assertTrue(any("does not bind" in item["error"] for item in failures), failures)

    def test_caps_fail_closed_and_unrelated_domain_is_not_mutated(self):
        state = self.tmp / "state"
        unrelated = state / "training" / "sentinel.json"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_bytes(b'{"authority":"unchanged"}\n')
        before = unrelated.read_bytes()
        config = _config(state)
        config["max_artifact_bytes"] = 8
        receipt = run_self_expansion_cycle(config, now=NOW_A)
        agentic = receipt["domains"][0]
        self.assertTrue(any("preserved-byte limit" in item["error"] for item in agentic["failures"]))
        self.assertEqual(unrelated.read_bytes(), before)
        self.assertEqual(list((state / "training").iterdir()), [unrelated])

    def test_candidate_cap_limits_new_additions_without_stalling_mature_corpus(self):
        config = _config(self.tmp / "state")
        config["max_candidates_per_cycle"] = 1
        blocked = "https://example.org/people/blocked-this-cycle"
        config["domains"][0]["source_artifacts"].append(
            _semantic_artifact(
                "https://third.example.net/item/3",
                "independent-third-source",
                blocked,
            )
        )
        first = run_self_expansion_cycle(config, now=NOW_A)
        domain_first = first["domains"][0]
        self.assertEqual(len(domain_first["policy"]["watch_work_ids"]), 1)
        self.assertTrue(
            any("max_candidates_per_cycle" in item["error"] for item in domain_first["failures"]),
            domain_first["failures"],
        )
        config["domains"][0]["source_artifacts"] = config["domains"][0]["source_artifacts"][:2]
        second = run_self_expansion_cycle(config, now=NOW_B)
        self.assertEqual(second["domains"][0]["failures"], [])
        engine = DiscoveryEngine(self.tmp / "state" / DOMAIN / "discovery-ledger.jsonl")
        urls = {item.canonical_url for item in engine.candidates.values()}
        self.assertNotIn(blocked, urls)
        self.assertIn(SECOND_ORDER, urls)
        self.assertEqual(len(engine.candidates), 2)

    def test_exact_scheduler_wrapper_two_cycle_simulation_is_semantically_repeatable(self):
        def run_pair(root: Path):
            config = _config(root / "state")
            config_path = root / "self-expansion.json"
            root.mkdir(parents=True)
            config_path.write_text(json.dumps(config), encoding="utf-8")
            env = dict(os.environ)
            env.update({
                "CORPUS_CONFIG_DIR": str(ROOT / "config" / "domains"),
                "CORPUS_BUDGET_PATH": str(root / "budget.json"),
                "CORPUS_CORPORA_ROOT": str(root / "corpora"),
                "CORPUS_ACQUISITION_EXECUTE": "0",
                "CORPUS_LEGACY_SHIMS": "0",
                "CORPUS_SELF_EXPANSION_CONFIG": str(config_path),
                "CORPUS_SELF_EXPANSION_TEST_NOW": "2026-07-28T10:00:00Z",
            })
            command = ["bash", str(ROOT / "scripts" / "corpus_engine_cycle_v1.sh")]
            a = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(a.returncode, 0, a.stderr)
            env["CORPUS_SELF_EXPANSION_TEST_NOW"] = "2026-07-28T10:05:00Z"
            b = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(b.returncode, 0, b.stderr)
            return json.loads(a.stdout), json.loads(b.stdout)

        a1, b1 = run_pair(self.tmp / "run-one")
        a2, b2 = run_pair(self.tmp / "run-two")
        work1 = a1["self_expansion"]["domains"][0]["policy"]["watch_work_ids"]
        work2 = a2["self_expansion"]["domains"][0]["policy"]["watch_work_ids"]
        self.assertEqual(work1, work2)
        self.assertEqual(b1["self_expansion"]["domains"][0]["consumed_work_ids"], work1)
        self.assertEqual(b2["self_expansion"]["domains"][0]["consumed_work_ids"], work2)
        for result in (b1, b2):
            domain = result["self_expansion"]["domains"][0]
            self.assertEqual(domain["supplied_inspection_relationships"], 0)
            self.assertEqual(domain["candidate_count_after"], 2)
        semantic = lambda value: {
            "status": value["self_expansion"]["status"],
            "promotion_mode": value["self_expansion"]["promotion_mode"],
            "work_ids": value["self_expansion"]["domains"][0]["consumed_work_ids"],
            "candidate_count": value["self_expansion"]["domains"][0]["candidate_count_after"],
            "relationships": value["self_expansion"]["domains"][0]["relationships_appended"],
        }
        self.assertEqual(semantic(b1), semantic(b2))


if __name__ == "__main__":
    unittest.main()
