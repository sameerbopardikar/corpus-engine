# Generalized Corpus Engine — Declarative Domain Layer

The Corpus Engine's contract stack (`corpus_engine_models`, `corpus_discovery`,
`corpus_priority`, `corpus_doctrine`, `corpus_eval`, `corpus_shadow`,
`corpus_adapters`) is already domain-neutral — a domain enters only as string
fields, a priority policy, and injected callables. This layer adds a strict
**declarative domain contract** plus a **generic runner** so a new corpus domain
is created from data alone, with no domain-specific engine code.

The existing Agentic Engineering vertical, its launchers, the deployed shadow
proof, and the single global scheduler are untouched. This layer is additive.

## Components

### `src/corpus_domain_spec.py` — `DomainSpec`
A strict, versioned declarative spec. `load_domain_spec()` validates and rejects:
unknown/missing fields (top-level and nested), duplicate JSON keys, non-finite
numbers, non-kebab identifiers, duplicate axis keys / topics / source families,
`automatic_promotion_enabled: true`, mis-ordered promotion thresholds, negative
or non-finite budgets, absolute or `..`-traversing roots and `seed_ref`, unknown
rights states, and **private or rights-unclear source families marked
`shared_corpus_eligible`** (private-personal-data separation). It carries:
identity, provisional ontology axes, evidence-lane weights, source-family policy,
a data-only feedback profile, acquisition-suggestion policy, bootstrap budgets,
relative corpus roots, and eval requirements.

### `src/corpus_scout_import.py` — bounded scout import
LLM terrain mapping stays behind this boundary. `import_scout_result()`
deterministically validates a bounded scout document and projects it into a
**provisional field map** (grouped by declared axis; axes the scout discovered
that are not yet in the spec are preserved as `unmapped_fields`, never dropped)
and **ranked acquisition suggestions** (scored by confidence × family priority ×
rights posture, capped, with private/unclear suggestions flagged
`requires_human_gate` and never `shared_corpus_eligible`). Scout provenance
(`scout_id`, `model`, `generated_at`) and per-item confidence are preserved and
never adopted. No model call happens here.

### `src/corpus_domain_runner.py` — generic cycle
`run_domain_cycle()` runs one deterministic bounded cycle for any spec:

1. **validate** — load and cross-check spec and candidate seed (domain match).
2. **rank** — lexical topic-coverage gap ranking over corpus pages, reusing
   `score_observation` + `CandidateRecord`; bounded inspect work is enqueued
   through `DiscoveryEngine` (idempotent by `idempotency_key`).
3. **project** — a domain-scoped field map and ranked acquisition suggestions,
   written as private `0o600` JSON/Markdown projections.
4. **doctrine** — propose a doctrine-structure concept for the best
   rights-cleared, citeable ontology axis (idempotent by `cycle_id`), or
   **decline with an explicit reason**. Proposals stay
   `epistemic_layer: external_corpus_synthesis`, `sameer_adopted: false`.
5. **evaluate** — a deterministic domain evaluation against the spec's
   `eval_requirements`.
6. **next gap** — select the highest-gap uncovered axis.

The receipt is content-addressed (`receipt_sha256`) and stable across replays
with the same inputs. Promotion and production mutation are always disabled.

## Training proof

`config/domains/training.json` + `docs/source-maps/training-seed-candidates.json`
define Sameer's named scope (hypertrophy, strength, power, speed, endurance,
vertical jump / explosiveness, experimental fascia training, VO2 max, cardio),
with force–velocity represented as one mechanical axis alongside adaptation,
method, performance expression, and constraints.
`tests/test_training_domain_proof.py` runs a real bounded cycle against the
deterministic fixture corpus under `tests/fixtures/training-corpus/`, producing a
stable receipt with a doctrine proposal (`mechanical-force-velocity`) and a
next-gap selection — with no writes outside a temporary directory.

## Out of scope for this slice

Removing vertical coupling from the deployed launchers / `corpus_engine_cycle_v1.sh`
(Phase 2), migrating `/root/corpora` training data (Phase 3.4), and running a real
production cycle (Phase 3.5) are deliberately excluded to preserve the proven
vertical and the single scheduler.
