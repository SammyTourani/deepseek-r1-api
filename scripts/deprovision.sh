#!/usr/bin/env bash
# =============================================================================
# deprovision.sh — Tear down all SF Compute nodes cleanly
# =============================================================================
# Reads config/nodes.json, terminates all nodes via sf CLI, archives the file.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_DIR="$PROJECT_DIR/config"
NODES_FILE="$CONFIG_DIR/nodes.json"

# ─── Validate ────────────────────────────────────────────────────────────────
if [[ -z "${SFCOMPUTE_API_KEY:-}" ]]; then
  echo "❌  SFCOMPUTE_API_KEY is not set." >&2; exit 1
fi
if ! command -v sf &>/dev/null; then
  echo "❌  sf CLI not found." >&2; exit 1
fi
if [[ ! -f "$NODES_FILE" ]]; then
  echo "❌  No nodes.json found at $NODES_FILE — nothing to deprovision." >&2; exit 0
fi
if ! command -v jq &>/dev/null; then
  echo "❌  jq not found." >&2; exit 1
fi

NODE_COUNT=$(jq '.nodes | length' "$NODES_FILE")
echo "╔══════════════════════════════════════════════════╗"
echo "║   DeepSeek R1 — SF Compute Deprovisioning        ║"
echo "╚══════════════════════════════════════════════════╝"
echo "  Nodes to terminate: $NODE_COUNT"

if [[ "$NODE_COUNT" -eq 0 ]]; then
  echo "  Nothing to do." && exit 0
fi

# ─── Confirm (skip with FORCE=1) ─────────────────────────────────────────────
if [[ "${FORCE:-0}" != "1" ]]; then
  echo ""
  read -r -p "⚠️  This will TERMINATE all $NODE_COUNT node(s). Continue? [y/N] " CONFIRM
  if [[ ! "$CONFIRM" =~ ^[yY]$ ]]; then
    echo "Aborted." && exit 0
  fi
fi

# ─── Stop vLLM service on each node before termination ──────────────────────
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
while IFS= read -r NODE; do
  IP=$(echo "$NODE" | jq -r '.ip')
  ID=$(echo "$NODE" | jq -r '.id')
  echo ""
  echo "🛑 Stopping vLLM on $IP (node: $ID) ..."
  ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
    "ubuntu@$IP" \
    "sudo systemctl stop deepseek-vllm && sudo docker rm -f deepseek-vllm 2>/dev/null || true" \
    2>/dev/null && echo "   ✅ Service stopped" || echo "   ⚠️  Could not SSH (node may already be offline)"
done < <(jq -c '.nodes[]' "$NODES_FILE")

# ─── Terminate instances ──────────────────────────────────────────────────────
echo ""
while IFS= read -r NODE; do
  ID=$(echo "$NODE" | jq -r '.id')
  IP=$(echo "$NODE" | jq -r '.ip')
  echo "🗑️  Terminating node $ID ($IP) ..."
  if sf instances terminate "$ID" --yes 2>/dev/null; then
    echo "   ✅ Terminated"
  else
    echo "   ⚠️  sf CLI returned non-zero (may already be gone)"
  fi
done < <(jq -c '.nodes[]' "$NODES_FILE")

# ─── Archive nodes.json ───────────────────────────────────────────────────────
ARCHIVE_FILE="$CONFIG_DIR/nodes.$(date '+%Y%m%d-%H%M%S').json.bak"
mv "$NODES_FILE" "$ARCHIVE_FILE"
echo "{\"nodes\": []}" > "$NODES_FILE"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   ✅ Deprovisioning complete                      ║"
echo "╚══════════════════════════════════════════════════╝"
echo "   All nodes terminated."
echo "   Archive: $ARCHIVE_FILE"
echo "   Node pool reset to empty: $NODES_FILE"
