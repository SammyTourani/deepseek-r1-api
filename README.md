# DeepSeek R1 — Auto-Scaling Inference API (Reference Architecture)

An infrastructure-as-code blueprint for serving an OpenAI-compatible DeepSeek R1
API. It runs [vLLM](https://github.com/vllm-project/vllm) on SF Compute H100 GPU
nodes, fronted by an AWS HTTP API Gateway (v2) + a Lambda proxy for round-robin
load balancing and `x-api-key` auth, with a CloudWatch-alarm-driven Lambda
auto-scaler that adds and removes GPU nodes via the SF Compute API.

> **Status: reference architecture, not a running service.** This is a
> blueprint. The CDK stack, Lambda handlers, Docker image, and provisioning
> scripts are all here and self-consistent, but the system has **not been
> deployed end-to-end** from this repo. Treat the cost and scaling numbers as
> design estimates, not measured production figures. See
> [Design notes / known limitations](#design-notes--known-limitations) before
> you rely on the auto-scaling behavior.

```
                    ┌─────────────────────────────────────┐
                    │       AWS HTTP API Gateway (v2)      │
                    │   (HTTPS /v1/chat/completions)       │
                    └──────────────┬──────────────────────┘
                                   │
                    ┌──────────────▼──────────────────────┐
                    │         Lambda Proxy                  │
                    │  Round-robin → healthy vLLM nodes     │
                    │  x-api-key auth (Secrets Manager)     │
                    └──────┬─────────────────┬─────────────┘
                           │                 │
               ┌───────────▼──┐    ┌─────────▼────────┐
               │  SF Node 1   │    │   SF Node 2-4    │
               │  2x H100 80G │    │  (auto-scaled)   │
               │  vLLM + R1   │    │  vLLM + R1       │
               └──────────────┘    └──────────────────┘
                           ▲
               ┌───────────┴───────────────────────────────┐
               │  Lambda Scaler (CloudWatch-alarm triggered) │
               │  Calls SF Compute API to add/remove nodes   │
               │  Node-pool state in DynamoDB                │
               └─────────────────────────────────────────────┘
```

## What's in this repo

| Component | Path | What it does |
|-----------|------|--------------|
| CDK infra (Python) | `infra/aws-cdk/app.py` | HTTP API Gateway v2, proxy + scaler Lambdas, DynamoDB node table, CloudWatch alarms, SNS, Secrets Manager, IAM |
| Proxy Lambda | `infra/aws-cdk/lambda/proxy.py` | Round-robin forwarding to healthy vLLM nodes, `x-api-key` auth, emits a custom `DeepSeekR1/InferenceLatencyMs` metric |
| Scaler Lambda | `infra/aws-cdk/lambda/scaler.py` | Reacts to CloudWatch alarms via SNS, calls the SF Compute API to add/remove nodes, updates DynamoDB |
| vLLM container | `docker/Dockerfile`, `docker/docker-compose.yml` | Image and compose file that run vLLM's OpenAI-compatible server |
| Provisioning scripts | `scripts/*.sh` | `provision`, `setup-node`, `health-check`, `deprovision` for SF Compute nodes |
| Config | `config/config.yaml`, `config/models.yaml` | Model selection, scaling thresholds, AWS/SF Compute settings, model variant catalog |
| Orchestration | `Makefile` | `install`, `deploy-infra`, `provision`, `test`, `status`, `logs`, `deprovision`, `destroy` |

## Prerequisites

| Credential | Where to get it |
|-----------|----------------|
| `SFCOMPUTE_API_KEY` | [sfcompute.com](https://sfcompute.com) → sign up |
| `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` | AWS Console → IAM → Create User |
| `HF_TOKEN` | [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) |
| `DEEPSEEK_API_KEY` | Generate any secret string (auth for your API clients) |

You will also need Python 3.12+, the AWS CDK CLI (`npm install -g aws-cdk`), the
SF Compute CLI, and Docker.

Copy `.env.example` → `.env` and fill in all values. `.env` is gitignored.

## Quickstart

```bash
# 1. Install deps (CDK Python libs + aws-cdk CLI; prints SF CLI install hint)
make install

# 2. Copy + fill in credentials
cp .env.example .env
$EDITOR .env

# 3. Deploy AWS infrastructure (API Gateway + Lambdas + DynamoDB + alarms)
make deploy-infra

# 4. Provision SF Compute GPU nodes + start vLLM
make provision

# 5. Test the API
make test
```

`make deploy-infra` runs `cdk deploy` inside `infra/aws-cdk/`, which reads
`infra/aws-cdk/cdk.json` to locate the app entry point (`python3 app.py`).

## Model selection

Costs below assume an SF Compute rate of ~$2/GPU-hour and match the variant
catalog in [`config/models.yaml`](config/models.yaml), which is the single
source of truth for model specs and pricing.

| Model | GPUs | Cost/hr | Context (`max_model_len`) | Status |
|-------|------|---------|---------------------------|--------|
| R1 671B (full) | 8×H100 80GB | ~$16/hr | 32K | Not available (needs InfiniBand) |
| **R1-Distill-Llama-70B** ← **RECOMMENDED** | 2×H100 80GB | ~$4/hr | 32K | Recommended |
| R1-Distill-Qwen-32B | 1×H100 80GB | ~$2/hr | 32K | Good |
| R1-Distill-Qwen-14B | 1×A100 40GB | ~$1.20/hr | 16K | Budget |
| R1-Distill-Llama-8B | 1×A10G | ~$0.60/hr | 8K | Lightweight |
| R1-Distill-Qwen-7B | 1×A10G | ~$0.60/hr | 8K | Lightweight |

Change the model in `config/config.yaml`. The 70B distill is reported at ~90% of
full R1 quality on reasoning benchmarks (see the comparison table in
`config/models.yaml`).

## API usage

The proxy exposes an OpenAI-compatible surface under `/v1/`:

```bash
curl -X POST https://YOUR_API_GATEWAY_URL/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "x-api-key: YOUR_DEEPSEEK_API_KEY" \
  -d '{
    "model": "deepseek-r1",
    "messages": [{"role": "user", "content": "Solve: what is the derivative of x^3?"}],
    "max_tokens": 2048,
    "temperature": 0.6
  }'
```

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://YOUR_API_GATEWAY_URL/v1",
    api_key="YOUR_DEEPSEEK_API_KEY",
)

response = client.chat.completions.create(
    model="deepseek-r1",
    messages=[{"role": "user", "content": "Your prompt here"}],
    max_tokens=2048,
)
print(response.choices[0].message.content)
```

## Auto-scaling behavior (as designed)

| Condition | Action |
|-----------|--------|
| Latency alarm breaches scale-up threshold | Add 1 SF Compute node (up to `max_nodes`, default 4) |
| Latency alarm breaches scale-down threshold | Remove 1 SF Compute node (down to `min_nodes`, default 1) |
| Node health check fails | Mark unhealthy; proxy skips it in round-robin |
| All nodes unhealthy | Proxy returns 503 |

Thresholds and durations live in `config/config.yaml` under `scaling:`.

> **Important:** see [Design notes / known limitations](#design-notes--known-limitations)
> for what the CloudWatch alarms actually measure today.

**Note:** SF Compute bills by the hour. Minimum cost = 1 node running 24/7.
For true scale-to-zero, a serverless GPU provider (e.g. RunPod Serverless) would
be a better fit; this design keeps at least `min_nodes` warm.

## Cost estimate (70B Distill, 1–4 nodes)

Estimates at ~$4/hr for a 2×H100 node (~$2/GPU-hour), plus a few dollars of AWS
spend for API Gateway + Lambda. These are design estimates, not billed figures.

| Usage | SF Compute | AWS | Total/month |
|-------|-----------|-----|-------------|
| 1 node, 24/7 | ~$2,880 | ~$5 | **~$2,885** |
| 1 node, 8 hr/day | ~$960 | ~$5 | **~$965** |
| Scale 1–4 nodes, 8 hr/day avg | ~$960–3,840 | ~$5 | **~$965–3,845** |

**Recommendation:** tear down nodes when not in use with `make deprovision` to
avoid idle GPU spend.

## Design notes / known limitations

These are documented intentionally; they are **not fixed** in this repo.

- **Scaling keys off Lambda duration, not true inference latency.** The
  scale-up/scale-down CloudWatch alarms in `infra/aws-cdk/app.py` are wired to
  the proxy Lambda's built-in `metric_duration` (total Lambda execution time),
  **not** the custom `DeepSeekR1/InferenceLatencyMs` metric that `proxy.py`
  emits via `put_metric_data`. So scaling decisions currently follow Lambda
  execution time rather than measured vLLM inference latency. Pointing the
  alarms at the custom metric would make scaling reflect real inference latency.
- **`with-nginx` compose profile references a missing file.** The
  `with-nginx` profile in `docker/docker-compose.yml` mounts `./nginx.conf`,
  which is not committed to this repo. This is an optional profile; the default
  `docker compose up` does not use it and is unaffected.
- **Never deployed end-to-end here.** Cost, latency, and scaling numbers are
  design estimates.

## Troubleshooting

**`sf: command not found`** → Install the SF CLI: `curl -sSL https://sfcompute.com/install.sh | sh`

**vLLM OOM error** → Reduce `gpu_memory_utilization` in `config/config.yaml` or switch to a smaller model.

**Lambda timeout** → DeepSeek R1 can take 30–90s for long generations. The proxy Lambda timeout is set to 120s by default (`lambda_timeout_seconds` in `config/config.yaml`). Increase if needed.

**Node not showing healthy** → SSH in and check the container: `ssh ubuntu@NODE_IP "sudo docker logs deepseek-vllm --tail 100"`

## License

MIT. See [LICENSE](LICENSE).
