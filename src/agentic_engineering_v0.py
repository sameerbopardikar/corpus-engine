#!/usr/bin/env python3
"""Same-day personal V0 for the Agentic Engineering corpus.

This is intentionally narrower than the fully autonomous engine. It loads the
reviewed candidate seed into the durable discovery ledger, measures deterministic
topic coverage against the current corpus, ranks gaps, queues bounded inspection
work, and writes inspectable JSON/Markdown projections. It never promotes a
candidate or mutates Expert/Percival production.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from corpus_discovery import DiscoveryEngine, score_observation
from corpus_engine_models import CandidateRecord
from corpus_seed_loader import CandidateSeedBundle, ingest_candidate_seed, load_candidate_seed

DEFAULT_CORPUS_ROOT = Path("/root/corpora/agentic-engineering")
DEFAULT_STATE_ROOT = Path("/root/exports/thinker-corpora/agentic-engineering/self-expansion-v1")
DEFAULT_OUTPUT_ROOT = DEFAULT_CORPUS_ROOT / "discovery" / "personal-v0"
DEFAULT_SEED = Path(__file__).resolve().parents[1] / "docs" / "source-maps" / "agentic-engineering-seed-candidates.json"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_cycle_id(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-")
    if not normalized:
        raise ValueError("cycle_id must contain a safe non-empty identifier")
    return normalized


def _corpus_pages(corpus_root: Path) -> list[tuple[str, str]]:
    pages: list[tuple[str, str]] = []
    for path in sorted(corpus_root.rglob("*.md")):
        relative = path.relative_to(corpus_root)
        if relative.parts[:2] == ("discovery", "personal-v0"):
            continue
        try:
            text = path.read_text(encoding="utf-8").lower()
        except (OSError, UnicodeDecodeError):
            continue
        pages.append((str(relative), text))
    return pages


def _source_card_states(corpus_root: Path) -> dict[str, dict[str, str]]:
    fields = {"source_url", "source_revision", "rights_state", "candidate_status"}
    states: dict[str, dict[str, str]] = {}
    for path in sorted(corpus_root.rglob("*.md")):
        relative = path.relative_to(corpus_root)
        if relative.parts[:2] == ("discovery", "personal-v0"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            continue
        values: dict[str, str] = {}
        for line in lines[1:]:
            if line.strip() == "---":
                break
            key, separator, raw_value = line.partition(":")
            if separator and key.strip() in fields:
                value = raw_value.strip().strip('"').strip("'")
                if value:
                    values[key.strip()] = value
        source_url = values.get("source_url")
        if not source_url or "source_revision" not in values:
            continue
        state = {
            "source_page": str(relative),
            "source_revision": values["source_revision"],
            "rights_state": values.get("rights_state", "unknown"),
            "status": values.get("candidate_status", "acquired"),
        }
        canonical_url = source_url.rstrip("/")
        prior = states.setdefault(canonical_url, state)
        if prior != state:
            raise ValueError(f"conflicting acquired source cards for {canonical_url}")
    return states


def _topic_pattern(topic: str) -> re.Pattern[str]:
    words = [re.escape(part) for part in re.split(r"[-_\s]+", topic.lower()) if part]
    return re.compile(r"\b" + r"[-_\s]+".join(words) + r"\b")


def _topic_page_hits(topic: str, pages: Iterable[tuple[str, str]]) -> int:
    pattern = _topic_pattern(topic)
    return sum(1 for _, text in pages if pattern.search(text))


def _records_from_bundle(bundle: CandidateSeedBundle, seed_path: Path) -> list[CandidateRecord]:
    records: list[CandidateRecord] = []
    for observation in bundle.observations(source_ref=str(seed_path)):
        scores, rationale = score_observation(observation)
        records.append(CandidateRecord.from_observation(observation, scores, rationale=rationale))
    return records


def _rank_candidates(
    bundle: CandidateSeedBundle,
    records: Iterable[CandidateRecord],
    pages: list[tuple[str, str]],
    source_states: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    observations = bundle.observations(source_ref="candidate-ranking")
    seeds_by_candidate_id = {
        observation.candidate_key: seed
        for seed, observation in zip(bundle.candidates, observations, strict=True)
    }
    ranked: list[dict[str, Any]] = []
    for record in records:
        seed = seeds_by_candidate_id[record.candidate_id]
        source_state = source_states.get(record.canonical_url.rstrip("/"))
        topic_hits = {topic: _topic_page_hits(topic, pages) for topic in record.topics}
        gap_score = sum(1.0 / (1 + hits) for hits in topic_hits.values()) / max(len(topic_hits), 1)
        base_score = record.compute_score()["total"]
        priority_score = base_score * (1.0 + gap_score)
        ranked.append(
            {
                "seed_id": seed.seed_id,
                "candidate_id": record.candidate_id,
                "entity_type": record.entity_type,
                "canonical_url": record.canonical_url,
                "source_type": seed.source_type,
                "evidence_lane": record.evidence_lane,
                "authority_tier": seed.authority_tier,
                "refresh_class": seed.refresh_class,
                "rights_state": source_state["rights_state"] if source_state else record.rights_state,
                "topics": list(record.topics),
                "topic_page_hits": topic_hits,
                "base_score": round(base_score, 8),
                "gap_score": round(gap_score, 8),
                "priority_score": round(priority_score, 8),
                "status": source_state["status"] if source_state else record.status,
                "acquired": source_state is not None,
                "source_revision": source_state["source_revision"] if source_state else None,
                "source_page": source_state["source_page"] if source_state else None,
            }
        )
    ranked.sort(key=lambda item: (-item["priority_score"], item["seed_id"]))
    return ranked


def _doctrine_proposals(ranked: Iterable[dict[str, Any]], limit: int = 12) -> list[str]:
    gap_counts: Counter[str] = Counter()
    for item in ranked:
        for topic, hits in item["topic_page_hits"].items():
            if hits == 0:
                gap_counts[topic] += 1
    return [topic for topic, _ in sorted(gap_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    setfacl = shutil.which("setfacl")
    if setfacl:
        subprocess.run(
            [setfacl, "-m", "u:agentic-dashboard:rX,d:u:agentic-dashboard:rX", str(path.parent)],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [setfacl, "-m", "u:agentic-dashboard:r--", str(path)],
            capture_output=True,
            check=True,
        )


def _render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "---",
        'type: "report"',
        'title: "Agentic Engineering Personal V0 — Latest Cycle"',
        'privacy: "private"',
        f'effective_date: "{result["generated_at"]}"',
        'status: "personal-v0-candidate-ranking"',
        "---",
        "",
        "# Agentic Engineering Personal V0 — Latest Cycle",
        "",
        f"- Cycle: `{result['cycle_id']}`",
        f"- Candidates: **{result['candidate_count']}**",
        f"- Existing Markdown pages scanned: **{result['corpus_page_count']}**",
        f"- Bounded inspect work queued: **{result['queued_work_count']}**",
        "- Promotion: **disabled**",
        "- Expert/Percival production mutation: **none**",
        "",
        "## Next highest-value evidence",
        "",
    ]
    for index, item in enumerate(result["ranked_candidates"][:10], start=1):
        gaps = [topic for topic, hits in item["topic_page_hits"].items() if hits == 0]
        lines.extend(
            [
                f"{index}. **{item['seed_id']}** — `{item['evidence_lane']}` — priority `{item['priority_score']}`",
                f"   - {item['canonical_url']}",
                f"   - Uncovered topics: {', '.join(gaps) if gaps else 'none by deterministic lexical check'}",
            ]
        )
        if item["acquired"]:
            lines.append(
                f"   - Acquisition: `{item['status']}`; rights: `{item['rights_state']}`; "
                f"revision: `{item['source_revision']}`; page: `{item['source_page']}`"
            )
    lines.extend(["", "## Probationary doctrine concepts", ""])
    if result["doctrine_proposals"]:
        lines.extend(f"- `{topic}`" for topic in result["doctrine_proposals"])
    else:
        lines.append("- None detected by this bounded lexical pass.")
    lines.extend(
        [
            "",
            "## Boundary",
            "",
            "These are inspection and doctrine-structure candidates, not accepted doctrine. V0 does not promote sources or run production experiments.",
            "",
        ]
    )
    return "\n".join(lines)


def run_v0(
    *,
    seed_path: Path,
    corpus_root: Path,
    state_root: Path,
    output_root: Path,
    cycle_id: str,
    queue_top: int = 3,
    dry_run: bool = False,
) -> dict[str, Any]:
    if queue_top < 0:
        raise ValueError("queue_top must be non-negative")
    safe_cycle = _safe_cycle_id(cycle_id)
    bundle = load_candidate_seed(seed_path)
    pages = _corpus_pages(corpus_root)
    source_states = _source_card_states(corpus_root)
    engine: DiscoveryEngine | None = None
    queued_work_ids: list[str] = []

    if dry_run:
        records = _records_from_bundle(bundle, seed_path)
    else:
        engine = DiscoveryEngine(state_root / "discovery-ledger.jsonl")
        ingest_candidate_seed(engine, bundle, source_ref=str(seed_path), dry_run=False)
        seed_candidate_ids = {
            observation.candidate_key
            for observation in bundle.observations(source_ref=str(seed_path))
        }
        records = [
            record
            for record in engine.candidates.values()
            if record.candidate_id in seed_candidate_ids
        ]

    ranked = _rank_candidates(bundle, records, pages, source_states)

    if not dry_run:
        assert engine is not None
        by_id = {record.candidate_id: record for record in records}
        for item in ranked[:queue_top]:
            record = by_id[item["candidate_id"]]
            work = engine.enqueue_work(
                domain=bundle.domain,
                candidate_id=record.candidate_id,
                action="inspect",
                score_components={**record.compute_score(), "priority_score": item["priority_score"]},
                budget_estimate=0.0,
                idempotency_key=f"{safe_cycle}:inspect",
            )
            queued_work_ids.append(work.work_id)

    result = {
        "schema_version": 1,
        "product": "agentic-engineering-corpus-personal-v0",
        "cycle_id": safe_cycle,
        "generated_at": _utc_iso(),
        "dry_run": dry_run,
        "candidate_count": len(ranked),
        "corpus_page_count": len(pages),
        "queued_work_count": len(queued_work_ids),
        "queued_work_ids": queued_work_ids,
        "doctrine_proposals": _doctrine_proposals(ranked),
        "ranked_candidates": ranked,
        "promotion_enabled": False,
        "production_mutation": False,
    }
    if not dry_run:
        encoded = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        _atomic_write(output_root / f"cycle-{safe_cycle}.json", encoded)
        _atomic_write(output_root / "latest.json", encoded)
        _atomic_write(output_root / "latest.md", _render_markdown(result))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--corpus-root", type=Path, default=DEFAULT_CORPUS_ROOT)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--cycle-id", default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--queue-top", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = run_v0(
        seed_path=args.seed,
        corpus_root=args.corpus_root,
        state_root=args.state_root,
        output_root=args.output_root,
        cycle_id=args.cycle_id,
        queue_top=args.queue_top,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
