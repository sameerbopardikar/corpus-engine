from __future__ import annotations

import dataclasses
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_domain_spec import (
    DomainBootstrapError,
    bootstrap_domain,
    load_domain_spec,
    spec_digest,
)

CONFIG_DOMAINS = ROOT / "config" / "domains"
FIXTURE_DOMAINS = ROOT / "tests" / "fixtures" / "domains"
NOW = "2026-07-18T12:00:00Z"


def _load(name: str, in_config: bool = True):
    base = CONFIG_DOMAINS if in_config else FIXTURE_DOMAINS
    return load_domain_spec(base / name)


class BootstrapDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.base = Path(self.tempdir.name)

    def test_bootstrap_creates_all_roots_and_marker(self):
        spec = _load("training.json")
        receipt = bootstrap_domain(spec, self.base, now=NOW)
        for name, rel in spec.roots.items():
            self.assertTrue((self.base / rel).is_dir(), f"{name} not created")
        self.assertEqual(receipt["domain"], "training")
        self.assertEqual(receipt["spec_digest"], spec_digest(spec))
        marker = self.base / spec.roots["state_root"] / "domain-state.json"
        self.assertTrue(marker.is_file())

    def test_bootstrap_never_creates_scheduler_or_source(self):
        spec = _load("training.json")
        bootstrap_domain(spec, self.base, now=NOW)
        created = [p.name for p in self.base.rglob("*") if p.is_file()]
        self.assertNotIn("sources.json", created)
        for name in created:
            self.assertFalse(name.endswith((".service", ".timer", ".cron")))
            self.assertNotEqual(name, "crontab")

    def test_bootstrap_retry_is_physical_noop(self):
        spec = _load("training.json")
        first = bootstrap_domain(spec, self.base, now=NOW)
        marker = self.base / spec.roots["state_root"] / "domain-state.json"
        before = marker.read_bytes()
        second = bootstrap_domain(spec, self.base, now="2026-07-19T00:00:00Z")
        self.assertEqual(first["spec_digest"], second["spec_digest"])
        self.assertEqual(before, marker.read_bytes())  # byte-identical, no rewrite


class BootstrapFailClosedTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.base = Path(self.tempdir.name)

    def test_conflicting_spec_version_fails_before_mutation(self):
        spec = _load("training.json")
        bootstrap_domain(spec, self.base, now=NOW)
        # Same domain/roots, different content -> different digest.
        conflicting = dataclasses.replace(spec, title="Training Operating Corpus (edited)")
        with self.assertRaises(DomainBootstrapError):
            bootstrap_domain(conflicting, self.base, now=NOW)

    def test_symlink_escape_fails_before_mutation(self):
        spec = _load("training.json")
        outside = Path(self.tempdir.name).parent / "outside-escape"
        outside.mkdir(exist_ok=True)
        self.addCleanup(lambda: outside.rmdir() if outside.exists() else None)
        # Make the corpus root a symlink escaping base.
        link = self.base / spec.roots["corpus_root"]
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, link)
        with self.assertRaises(DomainBootstrapError):
            bootstrap_domain(spec, self.base, now=NOW)

    def test_source_override_fails_closed(self):
        spec = _load("training.json")
        with self.assertRaises(DomainBootstrapError):
            bootstrap_domain(spec, self.base, now=NOW, source_id="training-source")


class ArbitraryDomainBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.base = Path(self.tempdir.name)

    def test_three_checked_in_domains_boot_without_python_edits(self):
        specs = [
            _load("agentic-engineering.json"),
            _load("training.json"),
            _load("arbitrary-observatory.json", in_config=False),
        ]
        domains = set()
        for spec in specs:
            receipt = bootstrap_domain(spec, self.base / spec.domain, now=NOW)
            domains.add(receipt["domain"])
            self.assertTrue((self.base / spec.domain / spec.roots["corpus_root"]).is_dir())
        self.assertEqual(domains, {"agentic-engineering", "training", "astronomy-observing"})


if __name__ == "__main__":
    unittest.main()
