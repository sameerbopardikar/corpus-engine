import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "corpus_engine_cycle_v1.sh"


class WrapperExecuteModeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.args_file = self.tmp / "python3_args.txt"
        # A fake python3 on PATH that records the args the wrapper invokes it
        # with and returns a valid JSON result so `set -e` stays happy.
        bindir = self.tmp / "bin"
        bindir.mkdir()
        shim = bindir / "python3"
        shim.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{self.args_file}"\n'
            'echo \'{"status":"executed","report":{}}\'\n'
            "exit 0\n"
        )
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        self.bindir = bindir

    def _run(self, execute_env=None):
        env = dict(os.environ)
        env["PATH"] = f"{self.bindir}:{env['PATH']}"
        env["CORPUS_CONFIG_DIR"] = str(ROOT / "config" / "domains")
        env["CORPUS_BUDGET_PATH"] = str(self.tmp / "budget.json")
        env["CORPUS_CORPORA_ROOT"] = str(self.tmp / "corpora")
        if execute_env is not None:
            env["CORPUS_ACQUISITION_EXECUTE"] = execute_env
        proc = subprocess.run(
            ["bash", str(WRAPPER)], env=env, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.args_file.read_text()

    def test_default_runs_bounded_execute_mode(self):
        recorded = self._run()
        self.assertIn("--execute", recorded)
        self.assertIn("--corpora-root", recorded)
        self.assertIn(str(self.tmp / "corpora"), recorded)
        # Still exactly one global budget authority.
        self.assertIn(str(self.tmp / "budget.json"), recorded)

    def test_dry_run_override_plans_only(self):
        recorded = self._run(execute_env="0")
        self.assertNotIn("--execute", recorded)
        self.assertNotIn("--corpora-root", recorded)

    def test_script_passes_bash_syntax_check(self):
        proc = subprocess.run(["bash", "-n", str(WRAPPER)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
