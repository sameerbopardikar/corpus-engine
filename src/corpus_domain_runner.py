#!/usr/bin/env python3
"""Generic, domain-agnostic corpus cycle driven entirely by a DomainSpec.

One deterministic bounded cycle: validate the spec and seed, rank evidence gaps,
project a domain-scoped field map and acquisition suggestions, propose or
explicitly decline a doctrine-structure change, run a deterministic domain
evaluation, and select the next gap. No domain-specific branches live here — a
new domain is a spec plus a seed, nothing more. The cycle never promotes
candidates, never adopts doctrine as belief, and never mutates production.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from corpus_discovery import DiscoveryEngine, score_observation
from corpus_doctrine import DoctrineEngine
from corpus_domain_spec import DomainSpec
from corpus_engine_models import CandidateRecord
from corpus_seed_loader import CandidateSeedBundle, load_candidate_seed

_FRONTMATTER_FIELDS = {"source_url", "source_revision", "rights_state", "evidence_lane", "candidate_status"}


class DomainCycleError(ValueError):
    """Raised when a domain cycle cannot run safely."""


def _safe_cycle_id(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-")
    if not normalized:
        raise DomainCycleError("cycle_id must contain a safe non-empty identifier")
    return normalized


def _topic_pattern(topic: str) -> re.Pattern[str]:
    words = [re.escape(part) for part in re.split(r"[-_\s]+", topic.lower()) if part]
    return re.compile(r"\b" + r"[-_\s]+".join(words) + r"\b")


def _scan_pages(corpus_root: Path, output_root: Path) -> list[tuple[str, str, dict[str, str]]]:
    """Return (relpath, lowered_text, frontmatter) for every corpus page.

    The domain's own projection subtree is skipped so the cycle never scores its
    own output.
    """
    pages: list[tuple[str, str, dict[str, str]]] = []
    if not corpus_root.exists():
        return pages
    try:
        output_rel = output_root.resolve().relative_to(corpus_root.resolve())
        skip_parts = output_rel.parts
    except ValueError:
        skip_parts = ()
    for path in sorted(corpus_root.rglob("*.md")):
        relative = path.relative_to(corpus_root)
        if skip_parts and relative.parts[: len(skip_parts)] == skip_parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        pages.append((str(relative), text.lower(), _frontmatter(text)))
    return pages


def _frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    values: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, raw = line.partition(":")
        if sep and key.strip() in _FRONTMATTER_FIELDS:
            value = raw.strip().strip('"').strip("'")
            if value:
                values[key.strip()] = value
    return values


def _topic_hits(topic: str, pages: Iterable[tuple[str, str, dict[str, str]]]) -> int:
    pattern = _topic_pattern(topic)
    return sum(1 for _, text, _ in pages if pattern.search(text))


def _rank_candidates(
    bundle: CandidateSeedBundle,
    pages: list[tuple[str, str, dict[str, str]]],
    seed_ref: str,
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for seed, observation in zip(bundle.candidates, bundle.observations(source_ref=seed_ref), strict=True):
        scores, rationale = score_observation(observation)
        record = CandidateRecord.from_observation(observation, scores, rationale=rationale)
        topic_hits = {topic: _topic_hits(topic, pages) for topic in record.topics}
        gap_score = sum(1.0 / (1 + hits) for hits in topic_hits.values()) / max(len(topic_hits), 1)
        base_score = record.compute_score()["total"]
        priority_score = base_score * (1.0 + gap_score)
        ranked.append(
            {
                "seed_id": seed.seed_id,
                "candidate_id": record.candidate_id,
                "canonical_url": record.canonical_url,
                "evidence_lane": record.evidence_lane,
                "topics": list(record.topics),
                "topic_page_hits": topic_hits,
                "base_score": round(base_score, 8),
                "gap_score": round(gap_score, 8),
                "priority_score": round(priority_score, 8),
            }
        )
    ranked.sort(key=lambda item: (-item["priority_score"], item["seed_id"]))
    return ranked


def _build_field_map(spec: DomainSpec, pages: list[tuple[str, str, dict[str, str]]]) -> dict[str, Any]:
    field_map: dict[str, Any] = {}
    for axis in spec.ontology_axes:
        topic_hits = {topic: _topic_hits(topic, pages) for topic in axis.topics}
        covered = [topic for topic, hits in topic_hits.items() if hits > 0]
        field_map[axis.key] = {
            "title": axis.title,
            "topic_page_hits": topic_hits,
            "covered_topics": covered,
            "uncovered_topics": [topic for topic in axis.topics if topic_hits[topic] == 0],
            "coverage_ratio": round(len(covered) / len(axis.topics), 8),
        }
    return field_map


def _rights_clear_axis_coverage(
    spec: DomainSpec, pages: list[tuple[str, str, dict[str, str]]]
) -> dict[str, tuple[int, str | None, str]]:
    """For each axis, count matched topics in rights-cleared pages and note the best page."""
    result: dict[str, tuple[int, str | None, str]] = {}
    clear_pages = [
        (relpath, text, front)
        for relpath, text, front in pages
        if front.get("rights_state") == "public_rights_clear"
    ]
    for axis in spec.ontology_axes:
        best_page: str | None = None
        best_lane = "attributed_source_observation"
        best_matches = 0
        for relpath, text, front in sorted(clear_pages):
            matches = sum(1 for topic in axis.topics if _topic_pattern(topic).search(text))
            if matches > best_matches:
                best_matches = matches
                best_page = relpath
                best_lane = front.get("evidence_lane", best_lane)
        result[axis.key] = (best_matches, best_page, best_lane)
    return result


def _doctrine_decision(
    spec: DomainSpec,
    coverage: dict[str, tuple[int, str | None, str]],
    corpus_root: Path,
    doctrine: DoctrineEngine | None,
    cycle_id: str,
) -> dict[str, Any]:
    citeable = [
        (axis, coverage[axis.key])
        for axis in spec.ontology_axes
        if coverage[axis.key][0] > 0 and coverage[axis.key][1] is not None
    ]
    if not citeable:
        return {
            "decision": "declined",
            "reason": "no rights-cleared corpus page covers an ontology axis; doctrine structure unchanged this cycle",
        }
    # Most-covered citeable axis, ties broken by spec order (stable sort).
    citeable.sort(key=lambda pair: -pair[1][0])
    for axis, (matches, page_rel, evidence_lane) in citeable:
        assert page_rel is not None
        page_text = (corpus_root / page_rel).read_text(encoding="utf-8")
        claim_sha = hashlib.sha256(page_text.encode("utf-8")).hexdigest()
        citation = {
            "source_id": "corpora",
            "page_slug": page_rel[:-3] if page_rel.endswith(".md") else page_rel,
            "locator": page_rel,
            "claim_sha256": claim_sha,
            "evidence_class": evidence_lane,
        }
        if doctrine is None:
            # Dry-run preview: report the change that a real cycle would make.
            return {
                "decision": "proposed",
                "concept_key": axis.key,
                "status": "probationary",
                "version": 1,
                "sameer_adopted": False,
                "epistemic_layer": "external_corpus_synthesis",
                "citation": citation,
            }
        try:
            concept = doctrine.propose(
                command_id=f"{cycle_id}:{axis.key}",
                concept_key=axis.key,
                title=axis.title,
                statement=(
                    f"{axis.title} is a provisional organizing axis for the {spec.domain} corpus, "
                    f"covered by {matches} rights-cleared topic(s)."
                ),
                citations=[citation],
                rationale=f"Rights-cleared evidence covers {matches} topic(s) of axis {axis.key}.",
            )
        except ValueError:
            # Concept key already established by a different command/cycle; try the next axis.
            continue
        return {
            "decision": "proposed",
            "concept_key": concept["concept_key"],
            "status": concept["status"],
            "version": concept["version"],
            "sameer_adopted": concept["sameer_adopted"],
            "epistemic_layer": concept["epistemic_layer"],
            "citation": citation,
        }
    return {
        "decision": "declined",
        "reason": "all citeable ontology axes already have doctrine concepts; doctrine structure unchanged this cycle",
    }


def _select_next_gap(spec: DomainSpec, field_map: dict[str, Any]) -> dict[str, Any] | None:
    ranked_axes = sorted(
        spec.ontology_axes,
        key=lambda axis: (-len(field_map[axis.key]["uncovered_topics"]), axis.key),
    )
    for axis in ranked_axes:
        uncovered = field_map[axis.key]["uncovered_topics"]
        if uncovered:
            return {
                "axis": axis.key,
                "uncovered_topics": list(uncovered),
                "representative_topic": uncovered[0],
            }
    return None


def _evaluate(
    spec: DomainSpec,
    field_map: dict[str, Any],
    pages_scanned: int,
    next_gap: dict[str, Any] | None,
    doctrine_decision: dict[str, Any],
) -> dict[str, Any]:
    requirements = spec.eval_requirements
    checks: dict[str, bool] = {}
    checks["min_corpus_pages_scanned"] = pages_scanned >= requirements["min_corpus_pages_scanned"]
    if requirements["require_axes_in_field_map"]:
        checks["axes_in_field_map"] = all(axis.key in field_map for axis in spec.ontology_axes)
    if requirements["require_next_gap"]:
        checks["next_gap_selected"] = next_gap is not None
    if requirements["forbid_private_export_to_shared"]:
        citation = doctrine_decision.get("citation")
        checks["doctrine_citation_rights_clear"] = citation is None or citation["source_id"] == "corpora"
    failures = [name for name, ok in checks.items() if not ok]
    return {"passed": not failures, "failures": failures, "checks": checks}


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def _render_markdown(receipt: dict[str, Any]) -> str:
    lines = [
        "---",
        'type: "report"',
        f'title: "{receipt["title"]} — Domain Cycle"',
        'privacy: "private"',
        f'effective_date: "{receipt["generated_at"]}"',
        'status: "generalized-domain-cycle"',
        "---",
        "",
        f"# {receipt['title']} — Domain Cycle",
        "",
        f"- Domain: `{receipt['domain']}`",
        f"- Cycle: `{receipt['cycle_id']}`",
        f"- Corpus pages scanned: **{receipt['validate']['corpus_pages_scanned']}**",
        f"- Doctrine decision: **{receipt['doctrine']['decision']}**",
        f"- Evaluation passed: **{receipt['evaluate']['passed']}**",
        "- Promotion: **disabled**",
        "- Production mutation: **none**",
        "",
        "## Field map coverage",
        "",
    ]
    for axis_key, axis in receipt["project"]["field_map"].items():
        lines.append(f"- **{axis['title']}** (`{axis_key}`): coverage `{axis['coverage_ratio']}`; "
                     f"uncovered: {', '.join(axis['uncovered_topics']) or 'none'}")
    gap = receipt["next_gap"]
    lines.extend(["", "## Next gap", "", f"- {gap['axis']}: {gap['representative_topic']}" if gap else "- none", ""])
    return "\n".join(lines)


def _receipt_sha256(receipt: dict[str, Any]) -> str:
    payload = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def run_domain_cycle(
    *,
    spec: DomainSpec,
    seed_path: Path | str,
    corpus_root: Path | str,
    state_root: Path | str,
    output_root: Path | str,
    cycle_id: str,
    now: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    corpus_root = Path(corpus_root)
    state_root = Path(state_root)
    output_root = Path(output_root)
    safe_cycle = _safe_cycle_id(cycle_id)

    bundle = load_candidate_seed(seed_path)
    if bundle.domain != spec.domain:
        raise DomainCycleError(f"seed domain {bundle.domain!r} does not match spec domain {spec.domain!r}")

    pages = _scan_pages(corpus_root, output_root)
    ranked = _rank_candidates(bundle, pages, str(seed_path))
    field_map = _build_field_map(spec, pages)
    coverage = _rights_clear_axis_coverage(spec, pages)

    discovery: DiscoveryEngine | None = None
    doctrine: DoctrineEngine | None = None
    queued_work_ids: list[str] = []
    if not dry_run:
        discovery = DiscoveryEngine(state_root / "discovery-ledger.jsonl")
        doctrine = DoctrineEngine(state_root / "doctrine-ledger.jsonl")
        for observation in bundle.observations(source_ref=str(seed_path)):
            discovery.observe(observation)
        by_seed = {item["candidate_id"]: item for item in ranked}
        for item in ranked[: spec.bootstrap["max_items_per_cycle"]]:
            record = discovery.candidates[item["candidate_id"]]
            work = discovery.enqueue_work(
                domain=spec.domain,
                candidate_id=record.candidate_id,
                action="inspect",
                score_components={**record.compute_score(), "priority_score": by_seed[record.candidate_id]["priority_score"]},
                budget_estimate=0.0,
                idempotency_key=f"{safe_cycle}:inspect",
            )
            queued_work_ids.append(work.work_id)

    doctrine_decision = _doctrine_decision(spec, coverage, corpus_root, doctrine, safe_cycle)
    next_gap = _select_next_gap(spec, field_map)
    evaluation = _evaluate(spec, field_map, len(pages), next_gap, doctrine_decision)

    acquisition_suggestions = [
        {
            "seed_id": item["seed_id"],
            "canonical_url": item["canonical_url"],
            "evidence_lane": item["evidence_lane"],
            "priority_score": item["priority_score"],
            "uncovered_topics": [topic for topic, hits in item["topic_page_hits"].items() if hits == 0],
        }
        for item in ranked[: spec.acquisition_policy["max_suggestions"]]
    ]

    receipt: dict[str, Any] = {
        "schema_version": 1,
        "domain": spec.domain,
        "title": spec.title,
        "cycle_id": safe_cycle,
        "generated_at": now,
        "dry_run": dry_run,
        "validate": {
            "ok": True,
            "seed_domain": bundle.domain,
            "candidate_count": len(bundle.candidates),
            "corpus_pages_scanned": len(pages),
        },
        "rank": {
            "ranked_candidates": ranked,
            "queued_work_ids": queued_work_ids,
        },
        "project": {
            "field_map": field_map,
            "acquisition_suggestions": acquisition_suggestions,
        },
        "doctrine": doctrine_decision,
        "evaluate": evaluation,
        "next_gap": next_gap,
        "promotion_enabled": False,
        "production_mutation": False,
    }
    receipt["receipt_sha256"] = _receipt_sha256(receipt)

    if not dry_run:
        encoded = json.dumps(receipt, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        _atomic_write(output_root / f"cycle-{safe_cycle}.json", encoded)
        _atomic_write(output_root / "latest.json", encoded)
        _atomic_write(output_root / "latest.md", _render_markdown(receipt))

    return receipt
