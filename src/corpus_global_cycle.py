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

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from corpus_priority import BudgetLedger, CandidateTask, PriorityPolicy

DomainLoader = tuple[str, Callable[[], list[CandidateTask]]]


class GlobalCycleError(ValueError):
    """Raised when a global cycle cannot be assembled safely."""


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
        for task in tasks:
            if not isinstance(task, CandidateTask):
                failures.append({"domain": domain, "error": "loader returned a non-CandidateTask"})
                break
            candidate_id = task.candidate.candidate_id
            if candidate_id in candidate_domains:
                failures.append({"domain": domain, "error": f"duplicate candidate across domains: {candidate_id}"})
                break
            candidate_domains[candidate_id] = domain
            all_tasks.append(task)
        else:
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
