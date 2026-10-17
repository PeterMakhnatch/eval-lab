#!/usr/bin/env python3
"""Aggregate an explicitly supplied OMP session's normalized usage, never its text.

This is diagnostic log accounting, not a bill or a count of physical upstream
requests. Cache counters are disjoint from normalized input; reasoning is a
subset of output. Only top-level assistant messages count, not context snapshots.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FIELDS = ("input", "output", "cacheRead", "cacheWrite", "totalTokens", "reasoningTokens")


def instant(text: str) -> datetime:
    result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps require an explicit UTC offset")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--since", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    since = instant(args.since)
    snapshot = args.session.read_bytes()
    captured = datetime.now(UTC).isoformat()
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    seen: set[str] = set()
    tools: dict[str, Counter[str]] = defaultdict(Counter)
    unknown_usage = invalid_lines = duplicate_entries = 0
    anomalies: Counter[str] = Counter()
    for line in snapshot.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            invalid_lines += 1
            continue
        if not isinstance(record, dict) or record.get("type") != "message":
            continue
        stamp = record.get("timestamp")
        if not isinstance(stamp, str) or instant(stamp) < since:
            continue
        identifier = record.get("id")
        if isinstance(identifier, str):
            if identifier in seen:
                duplicate_entries += 1
                continue
            seen.add(identifier)
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        if message.get("role") == "toolResult":
            name = str(message.get("toolName", "unknown"))
            content = message.get("content", [])
            tools[name]["results"] += 1
            tools[name]["stored_text_bytes"] += sum(
                len(part["text"].encode())
                for part in content
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            )
        if message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            unknown_usage += 1
            continue
        normalized = {
            key: value if type(value := usage.get(key)) is int and value >= 0 else None
            for key in FIELDS
        }
        if all(normalized[key] == 0 for key in FIELDS[:4]):
            anomalies["all_zero_usage_messages_not_proven_free"] += 1
        if (
            all(normalized[key] is not None for key in FIELDS[:5])
            and sum(normalized[key] for key in FIELDS[:4]) != normalized["totalTokens"]
        ):
            anomalies["normalized_total_mismatch"] += 1
        if (
            normalized["reasoningTokens"] is not None
            and normalized["output"] is not None
            and normalized["reasoningTokens"] > normalized["output"]
        ):
            anomalies["recorded_reasoning_exceeds_output"] += 1
        groups[
            (str(message.get("provider", "unknown")), str(message.get("model", "unknown")))
        ].append(normalized)
    rows = []
    for (provider, model), usages in sorted(groups.items()):
        totals = {
            field: {
                "recorded_subtotal": sum(values) if values else None,
                "known_messages": len(values),
                "metered_messages": len(usages),
            }
            for field in FIELDS
            for values in [[usage[field] for usage in usages if usage[field] is not None]]
        }
        prompt_fields = ("input", "cacheRead", "cacheWrite")
        prompt_complete = all(
            totals[field]["known_messages"] == len(usages) for field in prompt_fields
        )
        prompt = (
            sum(totals[field]["recorded_subtotal"] for field in prompt_fields)
            if prompt_complete
            else None
        )
        output = totals["output"]["recorded_subtotal"]
        reasoning = totals["reasoningTokens"]["recorded_subtotal"]
        reasoning_complete = all(
            totals[field]["known_messages"] == len(usages)
            for field in ("output", "reasoningTokens")
        )
        rows.append(
            {
                "provider": provider,
                "model": model,
                "metered_messages": len(usages),
                "normalized_usage": totals,
                "cache_read_fraction_of_recorded_prompt": totals["cacheRead"]["recorded_subtotal"]
                / prompt
                if prompt
                else None,
                "recorded_reasoning_fraction_of_output": reasoning / output
                if reasoning_complete and output
                else None,
            }
        )
    report = {
        "schema": "agent-efficiency-omp-session-usage/v1",
        "since_utc": since.astimezone(UTC).isoformat(),
        "captured_at_utc": captured,
        "source_prefix_bytes": len(snapshot),
        "source_prefix_sha256": hashlib.sha256(snapshot).hexdigest(),
        "analyzer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "models": rows,
        "assistant_messages_without_usage": unknown_usage,
        "invalid_jsonl_lines": invalid_lines,
        "duplicate_message_entries": duplicate_entries,
        "anomalies": dict(sorted(anomalies.items())),
        "tools": [
            {"tool": name, **counts}
            for name, counts in sorted(
                tools.items(), key=lambda item: -item[1]["stored_text_bytes"]
            )
        ],
        "limits": [
            "Only the supplied session, not child workers or external coding/navigation/policy episodes.",
            "Normalized recorded counters; unknown physical retries, serving identity and subscription charge. Catalog cost estimates are intentionally omitted.",
            "Reasoning is a subset of output, never added again. All-zero initialized/error records are not proven free.",
            "Tool bytes are stored text only, not causal input-token attribution or proof of avoidable work; actual provider context may be compacted/truncated.",
            "No conversation text, reasoning text, entry IDs, account identifiers, source filename or filesystem paths are exported.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "metered_messages": sum(row["metered_messages"] for row in rows),
                "models": [
                    {
                        "provider": row["provider"],
                        "model": row["model"],
                        "cache_read_fraction": row["cache_read_fraction_of_recorded_prompt"],
                        "reasoning_fraction_of_output": row[
                            "recorded_reasoning_fraction_of_output"
                        ],
                    }
                    for row in rows
                ],
                "anomalies": report["anomalies"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
