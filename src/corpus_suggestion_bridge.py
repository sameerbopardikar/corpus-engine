#!/usr/bin/env python3
"""Bridge validated acquisition suggestions into discovery-only candidates.

A scout projection or an intake suggestion is a *hint*, not evidence. This
bridge turns a validated suggestion into a domain- and profile-bound
:class:`CandidateObservation` and records it in the discovery ledger only — it
never writes a corpus card, never proposes doctrine, and never establishes or
upgrades a rights classification. Duplicate origins converge on one candidate
(discovery handles the merge) while each call returns its own origin receipt.

Only fully rights-cleared, non-gated, shared-corpus-eligible suggestions are
marked auto-actionable (an inspect work item is enqueued). Human-gated, private,
or rights-unclear suggestions are still discovered but remain non-automatic:
they wait for a human decision and independent rights classification.
"""
from __future__ import annotations

from typing import Any

from corpus_discovery import DiscoveryEngine
from corpus_engine_models import CandidateObservation
from corpus_feedback import FeedbackProfile, profile_binding

# A suggestion is auto-actionable only when it is public-rights-clear, shared
# eligible, and not human-gated. Everything else is discovered but non-automatic.
_AUTO_RIGHTS_HINT = "public_rights_clear"


class SuggestionBridgeError(ValueError):
    """Raised when a suggestion cannot be safely bridged into discovery."""


def _as_dict(suggestion: Any) -> dict[str, Any]:
    if hasattr(suggestion, "to_dict"):
        return suggestion.to_dict()
    if isinstance(suggestion, dict):
        return dict(suggestion)
    raise SuggestionBridgeError("suggestion must be a dict or expose to_dict()")


def bridge_acquisition_suggestion(
    discovery: DiscoveryEngine,
    suggestion: Any,
    *,
    domain: str,
    profile: FeedbackProfile | None,
    entity_type: str,
    evidence_lane: str,
    discovery_source: str,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Observe a suggestion as a discovery-only candidate; return its receipt."""
    if not isinstance(domain, str) or not domain.strip():
        raise SuggestionBridgeError("domain is required")
    if profile is None or not isinstance(profile, FeedbackProfile):
        raise SuggestionBridgeError("a versioned feedback profile is required")
    if profile.domain != domain:
        raise SuggestionBridgeError(
            f"profile domain {profile.domain!r} does not match suggestion domain {domain!r}"
        )

    data = _as_dict(suggestion)
    locator = data.get("locator")
    if not isinstance(locator, str) or not locator.strip():
        raise SuggestionBridgeError("suggestion requires a locator")
    provenance = data.get("provenance")
    if not isinstance(provenance, dict) or not provenance.get("source"):
        raise SuggestionBridgeError("suggestion requires provenance.source")

    observation = CandidateObservation.create(
        domain=domain,
        entity_type=entity_type,
        canonical_url=locator,
        discovery_source=discovery_source,
        evidence_pointer=locator,
        evidence_lane=evidence_lane,
        topics=tuple(data.get("topics", ())),
        observed_at=observed_at,
    )
    # rights_state="unknown": a suggestion never classifies rights. Independent
    # acquisition must set the authoritative rights later; this cannot upgrade.
    record = discovery.observe(observation, rights_state="unknown")

    requires_human_gate = bool(data.get("requires_human_gate", True))
    shared_eligible = bool(data.get("shared_corpus_eligible", False))
    rights_hint = data.get("rights_hint")
    auto_actionable = (
        not requires_human_gate
        and shared_eligible
        and rights_hint == _AUTO_RIGHTS_HINT
    )

    queued_work_ids: list[str] = []
    if auto_actionable:
        work = discovery.enqueue_work(
            domain=domain,
            candidate_id=record.candidate_id,
            action="inspect",
            score_components={**record.compute_score(), "suggestion_rank": float(data.get("rank_score", 0.0))},
            budget_estimate=0.0,
            idempotency_key=f"suggestion:{discovery_source}:inspect",
        )
        queued_work_ids.append(work.work_id)

    return {
        "status": "observed",
        "candidate_id": record.candidate_id,
        "domain": domain,
        "binding": profile_binding(profile),
        "origin": {
            "discovery_source": discovery_source,
            "locator": locator,
            "family": data.get("family"),
            "rights_hint": rights_hint,
            "provenance": dict(provenance),
        },
        "auto_actionable": auto_actionable,
        "requires_human_gate": requires_human_gate,
        "queued_work_ids": queued_work_ids,
    }
