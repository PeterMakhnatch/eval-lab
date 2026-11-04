#!/usr/bin/env python3
"""Count native OMP attribution metadata from explicit JSONL files; emit no text."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def fallback_counts() -> dict[str, int]:
    return {"true": 0, "false": 0, "absent": 0, "other": 0}


def count_fallback(record: dict, counts: dict[str, int]) -> None:
    if "resolvedModelIsFallback" not in record:
        counts["absent"] += 1
    elif record["resolvedModelIsFallback"] is True:
        counts["true"] += 1
    elif record["resolvedModelIsFallback"] is False:
        counts["false"] += 1
    else:
        counts["other"] += 1


def scalar(value):
    """The inspected native schema stores these fields as strings or null."""
    return value if isinstance(value, str) else None


def summarize(path: Path) -> dict:
    result = {"path": str(path), "status": "missing", "sha256": None}
    if not path.is_file():
        return result
    try:
        data = path.read_bytes()
    except OSError:
        result["status"] = "unreadable"
        return result
    assistant_counts: Counter = Counter()
    model_changes: Counter = Counter()
    assistant_fallback = fallback_counts()
    model_change_fallback = fallback_counts()
    top_level_fallback = fallback_counts()
    malformed = []
    parsed = 0
    for number, line in enumerate(data.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            malformed.append(number)
            continue
        if not isinstance(record, dict):
            malformed.append(number)
            continue
        parsed += 1
        count_fallback(record, top_level_fallback)
        if record.get("type") == "model_change":
            model_changes[scalar(record.get("model"))] += 1
            count_fallback(record, model_change_fallback)
        message = record.get("message")
        if record.get("type") == "message" and isinstance(message, dict) and message.get("role") == "assistant":
            assistant_counts[(scalar(message.get("provider")), scalar(message.get("model")))] += 1
            count_fallback(message, assistant_fallback)
    result.update({
        "status": "malformed" if malformed else "ok",
        "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
        "parsed_records": parsed, "malformed_json_lines": malformed,
        "assistant_messages": sum(assistant_counts.values()),
        "assistant_messages_by_provider_model": [
            {"provider": provider, "model": model, "count": count}
            for (provider, model), count in sorted(assistant_counts.items(), key=lambda item: (item[0][0] or "", item[0][1] or ""))
        ],
        "model_change_records": sum(model_changes.values()),
        "model_change_records_by_model": [
            {"model": model, "count": count}
            for model, count in sorted(model_changes.items(), key=lambda item: item[0] or "")
        ],
        "resolvedModelIsFallback": {
            "assistant_message_fields": assistant_fallback,
            "model_change_record_fields": model_change_fallback,
            "all_top_level_record_fields": top_level_fallback,
        },
    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sessions", type=Path, nargs="+", help="Explicit native session JSONL file paths; no directories or discovery")
    parser.add_argument("--out", type=Path, help="Optional JSON output file; the same JSON also prints to stdout")
    args = parser.parse_args()
    paths = [path.expanduser().absolute() for path in args.sessions]
    if len(set(paths)) != len(paths):
        parser.error("Duplicate input paths would double count records")
    files = [summarize(path) for path in paths]
    assistant: Counter = Counter()
    models: Counter = Counter()
    fallbacks = {key: fallback_counts() for key in ("assistant_message_fields", "model_change_record_fields", "all_top_level_record_fields")}
    for record in files:
        for row in record.get("assistant_messages_by_provider_model", []):
            assistant[(row["provider"], row["model"])] += row["count"]
        for row in record.get("model_change_records_by_model", []):
            models[row["model"]] += row["count"]
        for key, counts in record.get("resolvedModelIsFallback", {}).items():
            for value, count in counts.items():
                fallbacks[key][value] += count
    output = {
        "schema_version": "ov1104-native-worker-attestation-v1",
        "policy": "Explicit file snapshots only; no session text emitted. Absent fallback fields are not explicit false values. Counts are deterministic, not sampled estimates.",
        "files": files,
        "totals": {
            "requested_files": len(files), "ok_files": sum(row["status"] == "ok" for row in files),
            "missing_or_unreadable_files": sum(row["status"] in ("missing", "unreadable") for row in files),
            "assistant_messages": sum(assistant.values()),
            "assistant_messages_by_provider_model": [
                {"provider": provider, "model": model, "count": count}
                for (provider, model), count in sorted(assistant.items(), key=lambda item: (item[0][0] or "", item[0][1] or ""))
            ],
            "model_change_records": sum(models.values()),
            "model_change_records_by_model": [
                {"model": model, "count": count}
                for model, count in sorted(models.items(), key=lambda item: item[0] or "")
            ],
            "resolvedModelIsFallback": fallbacks,
        },
    }
    text = json.dumps(output, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    print(text, end="")
    return 0 if all(row["status"] == "ok" for row in files) else 1


if __name__ == "__main__":
    raise SystemExit(main())
