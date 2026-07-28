from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "corpus_x_radar.py"


class XRadarCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = self.root / "sources.json"
        self.registry.write_text(
            json.dumps({
                "sources": [{
                    "id": "x-proof",
                    "queries": ["query zero", "query one"],
                }]
            }) + "\n",
            encoding="utf-8",
        )
        self.state = self.root / "state.json"

    def run_cli(self, *arguments: str, check: bool = True):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--registry", str(self.registry),
                "--source-id", "x-proof",
                "--state", str(self.state),
                *arguments,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=check,
        )

    def make_receipt(self, lease: dict, name: str) -> Path:
        artifact = self.root / f"{name}.artifact.json"
        artifact.write_text('{"signals": []}\n', encoding="utf-8")
        receipt = self.root / f"{name}.receipt.json"
        receipt.write_text(
            json.dumps({
                "schema_version": 1,
                "cycle_id": lease["cycle_id"],
                "lease_id": lease["lease_id"],
                "queries_sha256": lease["queries_sha256"],
                "query_index": lease["index"],
                "query_sha256": lease["query_sha256"],
                "result": "material_signal_ingested",
                "checked_at": "2026-07-28T00:00:00Z",
                "artifacts": [{
                    "path": str(artifact.resolve()),
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                }],
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return receipt

    def test_cli_persists_verified_success_and_restarts_at_next_query(self):
        first_path = self.root / "lease-0.json"
        first = self.run_cli("lease", "--cycle-id", "cycle-0", "--output", str(first_path))
        lease = json.loads(first.stdout)
        self.assertEqual(lease["index"], 0)
        receipt = self.make_receipt(lease, "cycle-0")
        success = self.run_cli(
            "success", "--lease", str(first_path), "--result-receipt", str(receipt)
        )
        self.assertEqual(json.loads(success.stdout)["next_index"], 1)

        second = self.run_cli("lease", "--cycle-id", "cycle-1")
        self.assertEqual(json.loads(second.stdout)["index"], 1)

    def test_cli_returns_terminal_code_instead_of_reissuing_finalized_cycle(self):
        lease_path = self.root / "lease.json"
        lease_result = self.run_cli(
            "lease", "--cycle-id", "cycle-final", "--output", str(lease_path)
        )
        receipt = self.make_receipt(json.loads(lease_result.stdout), "final")
        self.run_cli(
            "success", "--lease", str(lease_path), "--result-receipt", str(receipt)
        )
        replay = self.run_cli("lease", "--cycle-id", "cycle-final", check=False)
        self.assertEqual(replay.returncode, 3)
        self.assertEqual(json.loads(replay.stdout)["status"], "finalized")


if __name__ == "__main__":
    unittest.main()
