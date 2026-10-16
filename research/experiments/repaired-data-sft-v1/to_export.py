"""Consume QA Trace training arms (`build_arms.py` A/B/C.jsonl) as `sft.py` exports.

Two outputs, both `evallab.sft_terminus/1` directories (conversations.jsonl + manifest.json):

**`all/`, the lossless conversion of every A row (A ⊇ B, C).**
- Terminus-2 rows are already role/content (+ reasoning_content).
- mimoagent rows carry native tool calls. They are rewritten to exactly the text the
  model's chat template renders for them:
  - the `tools` schema becomes the template's own system turn;
  - each assistant `tool_calls` becomes `<tool_call>` text;
  - tool results stay `tool` turns.
- Every converted row must render byte-identically to the template's render of the
  recorded row, so no reasoning, tool call or tool result is lost.
- `provenance.jsonl` keeps every other field of the source row (regrades, evidence,
  flags, trajectory sha256, fidelity, ...) plus the sha256 of the recorded messages and
  tools, line-aligned with conversations.jsonl.
- The script loads `all/` back through `sft.verify_export` and fails unless every A row
  loads.

**`A/`, `B/`, `C/`, the training arms.** One eligibility rule, identical for every arm,
applied before C is drawn:
- `fidelity == raw_completion`: `parsed_actions_only` rows are reconstructions, not
  model output;
- render ≤ `--max-length` tokens: sft.py's ceiling; rows are never truncated.

C is then redrawn as a seeded sample of eligible A with |C| == |eligible B|, so it stays
a same-size random control.

    HF_HUB_OFFLINE=1 uv run --project tools/modal-mimo-sft --locked python \
        research/experiments/repaired-data-sft-v1/to_export.py --arms <build_arms out> --out <dir>
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools/modal-mimo-sft"))
import sft  # noqa: E402

SEED = 20261016


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def structured(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The recorded messages with tool_calls parsed back into template form."""
    out = []
    for message in messages:
        item = dict(message)
        if "tool_calls" in item:
            calls = item["tool_calls"]
            calls = ast.literal_eval(calls) if isinstance(calls, str) else calls
            for call in calls:
                args = call["function"]["arguments"]
                if isinstance(args, str):
                    call["function"]["arguments"] = json.loads(args)
            item["tool_calls"] = calls
        out.append(item)
    return out


def flatten(tok: Any, row: dict[str, Any]) -> list[dict[str, Any]]:
    """Rewrite a row into role/content(/reasoning_content) turns with the same render."""
    messages = row["messages"]
    tools = row.get("tools")
    if row["agent"] != "mimoagent":
        return [
            {k: m[k] for k in ("role", "content", "reasoning_content") if k in m} for m in messages
        ]
    original = structured(messages)
    flat: list[dict[str, Any]] = []
    for message in original:
        role = message["role"]
        if role == "system" and tools:
            # The template renders the tools schema as its own system turn, then the
            # recorded system message as a second one.
            text = tok.apply_chat_template([message], tools=tools, tokenize=False)
            head, tail = "<|im_start|>system\n", "<|im_end|>"
            blocks = [b for b in text.split(tail) if b]
            if not blocks or not all(b.startswith(head) for b in blocks):
                raise sft.SftError(f"{row['execution_id']}: unexpected system render")
            flat.extend({"role": "system", "content": b[len(head) :]} for b in blocks)
        elif role == "assistant" and message.get("tool_calls"):
            only = tok.apply_chat_template(
                [{"role": "user", "content": "x"}, {**message, "reasoning_content": ""}],
                tokenize=False,
                enable_thinking=True,
            )
            marker = "<|im_start|>assistant\n<think></think>"
            body = only[only.index(marker) + len(marker) : only.rindex("<|im_end|>")]
            turn = {"role": "assistant", "content": body}
            if message.get("reasoning_content") is not None:
                turn["reasoning_content"] = message["reasoning_content"]
            flat.append(turn)
        else:
            flat.append(
                {k: message[k] for k in ("role", "content", "reasoning_content") if k in message}
            )

    want = tok.apply_chat_template(original, tools=tools, tokenize=False, enable_thinking=True)
    got = tok.apply_chat_template(
        sft.to_template_messages(flat), tokenize=False, enable_thinking=True
    )
    if want != got:
        at = next(
            (k for k in range(min(len(want), len(got))) if want[k] != got[k]),
            min(len(want), len(got)),
        )
        raise sft.SftError(
            f"{row['execution_id']}: rewritten render differs from the original at char {at}: "
            f"want {want[max(0, at - 200) : at + 200]!r} got {got[max(0, at - 200) : at + 200]!r}"
        )
    return flat


def jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_export(
    out: Path, messages: list[list[dict[str, Any]]], fields: dict[str, Any]
) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    lines = "".join(json.dumps({"messages": m}) + "\n" for m in messages)
    (out / sft.CONVERSATIONS_FILE).write_text(lines)
    manifest = {
        "contract": sft.EXPORT_CONTRACT,
        "conversations_sha256": sha256_bytes(lines.encode()),
    }
    manifest |= fields
    (out / sft.MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arms", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--max-length", type=int, default=sft.DEFAULT_MAX_LENGTH)
    args = ap.parse_args()
    tok = sft.get_tokenizer()
    source_manifest = json.loads((args.arms / "manifest.json").read_text())
    source_files = {
        n: sha256_bytes((args.arms / f"{n}.jsonl").read_bytes()) for n in ("A", "B", "C")
    }
    arms = {name: jsonl(args.arms / f"{name}.jsonl") for name in ("A", "B")}
    builder = "research/experiments/repaired-data-sft-v1/to_export.py"

    # Lossless conversion of every row.
    converted: list[dict[str, Any]] = []
    for row in sorted(arms["A"], key=lambda r: r["execution_id"]):
        flat = flatten(tok, row)
        try:
            tokens = len(
                sft.render_and_mask(
                    tok, flat, max_length=10**9, label=row["execution_id"]
                ).input_ids
            )
        except sft.SftError as exc:
            raise sft.SftError(f"{row['execution_id']}: does not render: {exc}") from exc
        provenance = {k: v for k, v in row.items() if k not in ("messages", "tools")}
        provenance |= {
            "source_messages_sha256": sha256_bytes(
                json.dumps(row["messages"], sort_keys=True).encode()
            ),
            "source_tools_sha256": sha256_bytes(
                json.dumps(row.get("tools"), sort_keys=True).encode()
            ),
            "render_equal_to_recorded": True,
            "sequence_tokens": tokens,
        }
        converted.append({"row": row, "messages": flat, "tokens": tokens, "provenance": provenance})
    all_dir = args.out / "all"
    write_export(
        all_dir,
        [c["messages"] for c in converted],
        {
            "arm": "all",
            "source_files": source_files,
            "builder": builder,
            "heldout_applied": source_manifest.get("heldout_applied"),
        },
    )
    (all_dir / "provenance.jsonl").write_text(
        "".join(json.dumps(c["provenance"], sort_keys=True) + "\n" for c in converted)
    )
    loaded = sft.verify_export(all_dir)
    if len(loaded.conversations) != len(arms["A"]):
        raise sft.SftError(f"only {len(loaded.conversations)} of {len(arms['A'])} rows load")

    # Training arms: one eligibility rule for every arm.
    eligible: dict[str, dict[str, Any]] = {}
    dropped: dict[str, str] = {}
    for c in converted:
        key = c["row"]["execution_id"]
        if c["row"].get("fidelity") != "raw_completion":
            dropped[key] = f"fidelity={c['row'].get('fidelity')}"
        elif c["tokens"] > args.max_length:
            dropped[key] = "over_max_length"
        else:
            eligible[key] = c
    b_ids = sorted(r["execution_id"] for r in arms["B"] if r["execution_id"] in eligible)
    c_ids = sorted(random.Random(SEED).sample(sorted(eligible), len(b_ids)))
    selection = {"A": sorted(eligible), "B": b_ids, "C": c_ids}
    summary: dict[str, Any] = {
        "source_manifest": source_manifest,
        "source_files": source_files,
        "lossless": {
            "rows": len(converted),
            "loaded": len(loaded.conversations),
            "conversations_sha256": loaded.conversations_sha256,
        },
        "rules": {
            "format": "mimoagent tool calls rewritten to template text; render equality checked",
            "eligible": "fidelity == raw_completion and render <= max_length (never truncated)",
            "c_redraw": f"random.Random({SEED}).sample(sorted eligible A, |eligible B|)",
            "max_length": args.max_length,
        },
        "dropped_before_arms": dropped,
        "arms": {},
    }
    for name, ids in selection.items():
        if not ids:
            summary["arms"][name] = {"rows": 0}
            continue
        manifest = write_export(
            args.out / name,
            [eligible[i]["messages"] for i in ids],
            {
                "arm": name,
                "execution_ids": ids,
                "tasks": sorted({eligible[i]["row"]["task"] for i in ids}),
                "sequence_tokens": sum(eligible[i]["tokens"] for i in ids),
                "heldout_applied": source_manifest.get("heldout_applied"),
                "source_files": source_files,
                "builder": builder,
            },
        )
        summary["arms"][name] = {
            k: manifest[k] for k in ("conversations_sha256", "sequence_tokens", "heldout_applied")
        } | {"rows": len(ids), "tasks": len(manifest["tasks"])}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"lossless": summary["lossless"], "arms": summary["arms"]}, indent=2))
    print(f"dropped from training: {len(dropped)} {dict(sorted(dropped.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
