# Self-Expanding Corpus Engine

Private implementation workspace for Sameer's shared GBrain-native Corpus Engine.

The first production proof is the Agentic Engineering corpus. The architecture remains domain-general:

`discover → score → acquire → preserve → normalize → sync/embed → retrieve → synthesize → evaluate → refresh`

## Boundaries

- One federated GBrain source: `corpora`.
- One shared engine and global scheduler.
- Raw evidence remains checksummed and retrievable.
- Cheap discovery precedes selective deep ingestion.
- External claims never silently become Sameer's beliefs.
- Runtime deployment into `~/.hermes/scripts/` occurs only after tests, shadow verification, backup, and rollback preparation.

## Personal V0 — use now

The first usable slice is local, sequential, deterministic-first, and promotion-disabled:

```bash
./scripts/agentic-engineering-v0 --cycle-id "$(date -u +%F)-personal"
```

It validates and loads the reviewed Agentic Engineering candidate seed, scans the current corpus for topic coverage, ranks evidence gaps, queues bounded inspection work, and writes:

- `/root/corpora/agentic-engineering/discovery/personal-v0/latest.md`
- `/root/corpora/agentic-engineering/discovery/personal-v0/latest.json`
- `/root/exports/thinker-corpora/agentic-engineering/self-expansion-v0/discovery-ledger.jsonl`

It does **not** promote sources or mutate Expert/Percival production. Use `--dry-run` for a zero-write preview and `--queue-top N` to control how many inspect items enter the local queue.

## Generic global cycle — dry run and execute

One scheduler and one global budget drive every checked-in domain spec. The
generic cycle has two modes; both use stdlib-only, SSRF-guarded transport (no
`requests` dependency).

Safe planning dry run (no network acquisition, no corpus mutation):

```bash
# explicit dry run of the entrypoint
python3 scripts/corpus_global_cycle.py --config-dir config/domains \
  --budget-path /tmp/engine-budget.json

# or via the recurring wrapper, forced to plan only
CORPUS_ACQUISITION_EXECUTE=0 CORPUS_CORPORA_ROOT=/tmp/corpora-smoke \
  bash scripts/corpus_engine_cycle_v1.sh
```

Bounded execute mode (discover → resolve rights fail-closed → acquire only
budget-selected rights-clear content → admit eval-passed evidence per domain
into `<corpora-root>/<domain>/sources/acquired/` with an emitted redacted
projection at `<corpora-root>/<domain>/acquisition/latest.json`):

```bash
# direct entrypoint against a scratch root (never write /root/corpora in tests)
python3 scripts/corpus_global_cycle.py --execute \
  --config-dir config/domains \
  --budget-path /tmp/engine-budget.json \
  --corpora-root /tmp/corpora-smoke

# the recurring wrapper defaults to execute mode against $CORPUS_CORPORA_ROOT
# (default /root/corpora); override the root for a smoke test
CORPUS_CORPORA_ROOT=/tmp/corpora-smoke bash scripts/corpus_engine_cycle_v1.sh
```

Rights are fail-closed: only CC0/public-domain, CC-BY, and CC-BY-SA are
auto-ingested as normalized text; NC/ND licenses and PDFs route to a human/
extractor gate; everything else stays metadata-only or unclear. Idempotency is
bound to domain + candidate identity + source revision, so re-running unchanged
discovery on a later day refetches and rewrites nothing.

## Start a corpus from a topic — `corpus-start`

`start a corpus on <topic>` routes to one deterministic, resumable orchestrator
(`scripts/corpus_start.py`) instead of ad-hoc manual seeding. The orchestrator
owns a phase ledger and can never report success until it has produced a field
map, acquired at least one source, and verified GBrain retrieval, Atlas
health/readiness, and scheduler uniqueness.

```bash
corpus-start begin        --topic Nutrition --run-root /tmp/nutrition-proof
corpus-start apply-packet --run /tmp/nutrition-proof --packet /tmp/nutrition-packet.json
corpus-start continue     --run /tmp/nutrition-proof
corpus-start status       --run /tmp/nutrition-proof --json
```

- `begin` only creates `<run-root>/.corpus-start/run.json` and asks for a
  strict topic-only bootstrap packet (see
  `tests/fixtures/nutrition-bootstrap-packet.json`).
- `apply-packet` validates the packet (clean mode rejects any
  `existing_context_refs`) and compiles a generic `DomainSpec` + candidate seed
  inside the run root — it can never hand-author corpus pages.
- `continue` runs bootstrap → field map → acquisition → GBrain → Atlas →
  scheduler in a resumable, physically idempotent phase order.
- `status --json` is the sole completion authority. `status=complete` is
  impossible without every required receipt, one acquired source, a healthy
  Atlas, a unique scheduler owner, and no manual seed substitution.

Acquisition, GBrain, Atlas, and scheduler are injected boundaries so the
orchestration is fully test-deterministic; `src/corpus_atlas_adapter.py` reuses
the existing environment-configurable Atlas release (`CORPUS_ROOT`,
`ATLAS_PRODUCT_NAME`) rather than building another frontend. Use
`--resume-live-root /root/corpora` to resume a non-clean existing namespace
(clearly labeled `clean_root=false`).

## Baseline

```bash
python3 tests/test_corpus_engine.py
python3 src/corpus_engine.py validate --domain agentic-engineering
```

The live corpus registry remains under `/root/corpora/agentic-engineering/`; this repository owns engine code and tests, not corpus data.
