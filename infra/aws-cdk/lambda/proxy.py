"""
proxy.py — DeepSeek R1 Lambda Proxy
======================================
Routes incoming API requests from API Gateway to a healthy vLLM node
using round-robin load balancing. Handles:
  - API key auth (x-api-key header vs Secrets Manager)
  - Round-robin node selection from DynamoDB
  - Request forwarding with full header/body pass-through
  - Unhealthy node marking + automatic retry
  - CORS headers on all responses
  - 120s timeout for long model generation
"""
import json
import logging
import os
import time
import urllib.request
import urllib.error
from typing import Any

import boto3
from botocore.exceptions import ClientError

try:
    from selection import eject_node, select_round_robin
except ImportError:  # pragma: no cover - package-relative import fallback
    from .selection import eject_node, select_round_robin

# ─── Config ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
NODE_TABLE_NAME = os.environ["NODE_TABLE_NAME"]
CLIENT_API_KEY_SECRET = os.environ["CLIENT_API_KEY_SECRET"]
VLLM_PORT = int(os.environ.get("VLLM_PORT", "8000"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "115"))  # < Lambda 120s limit
MAX_RETRIES = 2

logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

# ─── AWS clients (reused across warm invocations) ─────────────────────────────
dynamodb = boto3.resource("dynamodb")
secretsmanager = boto3.client("secretsmanager")
cloudwatch = boto3.client("cloudwatch")

_node_table = dynamodb.Table(NODE_TABLE_NAME)
_api_key_cache: dict[str, Any] = {}  # {key: value, ts: float}
_round_robin_counter = 0


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_expected_api_key() -> str:
    """Fetch client API key from Secrets Manager (cached 5 min)."""
    now = time.time()
    if _api_key_cache.get("ts", 0) + 300 > now:
        return _api_key_cache["key"]
    try:
        resp = secretsmanager.get_secret_value(SecretId=CLIENT_API_KEY_SECRET)
        secret = json.loads(resp["SecretString"])
        key = secret.get("api_key") or resp["SecretString"]
        _api_key_cache["key"] = key
        _api_key_cache["ts"] = now
        return key
    except ClientError as e:
        logger.error("Failed to fetch API key from Secrets Manager: %s", e)
        raise


def get_healthy_nodes() -> list[dict]:
    """Query DynamoDB for all healthy nodes via StatusIndex GSI."""
    resp = _node_table.query(
        IndexName="StatusIndex",
        KeyConditionExpression=boto3.dynamodb.conditions.Key("status").eq("healthy"),
    )
    return resp.get("Items", [])


def mark_node_unhealthy(node_id: str) -> None:
    """Mark a node as unhealthy in DynamoDB."""
    try:
        _node_table.update_item(
            Key={"node_id": node_id},
            UpdateExpression="SET #s = :unhealthy, last_checked = :ts",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":unhealthy": "unhealthy",
                ":ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
        logger.warning("Marked node %s as unhealthy", node_id)
    except ClientError as e:
        logger.error("Failed to mark node unhealthy: %s", e)


def emit_latency_metric(latency_ms: float, success: bool) -> None:
    """Emit custom CloudWatch metrics for auto-scaling decisions."""
    try:
        cloudwatch.put_metric_data(
            Namespace="DeepSeekR1",
            MetricData=[
                {
                    "MetricName": "InferenceLatencyMs",
                    "Value": latency_ms,
                    "Unit": "Milliseconds",
                    "Dimensions": [{"Name": "Service", "Value": "vLLM"}],
                },
                {
                    "MetricName": "InferenceSuccess",
                    "Value": 1 if success else 0,
                    "Unit": "Count",
                },
            ],
        )
    except Exception as e:
        logger.debug("CloudWatch metric emission failed: %s", e)


def cors_headers() -> dict:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type, x-api-key, Authorization",
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    }


def error_response(status: int, message: str) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **cors_headers()},
        "body": json.dumps({"error": {"message": message, "type": "proxy_error"}}),
    }


# ─── Main handler ─────────────────────────────────────────────────────────────

