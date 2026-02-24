#!/usr/bin/env bash
# =============================================================================
# provision.sh — Provision SF Compute H100 nodes for DeepSeek R1
# =============================================================================
# Usage:
#   NODE_COUNT=2 ./scripts/provision.sh
#
# Required env:
#   SFCOMPUTE_API_KEY   — SF Compute API key
#   HF_TOKEN            — HuggingFace token (passed to setup-node.sh)
#   MODEL_NAME          — Model name (default: DeepSeek-R1-Distill-Llama-70B)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_DIR="$PROJECT_DIR/config"

# ─── Config ──────────────────────────────────────────────────────────────────
NODE_COUNT="${NODE_COUNT:-1}"
SF_ZONE="${SF_ZONE:-landsend}"
INSTANCE_TYPE="${INSTANCE_TYPE:-h100-80gb-2x}"   # 2x H100 per node
MODEL_NAME="${MODEL_NAME:-deepseek-ai/DeepSeek-R1-Distill-Llama-70B}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-2}"
PRICE_CAP="${PRICE_CAP:-2.00}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
READY_TIMEOUT=300   # seconds to wait for a node to become READY

# ─── Validate ────────────────────────────────────────────────────────────────
if [[ -z "${SFCOMPUTE_API_KEY:-}" ]]; then
  echo "❌  SFCOMPUTE_API_KEY is not set." >&2; exit 1
fi
if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "❌  HF_TOKEN is not set." >&2; exit 1
fi
if ! command -v sf &>/dev/null; then
  echo "❌  sf CLI not found. Install from https://sfcompute.com/docs/cli" >&2; exit 1
fi
if ! command -v jq &>/dev/null; then
  echo "❌  jq not found. Install via brew install jq" >&2; exit 1
fi

mkdir -p "$CONFIG_DIR"

echo "╔══════════════════════════════════════════════════╗"
echo "║   DeepSeek R1 — SF Compute Provisioning          ║"
echo "╚══════════════════════════════════════════════════╝"
echo "  Zone:         $SF_ZONE"
echo "  Instance:     $INSTANCE_TYPE"
echo "  Nodes:        $NODE_COUNT"
echo "  Model:        $MODEL_NAME"
echo "  Price cap:    \$$PRICE_CAP / GPU-hr"
echo ""

# ─── Create nodes ────────────────────────────────────────────────────────────
NODE_IDS=()
for i in $(seq 1 "$NODE_COUNT"); do
  echo "🚀 Launching node $i / $NODE_COUNT ..."
  NODE_ID=$(sf instances create \
    --type "$INSTANCE_TYPE" \
    --zone "$SF_ZONE" \
    --price-cap "$PRICE_CAP" \
    --auto \
    --format json | jq -r '.id')

  if [[ -z "$NODE_ID" || "$NODE_ID" == "null" ]]; then
    echo "❌  Failed to create node $i" >&2; exit 1
  fi
  echo "   Node ID: $NODE_ID"
  NODE_IDS+=("$NODE_ID")
done

# ─── Wait for nodes to become READY ──────────────────────────────────────────
echo ""
echo "⏳ Waiting for nodes to reach READY state (timeout: ${READY_TIMEOUT}s) ..."
NODE_IPS=()
for NODE_ID in "${NODE_IDS[@]}"; do
  ELAPSED=0
  while true; do
    STATUS=$(sf instances get "$NODE_ID" --format json | jq -r '.status')
    IP=$(sf instances get "$NODE_ID" --format json | jq -r '.public_ip // empty')

    if [[ "$STATUS" == "READY" && -n "$IP" ]]; then
      echo "   ✅ $NODE_ID → $IP"
      NODE_IPS+=("$IP")
      break
    elif [[ "$STATUS" == "FAILED" || "$STATUS" == "TERMINATED" ]]; then
      echo "   ❌ Node $NODE_ID entered $STATUS state" >&2; exit 1
    fi

    if [[ $ELAPSED -ge $READY_TIMEOUT ]]; then
      echo "   ❌ Timeout waiting for $NODE_ID (status: $STATUS)" >&2; exit 1
    fi
    sleep 10
    ELAPSED=$((ELAPSED + 10))
  done
done

# ─── SSH fingerprint setup ───────────────────────────────────────────────────
echo ""
echo "🔐 Adding host keys to known_hosts ..."
for IP in "${NODE_IPS[@]}"; do
  ssh-keyscan -H "$IP" >> ~/.ssh/known_hosts 2>/dev/null || true
done

# ─── Run setup on each node ───────────────────────────────────────────────────
echo ""
echo "⚙️  Running setup-node.sh on each node ..."
for IP in "${NODE_IPS[@]}"; do
  echo "   Setting up $IP ..."
  scp -i "$SSH_KEY" -o StrictHostKeyChecking=no \
    "$SCRIPT_DIR/setup-node.sh" \
    "ubuntu@$IP:/tmp/setup-node.sh"

  ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "ubuntu@$IP" \
    "HF_TOKEN='$HF_TOKEN' \
     MODEL_NAME='$MODEL_NAME' \
     TENSOR_PARALLEL_SIZE='$TENSOR_PARALLEL_SIZE' \
     bash /tmp/setup-node.sh" &
done

# Wait for all background setup jobs
wait
echo "   ✅ All nodes configured"

# ─── Write nodes.json ────────────────────────────────────────────────────────
NODES_JSON=$(python3 -c "
import json, sys
nodes = []
ids = '$( IFS=','; echo "${NODE_IDS[*]}" )'.split(',')
ips  = '$( IFS=','; echo "${NODE_IPS[*]}" )'.split(',')
for nid, ip in zip(ids, ips):
    nodes.append({'id': nid, 'ip': ip, 'status': 'healthy', 'port': 8000})
print(json.dumps({'nodes': nodes}, indent=2))
")

echo "$NODES_JSON" > "$CONFIG_DIR/nodes.json"
echo ""
echo "📄 Node pool saved to config/nodes.json"
cat "$CONFIG_DIR/nodes.json"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   ✅ Provisioning complete!                       ║"
echo "╚══════════════════════════════════════════════════╝"
echo "   Nodes ready: ${#NODE_IPS[@]}"
echo "   API endpoint (per node): http://<IP>:8000/v1"
echo ""
echo "   Next: make deploy-infra   # Deploy AWS API Gateway + Lambda"
