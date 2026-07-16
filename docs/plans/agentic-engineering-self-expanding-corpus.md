# Agentic Engineering Self-Expanding Corpus Implementation Plan

> **For Hermes:** Use subagent-driven development and TDD to implement this plan task-by-task. Keep one shared Corpus Engine and one global scheduler; do not create per-source or per-corpus cron jobs.

**Goal:** Turn the existing Agentic Engineering corpus into a continuously self-expanding, evidence-ranked operating corpus that discovers, acquires, evaluates, synthesizes, and applies the best available agentic-engineering knowledge over time.

**Architecture:** Extend the existing `/root/.hermes/scripts/corpus_engine.py`, the `corpora` GBrain source, and the daily `corpus-engine-cycle`. Add a deterministic candidate graph, priority queue, and source adapters; retain Hermes as the reasoning/tool layer for X, browser/authenticated acquisition, source judgment, contradiction analysis, and applied synthesis. Run the system in shadow mode before allowing automatic source promotion.

**Tech stack:** Python 3.11, JSON/JSONL registries and state, requests, yt-dlp, Git/GitHub APIs, RSS/Atom, arXiv/OpenAlex APIs, Hermes cron/tool loop, GBrain CLI/MCP, filesystem raw archive with SHA-256, unittest.

---

## Current baseline

- Shared deterministic engine: `/root/.hermes/scripts/corpus_engine.py`
- Existing engine tests: `/root/.hermes/scripts/tests/test_corpus_engine.py` — 6 passing
- Agentic Engineering registry: `/root/corpora/agentic-engineering/registry/sources.json` — 19 registered sources
- Coverage ledger: `/root/corpora/agentic-engineering/coverage-ledger.md`
- Raw archive/state: `/root/exports/thinker-corpora/agentic-engineering/`
- Global scheduler: Hermes cron `corpus-engine-cycle` (`8dd792d2c75f`), daily at 04:10
- Scheduler prompt source: `/root/.hermes/workspace/corpus-engine-cycle-prompt-v2.txt`
- GBrain source: `corpora` → `/root/corpora`
- Existing deterministic adapters: web documents, GitHub repositories, YouTube feeds/transcripts
- Existing pointer-only lanes: X discovery, private communities

## Non-goals

- No second vector database or retrieval engine.
- No per-creator/source cron jobs.
- No indiscriminate full-channel ingestion for every discovered creator.
- No automatic promotion of X posts, practitioner claims, or benchmark marketing into doctrine.
- No production changes to Percival, Expert runtime, or DDIA as part of corpus acquisition.

---

## Phase 1 — Establish contracts, metrics, and regression fixtures

### Task 1: Freeze baseline fixtures

**Objective:** Capture the current registry, state, coverage, refresh results, and representative retrieval behavior before changing the engine.

**Files:**
- Create: `/root/.hermes/scripts/tests/fixtures/corpus_engine/agentic-engineering-registry.json`
- Create: `/root/.hermes/scripts/tests/fixtures/corpus_engine/sample-engine-state.json`
- Create: `/root/corpora/agentic-engineering/evals/self-expansion-baseline.md`

**Verification:**
- Validate the current registry.
- Run the existing six tests.
- Record representative GBrain queries across canonical, scientific, practitioner, and local-proof lanes.
- Record current source, candidate, freshness, and retrieval counts.

### Task 2: Define shared candidate and queue schemas

**Objective:** Create versioned data contracts for discovered candidates and bounded work items.

**Files:**
- Create: `/root/.hermes/scripts/corpus_engine_models.py`
- Create: `/root/.hermes/scripts/tests/test_corpus_engine_models.py`
- Create: `/root/corpora/agentic-engineering/registry/candidate-policy.md`

