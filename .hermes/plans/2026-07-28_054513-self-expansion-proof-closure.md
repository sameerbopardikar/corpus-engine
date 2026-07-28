# Agentic Engineering Self-Expansion Proof Closure Plan

> **For Hermes:** Execute this plan task-by-task. Do not expand scope beyond the locked acceptance contract.

**Goal:** Complete and verify the Agentic Engineering corpus engine's bounded two-cycle self-expansion path so an unseeded concept can be discovered through persisted source relationships, candidate promotion, durable recurring inspection, and a separately evaluated blinded holdout.

**Architecture:** Preserve one shared discovery ledger, source relationship graph, durable work queue, and scheduled wrapper. Cycle B must be causally produced by the real persisted watch/queue consumer from a promoted source artifact, never supplied as predeclared relationship objects. Every proof claim binds to canonical entity identity, authoritative artifact receipts, preserved bytes, hashes, timestamps, and validated spans.

**Tech Stack:** Python 3, unittest/pytest, JSON/JSONL durable ledgers, existing corpus scheduler wrapper and source-family adapters.

## Locked scope

Repair exactly the seven known P1 proof-invalidating defects plus the unfinished durable recurring-inspection integration. Do not add UI work, new source families, broad production hardening, unrelated corpus features, or P2-only improvements.

## Acceptance criteria

1. Cycle B is derived from an artifact fetched/registered through the promoted persisted watch source and processed by production relationship extractors; manually injected Cycle-B relationships cannot pass.
2. Concept and relationship claims bind to preserved artifact bytes using digest plus exact validated quote/span; unrelated URLs or quotes cannot ground a claim/entity.
3. Corroboration counts normalized independent evidence documents and publishers/owners; aliases of one document do not count twice, and observations later than `evaluated_at` cannot promote.
4. Seed, watch, and discovered entities use one canonical identity resolver that strips tracking aliases and compares stable candidate identities.
5. Same-batch duplicate relationships are deduplicated before durable append and cannot corrupt the graph.
6. Relationship grounding verifies the named entity and relation target within the validated span.
7. Existing rights upgrades are preserved and graph append plus graph-to-ledger projection is recoverable/idempotent after interruption.
8. Promotion enqueues exactly one idempotent daily `inspect` work item and the persisted watch projection includes its `work_id`.
9. Exact two-cycle path passes: persisted Cycle-A artifact → relationship → candidate → promotion → durable inspection → fetched Cycle-B artifact → second-order candidate/concept.
10. Blinded scratchpad holdout passes an independent evaluator after complete worker-visible packet leakage scan and artifact-byte/span grounding.
11. Targeted adversarial tests and the complete repository suite pass on current bytes.
12. An independent exact-current review finds no P0/P1 that invalidates this bounded path.
13. Final state is one clean committed branch plus an exact scheduled-wrapper proof receipt. Production cron behavior is not widened unless the exact deployed overlay is verified and remains within existing rights/budget boundaries.

## Stop rule

Use one implementation pass and no more than two adversarial closure passes. Only P0/P1 findings that invalidate an acceptance criterion block completion. Record P2 findings as follow-up without expanding this task.

---

### Task 1: Baseline and authority-path inspection

**Objective:** Reconcile committed code, dirty in-progress files, queue/scheduler consumers, fixtures, and all seven audit probes.

**Files:**
- Inspect: `src/corpus_candidate_policy.py`
- Inspect: `src/corpus_source_graph.py`
- Inspect: `src/corpus_relationship_extraction.py`
- Inspect: `src/corpus_recursive_discovery_eval.py`
- Inspect: `src/corpus_discovery_engine.py`
- Inspect: `scripts/corpus_candidate_policy.py`
- Inspect: scheduler/global-cycle wrappers under `scripts/`
- Inspect: corresponding tests and `evals/self-expansion/`

**Verification:** Record current branch/status and reproduce the two current recurring-inspection failures plus the seven audit probes.

### Task 2: Complete durable recurring inspection

**Objective:** Make promotion create one idempotent daily `inspect` queue item and bind it into the persisted watch projection.

