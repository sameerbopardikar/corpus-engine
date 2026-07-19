# End-to-End `start a corpus on X` Orchestrator Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make the exact trigger `start a corpus on <topic>` invoke one resumable workflow that cannot report success until it has produced a field map, discovered/acquired evidence, a native GBrain corpus, and a working Atlas; prove it from an empty root using only `Nutrition` as domain input.

**Architecture:** Keep Hermes as the reasoning/scouting layer and make a deterministic `corpus-start` CLI the workflow authority. The CLI owns a phase ledger and validates one strict bootstrap packet, then reuses the existing domain spec, bootstrap, acquisition, GBrain, scheduler, and Atlas components. The skill may perform web scouting, but it cannot manually substitute files or stop early because `corpus-start status` is the sole completion authority.

**Tech Stack:** Python 3 stdlib corpus engine, existing GBrain CLI/source `corpora`, existing Node/React Research Atlas release, Hermes skill trigger, JSON phase ledger, unittest/Node/Playwright.

---

## Bounded scope

### In scope

- One executable orchestrator for the exact natural-language trigger.
- Strict topic-only bootstrap packet.
- Existing generic field map/domain contract, source discovery/acquisition, corpus structure, GBrain sync, scheduler attachment, and Atlas instantiation wired into one resumable state machine.
- Empty-root Nutrition proof.
- Resume the existing partial Nutrition corpus after the clean proof, without deleting it.
- Local Atlas proof; private `/nutrition/` deployment only after local proof passes.

### Explicitly out of scope

- Corpus-only Ask.
- Friend accounts, contributors, or agent sharing.
- Multi-corpus convergence.
- Three unattended-cycle observation.
- New source-family framework beyond what Nutrition needs for one initial public wave.
- Corpus completeness.
- Automatic doctrine promotion.
- Redesigning the Atlas.

## Current truth

- Existing components are real: strict `DomainSpec`, `bootstrap_domain`, field-map projection, scholarly discovery, bounded acquisition, raw/checksum preservation, normalization/evaluation, GBrain source, one global scheduler/budget, and an environment-configurable Atlas.
- The missing primitive is orchestration. The failed Nutrition run used `/root/.hermes/scripts/corpus_engine.py`, copied Training’s structure, manually wrote pages, and stopped before Atlas creation.
- `bootstrap_domain()` currently creates only roots and a marker by design.
- `scripts/corpus_global_cycle.py` currently requires checked-in domain specs/seeds.
- The Atlas already accepts `CORPUS_ROOT` and `ATLAS_PRODUCT_NAME`; it needs instantiation, not another UI build.
- Existing `corpora:nutrition` is a useful but contaminated partial seed. Preserve it; do not call it the clean proof.

## Completion contract

`corpus-start status --run <id> --json` must return all of:

```json
{
  "status": "complete",
  "topic_input": "Nutrition",
  "clean_root": true,
  "field_map_ready": true,
  "sources_discovered": 1,
  "sources_acquired": 1,
  "gbrain_sync_verified": true,
  "atlas_ready": true,
  "scheduler_owner_count": 1,
  "manual_seed_substitution": false
}
```

Any missing field means the agent must continue. “Operational seed” is not a terminal status.

---

### Task 1: Add the resumable start-run ledger

**Objective:** Establish one deterministic authority for workflow phase, retries, and completion.

**Files:**
- Create: `src/corpus_start_state.py`
- Create: `tests/test_corpus_start_state.py`

**Steps:**

1. Write failing tests for:
   - safe run/domain IDs;
   - exact ordered phases;
   - atomic state writes;
   - retry no-op for completed phases;
   - conflicting topic/packet rejection;
   - `complete` forbidden unless every required receipt exists.
2. Run:
   ```bash
   python3 -m unittest tests.test_corpus_start_state -v
   ```
   Expected: failing imports.
3. Implement:
   ```python
   PHASES = (
       "initialized",
       "bootstrap_packet_validated",
       "domain_bootstrapped",
       "initial_acquisition_complete",
       "gbrain_verified",
       "atlas_verified",
       "scheduler_verified",
       "complete",
   )
   ```
4. Store state under `<run-root>/.corpus-start/run.json`; bind every transition to topic, domain slug, packet SHA-256, prior phase, receipt SHA-256, and timestamp.
5. Re-run tests and `git diff --check`.
6. Commit: `feat: add corpus start phase ledger`.

---

### Task 2: Define the strict topic-only bootstrap packet

**Objective:** Let Hermes reason about an arbitrary topic without hand-authoring canonical corpus files.

**Files:**
- Create: `src/corpus_bootstrap_packet.py`
- Create: `tests/test_corpus_bootstrap_packet.py`
- Create: `tests/fixtures/nutrition-bootstrap-packet.json`
- Modify: `src/corpus_domain_spec.py`

**Packet fields:**