**Candidate fields:**
- stable candidate ID;
- entity/source type;
- canonical URL/handle/repository/paper ID;
- discovery source and exact evidence pointer;
- evidence lane;
- topics/mechanisms;
- authority, demonstrated-practice, novelty, relevance, corroboration, production/scientific-value, and cost scores;
- status: discovered, probationary, promoted, rejected, blocked;
- first_seen_at, last_seen_at, occurrences;
- rights/access state;
- deduplication keys;
- rationale and rejection reason.

**Queue fields:**
- stable work ID and idempotency key;
- corpus/domain;
- candidate/source ID;
- action: inspect, acquire, normalize, verify, synthesize, evaluate, repair;
- priority and score components;
- budget estimate;
- state, attempts, lease owner/expiry, retry_after;
- proof receipt pointers.

**Tests:** schema validation, stable IDs, deterministic score calculation, duplicate convergence, malformed input rejection, lease expiry, and idempotent retry.

---

## Phase 2 — Build the creator/source discovery graph

### Task 3: Add deterministic candidate storage and deduplication

**Objective:** Persist discoveries without immediately promoting them into the canonical source registry.

**Files:**
- Modify: `/root/.hermes/scripts/corpus_engine.py`
- Modify: `/root/.hermes/scripts/tests/test_corpus_engine.py`
- Runtime state: `/root/exports/thinker-corpora/agentic-engineering/candidates.jsonl`
- Runtime queue: `/root/exports/thinker-corpora/agentic-engineering/priority-queue.jsonl`
- Corpus projection: `/root/corpora/agentic-engineering/discovery/candidate-index.md`

**Behavior:** append-only observations converge into one stable candidate; repeated independent mentions increase corroboration; rejected candidates remain remembered so they are not repeatedly rediscovered.

### Task 4: Implement evidence-based creator graph expansion

**Objective:** Discover serious builders, maintainers, researchers, and operators from existing first-rank sources.

**Mechanisms:**
- YouTube guests, collaborations, descriptions, channel links, and referenced repositories;
- X mentions, replies, quote-posts, lists, and linked primary sources;
- GitHub maintainers, contributors, dependencies, related repositories, issues, and releases;
- paper authors, citations, benchmark submissions, and associated code;
- conference speakers, podcasts, engineering blogs, and postmortems.

**Files:**
- Create: `/root/.hermes/scripts/corpus_discovery.py`
- Create: `/root/.hermes/scripts/tests/test_corpus_discovery.py`
- Create: `/root/corpora/agentic-engineering/discovery/creator-source-graph.md`

**Promotion rule:** a candidate must cross a configurable score threshold and have either demonstrated implementation, first-rank authority, multiple independent mentions, or strong corrective/critical value. Human review is required only for paid/private access, new credentials, or meaningful spend.

---

## Phase 3 — Add high-value acquisition adapters

### Task 5: Harden YouTube incremental acquisition

**Objective:** Monitor promoted channels cheaply and deeply ingest only high-value unseen videos.

**Files:**
- Refactor YouTube logic from `/root/.hermes/scripts/corpus_engine.py` into `/root/.hermes/scripts/corpus_adapters/youtube.py`
- Create: `/root/.hermes/scripts/tests/test_youtube_corpus_adapter.py`

**Behavior:**
- stable inventory across Videos, Shorts, and Streams;
- incremental video-ID ledger;
- metadata-only triage before transcription;
- captions first, authenticated yt-dlp fallback, audio transcription fallback, storyboard/OCR only when needed;
- exact transcript/provenance status;
- no page-summary substitution for video content.

### Task 6: Add papers and benchmark adapters

**Objective:** Establish scientific and benchmark coverage as a first-class lane.

**Files:**
- Create: `/root/.hermes/scripts/corpus_adapters/arxiv.py`
- Create: `/root/.hermes/scripts/corpus_adapters/openalex.py`
- Create: `/root/.hermes/scripts/corpus_adapters/benchmark.py`
- Create corresponding unittest files.

**Sources:** arXiv, OpenAlex, official benchmark repositories/sites, associated code repositories, and stable paper identifiers. Semantic Scholar can be an optional enrichment route, not a hard dependency.

