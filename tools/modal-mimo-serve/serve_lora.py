"""LoRA-enabled twin of ``serve.py`` for the HAR-129 paired eval.

One SGLang server serves the base model and one PEFT LoRA adapter under two
model names (SGLang's ``<base>:<adapter>`` request syntax,
``serving_base.py`` ``_resolve_lora_path`` at v0.5.20):

- ``XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`` -> base weights;
- ``XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:<adapter>`` -> base + adapter.

Everything else is ``serve.py``'s: the same SGLang image digest, weights
revision, GPU, context length, reasoning parser, CUDA-graph batch size, API
key secret and scale-to-zero policy. The only additions are the LoRA flags
and the read-only adapter volume. It deploys as its own Modal app, so the
production ``evallab-mimo-v26-9b`` deployment is never touched.

The adapter is chosen at deploy time and baked into the app::

    EVALLAB_MIMO_LORA_ADAPTER=<run>/adapter EVALLAB_MIMO_LORA_NAME=<name> \\
      uv run --project tools/modal-mimo-serve --locked \\
        modal deploy tools/modal-mimo-serve/serve_lora.py
    uv run --project tools/modal-mimo-serve --locked \\
        modal app stop evallab-mimo-v26-9b-lora
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import modal

sys.path.insert(0, str(Path(__file__).resolve().parent))

import serve  # noqa: E402

APP_NAME = "evallab-mimo-v26-9b-lora"
SFT_VOLUME_NAME = "evallab-mimo-v26-9b-sft"
SFT_ROOT = Path("/sft")
#: Upper bound for the adapter's rank; SGLang sizes its LoRA memory pool by it.
MAX_LORA_RANK = 64

ADAPTER_REL = os.environ.get("EVALLAB_MIMO_LORA_ADAPTER", "")
ADAPTER_NAME = os.environ.get("EVALLAB_MIMO_LORA_NAME", "")
if modal.is_local() and not (
    re.fullmatch(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*", ADAPTER_REL)
    and re.fullmatch(r"[A-Za-z0-9._-]+", ADAPTER_NAME)
):
    raise SystemExit(
        "set EVALLAB_MIMO_LORA_ADAPTER (volume-relative adapter dir) and "
        "EVALLAB_MIMO_LORA_NAME (adapter model name, [A-Za-z0-9._-]+)"
    )

sft_volume = modal.Volume.from_name(SFT_VOLUME_NAME)
app = modal.App(APP_NAME)


def lora_command(api_key: str, adapter_dir: Path, adapter_name: str) -> list[str]:
    """``serve.py``'s launch command plus the LoRA flags, nothing else."""
    return serve.sglang_command(api_key) + [
        "--enable-lora",
        "--lora-paths",
        f"{adapter_name}={adapter_dir}",
        "--max-lora-rank",
        str(MAX_LORA_RANK),
        "--max-loras-per-batch",
        "1",
        # Fail loudly if any adapter key does not map onto the served model.
        "--lora-strict-loading",
    ]


@app.server(
    image=serve.image.env(
        {"EVALLAB_MIMO_LORA_ADAPTER": ADAPTER_REL, "EVALLAB_MIMO_LORA_NAME": ADAPTER_NAME}
    ).add_local_python_source("serve"),
    gpu=serve.GPU,
    cpu=4.0,
    memory=16 * 1024,
    volumes={serve.WEIGHTS_ROOT: serve.weights, SFT_ROOT: sft_volume.read_only()},
    secrets=[modal.Secret.from_name(serve.SECRET_NAME, required_keys=["SGLANG_API_KEY"])],
    min_containers=0,
    max_containers=1,
    scaledown_window=5 * serve.MINUTES,
    startup_timeout=20 * serve.MINUTES,
    exit_grace_period=30,
    port=serve.PORT,
    unauthenticated=True,
)
class MimoLoraServer:
    @modal.enter()
    def start(self) -> None:
        if not (serve.MODEL_DIR / "config.json").exists():
            raise RuntimeError(f"weights missing at {serve.MODEL_DIR}")
        adapter_dir = SFT_ROOT / os.environ["EVALLAB_MIMO_LORA_ADAPTER"]
        if not (adapter_dir / "adapter_config.json").is_file():
            raise RuntimeError(f"adapter missing at {adapter_dir}")
        api_key = os.environ["SGLANG_API_KEY"]
        command = lora_command(api_key, adapter_dir, os.environ["EVALLAB_MIMO_LORA_NAME"])
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert self.process.stdout is not None
        threading.Thread(
            target=serve._relay_redacted, args=(self.process.stdout, api_key), daemon=True
        ).start()
        serve._wait_ready(self.process, api_key)

    @modal.exit()
    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
