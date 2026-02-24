#!/usr/bin/env bash
# =============================================================================
# health-check.sh — Ping all nodes in config/nodes.json
# =============================================================================
# Reports healthy / unhealthy status and optionally updates nodes.json.
# Used by the Lambda scaler to identify available inference endpoints.
#
# Exit codes:
#   0 — at least one node is healthy
#   1 — all nodes are unhealthy (or no nodes configured)
#
# Output (stdout): updated JSON (same shape as nodes.json) with current status
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
NODES_FILE="${NODES_FILE:-$PROJECT_DIR/config/nodes.json}"
TIMEOUT="${HEALTH_CHECK_TIMEOUT:-5}"    # curl timeout per node (seconds)
UPDATE_FILE="${UPDATE_NODES_FILE:-1}"   # set to 0 to skip writing updated status

if ! command -v jq &>/dev/null; then
  echo "❌  jq is required." >&2; exit 1
fi

if [[ ! -f "$NODES_FILE" ]]; then
  echo "❌  No nodes.json at $NODES_FILE" >&2
  echo '{"nodes":[]}'; exit 1
fi

NODE_COUNT=$(jq '.nodes | length' "$NODES_FILE")
if [[ "$NODE_COUNT" -eq 0 ]]; then
  echo "ℹ️  No nodes configured." >&2
  echo '{"nodes":[]}'; exit 1
fi

echo "🔍 Health-checking $NODE_COUNT node(s) ..." >&2

HEALTHY=0
UNHEALTHY=0
UPDATED_NODES="[]"

while IFS= read -r NODE; do
  IP=$(echo "$NODE"   | jq -r '.ip')
  PORT=$(echo "$NODE" | jq -r '.port // 8000')
  ID=$(echo "$NODE"   | jq -r '.id')

  URL="http://${IP}:${PORT}/health"
  HTTP_CODE=$(curl -o /dev/null -s -w "%{http_code}" \
    --connect-timeout "$TIMEOUT" \
    --max-time "$TIMEOUT" \
    "$URL" 2>/dev/null || echo "000")

  # Also measure response latency
  LATENCY_MS=$(curl -o /dev/null -s -w "%{time_total}" \
    --connect-timeout "$TIMEOUT" \
    --max-time "$TIMEOUT" \
    "$URL" 2>/dev/null | awk '{printf "%d", $1*1000}' || echo "0")

  if [[ "$HTTP_CODE" == "200" ]]; then
    STATUS="healthy"
    ICON="✅"
    HEALTHY=$((HEALTHY + 1))
  else
    STATUS="unhealthy"
    ICON="❌"
    UNHEALTHY=$((UNHEALTHY + 1))
  fi

  echo "  $ICON  $IP:$PORT  [$STATUS]  HTTP=$HTTP_CODE  latency=${LATENCY_MS}ms" >&2

  # Build updated node entry
  UPDATED_NODE=$(echo "$NODE" | jq \
    --arg status "$STATUS" \
    --argjson latency_ms "$LATENCY_MS" \
    --arg checked_at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '.status = $status | .latency_ms = $latency_ms | .last_checked = $checked_at')
  UPDATED_NODES=$(echo "$UPDATED_NODES" | jq --argjson node "$UPDATED_NODE" '. + [$node]')

done < <(jq -c '.nodes[]' "$NODES_FILE")

RESULT=$(jq -n \
  --argjson nodes "$UPDATED_NODES" \
  --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  '{nodes: $nodes, last_health_check: $ts}')

# Optionally write back to nodes.json
if [[ "$UPDATE_FILE" == "1" ]]; then
  echo "$RESULT" > "$NODES_FILE"
fi

echo "" >&2
echo "📊 Summary: $HEALTHY healthy, $UNHEALTHY unhealthy (of $NODE_COUNT total)" >&2

# Output JSON to stdout (for Lambda / scripts to consume)
echo "$RESULT"

if [[ $HEALTHY -gt 0 ]]; then
  exit 0
else
  echo "⚠️  All nodes unhealthy!" >&2
  exit 1
fi
