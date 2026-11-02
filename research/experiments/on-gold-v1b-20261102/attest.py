#!/usr/bin/env python3
"""Attest the synthesis model: count served ``provider/model`` of every assistant message and every fallback event.

Reads the nested ``omp -p`` session logs under ``<A4B>/sessions`` (local only). Prints and writes ``attestation.json``
next to this script. ``requested`` is ``google-antigravity/gemini-3.8-flash`` (thinking level ``medium`` is a separate
event); ``gemini-4-argon`` appearing anywhere is reported and must be zero.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REQUESTED = "google-antigravity/gemini-3.8-flash"


def attest(sessions: Path) -> dict:
    served: Counter[str] = Counter()
    changes: Counter[str] = Counter()
    fallbacks = argon = 0
    files = 0
    for f in sorted(sessions.rglob("*.jsonl")):
        files += 1
        for line in f.read_text(errors="replace").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "model_change":
                changes[d.get("model", "?")] += 1
                fallbacks += bool(d.get("resolvedModelIsFallback"))
            m = d.get("message")
            if isinstance(m, dict) and m.get("role") == "assistant":
                served[f"{m.get('provider')}/{m.get('model')}"] += 1
            if "gemini-4-argon" in line:
                argon += 1
    return {"requested": REQUESTED, "session_files": files, "assistant_messages": sum(served.values()),
            "served": dict(served), "served_equals_requested": served.get(REQUESTED, 0),
            "model_changes": dict(changes), "fallback_events": fallbacks, "gemini_4_argon_mentions": argon}


def main() -> None:
    sessions = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A4B/sessions")
    out = attest(sessions)
    (Path(__file__).resolve().parent / "attestation.json").write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
