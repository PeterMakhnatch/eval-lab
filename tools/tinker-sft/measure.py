"""Offline chat_sl render measurement for evallab.sft_tinker (HAR-81).

Runs inside the isolated ``tools/tinker-sft`` uv project (never imported by
Eval Lab code). Reads a ``conversations.jsonl`` in tinker-cookbook chat
format, renders every conversation with the chosen renderer and the model's
Hugging Face tokenizer (tokenizer files only, never weights), and prints one
JSON object with token statistics to stdout. Never contacts the Tinker
service and never spends.

Truncation semantics mirror tinker-cookbook's
``datum_from_model_input_weights``: the rendered sequence is cut from the
right at ``--max-length``, so the post-truncation supervised mass is the
prefix sum of the per-token weights.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ALLOWED_ROLES = frozenset({"system", "user", "assistant"})


def _load_conversations(path: Path) -> list[list[dict[str, Any]]]:
    conversations: list[list[dict[str, Any]]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise SystemExit(f"error: cannot read {path}: {exc}") from exc
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise SystemExit(f"error: {path}:{number} is not JSON: {exc}") from exc
        if not isinstance(row, dict) or set(row) != {"messages"}:
            raise SystemExit(f"error: {path}:{number} must hold exactly a 'messages' key")
        messages = row["messages"]
        if not isinstance(messages, list) or not messages:
            raise SystemExit(f"error: {path}:{number} 'messages' must be a non-empty list")
        for message in messages:
            if not isinstance(message, dict) or set(message) != {"role", "content"}:
                raise SystemExit(
                    f"error: {path}:{number} messages must have exactly role/content keys"
                )
            if message["role"] not in ALLOWED_ROLES or not isinstance(message["content"], str):
                raise SystemExit(
                    f"error: {path}:{number} unsupported role or non-string content"
                )
        conversations.append(messages)
    if not conversations:
        raise SystemExit(f"error: {path} contains no conversations")
    return conversations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversations", type=Path, required=True)
    parser.add_argument("--model", required=True, help="HF model id for the tokenizer")
    parser.add_argument("--renderer", required=True, help="tinker-cookbook renderer name")
    parser.add_argument("--max-length", type=int, required=True)
    parser.add_argument(
        "--train-on-what",
        default="all_assistant_messages",
        help="TrainOnWhat value (protocol pins all_assistant_messages)",
    )
    args = parser.parse_args(argv)

    from tinker_cookbook.renderers import TrainOnWhat, get_renderer
    from tinker_cookbook.tokenizer_utils import get_tokenizer

    tokenizer = get_tokenizer(args.model)
    renderer = get_renderer(args.renderer, tokenizer)
    train_on_what = TrainOnWhat(args.train_on_what)

    conversations = _load_conversations(args.conversations)
    lengths: list[int] = []
    total_after = 0
    supervised_after = 0.0
    truncated = 0
    for messages in conversations:
        model_input, weights = renderer.build_supervised_example(messages, train_on_what)
        tokens = int(model_input.length)
        lengths.append(tokens)
        total_after += min(tokens, args.max_length)
        supervised_after += float(weights[: args.max_length].sum())
        if tokens > args.max_length:
            truncated += 1
    lengths.sort()
    stats = {
        "conversations": len(conversations),
        "total_tokens": sum(lengths),
        "total_tokens_after_truncation": total_after,
        "supervised_tokens_after_truncation": round(supervised_after, 1),
        "truncated_conversations": truncated,
        "longest_conversation_tokens": lengths[-1] if lengths else 0,
        "median_conversation_tokens": float(lengths[len(lengths) // 2]) if lengths else 0.0,
        "renderer_extension_property": getattr(renderer, "has_extension_property", None),
        "model": args.model,
        "renderer": args.renderer,
        "train_on_what": args.train_on_what,
        "max_length": args.max_length,
    }
    json.dump(stats, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
