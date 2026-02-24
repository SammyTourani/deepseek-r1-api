# DeepSeek R1 — Auto-Scaling Inference API

OpenAI-compatible DeepSeek R1 API deployed on SF Compute H100s, fronted by AWS API Gateway with auto-scaling.

```
                    ┌─────────────────────────────────────┐
                    │         AWS API Gateway              │
                    │   (HTTPS /v1/chat/completions)       │
                    └──────────────┬──────────────────────┘
                                   │
                    ┌──────────────▼──────────────────────┐
                    │         Lambda Proxy                  │
                    │  Round-robin → healthy vLLM nodes     │
                    └──────┬─────────────────┬────────────┘
                           │                 │
               ┌───────────▼──┐    ┌─────────▼────────┐
               │  SF Node 1   │    │   SF Node 2-4    │
               │  2x H100 80G │    │  (auto-scaled)    │
               │  vLLM + R1   │    │  vLLM + R1        │
               └──────────────┘    └──────────────────┘
                           │
               ┌───────────▼──────────────────────────────┐
               │  Lambda Scaler (CloudWatch triggered)      │
               │  Scale up: latency >2s for 3min           │
               │  Scale down: latency <500ms for 10min      │
               └───────────────────────────────────────────┘
```

## Prerequisites

| Credential | Where to get it |
|-----------|----------------|
| `SFCOMPUTE_API_KEY` | [sfcompute.com](https://sfcompute.com) → sign up |
| `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` | AWS Console → IAM → Create User |
| `HF_TOKEN` | [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) |
| `DEEPSEEK_API_KEY` | Generate any secret string (auth for your API clients) |

Copy `.env.example` → `.env` and fill in all values.

## Quickstart

```bash
# 1. Install deps
make install

# 2. Copy + fill in credentials
cp .env.example .env
nano .env

# 3. Deploy AWS infrastructure (API Gateway + Lambda)
make deploy-infra

# 4. Provision SF Compute GPU nodes + start vLLM
make provision

# 5. Test the API
make test
```

## Model Selection

| Model | VRAM | Nodes | Cost/hr | Context |
|-------|------|-------|---------|---------|
| R1 671B (full) | 8×H100 80GB | 4 nodes | ~$12/hr | 128K | 
| **R1-Distill-Llama-70B** ← **RECOMMENDED** | 2×H100 80GB | 1 node | ~$3/hr | 128K |
| R1-Distill-Qwen-32B | 1×H100 80GB | 1 node | ~$1.50/hr | 128K |
| R1-Distill-Qwen-14B | 1×H100 40GB | 1 node | ~$1/hr | 64K |
| R1-Distill-Llama-8B | 1×A10G | 1 node | ~$0.50/hr | 32K |
| R1-Distill-Qwen-7B | 1×A10G | 1 node | ~$0.50/hr | 32K |

Change model in `config/config.yaml`. The 70B distill hits 90%+ of full R1 quality on reasoning tasks.

## API Usage

The API is fully OpenAI-compatible:

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
    api_key="YOUR_DEEPSEEK_API_KEY"
)

response = client.chat.completions.create(
    model="deepseek-r1",
    messages=[{"role": "user", "content": "Your prompt here"}],
    max_tokens=2048
)
print(response.choices[0].message.content)
```

## Auto-Scaling Behavior

| Condition | Action |
|-----------|--------|
| Avg latency > 2s for 3 min | Add 1 SF Compute node (up to max 4) |
| Avg latency < 500ms for 10 min | Remove 1 SF Compute node (min 1) |
| Node health check fails | Remove from load balancer, alert |
| All nodes unhealthy | Return 503 + page via CloudWatch alarm |

**Note:** SF Compute bills by the hour. Minimum cost = 1 node running 24/7.  
For true scale-to-zero, consider [RunPod Serverless](https://runpod.io/serverless) instead.

## Cost Breakdown (70B Distill, 1-4 nodes)

| Usage | SF Compute | AWS | Total/month |
|-------|-----------|-----|------------|
| 1 node, 24/7 | ~$2,160 | ~$5 | **~$2,165** |
| 1 node, 8hr/day | ~$720 | ~$5 | **~$725** |
| Scale 1-4 nodes, 8hr/day avg | ~$720-2,880 | ~$5 | **~$725-2,885** |

**Recommendation:** Stop nodes when not in use with `make deprovision` to save costs.

## Troubleshooting

**`sf: command not found`** → Install SF CLI: `curl -sSL https://sfcompute.com/install.sh | sh`

**vLLM OOM error** → Reduce `GPU_MEMORY_UTILIZATION` in config or switch to smaller model

**Lambda timeout** → DeepSeek R1 can take 30-90s for long generations. Lambda max timeout is 15min — configured to 120s by default. Increase if needed.

**Node not showing healthy** → SSH in and check: `ssh ubuntu@NODE_IP "sudo docker logs deepseek-vllm --tail 100"`