```json
{
  "schema_version": 1,
  "topic_input": "Nutrition",
  "domain": "nutrition",
  "title": "Nutrition Research Corpus",
  "objective": "...",
  "axes": [{"key": "...", "title": "...", "topics": ["..."]}],
  "evidence_lanes": {"systematic-review": 1.25, "default": 0.85},
  "source_families": [{"family": "...", "acquisition_mode": "public_open_access_fetch"}],
  "candidate_locators": [{"url": "https://...", "title": "...", "topics": ["..."]}],
  "scout_provenance": [{"query": "...", "result_url": "https://..."}],
  "existing_context_refs": []
}
```

**Steps:**

1. Test exact-key validation, finite numbers, safe slugs, minimum axis/topic/source diversity, HTTPS locators, duplicate rejection, and `existing_context_refs == []` for clean mode.
2. Test packet → generated `DomainSpec` JSON and seed bundle without Training-specific values.
3. Test that the packet cannot write corpus pages directly.
4. Implement `validate_bootstrap_packet()` and `compile_domain_inputs()`.
5. Keep generic defaults in code; keep domain content only in the packet.
6. Re-run all domain-spec/bootstrap tests.
7. Commit: `feat: compile topic bootstrap packets into domain inputs`.

---

### Task 3: Build the `corpus-start` CLI/state machine

**Objective:** Connect the existing components and prevent seed-only completion.

**Files:**
- Create: `scripts/corpus_start.py`
- Create: `src/corpus_start.py`
- Create: `tests/test_corpus_start.py`
- Modify: `README.md`

**CLI:**

```bash
corpus-start begin --topic Nutrition --run-root /tmp/nutrition-proof
corpus-start apply-packet --run /tmp/nutrition-proof --packet /tmp/nutrition-packet.json
corpus-start continue --run /tmp/nutrition-proof
corpus-start status --run /tmp/nutrition-proof --json
```

**Steps:**

1. Write a failing integration test proving `begin` only creates the ledger and emits `next_action=produce_bootstrap_packet`.
2. Write a failing test proving `continue` executes, in order:
   - packet validation;
   - generated config/seed write inside run root;
   - `bootstrap_domain()`;
   - `run_domain_cycle()` field map/next gap;
   - `run_execute()` initial acquisition;
   - corpus index/coverage/eval projections generated from receipts;
   - GBrain adapter step;
   - Atlas adapter step;
   - scheduler verification.
3. Inject acquisition, GBrain, Atlas, and scheduler functions so tests remain network/service-free.
4. Make every phase resumable and physically idempotent.
5. Reject completion when the acquisition count is zero, Atlas health is absent, or the agent manually created a source page outside the orchestrator receipt set.
6. Add `--resume-live-root /root/corpora` for a non-clean existing namespace, clearly labeled `clean_root=false`.
7. Run focused and full suites.
8. Commit: `feat: orchestrate corpus start end to end`.

---

### Task 4: Add the Hermes scout handoff without manual corpus assembly

**Objective:** Make the skill produce only the strict packet, then surrender workflow control to `corpus-start`.

**Files:**
- Modify: active-profile skill `thinker-corpus-metabolism/SKILL.md`
- Create: `thinker-corpus-metabolism/references/corpus-start-packet.md`
- Create: `tests/test_corpus_start_skill_contract.py`

**Steps:**

1. Add a deterministic contract test that the skill contains the exact trigger and commands.
2. Require the skill to:
   - run `corpus-start begin` first;
   - scout only public web/search sources;
   - write one packet to the path requested by the CLI;
   - run `apply-packet`, then `continue` until complete;
   - never use `write_file` under the canonical domain root during a clean proof;
   - never return a seed-only final.
3. Add a final-response guard: read `corpus-start status --json`; only `status=complete` permits “done.”
4. Keep the exact trigger already installed: `start a corpus on`.
5. Commit skill support files only after the CLI tests pass.

---

### Task 5: Make Atlas instantiation a reusable adapter

**Objective:** Reuse the existing Atlas release for any corpus without copying or redesigning the app.

**Files:**
- Create: `src/corpus_atlas_adapter.py`
- Create: `tests/test_corpus_atlas_adapter.py`
- Modify: `src/App.tsx` only to remove unavoidable hard-coded “Training” error/product strings.
- Modify: `server.mjs` only if additional env fields are required.

**Steps:**

1. Test generated Atlas launch configuration includes:
   - `CORPUS_ROOT=<run-root>/corpora/<domain>`;
   - `ATLAS_PRODUCT_NAME=<Title> Research Atlas`;
   - queue/acquisition paths;
   - loopback host and selected free port;
   - no embedded secret.
2. Test the adapter launches the existing immutable Atlas command in foreground-test mode, waits for `/healthz` and `/readyz`, verifies `/api/index` product/domain, and returns a receipt.
3. Replace hard-coded Training session-expiry text with `PRODUCT_NAME`-derived language.
4. Do not create a new React project or domain-specific UI.
5. Run Node tests, build, focused Playwright desktop Chromium and iPhone WebKit.
6. Commit: `feat: instantiate the research atlas by corpus config`.

