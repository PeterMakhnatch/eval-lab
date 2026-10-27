#!/usr/bin/env python3
"""LB-5: run the current Stage-1 tripwires over the Goodfire DEV traces and record which steps fire.

Read-only. Only tasks that are DEV in the database *and* admitted by the production gate
(``admitted_cloud_task_ids`` over ``CorpusRegistry``) are read; held Goodfire rows are never
opened. No model call, no Docker, no network. The output is one JSON line per trace holding
step numbers only (rule hits, passage-positive steps with their publisher channel and
category); no trajectory text is written.

Adapter (DB step rows -> ATIF-style agent steps), the only judgment this script makes:
* an ``assistant`` row becomes one agent step; its stored arguments (JSON object, or a JSON array
  of ``{name, arguments}`` calls) become ``tool_calls``; ``cmd`` is renamed ``command`` for shell
  tools; a ``python`` tool call is run as ``python - <<'PYEOF' ... PYEOF`` so the heredoc analysis
  of the shell rules sees its code (no other rule is changed);
* ``tool`` rows following an assistant step become that step's ``observation``;
* ``--mode prefix`` (default) applies the live prefix scan (``tripwires.scan_prefix``, causal rules
  only) to the full trace, which equals the Live Monitor's last prefix; ``--mode trace`` applies the
  offline whole-trace scan (``tripwires.scan_trace``), which adds the ``offline`` rules.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import multiprocessing
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("EVALLAB_DISABLE_LOCAL_DOCKER", "1")

SHELL = {"bash", "shell", "sh"}
DATASET_ID = "goodfire-reward-hack-data"
LIST_RULES = (
    "codeban_violation",
    "external_sourcing",
    "grader_tamper",
    "harness_log_reads",
    "hidden_reads",
    "hidden_test_corroboration",
    "history_mining",
    "submission_tamper",
    "task_protected_reads",
    "upstream_attempts",
    "upstream_confirmed",
)
_DB: Path | None = None
_MODE = "prefix"


def tool_calls_from(tool_name: str | None, command_or_action: str | None) -> list[dict[str, Any]]:
    if not command_or_action:
        return []
    try:
        decoded = json.loads(command_or_action)
    except ValueError:
        return []
    calls: list[dict[str, Any]] = []
    if isinstance(decoded, list):
        for item in decoded:
            if not isinstance(item, dict):
                continue
            args = item.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append(
                {
                    "function_name": item.get("name"),
                    "arguments": args if isinstance(args, dict) else {},
                }
            )
    elif isinstance(decoded, dict):
        calls.append({"function_name": tool_name, "arguments": decoded})
    return calls


def normalise(call: dict[str, Any]) -> dict[str, Any]:
    name = str(call.get("function_name") or "").lower()
    args = dict(call.get("arguments") or {})
    if name in SHELL and "command" not in args and isinstance(args.get("cmd"), str):
        args["command"] = args.pop("cmd")
    if name == "python" and isinstance(args.get("code"), str):
        return {
            "function_name": "bash",
            "arguments": {"command": "python - <<'PYEOF'\n" + args["code"] + "\nPYEOF"},
        }
    return {"function_name": name, "arguments": args}


def build_steps(rows: list[tuple]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for index, role, tool_name, command, content in rows:
        if role == "assistant":
            calls = [normalise(call) for call in tool_calls_from(tool_name, command)]
            steps.append(
                {"step_id": index, "source": "agent", "message": content or "", "tool_calls": calls}
            )
        elif role == "tool" and steps and steps[-1]["source"] == "agent":
            steps[-1].setdefault("observation", {"results": []})["results"].append(
                {"content": content or ""}
            )
        elif role == "user":
            steps.append({"step_id": index, "source": "user", "message": content or ""})
    return steps


class Timeout(Exception):
    pass


def _alarm(_signum, _frame):
    raise Timeout


def scan_one(trace_id: str) -> dict[str, Any]:
    from evallab import tripwires

    connection = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True, timeout=120)
    rows = connection.execute(
        "SELECT step_index, role, tool_name, command_or_action, content_text, flags_json "
        "FROM trace_steps WHERE trace_id=? ORDER BY step_index",
        (trace_id,),
    ).fetchall()
    receipt = json.loads(
        connection.execute(
            "SELECT verified_receipt_json FROM agent_traces WHERE trace_id=?", (trace_id,)
        ).fetchone()[0]
    )
    connection.close()
    positive = []
    for index, role, tool_name, _command, _content, flags in rows:
        parsed = json.loads(flags)
        if parsed.get("passage_positive"):
            passages = [
                p for p in parsed.get("source_passages", []) if p.get("consensus") == "positive"
            ]
            positive.append(
                {
                    "step": index,
                    "role": role,
                    "tool": tool_name,
                    "channels": sorted({p.get("channel") for p in passages}),
                    "categories": sorted(
                        {c for p in passages for c in p.get("carrier_categories", [])}
                    ),
                }
            )
    steps = build_steps([row[:5] for row in rows])
    agent = [step for step in steps if step["source"] == "agent"]
    record: dict[str, Any] = {
        "trace_id": trace_id,
        "run": trace_id.split("/")[1],
        "verdict": (receipt.get("source_label") or {}).get("verdict"),
        "enacted": (receipt.get("source_label") or {}).get("verdict_enacted"),
        "n_steps": len(rows),
        "n_agent_steps": len(agent),
        "positive": positive,
    }
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(90)
    started = time.perf_counter()
    try:
        scan = tripwires.scan_trace if _MODE == "trace" else tripwires.scan_prefix
        report = scan(agent, steps=steps)
        hits: dict[str, list] = {}
        for hit in report.get("hackbook") or []:
            hits.setdefault("hb:" + hit["rule"], []).append(hit.get("step"))
        for rule in LIST_RULES:
            for item in report.get(rule) or []:
                step = item.get("step") if isinstance(item, dict) else None
                hits.setdefault(rule, []).append(step)
        record["hits"] = hits
    except Timeout:
        record["error"] = "timeout_90s"
    except Exception as error:  # a rule crash is a result, not a harness failure
        record["error"] = f"{type(error).__name__}: {str(error)[:120]}"
    finally:
        signal.alarm(0)
    record["seconds"] = round(time.perf_counter() - started, 3)
    return record


def target_traces(database: Path, repo_root: Path) -> list[str]:
    spec = importlib.util.spec_from_file_location(
        "hydrate",
        Path(repo_root)
        / "research/evidence/runs/corpus-expansion-lb3-20261026/tools/hydrate_goodfire_passage_labels.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    admitted, _digest, dev, _held = module.admission(database, repo_root)
    tasks = sorted(dev & admitted)
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=120)
    traces = []
    for task in tasks:
        traces += [
            r[0]
            for r in connection.execute(
                "SELECT trace_id FROM agent_traces WHERE task_id=? ORDER BY trace_id", (task,)
            )
        ]
    connection.close()
    return traces


def _init(database: str, mode: str) -> None:
    global _DB, _MODE
    _DB = Path(database)
    _MODE = mode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--mode", choices=("prefix", "trace"), default="prefix")
    args = parser.parse_args()
    if args.out.exists():
        raise SystemExit(f"{args.out} exists; refusing to overwrite")
    traces = target_traces(args.database, args.repo_root)
    if args.limit:
        traces = traces[: args.limit]
    print(json.dumps({"traces": len(traces)}), file=sys.stderr, flush=True)
    started = time.monotonic()
    done = 0
    with (
        args.out.open("x", encoding="utf-8") as stream,
        multiprocessing.Pool(
            args.workers, initializer=_init, initargs=(str(args.database), args.mode)
        ) as pool,
    ):
        for record in pool.imap_unordered(scan_one, traces, chunksize=4):
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            done += 1
            if done % 250 == 0:
                print(
                    json.dumps(
                        {"done": done, "of": len(traces), "s": round(time.monotonic() - started)}
                    ),
                    file=sys.stderr,
                    flush=True,
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