**Behavior:** preserve paper versions, authors, dates, abstract/full-text boundary, code links, benchmark task definitions, methodology, reported scores, limitations, and later corrections. Do not treat abstracts or leaderboard claims as verified conclusions.

### Task 7: Add official changelog and production-evidence adapters

**Objective:** Capture source breadth, foundational literature, and production evidence efficiently.

**Files:**
- Create: `/root/.hermes/scripts/corpus_adapters/rss.py`
- Create: `/root/.hermes/scripts/corpus_adapters/postmortem.py`
- Create corresponding unittest files.

**Targets:** official documentation/release feeds, standards, engineering blogs, incident reports, deployment writeups, conference transcripts, and measured case studies.

**Classification:** explicitly distinguish vendor claims, operator reports, independent postmortems, measured production results, and source-code evidence.

### Task 8: Convert X from pointer-only to bounded discovery ingestion

**Objective:** Use X as a high-recall radar while preventing opinion-stream pollution.

**Implementation boundary:** Hermes `x_search` remains the acquisition tool; deterministic code owns candidate normalization, deduplication, scoring, and provenance storage.

**Files:**
- Create: `/root/.hermes/scripts/corpus_x_signal_ingest.py`
- Create: `/root/.hermes/scripts/tests/test_corpus_x_signal_ingest.py`
- Modify: `/root/.hermes/workspace/corpus-engine-cycle-prompt-v2.txt`

**Rules:** exact post/thread, author, timestamp, canonical URL, exact text, media transcript when relevant, and linked primary sources. X signals remain `unverified_discovery_signal` until corroborated.

---

## Phase 4 — Introduce a durable global priority queue

### Task 9: Implement budgeted priority selection

**Objective:** Spend acquisition and reasoning effort on the highest expected information gain.

**Priority model:**

`relevance × authority × novelty × corroboration × scientific_or_production_value × unresolved_gap_value ÷ expected_cost`

**Files:**
- Create: `/root/.hermes/scripts/corpus_priority.py`
- Create: `/root/.hermes/scripts/tests/test_corpus_priority.py`
- Modify: `/root/.hermes/scripts/corpus_engine.py`

**Required behavior:** deterministic ranking, per-run item and cost caps, starvation prevention, retries with backoff, leases, idempotency, and dead-letter state. A cron turn ending does not imply work completion.

### Task 10: Keep one cron and split cheap versus deep work inside it

**Objective:** Preserve one scheduler while separating low-cost discovery from high-cost ingestion.

**Files:**
- Modify: `/root/.hermes/workspace/corpus-engine-cycle-prompt-v2.txt`
- Update Hermes cron `corpus-engine-cycle` only after prompt and shadow verification pass.

**Per-run sequence:**
1. validate all registries;
2. perform due cheap inventories/deltas;
3. ingest new candidate observations;
4. rank the global queue;
5. execute one bounded highest-value task;
6. sync only intentional changed corpus namespaces;
7. run retrieval/integrity gates;
8. emit only material findings or blockers.

No additional recurring job should be created unless the single-job design fails a measured reliability or runtime constraint.

---

## Phase 5 — Close the GBrain metabolism and evaluation loop

### Task 11: Generate mechanism-level synthesis candidates

**Objective:** Turn new evidence into bounded comparisons rather than document summaries.

**Files:**
- Create/update pages under `/root/brain/concepts/corpus-synthesis/agentic-engineering/`
- Create: `/root/corpora/agentic-engineering/synthesis/mechanism-index.md`

**Synthesis contract:** claim, mechanism, evidence lanes, agreement, contradiction, applicability, failure modes, confidence, active Expert/Percival relevance, and what test would change the conclusion.

### Task 12: Build retrieval and applied-behavior evals

**Objective:** Prove that corpus expansion improves decisions and systems.