---

### Task 6: Install one runtime command

**Objective:** Make the natural-language skill route to the verified immutable engine release rather than the legacy refresher.

**Files:**
- Create: `scripts/install_corpus_start.py`
- Create: `tests/test_install_corpus_start.py`
- Runtime artifact after approval/execution: `/usr/local/bin/corpus-start`

**Steps:**

1. Test the installer refuses dirty/unverified source bytes.
2. Package an immutable engine release under `/opt/agentic-corpus-intake/releases/<commit>/`.
3. Point `/usr/local/bin/corpus-start` to that release’s `scripts/corpus_start.py`.
4. Verify:
   ```bash
   corpus-start --help
   corpus-start begin --topic Smoke --run-root /tmp/corpus-start-smoke
   ```
5. Confirm `/root/.hermes/scripts/corpus_engine.py` remains only the legacy registry refresher and is never called by the exact trigger.
6. Exercise rollback to the previous symlink/absence and restore.

---

### Task 7: Run the clean Nutrition proof

**Objective:** Prove the fixed workflow from an empty root with `Nutrition` as the only domain input.

**Isolation:**

- Run root: `/root/workspace/corpus-start-proofs/nutrition-<timestamp>/`
- No reads from `/root/corpora/nutrition`, `/root/corpora/training`, their registries, or the default personal brain.
- Use a fresh leaf worker whose brief contains only the topic, generic policies, command path, and proof root.
- Public web/search acquisition only; no paid/login-gated material.

**Steps:**

1. Snapshot an empty run root.
2. Invoke the exact trigger workflow.
3. Require at least:
   - 4 substantive field axes;
   - 3 source families;
   - 5 discovered public candidates;
   - 1 acquired full-text source with raw/normalized SHA-256;
   - 1 explicit unresolved/gated source if encountered, but do not block completion on it;
   - source-scoped retrieval through an isolated proof source/DB adapter;
   - local Atlas health/index/acquisition views;
   - exact retry no fetch/no rewrite.
4. Independently compare output against Training and existing Nutrition files to detect copied ontology/source lists.
5. Verify `status=complete` and write a machine-readable proof receipt.
6. Stop and remove the temporary Atlas process after screenshots/QA; preserve the proof root.

---

### Task 8: Resume the real Nutrition namespace and create its private Atlas

**Objective:** Turn the existing useful seed into the finished product without claiming it was the clean proof.

**Steps:**

1. Back up `/root/corpora/nutrition` and record commit/hash inventory.
2. Run:
   ```bash
   corpus-start begin --topic Nutrition --run-root <state-root> --resume-live-root /root/corpora
   corpus-start apply-packet --run <state-root> --packet <accepted-clean-proof-packet>
   corpus-start continue --run <state-root>
   ```
3. Reconcile existing pages by stable slug; do not overwrite conflicting authored content silently.
4. Sync source `corpora` and verify Nutrition retrieval.
5. Instantiate the existing Atlas release for `/root/corpora/nutrition`.
6. Deploy owner-only at `https://sameerbopardikar.com/nutrition/` using the existing private-session proxy pattern.
7. Run essential production QA: anonymous redirect, owner/no-second-login, collaborator denial, desktop Chromium, iPhone WebKit, accessibility, console, overflow, rollback.
8. Write final receipt distinguishing:
   - clean isolated proof;
   - resumed canonical Nutrition product;
   - no claim of corpus completeness.

---

## Verification commands

```bash
# Engine
python3 -m compileall -q src scripts
python3 -m unittest discover -s tests -v
bash -n scripts/corpus_engine_cycle_v1.sh
git diff --check

# Atlas
npm run check
npm run test:e2e -- e2e/acquisition.spec.ts
git diff --check

# Runtime
corpus-start status --run <proof-root> --json
curl -fsS http://127.0.0.1:<proof-port>/healthz
curl -fsS http://127.0.0.1:<proof-port>/readyz

# GBrain canonical readback after proof acceptance
GBRAIN_DISABLE_DIRECT_POOL=1 gbrain sync --source corpora --no-pull
gbrain query "nutrition protein evidence" --source corpora
```

## Release gate

Do not call the engine repeatable until all are true:

- exact trigger invoked the new runtime command, not the legacy refresher;
- empty-root Nutrition proof completed without prior corpus/brain/domain inputs;
- one real source was acquired and exact retry was a physical no-op;
- GBrain retrieval passed;
- a local Nutrition Atlas was healthy and usable;
- existing canonical Nutrition was resumed separately and not misrepresented as clean;
- no P0/P1 findings remain;
- rollback receipt exists.

## Expected implementation bound

- One engine worktree.
- One Atlas worktree only if hard-coded product text must change.
- No more than eight tasks above.
- One independent review after all focused/full tests pass, not repeated speculative review loops.
- Stop after Nutrition local proof and canonical `/nutrition/` product verification; do not expand into Ask, sharing, or multi-corpus work.
