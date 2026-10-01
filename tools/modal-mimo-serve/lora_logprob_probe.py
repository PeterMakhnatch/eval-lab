"""Is a LoRA adapter applied? Compare greedy token logprobs, base vs adapter.

A small adapter often leaves greedy text unchanged, and long greedy
generations can diverge between identical requests, so text equality proves
nothing either way. For each prompt this asks the LoRA server for 64 greedy
tokens with logprobs, twice under the base name and twice under the adapter
name, all inside Modal (the API key never leaves it). An applied adapter
shows base-vs-base and adapter-vs-adapter differences of exactly 0 and a
nonzero base-vs-adapter difference over the shared token prefix.

    uv run --project tools/modal-mimo-serve --locked modal run \\
        tools/modal-mimo-serve/lora_logprob_probe.py --prompts prompts.json \\
        --url <lora url> --adapter <name> --out logprob-probe.json
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

import modal

MODEL_ID = "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
SECRET_NAME = "evallab-mimo-v26-9b-api-key"
PROBE_TOKENS = 64

app = modal.App("evallab-mimo-v26-9b-lora-logprob-probe")


def _greedy(url: str, key: str, model: str, prompt: str) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": PROBE_TOKENS,
        "logprobs": True,
        "chat_template_kwargs": {"enable_thinking": True},
    }
    request = urllib.request.Request(
        url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=1200) as response:
        payload = json.loads(response.read())
    choice = payload["choices"][0]
    tokens = (choice.get("logprobs") or {}).get("content") or []
    return {
        "model": payload.get("model"),
        "tokens": [(item["token"], item["logprob"]) for item in tokens],
    }


def _compare(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    diffs = []
    for (token_a, logprob_a), (token_b, logprob_b) in zip(
        first["tokens"], second["tokens"], strict=False
    ):
        if token_a != token_b:
            break
        diffs.append(abs(logprob_a - logprob_b))
    return {
        "shared_prefix_tokens": len(diffs),
        "max_abs_logprob_diff": max(diffs) if diffs else None,
        "mean_abs_logprob_diff": sum(diffs) / len(diffs) if diffs else None,
    }


@app.function(
    secrets=[modal.Secret.from_name(SECRET_NAME, required_keys=["SGLANG_API_KEY"])],
    timeout=3600,
    cpu=0.25,
)
def probe(prompts: list[str], url: str, adapter: str) -> list[dict[str, Any]]:
    key = os.environ["SGLANG_API_KEY"]
    tuned_name = f"{MODEL_ID}:{adapter}"
    rows = []
    for prompt in prompts:
        base = _greedy(url, key, MODEL_ID, prompt)
        tuned = _greedy(url, key, tuned_name, prompt)
        base_again = _greedy(url, key, MODEL_ID, prompt)
        tuned_again = _greedy(url, key, tuned_name, prompt)
        rows.append(
            {
                "models": [base["model"], tuned["model"]],
                "base_vs_adapter": _compare(base, tuned),
                "base_vs_base": _compare(base, base_again),
                "adapter_vs_adapter": _compare(tuned, tuned_again),
            }
        )
    return rows


@app.local_entrypoint()
def main(prompts: str, url: str, adapter: str, out: str) -> None:
    items = [item["prompt"] for item in json.loads(Path(prompts).read_text())]
    rows = probe.remote(items, url, adapter)
    Path(out).write_text(json.dumps(rows, indent=1) + "\n")
    print(json.dumps(rows, indent=1))
