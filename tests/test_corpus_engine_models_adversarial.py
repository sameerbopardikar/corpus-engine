from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "corpus_engine_models.py"
spec = importlib.util.spec_from_file_location("corpus_engine_models_adversarial", MODULE_PATH)
assert spec and spec.loader
models = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = models
spec.loader.exec_module(models)

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
SCORES = {
    "authority": 0.8,
    "demonstrated_practice": 0.7,
    "novelty": 0.6,
    "relevance": 0.9,
    "corroboration": 0.4,
    "production_or_scientific_value": 0.8,
    "cost": 0.2,
}


def observation(**overrides):
    values = {
        "domain": "agentic-engineering",
        "entity_type": "repository",
        "canonical_url": "https://EXAMPLE.com/a/../Repo/#fragment",
        "discovery_source": "github:example/repo",
        "evidence_pointer": "https://example.com/evidence/1",
        "evidence_lane": "implementation",
        "topics": ("durability",),
        "observed_at": "2026-07-16T12:00:00Z",
    }
    values.update(overrides)
    return models.CandidateObservation.create(**values)


def pending_work(**overrides):
    values = {
        "domain": "agentic-engineering",
        "candidate_id": "cand_abc",
        "action": "verify",
        "score_components": {"priority": 1.0},
        "budget_estimate": 1.0,
        "now": NOW,
    }
    values.update(overrides)
    return models.WorkItem.create(**values)


class IdentityAndSerializationTests(unittest.TestCase):
    def test_stable_id_uses_collision_safe_framing(self):
        self.assertNotEqual(
            models.stable_id("work", "a", "b|c", "verify"),
            models.stable_id("work", "a|b", "c", "verify"),
        )

    def test_work_idempotency_keys_define_distinct_runs(self):
        first = pending_work(idempotency_key="cycle-1")
        duplicate = pending_work(idempotency_key="cycle-1")
        next_cycle = pending_work(idempotency_key="cycle-2")
        self.assertEqual(first.work_id, duplicate.work_id)
        self.assertNotEqual(first.work_id, next_cycle.work_id)

    def test_all_records_round_trip_through_strict_json(self):
        obs = observation()
        candidate = models.CandidateRecord.from_observation(obs, SCORES)
        work = pending_work()
        for record, cls in (
            (obs, models.CandidateObservation),
            (candidate, models.CandidateRecord),
            (work, models.WorkItem),
        ):
            payload = record.to_dict()
            encoded = json.dumps(payload, allow_nan=False, sort_keys=True)
            restored = cls.from_dict(json.loads(encoded))
            self.assertEqual(restored.to_dict(), payload)

    def test_direct_constructor_cannot_bypass_invariants_or_retain_mutable_mapping(self):
        with self.assertRaises(ValueError):
            models.WorkItem(
                schema_version=models.SCHEMA_VERSION,
                work_id="work_bad",
                idempotency_key="idempotent",
                domain="",
                candidate_id="cand_abc",
                action="verify",
                score_components={"priority": 1.0},
                budget_estimate=1.0,
                state="pending",
                attempts=0,
                lease_owner=None,
                lease_token=None,
                lease_generation=0,
                lease_expires_at=None,
                retry_after=None,
                proof_receipts=(),
                last_error=None,
                created_at="2026-07-16T12:00:00Z",
                updated_at="2026-07-16T12:00:00Z",
            )
        mutable = {"priority": 1.0}
        work = models.WorkItem(
            schema_version=models.SCHEMA_VERSION,
            work_id=models.stable_id("work", "agentic-engineering", "cand_abc", "verify", "idempotent"),
            idempotency_key="idempotent",
            domain="agentic-engineering",
            candidate_id="cand_abc",
            action="verify",
            score_components=mutable,
            budget_estimate=1.0,
            state="pending",
            attempts=0,
            lease_owner=None,
            lease_token=None,
            lease_generation=0,
            lease_expires_at=None,
            retry_after=None,
            proof_receipts=(),
            last_error=None,
            created_at="2026-07-16T12:00:00Z",
            updated_at="2026-07-16T12:00:00Z",
        )
        mutable["priority"] = 99.0
        self.assertEqual(work.score_components["priority"], 1.0)


class CanonicalizationAndInputTests(unittest.TestCase):
    def test_persisted_locator_is_canonical_and_ipv6_remains_bracketed(self):
        obs = observation()
        self.assertEqual(obs.canonical_url, "https://example.com/Repo")
        ipv6 = observation(canonical_url="https://[::1]:443/a/../b/#x")
        self.assertEqual(ipv6.canonical_url, "https://[::1]/b")

    def test_non_finite_budget_blank_key_and_naive_now_are_rejected(self):
        with self.assertRaises(ValueError):
            pending_work(budget_estimate=float("nan"))
        with self.assertRaises(ValueError):
            pending_work(idempotency_key="  ")
        with self.assertRaises(ValueError):
            pending_work(now=datetime(2026, 7, 16, 12, 0))


