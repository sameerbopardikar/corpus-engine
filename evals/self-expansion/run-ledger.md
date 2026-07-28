# Self-Expansion Proof Run Ledger

| Run | Workflow/scenario | Observed failure | Expected behavior | Failure class | Root cause | Generalized fix | Anti-overfit evidence | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Untouched source-graph branch | Unit suite passes, but all five autonomous behavior gates remain unproven or failed | Deterministic rotation, artifact-derived edges, policy disposition, Cycle-B expansion, blinded concept emergence | Architecture mismatch / missing invariants | Durable candidate admission exists without deterministic radar ownership, artifact extraction, promotion policy, or recursive eval controller | Add one shared deterministic state machine and one source-family-agnostic proof harness; do not add target queries or fixture names | Require non-identical holdout plus no target leakage | failed baseline |
| 2 | Blinded AgentDojo holdout | Promotion, rights preservation, and second-order expansion passed; worker described benchmark composition but omitted the untrusted-data threat model required by the frozen evaluator | Recover the underlying problem, mechanism/state flow, and why it changes behavior, not merely the runnable interface | Instruction-following / evaluator-contract mismatch | The worker contract allowed a product-operation summary to count as a concept candidate and did not require problem → mechanism → consequence structure | Require every concept candidate to state the constraint, mechanism/state flow, cross-artifact consequence, and grounded evidence; validate on a non-identical historical holdout | Scratch-pad holdout was frozen before the revised worker ran | closer; unknown-concept gate failed |

## Closure pass and blinded run 3

The closure pass repaired the original seven P1 proof-invalidating defects and
completed the durable recurring-inspection path. The first exact-current review
then found three additional concrete mechanics defects: canonical-target
substitution inside a valid span, expired inspection leases that stayed stranded,
and full-body preservation without a rights gate. All three now have adversarial
regressions. The watch consumer releases expired leases, rejects full-body work
unless the durable candidate has `public_rights_clear` or `private_authorized`,
and semantic claims bind the canonical target to the entity locator/name in the
validated span.

Machine-readable receipts produced on current bytes:

- `two-cycle-receipt.json` — isolated exact two-cycle proof, `overall_status: passed`,
  all six gates true, zero packet-supplied Cycle-B relationships. Cycle B is produced
  by the durable watch consumer leasing the `inspect` work item that Cycle A's
  promotion enqueued, fetching rights-authorized artifacts bound to that promoted
  source, and deriving every edge through production extractors from preserved bytes.
- `scheduled-wrapper-proof-receipt.json` — the recurring shell wrapper invoked the
  same proof path under an isolated root while the normal global cycle remained in
  dry planning mode; legacy compatibility shims were disabled and production cron
  was not modified.
- `scratchpad-holdout-freeze-receipt.json` — preserves the truthful negative result
  for the original hidden label: its worker packet leaked `scratchpad` and `active memory`.
- `state-feedback-holdout-v2-freeze-receipt.json` — hidden evaluator frozen before
  the worker ran; the complete worker-visible packet did not name the higher-order
  target `co-authored execution-state feedback loop` or accepted aliases.
- `state-feedback-holdout-v2-worker-output.json` — independent blinded worker output.
- `state-feedback-holdout-v2-grounded-evaluation.json` — independent evaluator pass:
  all six required dimensions bound to exact packet artifact bytes by URL, full-text
  SHA-256, exact quote, and validated character span; both Cycle-A → Cycle-B lineage
  edges passed.
- `agentdojo-run-2-freeze-receipt.json` — complete retrospective leakage scan for run 2.

### Retrospective findings preserved, not hidden

1. **Run 2's old `leakage_absent` claim was invalid.** The legacy harness scanned only
   `seed_packet`; the complete packet leaked all four forbidden terms.
2. **The original scratchpad-label holdout was not blinded.** Its source material names
   `scratchpad` and `active memory`, so that exact-label test remains a recorded failure.
3. **Run 3 tests an unnamed higher-order mechanism rather than relabeling that failure.**
   The evaluator was frozen before worker execution, the packet passed the target/alias
   leakage scan, and an independent evaluator grounded the recovered state-feedback
   mechanism to exact source bytes. The old negative receipt remains unchanged.

`fixtures/agentdojo-proof-config.json` remains the historical run-2 record. It is a
`schema_version: 1` packet that supplied Cycle-B relationships directly, so the
hardened harness refuses it by design.

## Run budget

- Used: 3 / 5
- Remaining: 2
- Baseline unit tests: 612 passed
- Current closure suite: 735 passed
- Run 2: historical unknown-concept failure; its old leakage pass was invalidated
- Run 3: higher-order target leakage scan passed; blinded worker recovered all six
  dimensions; independent exact-byte grounding and lineage evaluation passed
- Production mutation during runs 1-3: none

## Frozen finish line

1. X rotation survives restart and failure.
2. Five source families produce grounded edges and rights-unclear candidates.
3. Policy dispositions and promotes without manual selection.
4. Cycle A watch expansion affects Cycle B.
5. A blinded non-identical target mechanism is surfaced without label leakage.
