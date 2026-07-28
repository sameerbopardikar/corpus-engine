#!/usr/bin/env python3
"""Lease and commit deterministic X-radar queries from a corpus registry."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_x_radar import XRadarLease, commit_failure, commit_success, lease_next


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_queries(registry_path: Path, source_id: str) -> tuple[str, ...]:
    document = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    matches = [source for source in document.get("sources", []) if source.get("id") == source_id]
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one registry source {source_id!r}")
    queries = matches[0].get("queries")
    if not isinstance(queries, list):
        raise SystemExit(f"registry source {source_id!r} has no query list")
    return tuple(queries)


def parse_lease(path: Path) -> XRadarLease:
    return XRadarLease.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay-safe X-radar query rotation")
    parser.add_argument("--registry", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--state", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    lease_parser = subparsers.add_parser("lease")
    lease_parser.add_argument("--cycle-id", required=True)
    lease_parser.add_argument("--output")

    success_parser = subparsers.add_parser("success")
    success_parser.add_argument("--lease", required=True)
    success_parser.add_argument("--result-receipt", required=True)

    failure_parser = subparsers.add_parser("failure")
    failure_parser.add_argument("--lease", required=True)
    failure_parser.add_argument("--error", required=True)

    args = parser.parse_args(argv)
    queries = load_queries(Path(args.registry), args.source_id)
    state = Path(args.state)
    if args.command == "lease":
        lease = lease_next(queries, state, cycle_id=args.cycle_id, leased_at=now_iso())
        encoded = json.dumps(lease.to_dict(), indent=2, sort_keys=True) + "\n"
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0

    lease = parse_lease(Path(args.lease))
    if args.command == "success":
        receipt = Path(args.result_receipt)
        digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
        result = commit_success(
            queries, state, lease=lease, result_sha256=digest, committed_at=now_iso()
        )
    else:
        result = commit_failure(
            queries, state, lease=lease, error=args.error, committed_at=now_iso()
        )
    print(json.dumps({"next_index": result["next_index"], "status": result["history"][-1]["status"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
