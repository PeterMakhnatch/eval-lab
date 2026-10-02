"""Serve XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B on Modal with SGLang (HAR-90).

One A100-80GB, 64K context, OpenAI-compatible ``/v1/chat/completions``. Every
``/v1`` call must carry ``Authorization: Bearer <SGLANG_API_KEY>``; the key
comes from the Modal Secret ``evallab-mimo-v26-9b-api-key``. Weights live on
the Modal Volume ``evallab-mimo-v26-9b-weights``. At most one container runs,
and it scales to zero after 5 idle minutes.

From the repository root::

    # once per revision, CPU only (no GPU is billed while downloading)
    uv run --project tools/modal-mimo-serve --locked \\
        modal run tools/modal-mimo-serve/serve.py::download_weights
    # deploy / stop
    uv run --project tools/modal-mimo-serve --locked \\
        modal deploy tools/modal-mimo-serve/serve.py
    uv run --project tools/modal-mimo-serve --locked \\
        modal app stop evallab-mimo-v26-9b

Every request must send ``chat_template_kwargs: {"enable_thinking": true}``.
SGLang's ``mimo`` reasoning parser only splits ``<think>`` into
``reasoning_content`` when it is set; otherwise the reasoning text lands in
``content``. The Eval Lab proxy provider ``mimo_selfhosted`` enforces this.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO

import modal

APP_NAME = "evallab-mimo-v26-9b"
MODEL_ID = "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
# Hugging Face commit of the release weights (2026-09-22).
MODEL_REVISION = "2367e865d009c13ac81713a2878291d33ab28177"
# lmsysorg/sglang:v0.5.20-runtime (CUDA 13.0; ships the qwen3_5 model and the
# `mimo` reasoning parser). Pinned by manifest-list digest; Modal's host driver
# (580.x, CUDA 13.0) runs it.
SGLANG_IMAGE = (
    "lmsysorg/sglang@sha256:00b02004501e402332827ffd5343a225a8960d99adc990b37d6f31085b8f6800"
)
VOLUME_NAME = "evallab-mimo-v26-9b-weights"
SECRET_NAME = "evallab-mimo-v26-9b-api-key"
GPU = "A100-80GB"
CONTEXT_LENGTH = 65_536
TOOL_CALL_PARSER = "mimo"
PORT = 8000
MINUTES = 60
WEIGHTS_ROOT = Path("/weights")
MODEL_DIR = WEIGHTS_ROOT / MODEL_ID / MODEL_REVISION

image = (
    modal.Image.from_registry(SGLANG_IMAGE)
    .entrypoint([])
    .env({"HF_XET_HIGH_PERFORMANCE": "1", "HF_HUB_DISABLE_TELEMETRY": "1"})
)
weights = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
app = modal.App(APP_NAME)


@app.function(image=image, volumes={WEIGHTS_ROOT: weights}, cpu=4.0, timeout=30 * MINUTES)
def download_weights() -> str:
    """Download the pinned revision into the volume (idempotent)."""
    from huggingface_hub import snapshot_download

    if (MODEL_DIR / "config.json").exists():
        return f"present: {MODEL_DIR}"
    snapshot_download(repo_id=MODEL_ID, revision=MODEL_REVISION, local_dir=MODEL_DIR)
    weights.commit()
    return f"downloaded: {MODEL_DIR}"


def _relay_redacted(stream: IO[bytes], secret: str) -> None:
    """Copy SGLang's merged output to the container log with the key removed.

    SGLang logs ``server_args=`` (including ``api_key``) at startup; the key
    must never reach Modal's log store.
    """
    for raw in iter(stream.readline, b""):
        sys.stdout.write(raw.decode("utf-8", "replace").replace(secret, "<redacted>"))
        sys.stdout.flush()


def _wait_ready(process: subprocess.Popen[bytes], api_key: str) -> None:
    deadline = time.monotonic() + 18 * MINUTES
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/health", headers={"Authorization": f"Bearer {api_key}"}
    )
    while time.monotonic() < deadline:
        if (code := process.poll()) is not None:
            raise RuntimeError(f"sglang exited with {code} before becoming healthy")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(2)
    raise TimeoutError("sglang did not become healthy in 18 minutes")


def sglang_command(api_key: str) -> list[str]:
    """The SGLang launch command; ``serve_lora.py`` reuses it unchanged."""
    return [
        "python3",
        "-m",
        "sglang.launch_server",
        "--model-path",
        str(MODEL_DIR),
        "--served-model-name",
        MODEL_ID,
        "--reasoning-parser",
        "mimo",
        "--tool-call-parser",
        TOOL_CALL_PARSER,
        "--context-length",
        str(CONTEXT_LENGTH),
        # Capture decode CUDA graphs only for the batch sizes one trial lane
        # uses (v0.5.20 split --cuda-graph-max-bs into decode/prefill).
        "--cuda-graph-max-bs-decode",
        "16",
        "--host",
        "0.0.0.0",
        "--port",
        str(PORT),
        # Prometheus /metrics (running/queued requests, throughput, KV usage)
        # for the HAR-126 telemetry sampler; observability only.
        "--enable-metrics",
        "--api-key",
        api_key,
    ]


@app.server(
    image=image,
    gpu=GPU,
    cpu=4.0,
    memory=16 * 1024,
    volumes={WEIGHTS_ROOT: weights},
    secrets=[modal.Secret.from_name(SECRET_NAME, required_keys=["SGLANG_API_KEY"])],
    min_containers=0,
    max_containers=1,
    scaledown_window=5 * MINUTES,
    startup_timeout=20 * MINUTES,
    exit_grace_period=30,
    port=PORT,
    # SGLang's --api-key authenticates every /v1 call. Modal proxy tokens are
    # not used: the Eval Lab proxy carries exactly one upstream credential.
    unauthenticated=True,
)
class MimoServer:
    @modal.enter()
    def start(self) -> None:
        if not (MODEL_DIR / "config.json").exists():
            raise RuntimeError(
                f"weights missing at {MODEL_DIR}; run serve.py::download_weights first"
            )
        api_key = os.environ["SGLANG_API_KEY"]
        command = sglang_command(api_key)
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert self.process.stdout is not None
        threading.Thread(
            target=_relay_redacted, args=(self.process.stdout, api_key), daemon=True
        ).start()
        _wait_ready(self.process, api_key)

    @modal.exit()
    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
