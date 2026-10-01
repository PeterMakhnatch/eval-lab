#!/usr/bin/env python3
"""Check an ``sft_terminus --per-turn-stride`` export against what was served.

For every exported row (one model call):

* **prompt**: the row's history (``messages[:-1]``) rendered through the
  model's pinned chat template with ``add_generation_prompt=True`` must have
  exactly the call's recorded ``prompt_tokens`` (ATIF ``metrics``, equal to
  the proxy ledger's ``input_tokens``);
* **target**: the target turn must have the call's recorded
  ``completion_tokens``. The target is tokenized as the template writes an
  assistant turn after the generation prompt (``<think>…</think>…`` plus
  ``<|im_end|>``); the comparison is reported, with the exact delta, and
  gates only when ``--gate-target`` is set.
* **captured** (``--capture calls.jsonl``): the row's history must equal the
  captured request's ``messages`` (role and content, byte for byte) for the
  call whose recorded prompt tokens match; any difference is reported with
  the first differing message.

Prints one JSON summary and writes ``fidelity.json`` (per-row results) next
to the export. With ``--show N`` it also writes the first N rows' rendered
prompts to ``rendered/`` for a human diff.

Needs the tokenizer files only (``tokenizer.json``, ``tokenizer_config.json``,
``chat_template.jinja``) of ``XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`` at the
pinned revision; it never loads model weights. Run with
``uv run --no-project --with transformers --with jinja2 python fidelity.py
EXPORT_DIR --tokenizer DIR``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from transformers import AutoTokenizer

REVISION = "2367e865d009c13ac81713a2878291d33ab28177"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("export", type=Path)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--capture", type=Path, action="append", default=[])
    parser.add_argument("--gate-target", action="store_true")
    parser.add_argument("--show", type=int, default=0)
    args = parser.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)
    manifest = json.loads((args.export / "manifest.json").read_text())
    rows = [json.loads(line) for line in (args.export / "conversations.jsonl").open()]
    meta = manifest["rows"]
    if len(meta) != len(rows):
        raise SystemExit(f"manifest lists {len(meta)} rows, conversations.jsonl has {len(rows)}")
    captured = [json.loads(line) for path in args.capture for line in path.open() if line.strip()]

    results = []
    for index, (row, info) in enumerate(zip(rows, meta, strict=True)):
        history, target = row["messages"][:-1], row["messages"][-1]
        prompt = tokenizer.apply_chat_template(history, add_generation_prompt=True, tokenize=False)
        prompt_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
        full = tokenizer.apply_chat_template(history + [target], tokenize=False)
        target_ids = tokenizer(full[len(prompt) :], add_special_tokens=False)["input_ids"]
        recorded_prompt = info.get("prompt_tokens_recorded")
        recorded_target = info.get("completion_tokens_recorded")
        result = {
            "row_id": info["row_id"],
            "prompt_tokens": len(prompt_ids),
            "prompt_recorded": recorded_prompt,
            "prompt_ok": recorded_prompt is not None and len(prompt_ids) == recorded_prompt,
            "target_tokens": len(target_ids),
            "target_recorded": recorded_target,
            "target_delta": None if recorded_target is None else len(target_ids) - recorded_target,
        }
        if captured:
            match = next(
                (
                    call
                    for call in captured
                    if ((call.get("response_body") or {}).get("usage") or {}).get("prompt_tokens")
                    == recorded_prompt
                    and _messages(call) is not None
                    and len(_messages(call)) == len(history)
                ),
                None,
            )
            if match is None:
                result["capture"] = "no_matching_call"
            else:
                sent = _messages(match)
                diff = next(
                    (
                        i
                        for i, (a, b) in enumerate(zip(history, sent, strict=True))
                        if (a["role"], a["content"]) != (b.get("role"), b.get("content"))
                    ),
                    None,
                )
                result["capture"] = "identical" if diff is None else f"differs_at_message_{diff}"
        results.append(result)
        if index < args.show:
            out = args.export / "rendered"
            out.mkdir(exist_ok=True)
            (out / f"{info['row_id'].replace(':', '_')}.prompt.txt").write_text(prompt)

    gated = [r for r in results if not r["prompt_ok"]]
    if args.gate_target:
        gated += [r for r in results if r["target_delta"] not in (0, None)]
    summary = {
        "tokenizer_revision": REVISION,
        "rows": len(results),
        "prompt_exact": sum(r["prompt_ok"] for r in results),
        "target_exact": sum(r["target_delta"] == 0 for r in results),
        "target_delta_values": sorted({r["target_delta"] for r in results if r["target_delta"]}),
        "captured_identical": sum(r.get("capture") == "identical" for r in results),
        "captured_checked": sum("capture" in r for r in results),
        "failing_rows": [r["row_id"] for r in gated],
    }
    (args.export / "fidelity.json").write_text(
        json.dumps({"summary": summary, "rows": results}, indent=1) + "\n"
    )
    print(json.dumps(summary, indent=1))


def _messages(call: dict) -> list[dict] | None:
    body = call.get("request_body")
    messages = body.get("messages") if isinstance(body, dict) else None
    return messages if isinstance(messages, list) else None


if __name__ == "__main__":
    main()
