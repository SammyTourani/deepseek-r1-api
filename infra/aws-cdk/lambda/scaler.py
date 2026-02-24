"""
scaler.py — DeepSeek R1 Auto-Scaler Lambda
============================================
Triggered by SNS (which receives CloudWatch alarms).

Scale-UP triggers:
  - p99 inference latency > 2000ms for 3 consecutive minutes
  - High error rate alarm

Scale-DOWN triggers:
  - p99 inference latency < 500ms for 10 consecutive minutes

Actions:
  - Calls SF Compute API to add/remove H100 nodes
  - Registers new nodes in DynamoDB
  - Runs setup-node.sh on new nodes via paramiko SSH
  - Respects MIN_NODES / MAX_NODES bounds
"""
import json
import logging
import os
import socket
import time
import urllib.request
import urllib.error
from typing import Any

import boto3
from botocore.exceptions import ClientError

# ─── Config ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
NODE_TABLE_NAME = os.environ["NODE_TABLE_NAME"]
SF_SECRET_NAME = os.environ["SF_SECRET_NAME"]
HF_SECRET_NAME = os.environ["HF_SECRET_NAME"]
MIN_NODES = int(os.environ.get("MIN_NODES", "1"))
MAX_NODES = int(os.environ.get("MAX_NODES", "4"))
SF_ZONE = os.environ.get("SF_ZONE", "landsend")
INSTANCE_TYPE = os.environ.get("INSTANCE_TYPE", "h100-80gb-2x")
MODEL_NAME = os.environ.get("MODEL_NAME", "deepseek-ai/DeepSeek-R1-Distill-Llama-70B")
TENSOR_PARALLEL_SIZE = os.environ.get("TENSOR_PARALLEL_SIZE", "2")
VLLM_PORT = int(os.environ.get("VLLM_PORT", "8000"))
SF_API_BASE = "https://api.sfcompute.com/v1"

logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

dynamodb = boto3.resource("dynamodb")
secretsmanager = boto3.client("secretsmanager")
_node_table = dynamodb.Table(NODE_TABLE_NAME)

_secrets_cache: dict[str, Any] = {}


# ─── Secrets ──────────────────────────────────────────────────────────────────

def get_secret(secret_name: str) -> dict:
    now = time.time()
    cached = _secrets_cache.get(secret_name)
    if cached and cached["ts"] + 300 > now:
        return cached["value"]
    resp = secretsmanager.get_secret_value(SecretId=secret_name)
    val = json.loads(resp["SecretString"])
    _secrets_cache[secret_name] = {"value": val, "ts": now}
    return val


# ─── DynamoDB helpers ─────────────────────────────────────────────────────────

def get_all_nodes() -> list[dict]:
    resp = _node_table.scan()
    return resp.get("Items", [])


def get_healthy_nodes() -> list[dict]:
    resp = _node_table.query(
        IndexName="StatusIndex",
        KeyConditionExpression=boto3.dynamodb.conditions.Key("status").eq("healthy"),
    )
    return resp.get("Items", [])


