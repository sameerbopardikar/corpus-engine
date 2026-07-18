from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_adapters import (
    ADAPTER_FACTORIES,
    AdapterFamilyError,
    build_adapter,
)

MODULE_PATH = SRC / "corpus_engine.py"
_spec = importlib.util.spec_from_file_location("corpus_engine", MODULE_PATH)
assert _spec and _spec.loader
corpus_engine = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = corpus_engine
_spec.loader.exec_module(corpus_engine)


class AdapterFactoryRegistryTests(unittest.TestCase):
    def test_every_declared_family_resolves_to_matching_adapter(self):
        expected = {
            "web", "github", "youtube",
            "arxiv", "openalex", "benchmark", "rss", "postmortem",
        }
        self.assertTrue(expected <= set(ADAPTER_FACTORIES))
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td)
            entry = {
                "id": "s1", "url": "https://example.org/x", "repo": "o/r",
                "channel_id": "c1", "feed_kind": "operator_feed",
            }
            for family in expected:
                adapter = build_adapter(
                    family, raw, http_get=lambda *a, **k: None, fetched_at=lambda: "2026-07-18T00:00:00Z", entry=entry
                )
                self.assertEqual(adapter.family, family)

    def test_unknown_family_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(AdapterFamilyError):
                build_adapter("does-not-exist", Path(td), http_get=lambda *a, **k: None, fetched_at=lambda: "z", entry={})


class RefreshEntryDispatchTests(unittest.TestCase):
    def _engine(self):
        return object.__new__(corpus_engine.CorpusEngine)

    def test_unknown_source_type_raises_before_any_mutation(self):
        engine = self._engine()
        # Any real work would go through these; assert none are touched.
        engine.refresh_web = Mock()
        engine.refresh_generic_adapter = Mock()
        engine.write_card = Mock()
        with self.assertRaises(RuntimeError):
            engine.refresh_entry({"id": "x", "source_type": "totally_unknown"})
        engine.refresh_web.assert_not_called()
        engine.refresh_generic_adapter.assert_not_called()
        engine.write_card.assert_not_called()

    def test_standard_family_source_type_routes_to_generic_adapter(self):
        engine = self._engine()
        engine.refresh_generic_adapter = Mock(return_value="ok")
        result = engine.refresh_entry({"id": "a", "source_type": "arxiv"}, None)
        self.assertEqual(result, "ok")
        _, kwargs = engine.refresh_generic_adapter.call_args
        self.assertEqual(kwargs.get("family"), "arxiv")

    def test_generic_adapter_refresh_writes_card_from_observation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            normalized = root / "abstract.txt"
            normalized.write_text("A" * 500, encoding="utf-8")
            raw = root / "raw.xml"
            raw.write_bytes(b"raw-bytes")
            observation = SimpleNamespace(
                normalized_pointer=str(normalized),
                raw_pointer=str(raw),
                raw_sha256=corpus_engine.sha256_bytes(b"raw-bytes"),
                normalized_sha256=corpus_engine.sha256_bytes(normalized.read_bytes()),
                canonical_locator="https://arxiv.org/abs/2401.01234v3",
                title="Reported claim",
            )
            batch = SimpleNamespace(source_revision="b" * 64, observations=(observation,))

            engine = self._engine()
            engine.source_dir = Mock(return_value=root / "src")
            engine.source_spec = Mock(return_value=SimpleNamespace(rights_state=corpus_engine.RightsState.PUBLIC_METADATA_ONLY))
            engine.run_adapter = Mock(return_value=batch)
            engine.write_card = Mock(return_value="training/sources/scientific/arxiv-x")

            entry = {"id": "arxiv-x", "source_type": "arxiv", "url": observation.canonical_locator, "evidence_lane": "scientific"}
            result = engine.refresh_generic_adapter(entry, None, family="arxiv")
            self.assertEqual(result.status, "refreshed")
            engine.write_card.assert_called_once()


if __name__ == "__main__":
    unittest.main()
