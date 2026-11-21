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

if serve.WINDOW is not None:
    raise ValueError("an admitted BASE serving window cannot deploy LoRA adapters")

APP_NAME = "evallab-mimo-v26-9b-lora"
SFT_VOLUME_NAME = "evallab-mimo-v26-9b-sft"
SFT_ROOT = Path("/sft")
MAX_LORA_RANK = 64

LORAS_ENV = os.environ.get("EVALLAB_MIMO_LORAS", "")


def parse_loras(spec: str) -> dict[str, str]:
    loras: dict[str, str] = {}
    for part in spec.split(","):
        name, _, rel = part.partition("=")
        if not (
            re.fullmatch(r"[A-Za-z0-9._-]+", name)
            and re.fullmatch(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*", rel)
            and name not in loras
        ):
            raise ValueError(f"bad EVALLAB_MIMO_LORAS entry {part!r}")
        loras[name] = rel
    return loras


if modal.is_local():
    try:
        parse_loras(LORAS_ENV)
    except ValueError as exc:
        raise SystemExit(
            f"{exc}: set EVALLAB_MIMO_LORAS=<name>=<volume-relative adapter dir>[,...] "
            "(names [A-Za-z0-9._-]+, unique)"
        ) from exc

sft_volume = modal.Volume.from_name(SFT_VOLUME_NAME)
app = modal.App(APP_NAME)


def lora_command(api_key: str, adapters: dict[str, Path]) -> list[str]:
    return serve.sglang_command(api_key) + [
        "--enable-lora",
        "--lora-paths",
        *(f"{name}={path}" for name, path in adapters.items()),
        "--max-lora-rank",
        str(MAX_LORA_RANK),
        "--max-loras-per-batch",
        str(len(adapters)),
        "--lora-strict-loading",
    ]


@app.server(
    image=serve.image.env({"EVALLAB_MIMO_LORAS": LORAS_ENV}).add_local_python_source("serve"),
    gpu=serve.GPU,
    cpu=4.0,
    memory=16 * 1024,
    volumes={serve.WEIGHTS_ROOT: serve.weights, SFT_ROOT: sft_volume.read_only()},
    secrets=[modal.Secret.from_name(serve.SECRET_NAME, required_keys=["SGLANG_API_KEY"])],
    min_containers=0,
    max_containers=1,
    buffer_containers=0,
    scaledown_window=serve.SCALEDOWN_WINDOW_SECONDS,
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
        adapters = {
            name: SFT_ROOT / rel
            for name, rel in parse_loras(os.environ["EVALLAB_MIMO_LORAS"]).items()
        }
        for name, adapter_dir in adapters.items():
            if not (adapter_dir / "adapter_config.json").is_file():
                raise RuntimeError(f"adapter {name} missing at {adapter_dir}")
        api_key = os.environ["SGLANG_API_KEY"]
        command = lora_command(api_key, adapters)
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
