from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
FIXTURE = Path(__file__).resolve().parents[1] / "docs" / "source-maps" / "agentic-engineering-seed-candidates.json"


def _load(name: str, filename: str):
    path = SRC_ROOT / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


models = _load("corpus_engine_models", "corpus_engine_models.py")
discovery = _load("corpus_discovery", "corpus_discovery.py")
seed_loader = _load("corpus_seed_loader", "corpus_seed_loader.py")


class SeedLoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.ledger = Path(self.tmpdir.name) / "ledger.jsonl"

    def test_real_seed_loads_twenty_unique_candidates(self):
        bundle = seed_loader.load_candidate_seed(FIXTURE)
        self.assertEqual(bundle.domain, "agentic-engineering")
        self.assertEqual(bundle.status, "candidate-seed-not-promoted")
        self.assertEqual(len(bundle.candidates), 20)
        self.assertEqual(len({candidate.seed_id for candidate in bundle.candidates}), 20)
        observations = bundle.observations(source_ref="docs/source-maps/agentic-engineering-seed-candidates.json")
        self.assertEqual(len({observation.candidate_key for observation in observations}), 20)

    def test_dry_run_writes_nothing_and_apply_is_physically_idempotent(self):
        bundle = seed_loader.load_candidate_seed(FIXTURE)
        engine = discovery.DiscoveryEngine(self.ledger)
        preview = seed_loader.ingest_candidate_seed(
            engine,
            bundle,
            source_ref="docs/source-maps/agentic-engineering-seed-candidates.json",
            dry_run=True,
        )
        self.assertEqual(len(preview), 20)
        self.assertEqual(engine.ledger.read_events(), [])

        applied = seed_loader.ingest_candidate_seed(
            engine,
            bundle,
            source_ref="docs/source-maps/agentic-engineering-seed-candidates.json",
            dry_run=False,
        )
        self.assertEqual(len(applied), 20)
        self.assertTrue(all(record.status == "discovered" for record in applied))
        first_event_count = len(engine.ledger.read_events())
        self.assertEqual(first_event_count, 20)
        seed_loader.ingest_candidate_seed(
            engine,
            bundle,
            source_ref="docs/source-maps/agentic-engineering-seed-candidates.json",
            dry_run=False,
        )
        self.assertEqual(len(engine.ledger.read_events()), first_event_count)
        restarted = discovery.DiscoveryEngine(self.ledger)
        self.assertEqual(len(restarted.candidates), 20)
        self.assertTrue(all(record.status == "discovered" for record in restarted.candidates.values()))

    def test_loader_rejects_non_candidate_status_duplicate_ids_and_duplicate_urls(self):
        raw = json.loads(FIXTURE.read_text())
        for mutate in (
            lambda data: data.update(status="promoted"),
            lambda data: data["candidates"].append(dict(data["candidates"][0])),
            lambda data: data["candidates"].append({
                **data["candidates"][0],
                "id": "equivalent-url",
                "canonical_url": data["candidates"][0]["canonical_url"] + "#fragment",
            }),
        ):
            payload = json.loads(json.dumps(raw))
            mutate(payload)
            path = Path(self.tmpdir.name) / f"bad-{len(list(Path(self.tmpdir.name).glob('bad-*')))}.json"
            path.write_text(json.dumps(payload))
            with self.assertRaises(seed_loader.CandidateSeedError):
                seed_loader.load_candidate_seed(path)

    def test_loader_rejects_duplicate_json_keys_nonfinite_numbers_and_unknown_fields(self):
        duplicate = Path(self.tmpdir.name) / "duplicate.json"
        duplicate.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(seed_loader.CandidateSeedError):
            seed_loader.load_candidate_seed(duplicate)

        nonfinite = Path(self.tmpdir.name) / "nonfinite.json"
        nonfinite.write_text('{"schema_version": NaN}')
        with self.assertRaises(seed_loader.CandidateSeedError):
            seed_loader.load_candidate_seed(nonfinite)

        raw = json.loads(FIXTURE.read_text())
        raw["candidates"][0]["typo_field"] = True
        unknown = Path(self.tmpdir.name) / "unknown.json"
        unknown.write_text(json.dumps(raw))
        with self.assertRaises(seed_loader.CandidateSeedError):
            seed_loader.load_candidate_seed(unknown)


if __name__ == "__main__":
    unittest.main()
