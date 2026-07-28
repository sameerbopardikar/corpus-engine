#!/usr/bin/env python3
"""CLI for the shared self-expanding source/creator graph."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_source_graph import (
    ingest_relationships,
    relationships_from_x_projection,
    reconcile_source_graph,
)


def _read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest and reconcile corpus source-graph relationships")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="Ingest a JSON array of relationship observations")
    ingest.add_argument("--input", required=True)
    ingest.add_argument("--graph", required=True)
    ingest.add_argument("--discovery-ledger", required=True)
    ingest.add_argument("--max-items", type=int, default=100)

    ingest_x = subparsers.add_parser("ingest-x-projection", help="Derive creator/source candidates from an X projection")
    ingest_x.add_argument("--input", required=True)
    ingest_x.add_argument("--domain", required=True)
    ingest_x.add_argument("--topic", action="append", default=[])
    ingest_x.add_argument("--graph", required=True)
    ingest_x.add_argument("--discovery-ledger", required=True)
    ingest_x.add_argument("--max-items", type=int, default=100)

    reconcile = subparsers.add_parser("reconcile", help="Replay the durable source graph into the candidate ledger")
    reconcile.add_argument("--graph", required=True)
    reconcile.add_argument("--discovery-ledger", required=True)

    args = parser.parse_args(argv)
    if args.command == "ingest":
        material = _read_json(Path(args.input))
        if not isinstance(material, list):
            raise SystemExit("relationship input must be a JSON array")
        result = ingest_relationships(
            material,
            graph_path=Path(args.graph),
            discovery_ledger_path=Path(args.discovery_ledger),
            max_items=args.max_items,
        )
    elif args.command == "ingest-x-projection":
        projection = _read_json(Path(args.input))
        relationships = relationships_from_x_projection(
            projection, domain=args.domain, topics=tuple(args.topic)
        )
        result = ingest_relationships(
            relationships,
            graph_path=Path(args.graph),
            discovery_ledger_path=Path(args.discovery_ledger),
            max_items=args.max_items,
        )
    else:
        result = reconcile_source_graph(Path(args.graph), Path(args.discovery_ledger))

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