def handler(event: dict, context: Any) -> dict:
    global _round_robin_counter

    method = event.get("requestContext", {}).get("http", {}).get("method", "").upper()
    path = event.get("rawPath", "/")

    # Handle CORS preflight
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": cors_headers(), "body": ""}

    # ─── Auth ─────────────────────────────────────────────────────────────────
    client_key = (event.get("headers") or {}).get("x-api-key", "")
    if not client_key:
        # Also accept Authorization: Bearer <key>
        auth_header = (event.get("headers") or {}).get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            client_key = auth_header[7:]

    try:
        expected_key = get_expected_api_key()
        if client_key != expected_key:
            logger.warning("Unauthorized request from %s",
                          event.get("requestContext", {}).get("http", {}).get("sourceIp"))
            return error_response(401, "Invalid or missing API key")
    except Exception:
        return error_response(503, "Auth service unavailable")

    # ─── Health check passthrough ─────────────────────────────────────────────
    if path == "/health":
        nodes = get_healthy_nodes()
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json", **cors_headers()},
            "body": json.dumps({
                "status": "ok",
                "healthy_nodes": len(nodes),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }),
        }

    # ─── Load balancing ───────────────────────────────────────────────────────
    nodes = get_healthy_nodes()
    if not nodes:
        logger.error("No healthy nodes available")
        return error_response(503, "No inference nodes available. Please try again later.")

    body_str = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        import base64
        body_str = base64.b64decode(body_str).decode("utf-8")

    # Forward relevant headers (drop hop-by-hop and auth)
    forward_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    raw_headers = event.get("headers") or {}
    for h in ("x-request-id", "x-correlation-id"):
        if h in raw_headers:
            forward_headers[h] = raw_headers[h]

    # ─── Retry loop ───────────────────────────────────────────────────────────
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        # Round-robin selection
        node = select_round_robin(nodes, _round_robin_counter)
        _round_robin_counter += 1

        node_ip = node["ip"]
        node_id = node["node_id"]
        vllm_port = int(node.get("port", VLLM_PORT))

        # Reconstruct path for vLLM (strip leading slash, keep /v1/...)
        vllm_url = f"http://{node_ip}:{vllm_port}{path}"
        if event.get("rawQueryString"):
            vllm_url += f"?{event['rawQueryString']}"

        logger.info("Attempt %d → node %s (%s)", attempt + 1, node_id, vllm_url)
        t0 = time.time()

        try:
            req = urllib.request.Request(
                vllm_url,
                data=body_str.encode("utf-8") if body_str else None,
                headers=forward_headers,
                method=method,
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                response_body = resp.read().decode("utf-8")
                latency_ms = (time.time() - t0) * 1000
                emit_latency_metric(latency_ms, success=True)

                response_headers = {
                    "Content-Type": resp.headers.get("Content-Type", "application/json"),
                    **cors_headers(),
                }
                return {
                    "statusCode": resp.status,
                    "headers": response_headers,
                    "body": response_body,
                }

        except urllib.error.HTTPError as e:
            latency_ms = (time.time() - t0) * 1000
            error_body = e.read().decode("utf-8") if e.fp else ""
            logger.warning("Node %s returned HTTP %d: %s", node_id, e.code, error_body[:200])
            # Don't retry 4xx (client errors)
            if 400 <= e.code < 500:
                emit_latency_metric(latency_ms, success=False)
                return {
                    "statusCode": e.code,
                    "headers": {"Content-Type": "application/json", **cors_headers()},
                    "body": error_body,
                }
            last_error = f"HTTP {e.code}"

        except (urllib.error.URLError, TimeoutError, OSError) as e:
            latency_ms = (time.time() - t0) * 1000
            logger.error("Connection to node %s failed: %s", node_id, e)
            mark_node_unhealthy(node_id)
            emit_latency_metric(latency_ms, success=False)
            last_error = str(e)
            # Remove from local list for this invocation
            nodes = eject_node(nodes, node_id)
            if not nodes:
                break

    logger.error("All retries exhausted. Last error: %s", last_error)
    return error_response(502, f"Inference backend unavailable: {last_error}")
