# Agentic Engineering Candidate Policy

This policy controls deterministic candidate ranking and work admission. It does not promote sources by itself.

## Ranking

- Rank from candidate evidence scores, evidence-lane weight, bounded age/starvation boost, and estimated dollar cost.
- Break ties by canonical candidate ID, so input order cannot change the plan.
- Age boost is capped. It prevents indefinite starvation without allowing stale low-quality evidence to dominate forever.

## Rights and action boundaries

- `public_rights_clear`: eligible for bounded automatic acquisition.
- `public_metadata_only`: eligible only for deterministic inspection/metadata preservation.
- `private_authorized`: human-gated and never automatically scheduled by this policy.
- `rights_unclear` or `unknown`: fail closed; no automatic scheduling or acquisition.
- X/social radar remains an unverified discovery signal and cannot become doctrine directly.

## Budgets

The checked-in V1 defaults are hard caps:

- 3 work items per cycle.
- 3 deep public source packages per UTC day.
- 1 LLM-bearing task per cycle and per UTC day.
- 50,000 LLM tokens per UTC day.
- $1.00 estimated model cost per UTC day.

A candidate is admitted only if the complete task estimate fits the remaining cap. Caps do not overdraw and are evaluated before work starts. No-material-delta candidates schedule no work, including no LLM work.

## Retry and starvation

- Retry begins at 300 seconds and doubles per failure, capped at 24 hours.
- Five failures exhaust automatic retry and require a later explicit recovery path.
- Eligible old candidates receive `0.03` priority per waiting day, capped at `0.30`.
- Cost-normalized ranking ensures one expensive candidate cannot indefinitely crowd out cheaper high-yield evidence.

## Promotion boundary

The policy emits `inspect_only`, `probationary`, `promotion_eligible`, or `reject` as a deterministic suggestion. `automatic_promotion_enabled` remains `false` during shadow. Even after that gate is enabled, only `public_rights_clear` candidates can auto-promote; doctrine mutation remains a separate versioned event path with evidence and lineage.

## Stable reason codes

Decisions expose machine-readable causes, including:

- `no_material_delta`
- `rights_not_publicly_acquirable`
- `private_source_human_gate`
- `retry_backoff_active`
- `retry_attempts_exhausted`
- `per_cycle_item_cap`
- `daily_deep_acquisition_cap`
- `per_cycle_llm_task_cap`
- `daily_llm_task_cap`
- `daily_llm_token_cap`
- `daily_cost_cap`
- `automatic_promotion_disabled`
