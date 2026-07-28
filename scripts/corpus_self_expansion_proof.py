#!/usr/bin/env python3
"""Run the accelerated recursive source-expansion proof from a JSON packet."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_recursive_discovery_eval import run_recursive_proof


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a shadow two-cycle corpus proof")
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-root", required=True)
    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    receipt = run_recursive_proof(config, run_root=Path(args.run_root))
    print(json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if receipt["overall_status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
