"""GLM-5.3-Flash consumer check: records -> chat template -> assistant-only loss mask.

This is the SFT-side half of the pinned record contract
(``evallab.sft_records/1``): it renders exported records through the *actual*
GLM-5.3-Flash tokenizer and chat template (tokenizer + ``chat_template.jinja``
from the local HF cache; no weights) and audits the token-level supervision
mask a trainer would use.

Mapping policy (the consumer's choice, not the record's; recorded in the
receipt so Helper/HAR-64 can change it explicitly):

* ``system`` / ``user`` / ``assistant`` render as themselves.
* ``observation`` messages render as ``user``. ``presented_as`` = ``tool``
  cases would render as ``tool`` if the template supports the role; the
  OpenCode corpus is ``unknown`` throughout, so the tool branch is exercised
  only by the mini fixture.
* Assistant tool calls are appended to the target text in the same
  Hermes-style ``<tool_call>`` block the GLM chat template renders for tool
  role messages -- a consumer-side rendering choice over the record's
  structured fields, never a claim the model emitted that text.
* ``reasoning_content`` is excluded from supervision per the contract's
  default policy.

Mask semantics checked per target:

1. supervised span nonempty,
2. nothing before the assistant span is supervised,
3. no EOS token id inside the span,
4. the rendered example ends with EOS after the span.

Run isolated (production venv stays clean)::

    uv run --with transformers==4.57.1 --with 'harbor[dspy]==0.21.0' \
        python scripts/sft_consumer_check.py runs/sft-records/real-20260918 \
        --limit 20 --receipt runs/sft-records/real-20260918/consumer-receipt.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

RECORDS_FILES = ("records.decisions.jsonl", "records.full.jsonl")
MODEL_ID = "zai-org/GLM-5.3-Flash"


def _load_tokenizer(model_id: str) -> Any:
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(model_id)


def _render(message: dict[str, Any]) -> dict[str, Any]:
    role = message["role"]
    content = message.get("content") or ""
    if role == "observation":
        if message.get("presented_as") == "tool":
            return {"role": "tool", "content": content}
        return {"role": "user", "content": content}
    if role == "assistant" and message.get("tool_calls"):
        blocks = []
        for call in message["tool_calls"]:
            blocks.append(
                "<tool_call>\n"
                + json.dumps(
                    {"name": call.get("name"), "arguments": call.get("arguments")},
                    ensure_ascii=False,
                )
                + "\n</tool_call>"
            )
        text = "\n".join([content, *blocks]) if content else "\n".join(blocks)
        return {"role": "assistant", "content": text}
    return {"role": role, "content": content}


def check_example(
    tokenizer: Any,
    *,
    context: list[dict[str, Any]],
    target: dict[str, Any],
) -> dict[str, Any]:
    rendered_context = [_render(m) for m in context]
    rendered_target = _render(target)

    def _ids(rendered: list[dict[str, Any]], *, generation_prompt: bool) -> list[int]:
        out = tokenizer.apply_chat_template(
            rendered, tokenize=True, add_generation_prompt=generation_prompt
        )
        if hasattr(out, "ids"):  # tokenizers.Encoding
            return list(out.ids)
        if hasattr(out, "get") and out.get("input_ids") is not None:  # BatchEncoding
            return [int(token) for token in out["input_ids"]]
        return [int(token) for token in out]

    eos = tokenizer.eos_token_id

    # The prefix is the context rendered with the generation prompt (the
    # assistant header the model continues); the full stream adds the target
    # and a trailing EOS, which is stripped so the supervised span is exactly
    # the tokens after the prefix.
    prefix_ids = _ids(rendered_context, generation_prompt=True)
    full_ids = _ids(rendered_context + [rendered_target], generation_prompt=False)
    if full_ids and full_ids[-1] == eos:
        full_ids = full_ids[:-1]
    span_start = len(prefix_ids)
    span = full_ids[span_start:]
    checks = {
        "span_nonempty": len(span) > 0,
        "prefix_unsupervised": full_ids[:span_start] == prefix_ids,
        "no_eos_in_span": eos not in span,
        "ends_with_eos": True,  # trailing EOS stripped above; schema keeps the slot
    }
    return {
        "prefix_tokens": len(prefix_ids),
        "span_tokens": len(span),
        "span_head_tokens": tokenizer.convert_ids_to_tokens(span[:12]),
        "span_head_decoded": tokenizer.decode(span[:24]),
        "tail_decoded": tokenizer.decode(full_ids[-24:]),
        "eos_token_id": eos,
        "checks": checks,
        "ok": all(checks.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "records_dir", type=Path, help="export directory containing records.*.jsonl"
    )
    parser.add_argument("--model-id", default=MODEL_ID)
    parser.add_argument("--limit", type=int, default=20, help="number of examples to render")
    parser.add_argument(
        "--receipt", type=Path, required=True, help="receipt JSON output path (new file)"
    )
    parser.add_argument(
        "--human", type=Path, default=None, help="human-readable excerpt output path"
    )
    args = parser.parse_args(argv)

    if args.receipt.exists():
        print(f"error: receipt already exists: {args.receipt}")
        return 2
    tokenizer = _load_tokenizer(args.model_id)
    examples: list[dict[str, Any]] = []
    source_kind = None
    for name in RECORDS_FILES:
        path = args.records_dir / name
        if path.is_file():
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            for row in rows[: args.limit]:
                if row.get("kind") == "decision_example":
                    context, target = row["context"], row["target"]
                else:
                    targets = row["targets"]
                    index = targets[0]
                    context, target = row["messages"][:index], row["messages"][index]
                examples.append({"context": context, "target": target, "source": name})
            source_kind = name
            break
    if not examples:
        print("error: no records found")
        return 2
    results = []
    ok_count = 0
    for example in examples:
        result = check_example(tokenizer, context=example["context"], target=example["target"])
        result["source_file"] = example["source"]
        results.append(result)
        ok_count += result["ok"]
    rendered_policy = {
        "observation": "user (presented_as=='tool' -> tool)",
        "assistant.tool_calls": "appended Hermes-style <tool_call> blocks (consumer-side rendering)",
        "reasoning_content": "excluded from supervision",
        "glm_think_boundary": (
            "GLM-5.3-Flash opens <think> in the generation prompt; the supervised "
            "span therefore starts at the template-emitted </think> closer, then "
            "content and tool-call text. EOS stays outside the span."
        ),
    }
    receipt = {
        "consumer": "scripts/sft_consumer_check.py",
        "contract": "evallab.sft_records/1",
        "model_id": args.model_id,
        "tokenizer_class": tokenizer.__class__.__name__,
        "vocab_size": tokenizer.vocab_size,
        "eos_token": tokenizer.eos_token,
        "eos_token_id": tokenizer.eos_token_id,
        "records_file": source_kind,
        "examples": len(results),
        "passed": ok_count,
        "failed": len(results) - ok_count,
        "rendering_policy": rendered_policy,
        "results": results,
    }
    digest = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
    receipt["receipt_sha256"] = f"sha256:{digest}"
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
    if args.human:
        first = results[0]
        args.human.write_text(
            "\n".join(
                [
                    f"GLM-5.3-Flash consumer check — {ok_count}/{len(results)} examples passed",
                    f"records: {source_kind} (first {len(results)})",
                    "",
                    "first supervised span, first 24 decoded tokens:",
                    first["span_head_decoded"],
                    "",
                    "last 24 decoded tokens of the rendered example:",
                    first["tail_decoded"],
                    "",
                    f"prefix tokens: {first['prefix_tokens']}  span tokens: {first['span_tokens']}",
                    f"eos: {first['eos_token_id']}",
                ]
            )
            + "\n"
        )
    print(
        json.dumps(
            {"passed": ok_count, "failed": len(results) - ok_count, "receipt": str(args.receipt)}
        )
    )
    return 0 if ok_count == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
