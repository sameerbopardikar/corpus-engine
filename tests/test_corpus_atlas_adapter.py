from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_atlas_adapter import (  # noqa: E402
    AtlasAdapterError,
    build_atlas_launch_config,
    instantiate_atlas,
    select_free_port,
)


class _FakeHandle:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def _make_probe(*, health_ok=True, ready_ok=True, index=None):
    calls = []

    def probe(url):
        calls.append(url)
        if url.endswith("/healthz"):
            return (200 if health_ok else 503), {}
        if url.endswith("/readyz"):
            return (200 if ready_ok else 503), {}
        if url.endswith("/api/index"):
            return 200, (index or {})
        raise AssertionError(f"unexpected probe url {url}")

    probe.calls = calls  # type: ignore[attr-defined]
    return probe


class BuildConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_config_contains_required_fields(self):
        config = build_atlas_launch_config(
            run_root=self.run_root, domain="nutrition",
            title="Nutrition Research Corpus", port=54321,
        )
        env = config["env"]
        self.assertEqual(env["CORPUS_ROOT"], str(self.run_root / "corpora" / "nutrition"))
        self.assertEqual(env["ATLAS_PRODUCT_NAME"], "Nutrition Research Corpus Research Atlas")
        self.assertIn("queue", env["ATLAS_QUEUE_PATH"])
        self.assertIn("acquisition", env["ATLAS_ACQUISITION_PATH"])
        self.assertEqual(config["host"], "127.0.0.1")
        self.assertEqual(config["port"], 54321)

    def test_no_embedded_secret(self):
        config = build_atlas_launch_config(
            run_root=self.run_root, domain="nutrition", title="Nutrition Research Corpus", port=1,
        )
        blob = json.dumps(config).lower()
        for forbidden in ("secret", "password", "token", "api_key", "apikey"):
            self.assertNotIn(forbidden, blob)

    def test_loopback_only(self):
        config = build_atlas_launch_config(
            run_root=self.run_root, domain="nutrition", title="X", port=1,
        )
        self.assertTrue(config["base_url"].startswith("http://127.0.0.1:"))
        self.assertEqual(config["env"]["HOST"], "127.0.0.1")

    def test_free_port_selected_when_absent(self):
        config = build_atlas_launch_config(
            run_root=self.run_root, domain="nutrition", title="X",
        )
        self.assertIsInstance(config["port"], int)
        self.assertGreater(config["port"], 0)


class SelectPortTest(unittest.TestCase):
    def test_select_free_port_is_bindable(self):
        import socket

        port = select_free_port()
        self.assertIsInstance(port, int)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))  # must be free


class InstantiateAtlasTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_root = Path(self._tmp.name)
        self.config = build_atlas_launch_config(
            run_root=self.run_root, domain="nutrition",
            title="Nutrition Research Corpus", port=45678,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_healthy_launch_returns_receipt_and_stops(self):
        handle = _FakeHandle()
        probe = _make_probe(index={"product_name": "Nutrition Research Corpus Research Atlas", "domain": "nutrition"})
        receipt = instantiate_atlas(
            self.config, launcher=lambda cfg: handle, probe=probe,
        )
        self.assertTrue(receipt["atlas_ready"])
        self.assertTrue(receipt["healthz"])
        self.assertTrue(receipt["readyz"])
        self.assertTrue(receipt["index_verified"])
        self.assertEqual(receipt["domain"], "nutrition")
        self.assertEqual(len(receipt["receipt_sha256"]), 64)
        self.assertTrue(handle.stopped)  # foreground-test mode stops the process

    def test_keep_running_when_stop_false(self):
        handle = _FakeHandle()
        probe = _make_probe(index={"product_name": "Nutrition Research Corpus Research Atlas", "domain": "nutrition"})
        instantiate_atlas(self.config, launcher=lambda cfg: handle, probe=probe, stop=False)
        self.assertFalse(handle.stopped)

    def test_unhealthy_raises_and_stops(self):
        handle = _FakeHandle()
        probe = _make_probe(health_ok=False)
        with self.assertRaises(AtlasAdapterError):
            instantiate_atlas(
                self.config, launcher=lambda cfg: handle, probe=probe, retries=2, retry_sleep=lambda _: None,
            )
        self.assertTrue(handle.stopped)  # never leave a broken process running

    def test_index_mismatch_raises(self):
        handle = _FakeHandle()
        probe = _make_probe(index={"product_name": "Wrong Product", "domain": "training"})
        with self.assertRaises(AtlasAdapterError):
            instantiate_atlas(self.config, launcher=lambda cfg: handle, probe=probe)
        self.assertTrue(handle.stopped)


if __name__ == "__main__":
    unittest.main()
