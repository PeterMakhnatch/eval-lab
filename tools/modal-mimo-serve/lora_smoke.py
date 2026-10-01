"""Send the HAR-129 smoke prompts to the LoRA server and production (stdlib).

Runs inside Modal with the server key's Secret attached, so the key never
leaves Modal. For each prompt it asks the LoRA server under the base and the
adapter name, then production under the base name: temperature 0,
``enable_thinking`` (as the ``mimo_selfhosted`` proxy forces) and the
harness's 4096-token output cap, one request at a time. It writes the raw
responses; ``research/experiments/har129-lora/smoke_parity.py`` scores them.

    uv run --project tools/modal-mimo-serve --locked modal run \\
        tools/modal-mimo-serve/lora_smoke.py --prompts prompts.json \\
        --lora-url <url> --prod-url <url> --adapter <name> --out raw.json
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import modal

MODEL_ID = "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
SECRET_NAME = "evallab-mimo-v26-9b-api-key"

app = modal.App("evallab-mimo-v26-9b-lora-smoke")


def _chat(base: str, key: str, model: str, prompt: str) -> dict[str, Any]:
    """One completion; 502/503 while the server cold-starts are retried for 20 minutes."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 4096,
        "chat_template_kwargs": {"enable_thinking": True},
    }
    request = urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    deadline = time.monotonic() + 20 * 60
    started = time.monotonic()
    while True:
        try:
            with urllib.request.urlopen(request, timeout=1200) as response:
                payload = json.loads(response.read())
            break
        except urllib.error.HTTPError as error:
            if error.code not in (502, 503) or time.monotonic() > deadline:
                raise RuntimeError(f"{base}: HTTP {error.code}") from None
            time.sleep(10)
            started = time.monotonic()
    choice = payload["choices"][0]
    return {
        "model": payload.get("model"),
        "content": choice["message"].get("content") or "",
        "reasoning": choice["message"].get("reasoning_content") or "",
        "finish_reason": choice.get("finish_reason"),
        "completion_tokens": (payload.get("usage") or {}).get("completion_tokens"),
        "seconds": round(time.monotonic() - started, 1),
    }


@app.function(
    secrets=[modal.Secret.from_name(SECRET_NAME, required_keys=["SGLANG_API_KEY"])],
    timeout=3600,
    cpu=0.25,
)
def ask_all(
    prompts: list[dict[str, str]], lora_url: str, prod_url: str, adapter: str
) -> list[dict[str, Any]]:
    key = os.environ["SGLANG_API_KEY"]
    rows = []
    for item in prompts:
        rows.append(
            {
                "source": item["source"],
                "lora_base": _chat(lora_url, key, MODEL_ID, item["prompt"]),
                "lora_adapter": _chat(lora_url, key, f"{MODEL_ID}:{adapter}", item["prompt"]),
                "production": _chat(prod_url, key, MODEL_ID, item["prompt"]),
            }
        )
        print(item["source"], "done", flush=True)
    return rows


@app.local_entrypoint()
def main(prompts: str, lora_url: str, prod_url: str, adapter: str, out: str) -> None:
    rows = ask_all.remote(json.loads(Path(prompts).read_text()), lora_url, prod_url, adapter)
    Path(out).write_text(json.dumps(rows, indent=1) + "\n")
    print(f"wrote {len(rows)} prompts to {out}")