**Files:**
- Create: `/root/corpora/agentic-engineering/evals/retrieval-suite.jsonl`
- Create: `/root/corpora/agentic-engineering/evals/applied-decision-suite.jsonl`
- Create: `/root/.hermes/scripts/corpus_eval.py`
- Create: `/root/.hermes/scripts/tests/test_corpus_eval.py`

**Evaluation comparisons:**
- model-only answer;
- existing corpus snapshot;
- expanded corpus snapshot;
- recommendation versus later implementation outcome.

**Metrics:** retrieval precision/recall, citation validity, lane diversity, contradiction surfacing, freshness, false promotion, decision change, implementation success, and cost per material insight.

### Task 13: Feed local proof back into doctrine

**Objective:** Use Expert/Percival implementation receipts and incidents as evidence without copying private operational state into external doctrine.

**Files:**
- Extend `/root/corpora/agentic-engineering/receipts/`
- Modify synthesis/eval logic only to consume minimized pointers, dates, tests, and outcomes.

**Rule:** local proof can promote or reject a doctrine candidate; it cannot retroactively convert a practitioner claim into independent external evidence.

---

## Phase 6 — Shadow run, audit, and activation

### Task 14: Run a seven-day shadow cycle

**Objective:** Prove discovery and ranking quality before automatic promotion.

**Shadow behavior:** discover, score, queue, and project candidates, but do not automatically add probationary candidates to the canonical registry or deeply ingest them.

**Review sample:** top 20 candidates, bottom/rejected candidates, duplicate groups, source-lane balance, estimated costs, privacy/rights issues, and expected information gain.

### Task 15: Run adversarial and recovery tests

**Required probes:**
- duplicate source discovery from YouTube, X, GitHub, and a paper;
- restart during acquisition;
- expired lease;
- malformed feed/API response;
- source deletion or renamed handle;
- changed paper/repository version;
- missing transcript;
- viral X claim with no primary source;
- benchmark result with changed methodology;
- blocked/private source;
- all sources not due, proving a zero-write no-op;
- failed required validation stopping sync/promotion.

### Task 16: Activate bounded automatic promotion

**Objective:** Permit reversible automatic promotion only after the shadow/eval gates pass.

**Initial autonomy:**
- automatic: public metadata discovery, reversible candidate storage, public-source acquisition under budgets, source-card writes, GBrain corpus sync, retrieval evals, quiet repair;
- human-gated: paid access, credentials/access expansion, rights ambiguity, material spend, external outreach, destructive cleanup, core/config changes, production changes.

**Activation gates:**
- targeted and full shared tests pass;
- no secret/privacy leakage;
- candidate precision meets the chosen threshold;
- duplicate and restart tests pass;
- GBrain source-scoped readback and retrieval pass;
- seven-day cost and signal yield are acceptable;
- rollback restores the prior cron prompt, engine files, registry, and state.

---

## Initial acquisition priorities after activation

1. Foundational distributed-systems sources for leases, failure detectors, sagas, event sourcing, supervision, idempotency, and compensation.
2. Long-horizon agent evaluation and benchmark sources beyond SWE-bench.
3. Production deployments, postmortems, security failures, and measured operator reports.
4. Creator graph expansion from IndyDevDan, TAC, current repositories, papers, and repeated X/GitHub cross-mentions.
5. Framework breadth only where it adds a distinct mechanism or materially different production evidence.

## Final acceptance criteria

- One existing global cron remains authoritative.
- New high-value creators and sources are discovered without Sameer naming them.
- Cheap discovery is separated from expensive deep ingestion.
- Every ingested claim resolves to raw evidence and provenance.
- Scientific, foundational, production, practitioner, zeitgeist, canonical, and local-proof lanes remain distinguishable.
- Duplicate discovery converges safely.
- Restarts and retries do not lose or duplicate work.
- The corpus updates GBrain and passes source-scoped retrieval tests.
- At least one applied architecture recommendation demonstrably improves after the expansion.
- Routine cycles stay silent; only material findings, blockers, integrity failures, or human gates alert Sameer.
