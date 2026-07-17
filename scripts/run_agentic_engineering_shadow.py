#!/usr/bin/env python3
"""Run one real, bounded Agentic Engineering shadow vertical.

The V1 shadow candidate intentionally starts with the MIT-licensed AgentDojo
repository: it is public, rights-clear, directly relevant to tool-agent prompt
injection, and absent from the current corpus source cards. The second candidate
is retained only as the deterministic next gap. No source is auto-promoted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_engine_models import CandidateObservation, CandidateRecord
from corpus_priority import PriorityPolicy
from corpus_shadow import ShadowCyclePaths, run_shadow_cycle

CORPUS_ROOT = Path("/root/corpora/agentic-engineering")
ARCHIVE_ROOT = Path("/root/exports/thinker-corpora/agentic-engineering")
STATE_ROOT = ARCHIVE_ROOT / "shadow-v1"
UA = "GBrainCorpusEngine/1.0"
SCORES = {
    "authority": 0.95,
    "demonstrated_practice": 0.85,
    "novelty": 0.9,
    "relevance": 0.95,
    "corroboration": 0.75,
    "production_or_scientific_value": 0.95,
    "cost": 0.1,
}


def _get(url: str, *, accept: str = "application/vnd.github+json") -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read()


def _json(url: str) -> dict[str, Any]:
    value = json.loads(_get(url))
    if not isinstance(value, dict):
        raise RuntimeError(f"GitHub response is not an object: {url}")
    return value


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _candidate(repo: str, lane: str, topics: tuple[str, ...], observed_at: str, score_scale: float) -> CandidateRecord:
    observation = CandidateObservation.create(
        domain="agentic-engineering",
        entity_type="repository",
        canonical_url=f"https://github.com/{repo}",
        discovery_source="agentic-engineering-reviewed-seed-v1",
        evidence_pointer=f"https://api.github.com/repos/{repo}",
        evidence_lane=lane,
        topics=topics,
        observed_at=observed_at,
    )
    scores = {key: min(value * score_scale, 1.0) for key, value in SCORES.items()}
    return CandidateRecord.from_observation(
        observation,
        scores,
        rationale="Reviewed public repository with an SPDX-recognized permissive license.",
        rights_state="public_rights_clear",
    )


def build_candidates(now: datetime) -> list[CandidateRecord]:
    del now
    # Candidate identity is anchored to the reviewed seed revision, not process
    # wall time. A restart hours later must reconstruct the exact input contract.
    observed_at = "2026-07-16T00:00:00Z"
    return [
        _candidate(
            "ethz-spylab/agentdojo", "security-evaluation",
            ("prompt-injection", "tool-security", "security-evaluation", "utility-tradeoffs"),
            observed_at, 1.0,
        ),
        _candidate(
            "Aider-AI/aider", "practitioner-implementation",
            ("coding-agents", "edit-formats", "repository-maps", "benchmarks"),
            observed_at, 0.78,
        ),
    ]


def acquire_agentdojo(selected: CandidateRecord, paths: ShadowCyclePaths) -> Mapping[str, Any]:
    if selected.canonical_url != "https://github.com/ethz-spylab/agentdojo":
        raise RuntimeError("V1 live acquirer is bounded to the reviewed AgentDojo package")
    repo = "ethz-spylab/agentdojo"
    metadata_raw = _get(f"https://api.github.com/repos/{repo}")
    metadata = json.loads(metadata_raw)
    license_value = metadata.get("license") or {}
    if license_value.get("spdx_id") != "MIT":
        raise RuntimeError("AgentDojo rights gate failed: repository no longer reports MIT")
    default_branch = metadata.get("default_branch")
    commit_raw = _get(f"https://api.github.com/repos/{repo}/commits/{default_branch}")
    commit = json.loads(commit_raw)
    revision = commit.get("sha")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError("GitHub did not return an immutable commit SHA")
    readme_url = f"https://raw.githubusercontent.com/{repo}/{revision}/README.md"
    license_url = f"https://raw.githubusercontent.com/{repo}/{revision}/LICENSE"
    readme = _get(readme_url, accept="text/plain")
    license_bytes = _get(license_url, accept="text/plain")
    if b"MIT License" not in license_bytes or len(readme) < 1000:
        raise RuntimeError("AgentDojo minimum-content or license-byte gate failed")

    package_root = paths.archive_root / "raw" / "github" / "agentdojo" / revision
    metadata_path = package_root / "repository.json"
    commit_path = package_root / "commit.json"
    readme_path = package_root / "README.md"
    license_path = package_root / "LICENSE"
    for path, content in (
        (metadata_path, metadata_raw), (commit_path, commit_raw),
        (readme_path, readme), (license_path, license_bytes),
    ):
        _atomic_bytes(path, content)
    normalized_path = paths.archive_root / "normalized" / "github" / "agentdojo" / f"{revision}.md"
    _atomic_bytes(normalized_path, readme)

    retrieved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    page_path = paths.corpus_root / "sources" / "security-evaluation" / "agentdojo.md"
    readme_text = readme.decode("utf-8", errors="replace")
    page = f'''---
title: "AgentDojo — Prompt Injection Evaluation Environment"
type: "source"
domain: "agentic-engineering"
source_id: "corpora"
source_url: "https://github.com/{repo}"
source_revision: "{revision}"
rights_state: "public_rights_clear"
license: "MIT"
evidence_lane: "security-evaluation"
evidence_class: "scientific-benchmark"
epistemic_layer: "primary_source"
candidate_status: "probationary"
raw_storage_path: "{readme_path}"
raw_sha256: "{hashlib.sha256(readme).hexdigest()}"
normalized_storage_path: "{normalized_path}"
normalized_sha256: "{hashlib.sha256(readme).hexdigest()}"
retrieved_at: "{retrieved_at}"
privacy: "private"
---

# AgentDojo — Prompt Injection Evaluation Environment

> **External evidence boundary:** This is MIT-licensed benchmark source material. It is not Sameer's adopted doctrine; the shadow cycle may create only a versioned external-corpus synthesis proposal.

- Repository: <https://github.com/{repo}>
- Immutable revision: [`{revision}`](https://github.com/{repo}/tree/{revision})
- Archived README: `{readme_path}`
- Archived license: `{license_path}`

## Preserved README

{readme_text}
'''
    _atomic_bytes(page_path, page.encode("utf-8"))
    claim = "AgentDojo provides a dynamic environment to evaluate prompt-injection attacks and defenses for LLM agents."
    record = {
        "record_id": selected.candidate_id,
        "source_id": "corpora",
        "page_slug": "agentic-engineering/sources/security-evaluation/agentdojo",
        "locator": "README:title-and-running-the-benchmark",
        "claim_sha256": hashlib.sha256(claim.encode("utf-8")).hexdigest(),
        "raw_pointer": str(readme_path),
        "raw_sha256": hashlib.sha256(readme).hexdigest(),
        "normalized_pointer": str(normalized_path),
        "normalized_sha256": hashlib.sha256(readme).hexdigest(),
        "canonical_url": selected.canonical_url,
        "source_revision": revision,
        "retrieved_at": retrieved_at,
        "evidence_lane": "security-evaluation",
        "evidence_class": "scientific-benchmark",
        "epistemic_layer": "primary_source",
        "rights_state": "public_rights_clear",
        "candidate_status": "probationary",
        "contradiction_group": "tool-agent-security-evaluation",
        "contradiction_stance": "supports-adversarial-evaluation",
    }
    return {
        "record": record,
        "doctrine": {
            "concept_key": "tool-agent-adversarial-evaluation",
            "title": "Tool-Agent Adversarial Evaluation",
            "statement": "Tool-enabled agents should be evaluated against prompt-injection attacks and defenses in an executable task environment before higher autonomy is earned.",
            "rationale": "AgentDojo adds a rights-clear executable benchmark lane for prompt-injection attacks, defenses and utility tasks; this is a probationary external synthesis, not adopted doctrine.",
        },
        "page_path": str(page_path),
    }


def commit_corpus_page(repo_root: Path, page_path: Path, *, cycle_id: str) -> str:
    repo = repo_root.resolve()
    page = page_path.resolve()
    try:
        relative = page.relative_to(repo)
    except ValueError as exc:
        raise RuntimeError("corpus page is outside the corpus repository") from exc
    if page_path.is_symlink() or not page.is_file():
        raise RuntimeError("corpus page must be a regular non-symlink file")
    subprocess.run(["git", "-C", str(repo), "add", "--", str(relative)], check=True)
    changed = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--quiet", "--", str(relative)]
    )
    if changed.returncode not in {0, 1}:
        raise RuntimeError("git could not inspect the intentional corpus page")
    if changed.returncode == 1:
        subprocess.run(
            ["git", "-C", str(repo), "commit", "--only", "-m", f"corpus: AgentDojo shadow evidence ({cycle_id})", "--", str(relative)],
            check=True,
        )
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()


def sync_corpora(package: Mapping[str, Any]) -> Mapping[str, Any]:
    env = dict(os.environ)
    env["GBRAIN_DISABLE_DIRECT_POOL"] = "1"
    page = Path(package["page_path"])
    repo_root = Path(subprocess.check_output(
        ["git", "-C", str(page.parent), "rev-parse", "--show-toplevel"], text=True
    ).strip())
    corpus_commit = commit_corpus_page(repo_root, page, cycle_id=package["record"]["record_id"])
    process = subprocess.run(
        ["gbrain", "sync", "--source", "corpora", "--no-pull", "--json", "--yes"],
        text=True, capture_output=True, env=env, timeout=300,
    )
    if process.returncode:
        raise RuntimeError(f"gbrain corpora sync failed: {(process.stderr or process.stdout)[-2000:]}")
    return {
        "source_id": "corpora",
        "changed_pages": [package["record"]["page_slug"]],
        "corpus_commit": corpus_commit,
        "page_sha256": hashlib.sha256(page.read_bytes()).hexdigest(),
        "gbrain_stdout_sha256": hashlib.sha256(process.stdout.encode("utf-8")).hexdigest(),
    }


def retrieve_corpora(package: Mapping[str, Any]) -> list[Mapping[str, str]]:
    env = dict(os.environ)
    env["GBRAIN_DISABLE_DIRECT_POOL"] = "1"
    process = subprocess.run(
        ["gbrain", "query", "AgentDojo Dynamic Environment Evaluate Prompt Injection Attacks Defenses LLM Agents", "--source-id", "corpora", "--limit", "20", "--detail", "high"],
        text=True, capture_output=True, env=env, timeout=120,
    )
    if process.returncode:
        raise RuntimeError(f"gbrain source-scoped retrieval failed: {(process.stderr or process.stdout)[-2000:]}")
    results: list[Mapping[str, str]] = []
    for line in process.stdout.splitlines():
        match = re.match(r"^\[[0-9.]+\]\s+(\S+)\s+--", line)
        if match:
            results.append({"source_id": "corpora", "page_slug": match.group(1)})
    if not results:
        raise RuntimeError("gbrain source-scoped retrieval returned no parseable results")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle-id", default=datetime.now(timezone.utc).strftime("shadow-%Y%m%dT%H%M%SZ"))
    parser.add_argument("--state-root", type=Path, default=STATE_ROOT)
    parser.add_argument("--corpus-root", type=Path, default=CORPUS_ROOT)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    args = parser.parse_args()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    receipt = run_shadow_cycle(
        cycle_id=args.cycle_id,
        candidates=build_candidates(now),
        policy=PriorityPolicy.from_file(ROOT / "config" / "agentic-engineering-policy.json"),
        paths=ShadowCyclePaths(args.state_root, args.corpus_root, args.archive_root),
        now=now,
        acquire=acquire_agentdojo,
        sync_corpus=sync_corpora,
        retrieve=retrieve_corpora,
    )
    print(json.dumps(receipt, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
