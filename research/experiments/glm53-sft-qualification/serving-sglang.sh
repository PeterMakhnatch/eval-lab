#!/usr/bin/env bash
# HAR-66 pinned GLM-5.3-Flash serving commands (SGLang purpose-built image).
# Source: https://cookbook.sglang.io/autoregressive/GLM/GLM-5.3-Flash
# Support is NOT in a public SGLang release; nightly images do not work.
# Set: export HF_TOKEN=... ; MODEL=zai-org/GLM-5.3-Flash   (or -BF16 variant)
set -euo pipefail

IMAGE=lmsysorg/sglang:glm-5.3-flash
COMMON_FLAGS=(
  --dsa-prefill-backend tilelang
  --dsa-decode-backend tilelang
  --kv-cache-dtype bfloat16
  --moe-runner-backend deep_gemm
  --reasoning-parser glm45
  --tool-call-parser glm47
  --host 0.0.0.0
)

case "${1:-h100}" in
  h100)
    # Native FP8 base checkpoint (~306 GiB). Hopper: BF16 KV mandatory;
    # FP8 KV + trtllm DSA is Blackwell-only.
    exec docker run --gpus all --shm-size 32g --ipc=host --net=host \
      -v ~/.cache/huggingface:/root/.cache/huggingface \
      -e HF_TOKEN \
      "$IMAGE" sglang serve \
      --model-path "${MODEL:-zai-org/GLM-5.3-Flash}" \
      --tp-size 8 --ep-size 8 \
      --mem-fraction-static 0.70 \
      "${COMMON_FLAGS[@]}" \
      --port 30000
    ;;
  h200)
    # BF16 checkpoint (base or merged LoRA export, ~598 GiB): needs H200-class VRAM.
    exec docker run --gpus all --shm-size 32g --ipc=host --net=host \
      -v ~/.cache/huggingface:/root/.cache/huggingface \
      -e HF_TOKEN \
      "$IMAGE" sglang serve \
      --model-path "${MODEL:-zai-org/GLM-5.3-Flash-BF16}" \
      --tp-size 8 --ep-size 8 \
      --mem-fraction-static 0.75 \
      "${COMMON_FLAGS[@]}" \
      --port 30000
    ;;
  *)
    echo "usage: $0 [h100|h200]  (MODEL=<repo> to override)" >&2
    exit 2
    ;;
esac
