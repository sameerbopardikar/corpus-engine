#!/usr/bin/env bash
set -euo pipefail

CORPUS_ROOT=/root/corpora
REPORT_DIR="$CORPUS_ROOT/agentic-engineering/discovery/personal-v0"
CYCLE_ID="$(date -u +%F)-personal-v0"
SHADOW_CYCLE_ID="shadow-20260717-agentdojo-v1b"
RESULT="$(mktemp)"
SHADOW_RESULT="$(mktemp)"
SYNC_LOG="$(mktemp)"
trap 'rm -f "$RESULT" "$SHADOW_RESULT" "$SYNC_LOG"' EXIT

/usr/local/bin/agentic-engineering-corpus \
  --cycle-id "$CYCLE_ID" \
  --queue-top 5 > "$RESULT"

# Replay the accepted V1 vertical through the deployed package. The fixed cycle
# identity makes routine runs a physical no-op while still verifying the stable
# receipt before the agent selects the next corpus gap.
set -a
source /root/.hermes/.env
set +a
export GBRAIN_DISABLE_DIRECT_POOL=1
/usr/local/bin/agentic-engineering-shadow \
  --cycle-id "$SHADOW_CYCLE_ID" > "$SHADOW_RESULT"

python3 - "$SHADOW_RESULT" <<'PY'
import json
import sys
from pathlib import Path

receipt = json.loads(Path(sys.argv[1]).read_text())
if receipt.get("status") != "verified_shadow_complete":
    raise SystemExit("deployed Agentic Engineering shadow receipt is not verified complete")
if not receipt.get("evaluation", {}).get("passed"):
    raise SystemExit("deployed Agentic Engineering shadow evaluation did not pass")
if receipt.get("automatic_promotion_enabled") is not False:
    raise SystemExit("deployed Agentic Engineering shadow unexpectedly enabled promotion")
PY

cd "$CORPUS_ROOT"
git add \
  "$REPORT_DIR/latest.md" \
  "$REPORT_DIR/latest.json" \
  "$REPORT_DIR/cycle-$CYCLE_ID.json"

changed=false
if ! git diff --cached --quiet; then
  git commit -m "chore: refresh Agentic Engineering personal V0"
  changed=true
fi

if [[ "$changed" == true ]]; then
  export GBRAIN_POOL_SIZE=1
  gbrain sync --source corpora --no-pull > "$SYNC_LOG" 2>&1

  python3 - "$RESULT" <<'PY'
import json
import sys
from pathlib import Path

result = json.loads(Path(sys.argv[1]).read_text())
top = result["ranked_candidates"][0]
uncovered = [topic for topic, hits in top["topic_page_hits"].items() if hits == 0]
print(
    "Agentic Engineering personal V0 refreshed and synced: "
    f"top={top['seed_id']}; priority={top['priority_score']}; "
    f"uncovered={','.join(uncovered) or 'none'}; "
    "page=agentic-engineering/discovery/personal-v0/latest"
)
PY
fi
