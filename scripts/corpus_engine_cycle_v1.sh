#!/usr/bin/env bash
# Single recurring scheduler for the generalized Corpus Engine.
#
# The body is domain-agnostic: it enumerates every checked-in domain spec and
# runs ONE global cycle against ONE shared budget authority. There is exactly
# one scheduler and one budget; adding a domain is adding a spec file, never a
# second cron or a per-domain budget. By default it runs bounded execute mode
# (discover → rights → admit budget-selected evidence per domain under
# $CORPUS_CORPORA_ROOT, default /root/corpora); CORPUS_ACQUISITION_EXECUTE=0
# forces a safe planning-only dry run. Legacy Agentic Engineering intake/shadow
# steps run only as optional compatibility shims when their deployed binaries
# exist, so no Agentic-only assumption is baked into the scheduler itself.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${CORPUS_CONFIG_DIR:-$REPO_ROOT/config/domains}"
BUDGET_PATH="${CORPUS_BUDGET_PATH:-/root/exports/thinker-corpora/_engine/budget.json}"
CORPORA_ROOT="${CORPUS_CORPORA_ROOT:-/root/corpora}"
# Bounded execute mode is the default: one scheduler, one global budget, and
# per-domain isolated roots under $CORPORA_ROOT. Set CORPUS_ACQUISITION_EXECUTE=0
# for a safe planning-only dry run that touches no corpus bytes.
EXECUTE="${CORPUS_ACQUISITION_EXECUTE:-1}"
LEGACY_SHIMS="${CORPUS_LEGACY_SHIMS:-1}"
SELF_EXPANSION_PROOF_CONFIG="${CORPUS_SELF_EXPANSION_PROOF_CONFIG:-}"
SELF_EXPANSION_PROOF_ROOT="${CORPUS_SELF_EXPANSION_PROOF_ROOT:-}"
SELF_EXPANSION_CONFIG="${CORPUS_SELF_EXPANSION_CONFIG:-}"
# Test-only deterministic clock used by isolated integration fixtures.
SELF_EXPANSION_TEST_NOW="${CORPUS_SELF_EXPANSION_TEST_NOW:-}"
GLOBAL_RESULT="$(mktemp)"
trap 'rm -f "$GLOBAL_RESULT"' EXIT

if [[ -n "$SELF_EXPANSION_PROOF_CONFIG" || -n "$SELF_EXPANSION_PROOF_ROOT" ]]; then
  if [[ -z "$SELF_EXPANSION_PROOF_CONFIG" || -z "$SELF_EXPANSION_PROOF_ROOT" ]]; then
    echo "CORPUS_SELF_EXPANSION_PROOF_CONFIG and CORPUS_SELF_EXPANSION_PROOF_ROOT must be set together" >&2
    exit 2
  fi
fi

# One global, manifest-driven cycle across all enabled domains. In execute mode
# it discovers, resolves rights fail-closed, and admits budget-selected rights-
# clear evidence per domain; in dry-run mode it only plans.
CYCLE_ARGS=(--config-dir "$CONFIG_DIR" --budget-path "$BUDGET_PATH")
if [[ -n "$SELF_EXPANSION_CONFIG" ]]; then
  CYCLE_ARGS+=(--self-expansion-config "$SELF_EXPANSION_CONFIG")
fi
if [[ -n "$SELF_EXPANSION_TEST_NOW" ]]; then
  CYCLE_ARGS+=(--self-expansion-now "$SELF_EXPANSION_TEST_NOW")
fi
if [[ "$EXECUTE" != "0" ]]; then
  CYCLE_ARGS+=(--execute --corpora-root "$CORPORA_ROOT")
fi
python3 "$REPO_ROOT/scripts/corpus_global_cycle.py" "${CYCLE_ARGS[@]}" > "$GLOBAL_RESULT"
cat "$GLOBAL_RESULT"

# Optional acceptance-proof lane. It is disabled unless both paths are supplied,
# writes only beneath the caller-selected marked proof root, and does not widen
# the production acquisition or promotion policy. Running it through this exact
# wrapper proves the timer-owned entrypoint can execute the persisted two-cycle
# path without introducing another scheduler.
if [[ -n "$SELF_EXPANSION_PROOF_CONFIG" ]]; then
  python3 "$REPO_ROOT/scripts/corpus_self_expansion_proof.py" \
    --config "$SELF_EXPANSION_PROOF_CONFIG" \
    --run-root "$SELF_EXPANSION_PROOF_ROOT"
fi

# Optional compatibility shim: preserve the proven Agentic Engineering intake +
# shadow replay when (and only when) their deployed binaries are present. This
# keeps existing deployments working without making the scheduler Agentic-only.
if [[ "$LEGACY_SHIMS" != "0" ]] \
   && command -v corpus-intake >/dev/null 2>&1 \
   && [[ -d /root/corpora/agentic-engineering ]]; then
  CORPUS_INTAKE_ROOT="${AGENTIC_CORPUS_INTAKE_ROOT:-/var/lib/agentic-corpus-intake}" \
    corpus-intake process --all \
      --corpus-root /root/corpora/agentic-engineering || true
fi

if [[ "$LEGACY_SHIMS" != "0" ]] \
   && command -v agentic-engineering-shadow >/dev/null 2>&1 \
   && [[ -f /root/.hermes/.env ]]; then
  set -a; source /root/.hermes/.env; set +a
  export GBRAIN_DISABLE_DIRECT_POOL=1
  SHADOW_RESULT="$(mktemp)"
  trap 'rm -f "$GLOBAL_RESULT" "$SHADOW_RESULT"' EXIT
  agentic-engineering-shadow \
    --cycle-id "shadow-20260717-agentdojo-v1b" > "$SHADOW_RESULT" || true
  python3 - "$SHADOW_RESULT" <<'PY'
import json, sys
from pathlib import Path
try:
    receipt = json.loads(Path(sys.argv[1]).read_text())
except Exception:
    raise SystemExit(0)
if receipt.get("status") != "verified_shadow_complete":
    raise SystemExit("deployed Agentic Engineering shadow receipt is not verified complete")
if not receipt.get("evaluation", {}).get("passed"):
    raise SystemExit("deployed Agentic Engineering shadow evaluation did not pass")
if receipt.get("automatic_promotion_enabled") is not False:
    raise SystemExit("deployed Agentic Engineering shadow unexpectedly enabled promotion")
PY
fi
