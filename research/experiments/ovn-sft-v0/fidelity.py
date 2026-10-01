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
* **captured** (``--capture TRIAL=calls.jsonl``, one file per trial holding
  only that trial's calls): see :func:`capture_verdict`. A row of a trial
  with a capture fails unless it is ``identical``; with
  ``--require-capture`` every row needs one.

Prints one JSON summary, writes ``fidelity.json`` (per-row results) next to
the export and exits 1 when any row fails. With ``--show N`` it also writes
the first N rows' rendered prompts to ``rendered/`` for a human diff.

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
from typing import Any

REVISION = "2367e865d009c13ac81713a2878291d33ab28177"


def capture_verdict(history: list[dict], target: dict, calls: list[dict]) -> str:
    """Compare one exported row with its own trial's captured calls.

    ``calls`` holds only the row's trial (bound by ``--capture TRIAL=PATH``),
    so two trials with identical prompts can never borrow each other's call.
    The row's call is the one delivered call (status 200, no recorded
    ``error`` such as ``client_disconnect``) whose request carries exactly
    ``len(history)`` messages; none is ``missing`` (``undelivered`` when
    calls at that position exist but none was delivered), more than one is
    ``ambiguous`` (a retried call: the export cannot say which was trained).
    Then the history must equal the request ``messages`` (role and content,
    byte for byte) and the target must equal choice 0's ``content`` and
    ``reasoning_content``.
    """
    at_position = [call for call in calls if len(_messages(call) or []) == len(history)]
    matches = [
        call
        for call in at_position
        if call.get("error") is None and call.get("response_status") == 200
    ]
    if not matches:
        return "undelivered" if at_position else "missing"
    if len(matches) > 1:
        return "ambiguous"
    sent = _messages(matches[0]) or []
    for index, (ours, theirs) in enumerate(zip(history, sent, strict=True)):
        if (ours["role"], ours["content"]) != (theirs.get("role"), theirs.get("content")):
            return f"differs_at_message_{index}"
    expected = (target.get("content"), target.get("reasoning_content") or "")
    return "identical" if _reply(matches[0]) == expected else "differs_at_target"


def main() -> None:
    from transformers import AutoTokenizer

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("export", type=Path)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument(
        "--capture",
        action="append",
        default=[],
        help="TRIAL=calls.jsonl holding that trial's captured calls only (repeatable)",
    )
    parser.add_argument("--gate-target", action="store_true")
    parser.add_argument(
        "--require-capture",
        action="store_true",
        help="every row must have an identical capture (validation runs)",
    )
    parser.add_argument("--show", type=int, default=0)
    args = parser.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)
    manifest = json.loads((args.export / "manifest.json").read_text())
    rows = [json.loads(line) for line in (args.export / "conversations.jsonl").open()]
    meta = manifest["rows"]
    if len(meta) != len(rows):
        raise SystemExit(f"manifest lists {len(meta)} rows, conversations.jsonl has {len(rows)}")
    trial_of = {c["conversation_id"]: Path(c["trial"]).name for c in manifest["conversations"]}
    captured: dict[str, list[dict[str, Any]]] = {}
    for spec in args.capture:
        trial, _, path = spec.partition("=")
        if not path or trial in captured:
            raise SystemExit(f"--capture must be TRIAL=PATH, once per trial: {spec}")
        lines = Path(path).read_text().splitlines()
        captured[trial] = [json.loads(line) for line in lines if line.strip()]

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
        trial = trial_of[info["conversation_id"]]
        if trial in captured:
            result["capture"] = capture_verdict(history, target, captured[trial])
        results.append(result)
        if index < args.show:
            out = args.export / "rendered"
            out.mkdir(exist_ok=True)
            (out / f"{info['row_id'].replace(':', '_')}.prompt.txt").write_text(prompt)

    gated = [r for r in results if not r["prompt_ok"]]
    if args.gate_target:
        gated += [r for r in results if r["target_delta"] not in (0, None)]
    # A row whose trial has a capture must match it; with --require-capture,
    # every row must have one.
    gated += [
        r
        for r in results
        if r.get("capture", "missing" if args.require_capture else "identical") != "identical"
    ]
    summary = {
        "tokenizer_revision": REVISION,
        "rows": len(results),
        "prompt_exact": sum(r["prompt_ok"] for r in results),
        "target_exact": sum(r["target_delta"] == 0 for r in results),
        "target_delta_values": sorted({r["target_delta"] for r in results if r["target_delta"]}),
        "captured_identical": sum(r.get("capture") == "identical" for r in results),
        "captured_checked": sum("capture" in r for r in results),
        "failing_rows": sorted({r["row_id"] for r in gated}),
    }
    (args.export / "fidelity.json").write_text(
        json.dumps({"summary": summary, "rows": results}, indent=1) + "\n"
    )
    print(json.dumps(summary, indent=1))
    if summary["failing_rows"]:
        raise SystemExit(1)


def _messages(call: dict) -> list[dict] | None:
    body = call.get("request_body")
    messages = body.get("messages") if isinstance(body, dict) else None
    return messages if isinstance(messages, list) else None


def _reply(call: dict) -> tuple[str | None, str] | None:
    """The served completion: ``(content, reasoning_content)`` of choice 0."""
    body = call.get("response_body")
    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    return message.get("content"), message.get("reasoning_content") or ""


if __name__ == "__main__":
    main()
