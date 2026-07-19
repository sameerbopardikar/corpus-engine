#!/usr/bin/env python3
"""``corpus-start`` — the deterministic orchestrator CLI.

Verbs:

    corpus-start begin       --topic Nutrition --run-root <dir> [--resume-live-root <path>]
    corpus-start apply-packet --run <dir> --packet <file>
    corpus-start continue    --run <dir>
    corpus-start status      --run <dir> [--json]

``begin`` only creates the phase ledger and asks for a bootstrap packet.
``continue`` executes the existing generic engine components in a resumable
phase order and cannot report ``complete`` without every required receipt.

The acquisition / GBrain / Atlas / scheduler boundaries default to the live
adapters; tests inject deterministic fakes via ``main(..., boundaries=...)``.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from corpus_bootstrap_packet import build_topic_bootstrap_packet  # noqa: E402
from corpus_start import CorpusStartRun, StartBoundaries, StartOrchestrationError  # noqa: E402
from corpus_start_state import StartRunError  # noqa: E402


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emit(payload: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")


def main(argv: list[str] | None = None, *, boundaries: StartBoundaries | None = None, now: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog="corpus-start", description=__doc__)
    sub = parser.add_subparsers(dest="verb", required=True)

    p_start = sub.add_parser("start", help="topic-only end-to-end corpus start")
    p_start.add_argument("--topic", required=True)
    p_start.add_argument("--run-root", required=True)
    p_start.add_argument(
        "--resume-live-root",
        help="use an existing corpora base instead of an isolated clean root",
    )

    p_begin = sub.add_parser("begin", help="create the phase ledger for a topic")
    p_begin.add_argument("--topic", required=True)
    p_begin.add_argument("--run-root", required=True)
    p_begin.add_argument(
        "--resume-live-root",
        help="resume against a non-clean existing namespace (marks clean_root=false)",
    )

    p_apply = sub.add_parser("apply-packet", help="compile a validated bootstrap packet")
    p_apply.add_argument("--run", required=True)
    p_apply.add_argument("--packet", required=True)

    p_cont = sub.add_parser("continue", help="run remaining phases to completion")
    p_cont.add_argument("--run", required=True)

    p_status = sub.add_parser("status", help="print the completion-contract status")
    p_status.add_argument("--run", required=True)
    p_status.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    now = now or _utc_now()

    try:
        if args.verb == "start":
            clean_root = args.resume_live_root is None
            run = CorpusStartRun.begin(
                args.run_root, topic=args.topic, now=now,
                clean_root=clean_root, corpora_base=args.resume_live_root,
                boundaries=boundaries,
            )
            if run.status()["status"] == "initialized":
                packet_path = Path(args.run_root) / ".corpus-start" / "auto-bootstrap-packet.json"
                packet_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                packet_path.write_text(
                    json.dumps(build_topic_bootstrap_packet(args.topic), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                run.apply_packet(packet_path, now=now)
            if run.status()["status"] != "complete":
                run.continue_run(now=now)
            _emit(run.status(), as_json=True)
            return 0

        if args.verb == "begin":
            clean_root = args.resume_live_root is None
            run = CorpusStartRun.begin(
                args.run_root, topic=args.topic, now=now,
                clean_root=clean_root, corpora_base=args.resume_live_root,
                boundaries=boundaries,
            )
            _emit(
                {
                    "run_root": str(Path(args.run_root)),
                    "topic_input": run.status()["topic_input"],
                    "clean_root": clean_root,
                    "status": run.status()["status"],
                    "next_action": run.next_action(),
                },
                as_json=False,
            )
            return 0

        if args.verb == "apply-packet":
            run = CorpusStartRun.load(args.run, boundaries=boundaries)
            run.apply_packet(args.packet, now=now)
            _emit({"status": run.status()["status"], "next_action": run.next_action()}, as_json=False)
            return 0

        if args.verb == "continue":
            run = CorpusStartRun.load(args.run, boundaries=boundaries)
            run.continue_run(now=now)
            _emit(run.status(), as_json=True)
            return 0

        if args.verb == "status":
            run = CorpusStartRun.load(args.run, boundaries=boundaries)
            _emit(run.status(), as_json=args.json)
            return 0

    except (StartOrchestrationError, StartRunError) as exc:
        print(f"corpus-start: {exc}", file=sys.stderr)
        return 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
