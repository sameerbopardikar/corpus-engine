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

## Baseline

```bash
python3 tests/test_corpus_engine.py
python3 src/corpus_engine.py validate --domain agentic-engineering
```

The live corpus registry remains under `/root/corpora/agentic-engineering/`; this repository owns engine code and tests, not corpus data.
