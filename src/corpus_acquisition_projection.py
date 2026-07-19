#!/usr/bin/env python3
"""Rights-safe UI projection for the acquisition loop.

The projection is the single stable contract the Atlas reads. It is built purely
from executor receipts, the cycle's ranking, and any ranked-but-unexecuted
candidates, and it is *redacted by construction*: it exposes only public
locators, content-address digests, rights bases, dispositions, and timing. It
never carries a private filesystem path (raw/normalized/staged/quarantine
pointers), credentials, raw private text, or any model secret. Producing a card
is allow-list, not deny-list: only named safe fields are copied out of a receipt
or a pending descriptor, so a new field cannot silently leak.

Completeness: a per-cycle projection includes every ranked candidate, whether it
emitted a terminal receipt (acquired/gated/failed/metadata) *or* was ranked but
not executed this cycle (queued/deferred by the shared budget). Next-best
prioritizes actionable work only — queued/eligible candidates first, then human
gates — and never re-offers a completed, rejected, or failed card.
"""
from __future__ import annotations

from typing import Any

# Terminal dispositions emitted on receipts.
_ACQUIRED = {"probationary"}
_GATED = {"human_gate", "blocked"}
_FAILED = {"failed", "quarantined"}
# States for ranked-but-unexecuted candidates (no receipt yet). These are the
# actionable queue the engine can still fund and acquire on a future cycle.
_QUEUED = {"deferred", "queued", "eligible"}

_CARD_KEYS = (
    "candidate_id", "domain", "title", "topics", "canonical_locator", "evidence_lane",
    "source_family", "rights_state", "rights_basis", "disposition", "status",
    "acquired_at", "raw_sha256", "normalized_sha256", "staged_sha256",
    "canonical_sha256", "failure_reason", "why_now", "estimated_cost_usd",
    "gap_served", "priority_score",
)


def _blank_card() -> dict[str, Any]:
    return {key: None for key in _CARD_KEYS}


def _safe_card(receipt: dict[str, Any], ranked: dict[str, dict[str, Any]]) -> dict[str, Any]:
    candidate_id = receipt.get("candidate_id")
    enrichment = ranked.get(candidate_id, {}) if isinstance(ranked, dict) else {}
    rights = receipt.get("rights") or {}
    raw = receipt.get("raw") or {}
    normalized = receipt.get("normalized") or {}
    staged = receipt.get("staged") or {}
    canonical = receipt.get("canonical") or {}
    failure = receipt.get("failure") or {}
    card = _blank_card()
    card.update(
        candidate_id=candidate_id,
        domain=receipt.get("domain"),
        title=receipt.get("title"),
        topics=list(receipt.get("topics") or []),
        canonical_locator=receipt.get("canonical_locator"),
        evidence_lane=receipt.get("evidence_lane"),
        source_family=receipt.get("source_family"),
        rights_state=rights.get("rights_state"),
        rights_basis=rights.get("basis"),
        disposition=receipt.get("disposition"),
        status=receipt.get("terminal_state"),
        acquired_at=receipt.get("acquired_at"),
        # Content-address digests are safe (hashes, not paths).
        raw_sha256=raw.get("sha256"),
        normalized_sha256=normalized.get("sha256"),
        staged_sha256=staged.get("sha256"),
        canonical_sha256=canonical.get("sha256"),
        # Failure reason code only — never the detail string (may hold a path).
        failure_reason=failure.get("reason"),
        # Ranking enrichment (why-now / cost / gap served).
        why_now=enrichment.get("why_now"),
        estimated_cost_usd=enrichment.get("estimated_cost_usd", 0.0),
        gap_served=enrichment.get("gap_served"),
        priority_score=enrichment.get("priority_score"),
    )
    return card


