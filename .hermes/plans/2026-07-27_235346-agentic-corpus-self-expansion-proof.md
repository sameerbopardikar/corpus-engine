# Agentic Corpus Self-Expansion Proof Implementation Plan

> **For Hermes:** Execute this plan task-by-task with a bounded five-run agentic-workflow eval. Separate baseline discovery from repair, and require non-identical anti-overfit evidence before accepting changes.

**Goal:** Prove or falsify the Agentic Engineering corpus's X rotation, cross-family relationship extraction, candidate promotion, recursive source expansion, and unknown-concept discovery today through an isolated accelerated replay of the exact shared engine contracts.

**Architecture:** Extend the existing source-graph branch rather than creating a second engine. Add one deterministic X-radar state machine, one source-family-agnostic accelerated replay harness, and strict receipts. Use two independent proofs: a historical graph-engineering regression with all post-hoc terms removed, and a blinded non-identical holdout selected independently. Production corpus data stays read-only until the shadow suite passes; only then update the scheduled prompt/overlay and run a no-destructive exact-path smoke.

**Tech Stack:** Python 3.11 stdlib, existing `DiscoveryEngine`, `corpus_source_graph`, unittest, Git worktrees, Hermes cron prompt, X search tool for live evidence only.

---

## Locked finish line

All five gates must pass in one machine-readable receipt:

1. Four configured X queries rotate `0 → 1 → 2 → 3 → 0`, survive restart, and do not advance after a failed query.
2. Real-source-derived fixtures from X, YouTube/podcast, papers, GitHub, and conferences produce provenance-bearing relationship edges and durable rights-unclear candidates.
3. Candidate policy moves a corroborated high-value candidate through `discovered → probationary → promoted`, while weak/unclear candidates remain held or rejected with reasons.
4. Accelerated Cycle A discovers and promotes an unseeded source; Cycle B consumes only that expanded watch node and produces a second candidate or concept signal.
5. A target mechanism absent from seed registry, ontology, queries, and worker prompt is surfaced in a blinded non-identical holdout.

Mock-only success, manual candidate injection, target-term leakage, manual promotion, or one-cycle capture do not pass.

## Run budget

Maximum five eval runs:

- Run 1: untouched baseline against all gates.
- Runs 2-4: one generalized repair per dominant failure class, each with a non-identical regression.
- Run 5: clean final rerun from empty shadow state.

Stop rather than overfit if the same failure class persists after three credible fixes.

---

### Task 1: Create isolated proof worktree and baseline receipt

**Objective:** Preserve production and establish the unmodified branch's exact failures.

**Files:**
- Create worktree: `/root/workspace/self-expanding-corpus-engine/.worktrees/self-expansion-proof`
- Create: `evals/self-expansion/run-ledger.md`
- Create: `evals/self-expansion/baseline.json`

**Steps:**
1. Branch `agent/self-expansion-proof` from `agent/source-graph-root-fix`.
2. Record commit SHA, live overlay hash, corpus registry revision, and current cron prompt hash.
3. Run the existing 612-test branch suite.
4. Add no implementation changes before recording Run 1.
5. Commit only the baseline ledger and receipt.

**Verification:** Baseline names every failing gate separately and does not relabel the source-graph bridge as recursive discovery.

---

### Task 2: Add deterministic X-radar rotation

**Objective:** Replace prompt-owned `query_index` mutation with a replay-safe deterministic state machine.

**Files:**
- Create: `src/corpus_x_radar.py`
- Create: `scripts/corpus_x_radar.py`
- Create: `tests/test_corpus_x_radar.py`

**Required contract:**
- `lease_next(queries, state_path, cycle_id)` returns the current query without advancing.
- `commit_success(lease, result_receipt)` advances exactly once after durable result preservation.
- `commit_failure(lease, error)` records failure and leaves the index unchanged.
- Exact retries are physical no-ops.
- Conflicting cycle/query preimages fail closed.
- State uses one locked, fsync-backed atomic JSON projection.

**TDD sequence:**
1. Write tests for `0 → 1 → 2 → 3 → 0`.
2. Verify failure does not advance.
3. Verify restart resumes the correct index.
4. Verify duplicate success is idempotent.
5. Verify conflicting stale lease fails closed.
6. Implement minimal state machine and CLI.
7. Run `python -m unittest -v tests.test_corpus_x_radar`.
8. Commit.

---

### Task 3: Add cross-family artifact-to-relationship contract

**Objective:** Prove that relationships arise from source artifacts, not manually injected candidate records.

**Files:**
- Create: `src/corpus_relationship_extraction.py`
- Create: `scripts/corpus_relationship_extract.py`
- Create: `tests/test_corpus_relationship_extraction.py`
- Create fixtures under `tests/fixtures/self_expansion/` for:
  - `x-thread.json`
  - `youtube-transcript.md`
  - `podcast-transcript.md`
  - `openalex-work.json`
  - `github-repository.json`
  - `conference-schedule.html`

**Required behavior:**
- Deterministically extract structured relationships where metadata is explicit.
- For unstructured transcripts, validate a bounded semantic-extractor JSON result against exact source spans and canonical URLs before graph admission.
- Never infer a person solely from an ungrounded model claim.
- Every edge must preserve source family, relationship type, source URL, evidence pointer, observed time, and source span or metadata locator.
- Feed valid edges through `ingest_relationships`; all candidates remain rights-unclear.

**TDD sequence:**
1. Write one failing source-grounding test per family.
2. Add adversarial tests for hallucinated guest, malformed URL, unsupported edge, missing span, and duplicate replay.
3. Implement the minimal generic extractor/validator.
4. Run cross-family tests and existing source-graph tests.
5. Commit.

---

### Task 4: Add policy-driven candidate disposition and watch projection

