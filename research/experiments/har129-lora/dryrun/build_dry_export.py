"""Throwaway (HAR-129 dry run only): approximate sft_terminus/1 conversations
from recorded step layers (proposed raw message + reasoning, then the
observation). Plumbing/time/memory test data, never a training set."""

import hashlib
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
rows = []
for trial in sys.argv[2:]:
    data = json.loads((Path(trial) / "agent/trajectory.json").read_text())
    messages = []
    for step in data["steps"]:
        if step["source"] == "user" and not messages:
            messages.append({"role": "user", "content": step["message"]})
        if step["source"] != "agent":
            continue
        layers = (step.get("extra") or {}).get("step_layers") or {}
        proposed = layers.get("proposed") or {}
        text = proposed.get("message")
        if not isinstance(text, str) or not text:
            continue
        reasoning = proposed.get("reasoning")
        content = f"<think>\n{reasoning}\n</think>\n\n{text}" if reasoning else text
        messages.append({"role": "assistant", "content": content})
        obs = "\n".join(
            r.get("content") or "" for r in (step.get("observation") or {}).get("results") or []
        )
        messages.append({"role": "user", "content": obs or "(no output)"})
    while messages and messages[-1]["role"] != "assistant":
        messages.pop()
    rows.append({"messages": messages})
    print(Path(trial).name, "turns", sum(m["role"] == "assistant" for m in messages))
conv = out / "conversations.jsonl"
conv.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
digest = "sha256:" + hashlib.sha256(conv.read_bytes()).hexdigest()
(out / "manifest.json").write_text(
    json.dumps(
        {
            "contract": "evallab.sft_terminus/1",
            "conversations_sha256": digest,
            "note": "HAR-129 dry run only: approximated from step layers; not a training set",
            "sources": sys.argv[2:],
        },
        indent=2,
    )
)
print(digest)
