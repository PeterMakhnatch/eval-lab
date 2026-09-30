"""Minimal Z.ai chat client for GLM-5.3-Flash with a hard spend cap and a JSONL ledger.

The key comes from ZAI_API_KEY; run under `keys run --`. Prices are the pinned list prices in
`src/evallab/interpretation/price_table.py`: $0.15 / 1M input, $0.03 / 1M cached input, $0.50 / 1M output.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

URL = "https://api.z.ai/api/paas/v4/chat/completions"
MODEL = "glm-5.3-flash"
PRICE_IN, PRICE_CACHED, PRICE_OUT = 0.15e-6, 0.03e-6, 0.50e-6
LEDGER = Path(__file__).resolve().parent / "spend.jsonl"
CAP_USD = float(os.environ.get("HAR111_CAP_USD", "3.0"))


class SpendCapReached(RuntimeError):
    pass


def spent_usd() -> float:
    if not LEDGER.is_file():
        return 0.0
    return sum(json.loads(line)["cost_usd"] for line in LEDGER.read_text().splitlines() if line.strip())


def _cost(usage: dict) -> float:
    prompt = int(usage.get("prompt_tokens") or 0)
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    out = int(usage.get("completion_tokens") or 0)
    return (prompt - cached) * PRICE_IN + cached * PRICE_CACHED + out * PRICE_OUT


def chat(messages: list[dict], *, tag: str, max_tokens: int = 2000, reserve_usd: float = 0.01) -> tuple[str, dict]:
    """One completion. Refuses to call if spent + reserve would pass the cap; logs every call."""
    if spent_usd() + reserve_usd > CAP_USD:
        raise SpendCapReached(f"spent ${spent_usd():.4f} of ${CAP_USD:.2f}")
    body = {
        "model": MODEL,
        "messages": messages,
        "temperature": float(os.environ.get("HAR111_TEMPERATURE", "0")),
        "max_tokens": max_tokens,
        # GLM-5.3-Flash always thinks and rejects thinking.type "disabled" (error 1210); the top-level
        # reasoning_effort is the knob that actually shrinks reasoning (probe: 9 vs 57 reasoning tokens).
        "reasoning_effort": os.environ.get("HAR111_REASONING", "low"),
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {os.environ['ZAI_API_KEY']}", "Content-Type": "application/json"},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.load(resp)
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {exc.code}: {exc.read()[:300]!r}") from exc
    usage = data.get("usage") or {}
    cost = _cost(usage)
    with LEDGER.open("a") as fh:
        fh.write(json.dumps({"ts": time.time(), "tag": tag, "model": data.get("model"), "usage": usage, "cost_usd": cost}) + "\n")
    return data["choices"][0]["message"]["content"], usage
