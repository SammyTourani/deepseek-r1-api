#!/usr/bin/env bash
# =============================================================================
# setup-node.sh — Run ON each SF Compute H100 node after provisioning
# =============================================================================
# This script is SCP'd to the node and executed via SSH by provision.sh.
#
# Required env (passed by provision.sh):
#   HF_TOKEN               — HuggingFace token for model download
#   MODEL_NAME             — Model to download/serve
#   TENSOR_PARALLEL_SIZE   — Number of GPUs for tensor parallelism
# =============================================================================
set -euo pipefail

MODEL_NAME="${MODEL_NAME:-deepseek-ai/DeepSeek-R1-Distill-Llama-70B}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-2}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.95}"
PORT="${PORT:-8000}"
MODEL_CACHE_DIR="/models"
VLLM_IMAGE="vllm/vllm-openai:latest"
SERVICE_NAME="deepseek-vllm"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ─── 1. System update ─────────────────────────────────────────────────────────
log "📦 Updating system packages ..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
  curl wget git jq ufw ca-certificates \
  gnupg lsb-release apt-transport-https

# ─── 2. Install Docker ────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  log "🐳 Installing Docker ..."
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
    sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) \
    signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] \
    https://download.docker.com/linux/ubuntu \
    $(lsb_release -cs) stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
  sudo usermod -aG docker "$USER"
  log "   ✅ Docker installed"
else
  log "   ✅ Docker already installed"
fi

# ─── 3. Install NVIDIA Container Toolkit ─────────────────────────────────────
if ! dpkg -l | grep -q nvidia-container-toolkit; then
  log "🖥️  Installing NVIDIA Container Toolkit ..."
  distribution=$(. /etc/os-release; echo "$ID$VERSION_ID")
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
    sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L "https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list" | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list > /dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
  log "   ✅ NVIDIA Container Toolkit installed"
else
  log "   ✅ NVIDIA Container Toolkit already installed"
fi

# ─── 4. Pull vLLM Docker image ────────────────────────────────────────────────
log "📥 Pulling vLLM image: $VLLM_IMAGE ..."
sudo docker pull "$VLLM_IMAGE"
log "   ✅ Image pulled"

# ─── 5. Download model from HuggingFace ──────────────────────────────────────
log "🤗 Downloading model: $MODEL_NAME ..."
sudo mkdir -p "$MODEL_CACHE_DIR"

# Use huggingface-cli inside the vllm container (it includes the HF client)
sudo docker run --rm \
  -e HF_TOKEN="$HF_TOKEN" \
  -e HF_HOME="$MODEL_CACHE_DIR" \
  -v "$MODEL_CACHE_DIR:$MODEL_CACHE_DIR" \
  "$VLLM_IMAGE" \
  huggingface-cli download "$MODEL_NAME" \
    --token "$HF_TOKEN" \
    --local-dir "$MODEL_CACHE_DIR/hub/models--$(echo $MODEL_NAME | tr '/' '--')"
log "   ✅ Model downloaded to $MODEL_CACHE_DIR"

# ─── 6. Configure UFW firewall ────────────────────────────────────────────────
log "🔒 Configuring UFW firewall ..."
sudo ufw --force reset
sudo ufw default deny incoming
sudo ufw default allow outgoing
# Allow SSH from anywhere (management access)
sudo ufw allow 22/tcp comment "SSH management"
# Allow vLLM port from AWS IP ranges (add your specific ranges)
# These are representative AWS us-east-1 ranges — update as needed
AWS_IP_RANGES=(
  "3.80.0.0/12"
  "3.208.0.0/12"
  "18.204.0.0/14"
  "52.0.0.0/11"
  "54.80.0.0/13"
  "34.192.0.0/12"
)
for CIDR in "${AWS_IP_RANGES[@]}"; do
  sudo ufw allow from "$CIDR" to any port 8000 proto tcp comment "AWS Lambda" 2>/dev/null || true
done
sudo ufw --force enable
log "   ✅ Firewall configured (port 8000 restricted to AWS IPs)"

# ─── 7. Create systemd service ────────────────────────────────────────────────
log "⚙️  Creating systemd service: $SERVICE_NAME ..."
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null <<EOF
[Unit]
Description=DeepSeek R1 vLLM Inference Server
After=docker.service network-online.target
Requires=docker.service
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
Restart=always
RestartSec=30
User=root
Environment="MODEL_NAME=${MODEL_NAME}"
Environment="HF_HOME=${MODEL_CACHE_DIR}"
ExecStartPre=-/usr/bin/docker rm -f ${SERVICE_NAME}
ExecStart=/usr/bin/docker run --rm \\
  --name ${SERVICE_NAME} \\
  --runtime=nvidia \\
  --gpus all \\
  -e MODEL_NAME=${MODEL_NAME} \\
  -e MAX_MODEL_LEN=${MAX_MODEL_LEN} \\
  -e TENSOR_PARALLEL_SIZE=${TENSOR_PARALLEL_SIZE} \\
  -e GPU_MEMORY_UTILIZATION=${GPU_MEMORY_UTILIZATION} \\
  -e PORT=${PORT} \\
  -e HF_HOME=${MODEL_CACHE_DIR} \\
  -v ${MODEL_CACHE_DIR}:${MODEL_CACHE_DIR} \\
  -p ${PORT}:${PORT} \\
  ${VLLM_IMAGE}
ExecStop=/usr/bin/docker stop ${SERVICE_NAME}
TimeoutStartSec=600
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl start "$SERVICE_NAME"
log "   ✅ Service $SERVICE_NAME enabled and started"

# ─── 8. Wait for vLLM to be healthy ──────────────────────────────────────────
log "⏳ Waiting for vLLM health endpoint ..."
MAX_WAIT=600
ELAPSED=0
while true; do
  if curl -sf "http://localhost:${PORT}/health" &>/dev/null; then
    log "   ✅ vLLM is healthy on port $PORT"
    break
  fi
  if [[ $ELAPSED -ge $MAX_WAIT ]]; then
    log "   ⚠️  Timeout waiting for vLLM (will continue in background)"
    break
  fi
  sleep 15
  ELAPSED=$((ELAPSED + 15))
done

log ""
log "╔══════════════════════════════════════════════════╗"
log "║   ✅ Node setup complete!                         ║"
log "╚══════════════════════════════════════════════════╝"
log "   Model:   $MODEL_NAME"
log "   Serving: http://$(hostname -I | awk '{print $1}'):$PORT/v1"
log "   Service: systemctl status $SERVICE_NAME"