**Objective:** Demonstrate automatic probation, promotion, hold, and rejection without manually choosing the winner.

**Files:**
- Create: `src/corpus_candidate_policy.py`
- Create: `tests/test_corpus_candidate_policy.py`
- Create: `evals/self-expansion/policy.json`

**Required behavior:**
- Score from existing candidate components plus corroboration count and independent source-family count.
- `discovered → probationary` only above the probation threshold.
- `probationary → promoted` only with independent corroboration and sufficient authority/relevance.
- Promotion writes a shadow watch projection, never production registry bytes during tests.
- Weak or noisy candidates remain held or are rejected with explicit reasons.
- No rights upgrade occurs during disposition.

**TDD sequence:**
1. High-value corroborated candidate promotes.
2. Single viral X mention does not promote.
3. Same-source duplicate does not count as independent corroboration.
4. Rejected candidate retains reason.
5. Restart/replay preserves disposition.
6. Implement minimal policy and shadow projection.
7. Commit.

---

### Task 5: Build accelerated two-cycle replay harness

**Objective:** Run the real graph, candidate, policy, and watch contracts twice under controlled clocks.

**Files:**
- Create: `src/corpus_recursive_discovery_eval.py`
- Create: `scripts/corpus_self_expansion_proof.py`
- Create: `tests/test_corpus_recursive_discovery_eval.py`
- Create: `tests/fixtures/self_expansion/graph_engineering_regression/`
- Create: `tests/fixtures/self_expansion/blinded_holdout/`

**Cycle contract:**
- Cycle A sees only seed watch nodes and time-window-A artifacts.
- Cycle A extracts edges, scores candidates, and creates a shadow promoted watch projection.
- Cycle B may see only seed nodes plus sources promoted by Cycle A and time-window-B artifacts.
- The target concept label and mechanism tokens are checked against seed registry, ontology, queries, and worker input for leakage.
- Concept success may be a mechanism cluster, not exact phrase guessing.
- Receipt records discovery path, dispositions, watch delta, second-order discovery, leakage checks, and restart/idempotency hashes.

**Historical regression:** Reconstruct the exact parent `de4b5798f8c229b57d98511bb3a284edfbd29f5e` of corpus commit `565150e9dc95c4cde67c4d2cd7e7e7b4ec37c478`. Exclude the five paths changed by that commit (`agentic-engineering/discovery/x-radar/latest-check.json`, `agentic-engineering/discovery/x-radar/signals.json`, `agentic-engineering/registry/sources.json`, `agentic-engineering/sources/zeitgeist/x-agentic-engineering-radar.md`, and `agentic-engineering/sources/zeitgeist/x-heavy-agent-harness-engineering-2081455613075480822.md`). The canonical binary diff for that exclusion set has SHA-256 `5ee0b0f17027c5d8093515f7d98d2a52dc14a53691334cbd3e0c9ee50a7fbb2a`. Remove any separately identified graph-engineering synthesis from the worker-visible packet and record its path and digest in the run receipt. Because LangGraph already existed in the pre-state, this case tests terminology/source-network emergence, not repository discovery.

**Blinded holdout:** An independent evaluator chooses a non-identical target after the engine packet is frozen. The worker receives source artifacts without the target label; the parent evaluator scores the output afterward.

**Verification:** Clean-state replay passes without manual candidate insertion or promotion.

---

### Task 6: Run bounded eval and repair only shared failures

**Objective:** Converge without fixture-specific patches.

**Files:**
- Update: `evals/self-expansion/run-ledger.md`
- Write one immutable receipt per run under `evals/self-expansion/receipts/`

**Per-run sequence:**
1. Run the complete proof script from empty temp state.
2. Record observed versus expected behavior before editing.
3. Classify the dominant failure.
4. State the behavior-class root cause.
5. Apply one smallest shared fix.
6. Add a non-identical regression.
7. Rerun.

**Stop conditions:** all gates pass; five runs exhausted; or one failure class survives three credible repairs.

---

### Task 7: Exact-path deployment and readback

**Objective:** Prove the actual scheduled surface uses the verified mechanisms without waiting for wall-clock days.

**Files likely to change after shadow pass:**
- `/root/.hermes/corpus-engine-overlay/` corresponding verified files
- `/root/.hermes/workspace/corpus-engine-cycle-prompt-v3.txt`
- Hermes cron job `corpus-engine-cycle`
- `/root/corpora/agentic-engineering/evals/self-expansion-proof-<date>.json`

**Steps:**
1. Read the active self-expansion config before mutation and record the effective `enabled` flag. No config path means default-off; malformed/unreadable config is fail-soft and must report `effective_enabled=false` rather than changing scheduled behavior.
2. Back up every overlay/prompt file before mutation.
3. Install only exact reviewed bytes.
4. Update cron procedure to lease an X query before `x_search`, commit only after signal preservation, and run relationship extraction before completion.
5. Run four accelerated X leases in isolated state.
6. Run a production-wrapper planning smoke.
7. Run one bounded shadow cycle against real corpus sources without production promotion.
8. Verify file hashes, read back cron configuration, and record the post-activation effective self-expansion flag in the deployment receipt. The post value must equal the explicitly configured pre value; activation never silently enables the feature.
9. Commit only the intentional corpus receipt and sync `corpora`.
10. Push the code branch and update/open the PR. Do not self-merge.

---

## Final report contract

Report:

- runs used out of five;
- which gates passed or failed;
- exact discovered path in each proof;
- dominant failure class and generalized repair;
- anti-overfit evidence;
- production files changed and rollback path;
- branch/PR state;
- whether recursive discovery and unknown-concept discovery are now proven, partially proven, or falsified.

Do not claim success from a stored graph, green unit suite, or one successful scheduled command alone.