class LeaseFencingTests(unittest.TestCase):
    def test_wrong_owner_or_token_cannot_complete(self):
        leased = pending_work().lease(owner="worker-a", ttl_seconds=60, now=NOW, lease_token="token-a")
        with self.assertRaises(ValueError):
            leased.complete(owner="worker-b", lease_token="token-a", proof_receipt="receipt.json", now=NOW)
        with self.assertRaises(ValueError):
            leased.complete(owner="worker-a", lease_token="wrong", proof_receipt="receipt.json", now=NOW)

    def test_expired_lease_cannot_complete_or_fail(self):
        leased = pending_work().lease(owner="worker-a", ttl_seconds=1, now=NOW, lease_token="token-a")
        expired = NOW + timedelta(seconds=1)
        with self.assertRaises(ValueError):
            leased.complete(owner="worker-a", lease_token="token-a", proof_receipt="receipt.json", now=expired)
        with self.assertRaises(ValueError):
            leased.fail(owner="worker-a", lease_token="token-a", error="timeout", now=expired)

    def test_old_token_is_fenced_after_expiry_and_release(self):
        first = pending_work().lease(owner="worker-a", ttl_seconds=1, now=NOW, lease_token="token-a")
        released = first.release_if_expired(now=NOW + timedelta(seconds=1))
        second = released.lease(
            owner="worker-b",
            ttl_seconds=60,
            now=NOW + timedelta(seconds=2),
            lease_token="token-b",
        )
        with self.assertRaises(ValueError):
            second.complete(owner="worker-a", lease_token="token-a", proof_receipt="stale.json", now=NOW + timedelta(seconds=3))
        done = second.complete(owner="worker-b", lease_token="token-b", proof_receipt="fresh.json", now=NOW + timedelta(seconds=3))
        self.assertEqual(done.state, "done")

    def test_retry_metadata_clears_when_released_and_failure_context_is_retained(self):
        first = pending_work().lease(owner="worker-a", ttl_seconds=60, now=NOW, lease_token="token-a")
        pending = first.fail(
            owner="worker-a",
            lease_token="token-a",
            error="network timeout",
            retry_after_seconds=30,
            now=NOW,
        )
        leased = pending.lease(
            owner="worker-b",
            ttl_seconds=60,
            now=NOW + timedelta(seconds=30),
            lease_token="token-b",
        )
        self.assertIsNone(leased.retry_after)
        done = leased.complete(
            owner="worker-b",
            lease_token="token-b",
            proof_receipt="receipt.json",
            now=NOW + timedelta(seconds=31),
        )
        self.assertIsNone(done.retry_after)
        self.assertEqual(done.last_error, "network timeout")

    def test_terminal_item_rejects_every_further_transition(self):
        leased = pending_work().lease(owner="worker-a", ttl_seconds=60, now=NOW, lease_token="token-a")
        done = leased.complete(owner="worker-a", lease_token="token-a", proof_receipt="receipt.json", now=NOW)
        for operation in (
            lambda: done.lease(owner="worker-b", ttl_seconds=60, now=NOW, lease_token="token-b"),
            lambda: done.complete(owner="worker-a", lease_token="token-a", proof_receipt="again.json", now=NOW),
            lambda: done.fail(owner="worker-a", lease_token="token-a", error="again", now=NOW),
        ):
            with self.assertRaises(ValueError):
                operation()


class RegistryFixtureTests(unittest.TestCase):
    def test_registry_fixture_has_unique_ids_and_required_locators(self):
        fixture = Path(__file__).parent / "fixtures" / "agentic_engineering_registry.json"
        registry = json.loads(fixture.read_text(encoding="utf-8"))
        sources = registry["sources"]
        ids = [source["id"] for source in sources]
        self.assertEqual(len(ids), len(set(ids)))
        for source in sources:
            has_locator = bool(
                source.get("url")
                or source.get("repo")
                or source.get("channel_url")
                or source.get("channel_id")
                or source.get("canonical_url")
                or source.get("local_path")
                or source.get("raw_storage_path")
            )
            awaiting_identification = str(source.get("status", "")).startswith("awaiting-")
            self.assertTrue(has_locator or awaiting_identification, source["id"])


if __name__ == "__main__":
    unittest.main()
