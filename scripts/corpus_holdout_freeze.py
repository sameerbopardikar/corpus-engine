#!/usr/bin/env python3
"""Freeze a blinded worker packet and scan every worker-visible byte for leakage."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_holdout_freeze import freeze_worker_packet


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze and leakage-scan a blinded worker packet")
    parser.add_argument("--worker-packet", required=True)
    parser.add_argument("--hidden-evaluator", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    receipt = freeze_worker_packet(
        json.loads(Path(args.worker_packet).read_text(encoding="utf-8")),
        hidden_evaluator=json.loads(Path(args.hidden_evaluator).read_text(encoding="utf-8")),
        packet_label=args.label,
    )
    encoded = json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.output:
        Path(args.output).write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if receipt["leakage_absent"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
