"""Account for observed request exposure after flagged tool errors, not saved tokens.

Inputs are an explicitly selected private OMP cohort and its local stats database.
No prompts, tool arguments/results, session identifiers, or source paths are exported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

COUNTERS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
)


def usage(rows: list[dict]) -> dict:
    result = {}
    for key in COUNTERS:
        values = [r[key] for r in rows if isinstance(r[key], int) and not isinstance(r[key], bool)]
        result[key] = {"known_total": sum(values), "unknown_rows": len(rows) - len(values)}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--stats", type=Path, default=Path.home() / ".omp/stats.db")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cohort_bytes = args.cohort.read_bytes()
    cohort = json.loads(cohort_bytes)
    connection = sqlite3.connect(args.stats.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    reports, all_rows, all_followups = [], [], []
    for sample in cohort["sample"]:
        source_bytes = Path(sample["session_file"]).read_bytes()
        if hashlib.sha256(source_bytes).hexdigest() != sample["sha256"]:
            raise RuntimeError(f"source changed after cohort selection: {sample['audit_id']}")
        rows = [
            dict(r)
            for r in connection.execute(
                "SELECT entry_id,provider,model,input_tokens,output_tokens,cache_read_tokens,"
                "cache_write_tokens,total_tokens FROM messages WHERE session_file=? ORDER BY timestamp,id",
                (sample["session_file"],),
            )
        ]
        by_entry = {r["entry_id"]: r for r in rows}
        if len(by_entry) != len(rows):
            raise RuntimeError("multiple request rows share one source entry; mapping is ambiguous")
        pending_errors = 0
        flagged_results = 0
        followup_ids = set()
        unmapped_followups = 0
        for line in source_bytes.splitlines():
            record = json.loads(line)
            if record.get("type") != "message":
                continue
            message = record.get("message", {})
            if not isinstance(message, dict):
                continue
            if message.get("role") == "toolResult" and message.get("isError") is True:
                flagged_results += 1
                pending_errors += 1
            elif message.get("role") == "assistant":
                if pending_errors:
                    entry_id = record.get("id")
                    if entry_id in by_entry:
                        followup_ids.add(entry_id)
                    else:
                        unmapped_followups += 1
                pending_errors = 0
        followups = [r for r in rows if r["entry_id"] in followup_ids]
        tool_flags = connection.execute(
            "SELECT SUM(is_error) FROM tool_calls WHERE session_file=?", (sample["session_file"],)
        ).fetchone()[0]
        if flagged_results != tool_flags or len(rows) != sample["requests"]:
            raise RuntimeError(f"source/database counts disagree: {sample['audit_id']}")
        reports.append(
            {
                "audit_id": sample["audit_id"],
                "source_sha256": sample["sha256"],
                "requests": len(rows),
                "flagged_tool_results": flagged_results,
                "requests_immediately_after_flagged_tool_results": len(followups),
                "unmapped_followup_entries": unmapped_followups,
                "terminal_flagged_results_without_next_assistant": pending_errors,
                "all_usage": usage(rows),
                "following_error_usage": usage(followups),
            }
        )
        all_rows.extend(rows)
        all_followups.extend(followups)
    connection.close()
    all_usage = usage(all_rows)
    following_usage = usage(all_followups)
    prompt_keys = ("input_tokens", "cache_read_tokens", "cache_write_tokens")
    prompt_known = sum(all_usage[k]["known_total"] for k in prompt_keys)
    report = {
        "schema_version": 1,
        "cohort_sha256": hashlib.sha256(cohort_bytes).hexdigest(),
        "definition": "First subsequent assistant request after one or more flagged tool results; each request counted once.",
        "normalization": "OMP stats input/cache-read/cache-write are disjoint; output includes reasoning/tool arguments when reported. Monetary cost is not inferred.",
        "causality_limit": "Following-error exposure is observed association, not wasted work, removable cost, a counterfactual, or predicted token savings. Necessary recovery and useful edits may share the request.",
        "traces": len(reports),
        "requests": len(all_rows),
        "flagged_tool_results": sum(r["flagged_tool_results"] for r in reports),
        "requests_immediately_after_flagged_tool_results": len(all_followups),
        "unmapped_followup_entries": sum(r["unmapped_followup_entries"] for r in reports),
        "all_usage": all_usage,
        "following_error_usage": following_usage,
        "cache_read_fraction_of_known_prompt_tokens": (
            all_usage["cache_read_tokens"]["known_total"] / prompt_known if prompt_known else None
        ),
        "requests_by_provider": dict(Counter(r["provider"] for r in all_rows)),
        "rows": reports,
    }
    with args.output.open("x") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