def _pending_card(entry: dict[str, Any]) -> dict[str, Any]:
    """Allow-list a ranked-but-unexecuted candidate into a safe card.

    Only named public fields are copied; there is no receipt, so all
    content-address and filesystem fields stay ``None``.
    """
    card = _blank_card()
    card.update(
        candidate_id=entry.get("candidate_id"),
        domain=entry.get("domain"),
        title=entry.get("title"),
        topics=list(entry.get("topics") or []),
        canonical_locator=entry.get("canonical_locator"),
        evidence_lane=entry.get("evidence_lane"),
        source_family=entry.get("source_family"),
        rights_state=entry.get("rights_state"),
        rights_basis=entry.get("rights_basis"),
        disposition=entry.get("disposition", "deferred"),
        status=entry.get("status", "queued"),
        why_now=entry.get("why_now"),
        estimated_cost_usd=entry.get("estimated_cost_usd", 0.0),
        gap_served=entry.get("gap_served"),
        priority_score=entry.get("priority_score"),
    )
    return card


def _next_best_rank(card: dict[str, Any]) -> int | None:
    """Actionability rank for next-best (lower = higher priority); None = skip.

    Queued/eligible work outranks human gates. Completed (probationary),
    metadata-only, rejected, and failed cards are never actionable.
    """
    disposition = card["disposition"]
    if disposition in _QUEUED:
        return 0
    if disposition in _GATED:
        return 1
    return None


def build_acquisition_projection(
    *,
    receipts: list[dict[str, Any]],
    ranked_candidates: dict[str, dict[str, Any]] | None,
    corpus_revision: str,
    generated_at: str,
    cycle_time_seconds: float,
    pending: list[dict[str, Any]] | None = None,
    coverage_pressure: list[dict[str, Any]] | None = None,
    in_flight: list[dict[str, Any]] | None = None,
    domain: str | None = None,
    cycle_id: str | None = None,
) -> dict[str, Any]:
    ranked = ranked_candidates or {}
    receipt_cards = [_safe_card(receipt, ranked) for receipt in receipts]
    pending_cards = [_pending_card(entry) for entry in (pending or [])]
    cards = receipt_cards + pending_cards

    acquired = [c for c in receipt_cards if c["disposition"] in _ACQUIRED]
    gates = [c for c in receipt_cards if c["disposition"] in _GATED]
    failed = [c for c in receipt_cards if c["disposition"] in _FAILED]
    metadata_only = [c for c in receipt_cards if c["disposition"] == "metadata_only"]
    queued = [c for c in pending_cards if c["disposition"] in _QUEUED]

    # Ranked view: every candidate, sorted by priority_score desc then id.
    ranked_view = sorted(
        cards,
        key=lambda c: (-(c["priority_score"] or 0.0), str(c["candidate_id"])),
    )
    # Next best: only actionable work. Queued/eligible first, then human gates,
    # each by priority. Completed/rejected/failed are never re-offered; if
    # nothing is actionable the answer is truthfully null.
    actionable = [
        (rank, -(c["priority_score"] or 0.0), str(c["candidate_id"]), c)
        for c in ranked_view
        if (rank := _next_best_rank(c)) is not None
    ]
    next_best = min(actionable, key=lambda item: item[:3])[3] if actionable else None

    return {
        "schema_version": 1,
        "domain": domain,
        "cycle_id": cycle_id,
        "generated_at": generated_at,
        "corpus_revision": corpus_revision,
        "cycle_time_seconds": cycle_time_seconds,
        "next_best": next_best,
        "coverage_pressure": list(coverage_pressure or []),
        "ranked_candidates": ranked_view,
        "human_gates": gates,
        "queued_candidates": queued,
        "in_flight": list(in_flight or []),
        "recent_receipts": receipt_cards,
        "totals": {
            "discovered": len(cards),
            "rights_resolved": len([c for c in receipt_cards if c["rights_state"] is not None]),
            "acquired": len(acquired),
            "metadata_only": len(metadata_only),
            "gated": len(gates),
            "failed": len(failed),
            "queued": len(queued),
        },
    }
