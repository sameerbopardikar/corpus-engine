#!/usr/bin/env python3
"""Live boundaries for the topic-only ``corpus-start`` orchestrator.

Every boundary shells into an already-proven runtime surface and verifies its
readback.  The orchestration layer never fabricates receipts and clean proof
runs use only run-local storage plus a temporary, isolated GBrain source.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from corpus_start import PhaseContext, StartBoundaries, StartOrchestrationError

ENGINE_ROOT = Path(__file__).resolve().parents[1]
GLOBAL_CYCLE_SCRIPT = ENGINE_ROOT / "scripts" / "corpus_global_cycle.py"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], Mapping[str, str] | None, Path | None], CommandResult]


def _run_command(
    argv: Sequence[str], env: Mapping[str, str] | None = None, cwd: Path | None = None
) -> CommandResult:
    merged = os.environ.copy()
    if env:
        merged.update({str(k): str(v) for k, v in env.items()})
    proc = subprocess.run(
        list(argv), cwd=str(cwd) if cwd else None, env=merged,
        text=True, capture_output=True, check=False,
    )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def _require_ok(result: CommandResult, label: str) -> str:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:1000]
        raise StartOrchestrationError(f"{label} failed ({result.returncode}): {detail}")
    return result.stdout


def _json_stdout(result: CommandResult, label: str) -> dict[str, Any]:
    raw = _require_ok(result, label)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StartOrchestrationError(f"{label} returned malformed JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise StartOrchestrationError(f"{label} did not return a JSON object")
    return value


def _source_files(corpus_root: Path) -> list[str]:
    root = corpus_root / "sources" / "acquired"
    if not root.exists():
        return []
    return [str(path.relative_to(corpus_root)) for path in sorted(root.rglob("*.md"))]


def acquire_live(ctx: PhaseContext, *, runner: CommandRunner = _run_command) -> dict[str, Any]:
    """Run the existing bounded execute CLI against this run's isolated roots."""
    reservation = f"start-{ctx.domain}-{hashlib.sha256(str(ctx.run_root).encode()).hexdigest()[:12]}"
    budget = ctx.run_root / ".corpus-start" / "budget.json"
    argv = [
        sys.executable, str(GLOBAL_CYCLE_SCRIPT),
        "--config-dir", str(ctx.config_dir),
        "--budget-path", str(budget),
        "--reservation-id", reservation,
        "--execute",
        "--corpora-root", str(ctx.corpora_base),
        "--corpus-revision", reservation,
    ]
    summary = _json_stdout(runner(argv, None, ENGINE_ROOT), "corpus acquisition")
    if ctx.domain not in summary.get("domains_planned", []):
        raise StartOrchestrationError(f"acquisition did not plan target domain {ctx.domain!r}")
    failures = summary.get("failures")
    if failures:
        raise StartOrchestrationError(f"acquisition reported domain failures: {failures}")
    report = summary.get("report")
    if not isinstance(report, dict):
        raise StartOrchestrationError("acquisition summary has no report")
    acquired = int(report.get("acquired", 0))
    discovered = int(report.get("discovered", 0))
    if acquired < 1:
        raise StartOrchestrationError("live acquisition completed without acquiring a public source")
    owned = _source_files(ctx.corpus_root)
    if len(owned) < acquired:
        raise StartOrchestrationError(
            f"acquisition reported {acquired} sources but only {len(owned)} canonical pages exist"
        )
    return {
        "sources_discovered": discovered,
        "source_families_discovered": list(report.get("source_families_discovered", [])),
        "sources_acquired": acquired,
        "owned_sources": owned,
        "reservation_id": reservation,
        "changed_domains": list(report.get("changed_domains", [])),
    }


def _proof_source_id(ctx: PhaseContext) -> str:
    digest = hashlib.sha256(str(ctx.run_root.resolve()).encode()).hexdigest()[:10]
    return f"corpus-start-{ctx.domain[:9]}-{digest}"[:32]


def verify_gbrain_live(ctx: PhaseContext, *, runner: CommandRunner = _run_command) -> dict[str, Any]:
    """Import and query only the target corpus through an isolated GBrain source."""
    source_id = _proof_source_id(ctx) if ctx.clean_root else "corpora"
    proof_source = ctx.clean_root
    base_env = {"GBRAIN_DISABLE_DIRECT_POOL": "1", "GBRAIN_SOURCE": source_id}
    added = False
    try:
        if proof_source:
            listing = _require_ok(runner(["gbrain", "sources", "list"], None, None), "gbrain source list")
            if source_id not in listing:
                _require_ok(
                    runner(["gbrain", "sources", "add", source_id, "--path", str(ctx.corpora_base)], None, None),
                    "gbrain source add",
                )
                added = True
        _require_ok(
            runner(["gbrain", "import", str(ctx.corpora_base), "--no-embed"], base_env, None),
            "gbrain import",
        )
        query = _require_ok(
            runner(["gbrain", "search", ctx.domain, "--limit", "5"], base_env, None),
            "gbrain source-scoped search",
        )
        if not query.strip():
            raise StartOrchestrationError("GBrain source-scoped search returned no target corpus result")
        return {
            "verified": True,
            "source_id": source_id,
            "query": ctx.domain,
            "result_readback": True,
            "temporary_source": proof_source,
        }
    finally:
        if proof_source and added:
            # The source and its rows were created solely for this clean proof.
            # Cleanup keeps the default brain free of proof artifacts.
            runner(["gbrain", "sources", "remove", source_id, "--confirm-destructive"], None, None)


def verify_scheduler_live(ctx: PhaseContext, *, runner: CommandRunner = _run_command) -> dict[str, Any]:
    """Verify one global scheduler owner and an install-ready generated spec."""
    output = _require_ok(runner(["hermes", "cron", "list", "--all"], None, None), "Hermes cron list")
    blocks = [block for block in output.split("\n\n") if "Name:" in block]
    owners = [
        block for block in blocks
        if "Name:      corpus-engine-cycle" in block and "[active]" in block
    ]
    if len(owners) != 1:
        raise StartOrchestrationError(
            f"exactly one active corpus-engine-cycle scheduler is required, found {len(owners)}"
        )
    if not ctx.spec_path.is_file():
        raise StartOrchestrationError("generated domain spec is not install-ready")
    return {
        "owner_count": 1,
        "scheduler_name": "corpus-engine-cycle",
        "install_ready_spec": str(ctx.spec_path.relative_to(ctx.run_root)),
    }


def make_live_boundaries(*, runner: CommandRunner = _run_command) -> StartBoundaries:
    """Construct real default boundaries while retaining dependency injection."""
    from corpus_atlas_adapter import build_atlas_launch_config, instantiate_atlas

    def atlas(ctx: PhaseContext) -> dict[str, Any]:
        config = build_atlas_launch_config(
            run_root=ctx.run_root, domain=ctx.domain, title=ctx.title,
            corpus_root=ctx.corpus_root,
        )
        return instantiate_atlas(config)

    return StartBoundaries(
        acquire=lambda ctx: acquire_live(ctx, runner=runner),
        verify_gbrain=lambda ctx: verify_gbrain_live(ctx, runner=runner),
        verify_atlas=atlas,
        verify_scheduler=lambda ctx: verify_scheduler_live(ctx, runner=runner),
    )
