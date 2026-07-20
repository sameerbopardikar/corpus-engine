#!/usr/bin/env python3
"""One global corpus cycle across all enabled domains.

The generalized engine has exactly one scheduler and one budget authority.
This module enumerates enabled domains, loads each domain's candidate tasks,
and ranks them in a single :func:`corpus_priority.plan_cycle` plan reserved
against one shared :class:`corpus_priority.BudgetLedger`. Agentic Engineering
and Training candidates therefore compete in one ranking under one daily budget
that cannot be spent once per vertical, and starvation protection applies across
domains.

Domain isolation is fail-soft: if one domain's loader raises, its failure is
recorded and the remaining domains are still planned — one domain cannot corrupt
another. When no domain yields a candidate, the cycle is a read-only no-op: no
budget is created or spent and no LLM work is scheduled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from corpus_acquisition_executor import AcquisitionCandidate, AcquisitionExecutor
from corpus_acquisition_projection import build_acquisition_projection
from corpus_priority import BudgetLedger, CandidateTask, PriorityPolicy
from corpus_rights_resolver import resolve_rights

DomainLoader = tuple[str, Callable[[], list[CandidateTask]]]


class GlobalCycleError(ValueError):
    """Raised when a global cycle cannot be assembled safely."""


@dataclass(frozen=True)
class DomainAcquisitionInputs:
    """One domain's planning tasks paired with their executable candidates.

    ``tasks`` feed the single shared priority/budget plan (rights already
    resolved onto each candidate record). ``acquisition_candidates`` maps
    candidate_id → :class:`AcquisitionCandidate` for the executor. Every task
    must have a matching candidate.
    """

    domain: str
    tasks: list[CandidateTask]
    acquisition_candidates: dict[str, AcquisitionCandidate]


AcquisitionDomainLoader = tuple[str, Callable[[], DomainAcquisitionInputs]]


def _pending_entry(candidate, resolution, enrichment) -> dict[str, Any]:
    """A ranked-but-unexecuted (budget-deferred) candidate for the projection.

    Only public, path-free fields are surfaced: identity, canonical locator,
    resolved rights, and ranking enrichment (why-now / estimate / gap served).
    """
    return {
        "candidate_id": candidate.candidate_id,
        "domain": candidate.domain,
        "title": candidate.title,
        "topics": list(candidate.topics),
        "canonical_locator": candidate.canonical_locator,
        "evidence_lane": candidate.evidence_lane,
        "source_family": candidate.source_family,
        "rights_state": resolution.rights_state.value,
        "rights_basis": resolution.basis,
        "disposition": "deferred",
        "status": "queued",
        "why_now": enrichment.get("why_now"),
        "estimated_cost_usd": enrichment.get("estimated_cost_usd", 0.0),
        "gap_served": enrichment.get("gap_served"),
        "priority_score": enrichment.get("priority_score"),
    }


def run_global_cycle(
    *,
    reservation_id: str,
    domain_loaders: list[DomainLoader],
    policy: PriorityPolicy,
    budget_ledger_path: Path | str,
    now: datetime,
) -> dict[str, Any]:
    if not isinstance(reservation_id, str) or not reservation_id.strip():
        raise GlobalCycleError("reservation_id must be a non-blank string")
    if not isinstance(policy, PriorityPolicy):
        raise GlobalCycleError("policy must be a PriorityPolicy")

    all_tasks: list[CandidateTask] = []
    domains_planned: list[str] = []
    failures: list[dict[str, str]] = []
    candidate_domains: dict[str, str] = {}

    for domain, load in domain_loaders:
        try:
            tasks = list(load())
        except Exception as exc:  # fail-soft: one domain cannot corrupt another
            failures.append({"domain": domain, "error": f"{type(exc).__name__}: {exc}"})
            continue
        staged_tasks: list[CandidateTask] = []
        staged_ids: set[str] = set()
        for task in tasks:
            if not isinstance(task, CandidateTask):
                failures.append({"domain": domain, "error": "loader returned a non-CandidateTask"})
                break
            candidate_id = task.candidate.candidate_id
            if candidate_id in candidate_domains or candidate_id in staged_ids:
                failures.append({"domain": domain, "error": f"duplicate candidate across domains: {candidate_id}"})
                break
            staged_ids.add(candidate_id)
            staged_tasks.append(task)
        else:
            all_tasks.extend(staged_tasks)
            candidate_domains.update({task.candidate.candidate_id: domain for task in staged_tasks})
            domains_planned.append(domain)

    if not all_tasks:
        # Read-only no-op: never create or spend a budget when there is no work.
        return {
            "status": "no_candidates",
            "reservation_id": reservation_id,
            "domains_planned": domains_planned,
            "failures": failures,
            "plan": None,
            "candidate_domains": candidate_domains,
            "selected_candidate_ids": [],
            "llm_calls": 0,
        }

    budget = BudgetLedger(Path(budget_ledger_path))
    plan = budget.reserve_cycle(reservation_id, all_tasks, policy, now=now)
    return {
        "status": "planned",
        "reservation_id": reservation_id,
        "domains_planned": domains_planned,
        "failures": failures,
        "plan": plan,
        "candidate_domains": candidate_domains,
        "selected_candidate_ids": [item.candidate_id for item in plan.selected],
        "deep_acquisitions_scheduled": plan.deep_acquisitions_scheduled,
        "llm_tasks_scheduled": plan.llm_tasks_scheduled,
        "budget_ledger_path": str(budget_ledger_path),
        "llm_calls": 0,
    }


def run_global_acquisition_cycle(
    *,
    reservation_id: str,
    domain_loaders: list[AcquisitionDomainLoader],
    policy: PriorityPolicy,
    budget_ledger_path: Path | str,
    now: datetime,
    corpus_revision: str,
    executor: AcquisitionExecutor | None = None,
    executor_factory: Callable[[str], AcquisitionExecutor] | None = None,
    cycle_time_seconds: float = 0.0,
) -> dict[str, Any]:
    """One bounded execution cycle: plan under one budget, then acquire.

    This is the deployed daily wrapper's execution mode. It preserves every
    invariant of the planning dry-run — one scheduler, one shared budget, and
    fail-soft domain isolation — and adds bounded acquisition on top:

    * Candidates are ranked in a single :func:`BudgetLedger.reserve_cycle` plan.
    * Full-content fetches happen ONLY for candidates the plan selected with
      action ``acquire`` (rights-clear, within the shared daily budget). A
      rights-clear candidate the budget could not fund is deferred, never
      fetched.
    * Metadata-only, human-gated, and rights-unclear candidates are resolved
      into truthful receipts without any content fetch.
    * A crash in one domain's loader or candidate is recorded and never blocks
      another domain.

    Canonical mutation is evidence admission, never doctrine promotion: when a
    canonical root is configured, an eval-passed *probationary* page is
    transactionally admitted into the canonical evidence namespace
    (``<root>/<domain>/sources/acquired/``). Canonical *doctrine* is never
    mutated and automatic promotion stays disabled (``promotion_enabled`` and
    ``production_mutation`` remain false on every receipt).
    """
    if not isinstance(reservation_id, str) or not reservation_id.strip():
        raise GlobalCycleError("reservation_id must be a non-blank string")
    if not isinstance(policy, PriorityPolicy):
        raise GlobalCycleError("policy must be a PriorityPolicy")
    if (executor is None) == (executor_factory is None):
        raise GlobalCycleError("provide exactly one of executor or executor_factory")
    if executor is not None and not isinstance(executor, AcquisitionExecutor):
        raise GlobalCycleError("executor must be an AcquisitionExecutor")

    # Resolve one executor per domain (per-domain isolated roots) or reuse a
    # single shared executor. Either way the plan/budget below stays global.
    _executor_cache: dict[str, AcquisitionExecutor] = {}

    def get_executor(domain: str) -> AcquisitionExecutor:
        if executor is not None:
            return executor
        if domain not in _executor_cache:
            built = executor_factory(domain)
            if not isinstance(built, AcquisitionExecutor):
                raise GlobalCycleError("executor_factory must return an AcquisitionExecutor")
            _executor_cache[domain] = built
        return _executor_cache[domain]

    def clock() -> str:
        if executor is not None:
            return executor.now()
        # Never call a caller-provided factory solely to obtain a timestamp. A
        # rejecting factory must remain isolated to its own domain boundary.
        for cached in _executor_cache.values():
            return cached.now()
        return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    all_tasks: list[CandidateTask] = []
    candidates: dict[str, AcquisitionCandidate] = {}
    candidate_domains: dict[str, str] = {}
    domain_order: list[str] = []
    domains_planned: list[str] = []
    failures: list[dict[str, str]] = []
    seen_domains: set[str] = set()

    for domain, load in domain_loaders:
        if domain in seen_domains:
            failures.append({"domain": domain, "error": "duplicate domain loader"})
            continue
        seen_domains.add(domain)
        try:
            inputs = load()
        except Exception as exc:  # fail-soft: one domain cannot corrupt another
            failures.append({"domain": domain, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if not isinstance(inputs, DomainAcquisitionInputs) or inputs.domain != domain:
            failures.append({"domain": domain, "error": "loader returned mismatched DomainAcquisitionInputs"})
            continue
        conflict = False
        staged_tasks: list[CandidateTask] = []
        for task in inputs.tasks:
            candidate_id = task.candidate.candidate_id
            if candidate_id in candidate_domains:
                failures.append({"domain": domain, "error": f"duplicate candidate across domains: {candidate_id}"})
                conflict = True
                break
            if candidate_id not in inputs.acquisition_candidates:
                failures.append({"domain": domain, "error": f"task {candidate_id} has no acquisition candidate"})
                conflict = True
                break
            candidate_domains[candidate_id] = domain
            staged_tasks.append(task)
        if conflict:
            # Roll back this domain's partial registration; isolation is total.
            for task in staged_tasks:
                candidate_domains.pop(task.candidate.candidate_id, None)
            continue
        for task in staged_tasks:
            all_tasks.append(task)
            candidates[task.candidate.candidate_id] = inputs.acquisition_candidates[task.candidate.candidate_id]
        domain_order.append(domain)
        domains_planned.append(domain)

    if not all_tasks:
        return {
            "status": "no_candidates",
            "reservation_id": reservation_id,
            "domains_planned": domains_planned,
            "failures": failures,
            "plan": None,
            "receipts": [],
            "selected_acquire_ids": [],
            "report": {
                "discovered": 0, "source_families_discovered": [],
                "rights_resolved": 0, "acquired": 0,
                "metadata_only": 0, "gated": 0, "failed": 0, "changed_domains": [],
            },
            "projection": build_acquisition_projection(
                receipts=[], ranked_candidates={}, corpus_revision=corpus_revision,
                generated_at=clock(), cycle_time_seconds=cycle_time_seconds,
                cycle_id=reservation_id,
            ),
            "projections": {},
        }

    budget = BudgetLedger(Path(budget_ledger_path))
    plan = budget.reserve_cycle(reservation_id, all_tasks, policy, now=now)
    selected_acquire_ids = [d.candidate_id for d in plan.selected if d.action == "acquire"]
    selected_acquire = set(selected_acquire_ids)
    ranked_enrichment = {
        d.candidate_id: {
            "why_now": ", ".join(d.reason_codes) or ("selected" if d.scheduled else "ranked"),
            "estimated_cost_usd": d.estimated_cost_usd,
            "priority_score": d.priority_score,
            "gap_served": None,
        }
        for d in plan.decisions
    }

    receipts_by_domain: dict[str, list[dict[str, Any]]] = {d: [] for d in domain_order}
    pending_by_domain: dict[str, list[dict[str, Any]]] = {d: [] for d in domain_order}
    changed_domains: list[str] = []
    # Execute domain by domain so one domain's failure is isolated, and each
    # domain writes into its own isolated executor roots.
    for domain in domain_order:
        try:
            domain_executor = get_executor(domain)
        except Exception as exc:
            failures.append({"domain": domain, "error": f"{type(exc).__name__}: {exc}"})
            continue
        domain_candidate_ids = [cid for cid, dom in candidate_domains.items() if dom == domain]
        for candidate_id in domain_candidate_ids:
            candidate = candidates[candidate_id]
            try:
                resolution = resolve_rights(candidate.rights_evidence)
                if resolution.disposition == "auto_acquire_content" and candidate_id not in selected_acquire:
                    # Rights-clear but the shared budget could not fund it this
                    # cycle: defer without fetching (respect the single budget).
                    # Surface it as queued so it stays visible and actionable.
                    pending_by_domain[domain].append(
                        _pending_entry(candidate, resolution, ranked_enrichment.get(candidate_id, {}))
                    )
                    continue
                receipt = domain_executor.acquire(candidate)
            except Exception as exc:  # never let one candidate corrupt the cycle
                failures.append({"domain": domain, "error": f"{type(exc).__name__}: {exc}"})
                continue
            receipts_by_domain[domain].append(receipt)
            if receipt["disposition"] == "probationary" and domain not in changed_domains:
                changed_domains.append(domain)

    receipts = [r for domain in domain_order for r in receipts_by_domain[domain]]
    pending = [p for domain in domain_order for p in pending_by_domain[domain]]

    acquired = sum(1 for r in receipts if r["disposition"] == "probationary")
    metadata_only = sum(1 for r in receipts if r["disposition"] == "metadata_only")
    gated = sum(1 for r in receipts if r["disposition"] == "human_gate")
    failed = sum(1 for r in receipts if r["disposition"] in {"failed", "quarantined"})
    rights_resolved = sum(1 for r in receipts if r.get("rights") is not None)

    generated_at = clock()
    projection = build_acquisition_projection(
        receipts=receipts, ranked_candidates=ranked_enrichment, pending=pending,
        corpus_revision=corpus_revision, generated_at=generated_at,
        cycle_time_seconds=cycle_time_seconds, cycle_id=reservation_id,
    )
    projections = {
        domain: build_acquisition_projection(
            receipts=receipts_by_domain[domain], ranked_candidates=ranked_enrichment,
            pending=pending_by_domain[domain], corpus_revision=corpus_revision,
            generated_at=generated_at, cycle_time_seconds=cycle_time_seconds,
            domain=domain, cycle_id=reservation_id,
        )
        for domain in domain_order
    }

    return {
        "status": "executed",
        "reservation_id": reservation_id,
        "domains_planned": domains_planned,
        "failures": failures,
        "plan": plan,
        "receipts": receipts,
        "selected_acquire_ids": selected_acquire_ids,
        "projections": projections,
        "report": {
            "discovered": len(all_tasks),
            "source_families_discovered": sorted({candidate.source_family for candidate in candidates.values()}),
            "rights_resolved": rights_resolved,
            "acquired": acquired,
            "metadata_only": metadata_only,
            "gated": gated,
            "failed": failed,
            "changed_domains": changed_domains,
        },
        "projection": projection,
        "budget_ledger_path": str(budget_ledger_path),
    }
