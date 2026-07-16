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

## Baseline

```bash
python3 tests/test_corpus_engine.py
python3 src/corpus_engine.py validate --domain agentic-engineering
```

The live corpus registry remains under `/root/corpora/agentic-engineering/`; this repository owns engine code and tests, not corpus data.
