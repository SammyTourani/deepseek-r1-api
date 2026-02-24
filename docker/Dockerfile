FROM vllm/vllm-openai:latest

# ─── Environment defaults ────────────────────────────────────────────────────
ENV MODEL_NAME=deepseek-ai/DeepSeek-R1-Distill-Llama-70B
ENV MAX_MODEL_LEN=32768
ENV TENSOR_PARALLEL_SIZE=2
ENV GPU_MEMORY_UTILIZATION=0.95
ENV PORT=8000

# HuggingFace cache dir (mount a volume here for persistence)
ENV HF_HOME=/models
ENV TRANSFORMERS_CACHE=/models

# ─── Expose inference port ────────────────────────────────────────────────────
EXPOSE 8000

# ─── Entrypoint: vLLM OpenAI-compatible server ───────────────────────────────
# Shell form so ENV substitution works at runtime
ENTRYPOINT ["sh", "-c", "python3 -m vllm.entrypoints.openai.api_server \
  --model ${MODEL_NAME} \
  --tensor-parallel-size ${TENSOR_PARALLEL_SIZE} \
  --max-model-len ${MAX_MODEL_LEN} \
  --gpu-memory-utilization ${GPU_MEMORY_UTILIZATION} \
  --host 0.0.0.0 \
  --port ${PORT} \
  --served-model-name deepseek-r1 \
  --trust-remote-code \
  --disable-log-requests"]
