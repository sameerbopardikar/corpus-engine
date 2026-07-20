# Independent P1 Remediation Ledger

## Supersession and closure

**Status: historically superseded for the generalizable-training V1 release candidate.** The P1 findings below are retained verbatim as provenance, but they are no longer open release gates. Their discovery, priority, adapter, and global-cycle remediations are covered by the existing focused receipts and by `.hermes/receipts/2026-07-20T024409Z-generalizable-training-v1-release-candidate.json`, which binds the fully tested code baseline at `a631eb382c9bd90febcb3c066418b8435540927e`.

The older `.hermes/receipts/2026-07-17T090746Z-agentic-engineering-v1-verified-complete.json` remains unchanged and certifies only its historical `f426861…` campaign. It is not current release proof.

## Historical findings

- Replay must reject impossible state replacement such as `pending → done` without valid lease/transition events.
- Completion proof must be digest-bound, artifact-backed, verifier-identified, and authorized; non-empty self-attestation is insufficient.
- Acquire/promote must require an existing domain-matched candidate with an enumerated rights-clear state.
- Budget admission must be atomically reserved at lease/admission and survive concurrent controller ticks and crash/retry idempotency.
- Conflicting reuse of an idempotency key must fail closed without durable mutation.
- Expired leases must be reclaimed or dead-lettered automatically.
- Atomic writes must reject pre-planted symlinks and include durable rollback/readback proof.

## Core adapters

- Rights states must be enforced, not descriptive: metadata-only/unclear/private sources may not fetch/store/project full bodies or captions.
- YouTube cursor must eventually emit the full inventory exactly once and then emit only new items.
- Stable inventory must still retry pending captions.
- Engine-level no-delta retries must be physically idempotent with persisted cursor: unchanged bytes, inode, mtime, and batch count.
- Captions/transcripts must use immutable content-addressed raw and normalized paths whose hashes are verified before projection; historical revisions remain retrievable.
- Restore a meaningful minimum-content guard for web documents and test short anti-bot/error pages.

## Priority policy — closed on `27d1952`

- [x] Replaced check-only caller-supplied usage with one durable atomic daily reservation ledger covering tasks, deep acquisitions, tokens, and cost; concurrent full-cap admissions allow exactly one reservation.
- [x] Strict policy JSON parsing rejects duplicate keys and unknown/misspelled nested keys.
- [x] A reserved aging slot provides deterministic eventual service.
- [x] Reservation replay now binds each stored plan to its canonical request preimage, sequence, UTC day, and replay-derived prior usage; coherent plan rewrites fail closed.
- [x] Exact retries and conflicting reservation-ID reuse preserve state and lock bytes/inode/mtime/ctime.

Evidence: `.hermes/receipts/2026-07-17T071203Z-independent-p1-priority-remediation.json`; focused 16/16 and full 146/146 current-byte tests green.

## Closure rule

For each item: add a failing adversarial test, implement the minimal fix, run focused and full suites on current bytes, record commit/hash/receipt, and request an exact-current-commit read-only review before the vertical/deployment gate.