**Test first:** Keep the existing failing candidate-policy and CLI tests red; add replay/day-boundary tests.

**Implementation:** Use the existing `DiscoveryEngine.enqueue_work` contract and stable work identity; do not create a second queue. The recurring inspection idempotency key is exactly `daily-inspect:<YYYY-MM-DD UTC>`, combined by `WorkItem.create` with domain, candidate ID, and action. Same-candidate replays on one UTC date therefore resolve to the same `work_id`; the next UTC date resolves to a different `work_id`.

**Verification:** Candidate-policy and CLI tests pass; exact replay produces zero new work and a later UTC day can create the next bounded inspection item.

### Task 3: Repair graph identity, deduplication, time, rights, and transaction invariants

**Objective:** Close P1 findings 3, 4, 5, and 7 at shared boundaries.

**Test first:** Add adversarial fixtures for future observations, URL aliases, duplicate same-batch edges, rights-upgraded candidates, and interruption/reconciliation replay.

**Implementation:** Introduce one canonical entity/document identity path, deduplicate projected graph IDs before append, enforce evaluation cutoffs, preserve rights neutrally for existing candidates, and make append/projection recoverable.

**Verification:** New regressions pass and prior graph/discovery tests remain green.

### Task 4: Repair artifact and span grounding

**Objective:** Close P1 findings 2 and 6.

**Test first:** Add unrelated-URL, unrelated-quote, invented-entity, modified-artifact, and leaked-worker-packet negatives.

**Implementation:** Require authoritative artifact receipt, digest, span offsets/exact quote, and relation-specific normalized entity mention validation against preserved bytes.

**Verification:** Forged concepts/relationships fail closed while valid frozen evidence passes.

### Task 5: Replace injected Cycle B with real persisted consumption

**Objective:** Close P1 finding 1 and prove causal recursion.

**Test first:** Preserve the attack fixture where a promoted GitHub source unlocks an unrelated attacker artifact and require rejection.

**Implementation:** The recursive proof must read the persisted watch projection/work item, invoke the actual source adapter/fixture fetch receipt, validate parent-source binding, then derive relationships through the production extractor.

**Verification:** Manually supplied Cycle-B relationships cannot pass; exact causal artifact receipt does.

### Task 6: Run blinded holdout and exact scheduled wrapper

**Objective:** Produce the bounded positive proof.

**Steps:**
1. Freeze and hash the complete worker-visible scratchpad packet and all referenced artifact bytes.
2. Scan every worker-visible byte for hidden target labels and aliases.
3. Run the blinded worker without hidden evaluator access.
4. Evaluate output independently against the separately frozen hidden evaluator.
5. Run the exact scheduled wrapper twice against isolated persisted state.
6. Verify promotion, queued inspection, Cycle-B retrieval, second-order candidate, concept grounding, rights preservation, and replay idempotency.

**Verification:** Machine-readable receipts contain hashes, lineage, work IDs, gate results, and zero manually injected Cycle-B relationships.

### Task 7: Full verification, independent closure review, and commit

**Objective:** Freeze a trustworthy completion state.

**Steps:**
1. Run targeted suites.
2. Run complete repository suite.
3. Freeze current bytes/commit candidate and run independent P0/P1 review against the locked criteria.
4. If needed, perform one bounded remediation and one final review.
5. Inspect diff/secrets/status.
6. Commit the verified tree on `agent/self-expansion-proof` without pushing or deploying unrelated systems.
7. Write a GBrain completion receipt with exact maturity labels and source pointers. This is an operator-owned post-commit observability/readback step, not a behavioral code-acceptance gate. A GBrain outage records `operational_receipt_pending` and does not invalidate an otherwise accepted immutable commit; it does block claiming end-to-end operational closure until readback succeeds.

**Final verification:** Behavioral acceptance requires a clean worktree, commit SHA readback, current-byte test receipts, exact scheduled-wrapper receipt, and independent no-P0/P1 verdict. Operational closure additionally requires the operator-owned GBrain page readback under the failure semantics above.
