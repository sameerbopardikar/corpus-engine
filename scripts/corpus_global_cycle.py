#!/usr/bin/env python3
"""One global, manifest-driven corpus cycle across all enabled domains.

The single scheduler enumerates every checked-in domain spec, loads each
domain's seed candidates, and ranks them in ONE plan reserved against ONE
shared budget authority. No domain is special-cased here — adding a domain is
adding a spec file. Seed candidates enter as rights-unclear (unverified): the
plan ranks them, but nothing is auto-acquired until the discovery pipeline
independently classifies rights.

This is a planning entrypoint: it performs no network acquisition and mutates
no production corpus. It replaces the Agentic-only body of the legacy cycle
script while preserving the one-scheduler / one-global-budget invariant.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
# The script directory is sys.path[0] when this file is executed directly and
# contains this entrypoint under the same basename as src/corpus_global_cycle.py.
# Force src to the front even when PYTHONPATH already contains it, otherwise
# Python imports this partially initialized script as its own library module.
while str(_SRC) in sys.path:
    sys.path.remove(str(_SRC))
sys.path.insert(0, str(_SRC))

from corpus_discovery import score_observation
from corpus_domain_spec import load_domain_spec
from corpus_engine_models import CandidateRecord
from corpus_global_cycle import run_global_cycle
from corpus_priority import CandidateTask, PriorityPolicy
from corpus_seed_loader import load_candidate_seed

DEFAULT_CONFIG_DIR = _REPO_ROOT / "config" / "domains"
# One shared budget authority for the whole engine — never per-domain.
DEFAULT_BUDGET_PATH = Path("/root/exports/thinker-corpora/_engine/budget.json")


def default_reservation_id(
    config_dir: Path, day: str, *, planner_root: Path | None = None
) -> str:
    """Bind a daily reservation to exact planner, domain-spec, and seed bytes.

    The planner binding is required when immutable engine releases change during
    a UTC day. Without it, a corrected release can reuse the prior release's
    reservation ID with a different command preimage and fail every retry.
    """
    digest = hashlib.sha256()
    root = Path(planner_root) if planner_root is not None else _REPO_ROOT
    planner_paths = [root / "scripts" / "corpus_global_cycle.py"]
    planner_paths.extend(sorted((root / "src").glob("*.py")))
    for planner_path in planner_paths:
        if not planner_path.is_file():
            continue
        digest.update(str(planner_path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(planner_path.read_bytes())
    for spec_path in sorted(Path(config_dir).glob("*.json")):
        spec_bytes = spec_path.read_bytes()
        digest.update(spec_path.name.encode())
        digest.update(b"\0")
        digest.update(spec_bytes)
        raw = json.loads(spec_bytes)
        seed_ref = raw.get("seed_ref")
        if not isinstance(seed_ref, str) or not seed_ref:
            continue
        seed_path = _REPO_ROOT / seed_ref
        digest.update(seed_ref.encode())
        digest.update(b"\0")
        digest.update(seed_path.read_bytes())
    return f"global-{day}-{digest.hexdigest()[:16]}"


def enumerate_domain_specs(config_dir: Path):
    """Yield (spec, spec_path) for every checked-in domain spec, sorted."""
    for spec_path in sorted(Path(config_dir).glob("*.json")):
        yield load_domain_spec(spec_path), spec_path


def _domain_loader(spec, seed_path: Path):
    def _load() -> list[CandidateTask]:
        bundle = load_candidate_seed(seed_path)
        if bundle.domain != spec.domain:
            raise ValueError(f"seed domain {bundle.domain!r} != spec domain {spec.domain!r}")
        tasks: list[CandidateTask] = []
        for observation in bundle.observations(source_ref=spec.seed_ref):
            scores, rationale = score_observation(observation)
            record = CandidateRecord.from_observation(
                observation, scores, rationale=rationale, rights_state="rights_unclear"
            )
            tasks.append(CandidateTask(record, material_delta=True))
        return tasks

    return _load


def build_domain_loaders(config_dir: Path):
    loaders = []
    for spec, spec_path in enumerate_domain_specs(config_dir):
        seed_path = _REPO_ROOT / spec.seed_ref
        loaders.append((spec.domain, _domain_loader(spec, seed_path)))
    return loaders


def run(*, config_dir: Path, budget_ledger_path: Path, reservation_id: str, now: datetime):
    loaders = build_domain_loaders(config_dir)
    return run_global_cycle(
        reservation_id=reservation_id,
        domain_loaders=loaders,
        policy=PriorityPolicy.default(),
        budget_ledger_path=budget_ledger_path,
        now=now,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one global corpus cycle across all enabled domains.")
    parser.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR))
    parser.add_argument("--budget-path", default=str(DEFAULT_BUDGET_PATH))
    parser.add_argument("--reservation-id")
    args = parser.parse_args(argv)
    clock_now = datetime.now(timezone.utc)
    if args.reservation_id:
        reservation_id = args.reservation_id
        now = clock_now
    else:
        # A daily scheduler retry must replay the exact same command preimage.
        # Wall-clock microseconds would make an otherwise identical retry conflict.
        now = clock_now.replace(hour=0, minute=0, second=0, microsecond=0)
        reservation_id = default_reservation_id(
            Path(args.config_dir), now.date().isoformat()
        )
    result = run(
        config_dir=Path(args.config_dir),
        budget_ledger_path=Path(args.budget_path),
        reservation_id=reservation_id,
        now=now,
    )
    summary = {
        "status": result["status"],
        "reservation_id": reservation_id,
        "domains_planned": result["domains_planned"],
        "failures": result["failures"],
        "selected_candidate_ids": result["selected_candidate_ids"],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
