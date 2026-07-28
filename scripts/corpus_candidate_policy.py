#!/usr/bin/env python3
"""Evaluate corpus discovery candidates and materialize the recurring watch projection."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_candidate_policy import evaluate_candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic corpus candidate disposition")
    parser.add_argument("--discovery-ledger", required=True)
    parser.add_argument("--source-graph", required=True)
    parser.add_argument("--watch-projection", required=True)
    parser.add_argument("--evaluated-at", required=True)
    args = parser.parse_args(argv)

    result = evaluate_candidates(
        ledger_path=Path(args.discovery_ledger),
        graph_path=Path(args.source_graph),
        watch_projection_path=Path(args.watch_projection),
        evaluated_at=args.evaluated_at,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