def register_node(node_id: str, ip: str) -> None:
    _node_table.put_item(
        Item={
            "node_id": node_id,
            "ip": ip,
            "port": VLLM_PORT,
            "status": "provisioning",
            "instance_type": INSTANCE_TYPE,
            "zone": SF_ZONE,
            "model": MODEL_NAME,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "last_checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    logger.info("Registered node %s (%s) in DynamoDB", node_id, ip)


def mark_node_healthy(node_id: str) -> None:
    _node_table.update_item(
        Key={"node_id": node_id},
        UpdateExpression="SET #s = :h, last_checked = :ts",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":h": "healthy",
            ":ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


def remove_node_from_db(node_id: str) -> None:
    _node_table.delete_item(Key={"node_id": node_id})
    logger.info("Removed node %s from DynamoDB", node_id)


# ─── SF Compute API calls ─────────────────────────────────────────────────────

def sf_api(method: str, path: str, body: dict | None = None) -> dict:
    sf_secret = get_secret(SF_SECRET_NAME)
    api_key = sf_secret["api_key"]
    url = f"{SF_API_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def create_sf_node() -> tuple[str, str]:
    """Create one SF Compute instance. Returns (node_id, ip)."""
    logger.info("Creating SF Compute instance: %s in %s", INSTANCE_TYPE, SF_ZONE)
    result = sf_api("POST", "/instances", {
        "instance_type": INSTANCE_TYPE,
        "zone": SF_ZONE,
        "auto": True,
    })
    node_id = result["id"]
    logger.info("Created instance: %s", node_id)

    # Poll until READY (up to 5 minutes)
    for _ in range(30):
        time.sleep(10)
        info = sf_api("GET", f"/instances/{node_id}")
        if info.get("status") == "READY":
            ip = info.get("public_ip", "")
            logger.info("Instance %s is READY at %s", node_id, ip)
            return node_id, ip
        if info.get("status") in ("FAILED", "TERMINATED"):
            raise RuntimeError(f"Instance {node_id} entered {info['status']} state")

    raise TimeoutError(f"Instance {node_id} did not become READY within 5 minutes")


def terminate_sf_node(node_id: str) -> None:
    """Terminate an SF Compute instance."""
    logger.info("Terminating SF instance: %s", node_id)
    sf_api("DELETE", f"/instances/{node_id}")


# ─── Node setup via SSH ────────────────────────────────────────────────────────

def setup_node_via_ssh(ip: str) -> bool:
    """
    Run vLLM setup on the new node over SSH using paramiko.
    Returns True on success.
    NOTE: In production, ensure your Lambda has network access to the node
    (VPC peering or public IP) and the SSH key is stored in Secrets Manager.
    """
    try:
        import paramiko  # type: ignore
    except ImportError:
        logger.error("paramiko not available; node setup must be done externally")
        return False

    hf_secret = get_secret(HF_SECRET_NAME)
    hf_token = hf_secret.get("token", "")

    setup_script = f"""
set -euo pipefail
# Install Docker
apt-get update -qq && apt-get install -y -qq docker.io curl jq
# Install NVIDIA Container Toolkit
distribution=$(. /etc/os-release; echo $ID$VERSION_ID)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \\
  gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -sL https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \\
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \\
  tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update -qq && apt-get install -y -qq nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker
# Pull and start vLLM
docker pull vllm/vllm-openai:latest
docker run -d --name deepseek-vllm --restart unless-stopped \\
  --gpus all \\
  -e HF_TOKEN='{hf_token}' \\
  -e HF_HOME=/models \\
  -v /models:/models \\
  -p {VLLM_PORT}:{VLLM_PORT} \\
  vllm/vllm-openai:latest \\
  --model {MODEL_NAME} \\
  --tensor-parallel-size {TENSOR_PARALLEL_SIZE} \\
  --gpu-memory-utilization 0.95 \\
  --host 0.0.0.0 --port {VLLM_PORT} \\
  --served-model-name deepseek-r1
echo "Setup complete"
"""

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(ip, username="ubuntu", timeout=30)
        stdin, stdout, stderr = ssh.exec_command(f"sudo bash -s << 'EOFSCRIPT'\n{setup_script}\nEOFSCRIPT", timeout=600)
        exit_code = stdout.channel.recv_exit_status()
        if exit_code == 0:
            logger.info("Node %s setup complete", ip)
            return True
        else:
            logger.error("Node setup failed on %s: %s", ip, stderr.read().decode())
            return False
    except Exception as e:
        logger.error("SSH error on %s: %s", ip, e)
        return False
    finally:
        ssh.close()


def wait_for_vllm(ip: str, timeout: int = 600) -> bool:
    """Poll vLLM /health until it responds or timeout."""
    url = f"http://{ip}:{VLLM_PORT}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    logger.info("vLLM healthy on %s", ip)
                    return True
        except Exception:
            pass
        time.sleep(15)
    return False


# ─── Scaling logic ────────────────────────────────────────────────────────────

def scale_up() -> dict:
    """Add one node to the pool."""
    healthy = get_healthy_nodes()
    all_nodes = get_all_nodes()

    if len(all_nodes) >= MAX_NODES:
        msg = f"Already at max nodes ({MAX_NODES}); skipping scale-up"
        logger.info(msg)
        return {"action": "scale_up", "result": "skipped", "reason": msg}

    logger.info("Scaling UP: current=%d, max=%d", len(all_nodes), MAX_NODES)

    try:
        node_id, ip = create_sf_node()
        register_node(node_id, ip)

        # Run setup (may take several minutes)
        setup_ok = setup_node_via_ssh(ip)
        if not setup_ok:
            logger.error("Node setup failed; marking as unhealthy")
            _node_table.update_item(
                Key={"node_id": node_id},
                UpdateExpression="SET #s = :u",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":u": "setup_failed"},
            )
            return {"action": "scale_up", "result": "failed", "node_id": node_id}

        # Wait for vLLM to be ready
        if wait_for_vllm(ip):
            mark_node_healthy(node_id)
            logger.info("Node %s (%s) is now healthy and serving traffic", node_id, ip)
            return {"action": "scale_up", "result": "success", "node_id": node_id, "ip": ip}
        else:
            logger.error("vLLM on %s did not become healthy in time", ip)
            return {"action": "scale_up", "result": "vllm_timeout", "node_id": node_id}

    except Exception as e:
        logger.exception("Scale-up failed: %s", e)
        return {"action": "scale_up", "result": "error", "error": str(e)}


def scale_down() -> dict:
    """Remove one node (least-recently-checked) from the pool."""
    healthy = get_healthy_nodes()

    if len(healthy) <= MIN_NODES:
        msg = f"Already at min nodes ({MIN_NODES}); skipping scale-down"
        logger.info(msg)
        return {"action": "scale_down", "result": "skipped", "reason": msg}

    logger.info("Scaling DOWN: current=%d, min=%d", len(healthy), MIN_NODES)

    # Pick the oldest node (by last_checked)
    target = sorted(healthy, key=lambda n: n.get("last_checked", ""))[0]
    node_id = target["node_id"]
    ip = target["ip"]

    logger.info("Terminating node %s (%s)", node_id, ip)

    try:
        # Remove from DynamoDB first (stop routing traffic)
        remove_node_from_db(node_id)
        # Allow in-flight requests to drain
        time.sleep(30)
        # Terminate the SF Compute instance
        terminate_sf_node(node_id)
        logger.info("Node %s terminated", node_id)
        return {"action": "scale_down", "result": "success", "node_id": node_id, "ip": ip}

    except Exception as e:
        logger.exception("Scale-down failed: %s", e)
        return {"action": "scale_down", "result": "error", "error": str(e)}


# ─── Main handler ─────────────────────────────────────────────────────────────

def handler(event: dict, context: Any) -> dict:
    logger.info("Scaler triggered: %s", json.dumps(event, default=str)[:500])

    results = []

    # Handle both direct CloudWatch alarm events and SNS-wrapped events
    records = event.get("Records", [{"Sns": {"Message": json.dumps(event)}}])

    for record in records:
        message_str = record.get("Sns", {}).get("Message", "{}")
        try:
            message = json.loads(message_str)
        except json.JSONDecodeError:
            message = {"AlarmName": "unknown"}

        alarm_name = message.get("AlarmName", "")
        new_state = message.get("NewStateValue", "")

        logger.info("Alarm: %s → %s", alarm_name, new_state)

        if new_state != "ALARM":
            logger.info("Alarm not in ALARM state (%s); skipping", new_state)
            continue

        if "high-latency" in alarm_name or "high-errors" in alarm_name:
            result = scale_up()
        elif "low-latency" in alarm_name:
            result = scale_down()
        else:
            logger.warning("Unknown alarm: %s", alarm_name)
            result = {"action": "unknown", "alarm": alarm_name}

        results.append(result)

    return {"statusCode": 200, "results": results}
