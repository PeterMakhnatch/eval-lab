"""Run every current-main L0 rule family (hack_drop.evaluate_l0_stage) on DetBench views.

Usage (from the eval-lab worktree):
  uv run python research/experiments/ov1104-detbench/l0_rules.py --view full [--sets ...] [--counterfactual]

Writes, per view variant V in {full, actions_only} (prefix `l0v_<V>` or `l0cf_<V>` for the
counterfactual tool-shape mapping):
  signals/<prefix>_<reason>.jsonl      one binary signal per L0 reason code seen anywhere
  signals/<prefix>_any_hard.jsonl      1 if any L0_HARD_* reason
  signals/<prefix>_decision.jsonl      drop 1 / review 0.5 / pass 0
  <raw-dir>/<prefix>_raw.jsonl         per run: reasons, decision, unrecognized step count, tool names

The counterfactual maps every tool shape the line cannot read today onto one it can
(Terminus `bash_command{keystrokes}`, `shell{command}`, cloud-export names, read-only tools) before
the rules run. No src change: the mapping lives here only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from detbench_lib import RUNS, SIGNALS, read_jsonl, signal_row, view_to_atif, write_jsonl  # noqa: E402

RAW_DIR = Path("/private/tmp/ov1104/detbench/l0")

SHELL_LIKE = {
    "bash_command": ("keystrokes", "command", "cmd"),
    "shell": ("command", "keystrokes", "cmd"),
    "terminal": ("command", "keystrokes", "cmd"),
    "run_terminal_cmd": ("command", "cmd"),
    "execute_command": ("command", "cmd"),
    "run_shell": ("command", "cmd"),
    "run_shell_command": ("command", "cmd"),
    "run_command": ("command", "cmd"),
    "execute": ("command", "cmd", "code"),
    "computer": ("command", "cmd"),
    "container.exec": ("cmd", "command"),
    "exec_command": ("cmd", "command"),
    "shell_command": ("command", "cmd"),
    "execute_bash": ("command", "cmd"),
    "bash_tool": ("command", "cmd"),
    "terminal_command": ("command", "cmd"),
}
READ_ONLY = {
    "glob",
    "grep",
    "ls",
    "list_dir",
    "list_directory",
    "search",
    "find",
    "web_search",
    "search_files",
    "search_code",
    "list_files",
    "lsp",
    "todo",
    "todowrite",
    "toolsearch",
    "wait",
    "sleep",
    "task",
    "learn",
    "recall",
    "memory",
    "retain",
    "reflect",
    "web_fetch",
    "fetch",
    "browser",
    "image",
    "note",
    "plan",
    "update_plan",
    "report_progress",
    "ask_user",
    "message",
    "chat",
    "respond",
    "final",
    "answer",
    "complete",
    "done",
    "end",
    "stop",
    "mark_task_complete",
    "task_complete",
    "attempt_completion",
    "complete_task",
    "end_task",
    "finish_task",
    "submit_answer",
    "submit_final_answer",
    "final_answer",
    "report",
    "summary",
    "sequentialthinking",
    "sequential_thinking",
    "thinking",
    "reasoning",
    "request_expert_review",
    "close_file",
    "open_file_tab",
}
EDIT_LIKE = {
    "edit_file": "edit",
    "replace": "str_replace",
    "str_replace_editor": "str_replace_editor",
    "write_file": "write",
    "create_file": "create_file",
    "insert": "edit",
    "apply_diff": "edit",
    "patch": "apply_patch",
    "multi_edit": "multiedit",
    "file_edit": "edit",
    "edit_file_contents": "edit",
    "edit_notebook": "edit",
    "file_change": "edit",
}
READ_LIKE = {
    "read_file": "read",
    "cat": "read",
    "view_file": "view",
    "open": "read",
    "open_file": "open_file",
    "view": "view",
    "read": "read",
    "get_file": "read",
    "file_read": "read",
}
CODE_LIKE = {
    "execute_code",
    "execute_notebook_code",
    "run_code",
    "run_python",
    "python_exec",
    "ipython",
    "jupyter",
}


def map_tool_shapes(doc: dict[str, Any]) -> tuple[dict[str, Any], Counter[str]]:
    """Counterfactual: every known tool shape mapped onto one the L0 parser reads."""
    mapped: Counter[str] = Counter()
    for step in doc["steps"]:
        calls = step.get("tool_calls")
        if not calls:
            continue
        new_calls = []
        for call in calls:
            name = str(call.get("function_name") or "")
            args = call.get("arguments")
            low = name.lower()
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                    args = parsed if isinstance(parsed, dict) else {"command": args}
                except ValueError:
                    args = {"command": args}
            if not isinstance(args, dict):
                args = {}
            if low in SHELL_LIKE:
                text = next(
                    (
                        str(args[k])
                        for k in SHELL_LIKE[low]
                        if isinstance(args.get(k), str) and args[k]
                    ),
                    "",
                )
                new_calls.append({"function_name": "bash", "arguments": {"command": text}})
                mapped[f"{low}->bash"] += 1
            elif low in ("bash", "sh", "exec", "python", "python3", "cmd") and not any(
                isinstance(args.get(k), str) and args[k] for k in ("command", "cmd", "code")
            ):
                # a shell tool whose text sits under an unusual key
                text = next((str(v) for v in args.values() if isinstance(v, str)), "")
                new_calls.append({"function_name": "bash", "arguments": {"command": text}})
                mapped[f"{low}(odd-key)->bash"] += 1
            elif low in CODE_LIKE:
                code = next(
                    (
                        str(args[k])
                        for k in ("code", "source", "command", "cmd")
                        if isinstance(args.get(k), str) and args[k]
                    ),
                    "",
                )
                new_calls.append({"function_name": "python", "arguments": {"code": code}})
                mapped[f"{low}->python"] += 1
            elif low in READ_LIKE and low not in ("read", "view", "open_file"):
                path = (
                    args.get("path")
                    or args.get("file_path")
                    or args.get("file")
                    or args.get("filename")
                    or args.get("target_file")
                )
                new_calls.append(
                    {"function_name": READ_LIKE[low], "arguments": {"path": str(path or "")}}
                )
                mapped[f"{low}->{READ_LIKE[low]}"] += 1
            elif low in EDIT_LIKE and low not in ("str_replace_editor", "create_file"):
                path = (
                    args.get("path")
                    or args.get("file_path")
                    or args.get("file")
                    or args.get("filename")
                    or args.get("target_file")
                )
                a = dict(args)
                if path:
                    a["path"] = str(path)
                new_calls.append({"function_name": EDIT_LIKE[low], "arguments": a})
                mapped[f"{low}->{EDIT_LIKE[low]}"] += 1
            elif low in READ_ONLY:
                new_calls.append({"function_name": "think", "arguments": args})
                mapped[f"{low}->think"] += 1
            else:
                new_calls.append(call)
        step["tool_calls"] = new_calls
    return doc, mapped


# --- cf2: the "every known tool shape" upper bound -------------------------------------------------
# cf1 above maps shapes whose semantics are unambiguous. cf2 additionally maps the residual
# high-volume shapes seen in the corpus (wrappers, capitalised variants, benchmark-domain tools) so
# the question "what if L0 never complained about a tool name?" gets a number. Anything still
# unknown after cf2 is mapped to `think` and counted as `unknown->think`, so a cf2 run has zero
# unrecognized steps by construction and any residual flag is genuine content evidence.
CF2_SHELL = {
    "safe_bash",
    "run_command",
    "run_tests",
    "execute_mock_command",
    "safe_shell",
    "run_bash",
    "execute_bash_command",
    "shell_exec",
    "run_terminal",
    "terminal_exec",
}
CF2_CODE = {"safe_python", "run_python", "execute_python", "python_exec", "run_code"}
CF2_EDIT = {"text_editor", "edit_text", "apply_edit", "patch_file", "str_replace_editor"}
CF2_READ = {"read_file_content", "view_text"}
CF2_TERMINAL = {"submit", "deliver", "finish", "close", "done"}


def map_tool_shapes_v2(doc: dict[str, Any]) -> tuple[dict[str, Any], Counter[str]]:
    """cf2: cf1 first, then every residual known shape, then unknown -> think."""
    doc, mapped = map_tool_shapes(doc)
    for step in doc["steps"]:
        calls = step.get("tool_calls")
        if not calls:
            continue
        new_calls = []
        for call in calls:
            raw_name = str(call.get("function_name") or "")
            # case-fold so `Bash`/`Edit`/`Read` hit the same branch as their lower-case forms
            name = raw_name.lower()
            args = call.get("arguments")
            if not isinstance(args, dict):
                args = {}
            if name in CF2_SHELL:
                text = next((str(v) for v in args.values() if isinstance(v, str)), "")
                new_calls.append({"function_name": "bash", "arguments": {"command": text}})
                mapped[f"{raw_name}->bash"] += 1
            elif name in CF2_CODE:
                code = next(
                    (
                        str(args[k])
                        for k in ("code", "source", "command", "script")
                        if isinstance(args.get(k), str)
                    ),
                    "",
                )
                new_calls.append({"function_name": "python", "arguments": {"code": code}})
                mapped[f"{raw_name}->python"] += 1
            elif name in CF2_EDIT:
                path = (
                    args.get("path")
                    or args.get("file_path")
                    or args.get("filename")
                    or args.get("file")
                )
                a = dict(args)
                if path:
                    a["path"] = str(path)
                new_calls.append({"function_name": "edit", "arguments": a})
                mapped[f"{raw_name}->edit"] += 1
            elif name in CF2_READ:
                path = (
                    args.get("path")
                    or args.get("file_path")
                    or args.get("filename")
                    or args.get("file")
                )
                new_calls.append({"function_name": "read", "arguments": {"path": str(path or "")}})
                mapped[f"{raw_name}->read"] += 1
            elif name in CF2_TERMINAL:
                new_calls.append({"function_name": "think", "arguments": args})
                mapped[f"{raw_name}->think"] += 1
            elif name in ("bash", "read", "edit", "write", "think", "python", "grep", "glob"):
                # canonical tool, but the source may spell it `Bash`/`Read`; the L0 parser keys on the
                # lower-case name, so emit the canonical form and keep the arguments untouched.
                if raw_name != name:
                    new_calls.append({"function_name": name, "arguments": args})
                    mapped[f"{raw_name}->{name}"] += 1
                else:
                    new_calls.append(call)
            else:
                new_calls.append({"function_name": "think", "arguments": args})
                mapped[f"unknown({raw_name})->think"] += 1
        step["tool_calls"] = new_calls
    return doc, mapped


def _tool_names(doc: dict[str, Any]) -> Counter[str]:
    c: Counter[str] = Counter()
    for step in doc["steps"]:
        for call in step.get("tool_calls") or []:
            c[str(call.get("function_name") or "")] += 1
    return c


class _L0Timeout(BaseException):
    """Per-run watchdog for exponential regex backtracking in hack_drop."""


def _raise_l0_timeout(_signum: int, _frame: Any) -> None:
    raise _L0Timeout()


def evaluate_one(args: tuple[str, str, str]) -> dict[str, Any]:
    run_key, view_path, mode = args
    from evallab import hack_drop as hd
    from evallab.trace_anatomy import LoadedRun
    from evallab.tripwires import extract_content_files

    view = json.loads(Path(view_path).read_text(encoding="utf-8"))
    doc = view_to_atif(view)
    names = _tool_names(doc)
    mapped: Counter[str] = Counter()
    if mode == "cf1":
        doc, mapped = map_tool_shapes(doc)
    elif mode == "cf2":
        doc, mapped = map_tool_shapes_v2(doc)
    # an empty view (no steps, no diff) is what the line sees as "no trajectory": L0_NO_EVIDENCE
    run = LoadedRun(run_id=run_key, loader="detbench-view", doc=doc) if doc["steps"] else None
    steps = [dict(s) for s in doc["steps"]]
    old_handler = signal.signal(signal.SIGALRM, _raise_l0_timeout)
    signal.setitimer(signal.ITIMER_REAL, 2.0)
    try:
        try:
            files = extract_content_files(steps)
        except Exception:  # noqa: BLE001 - content extraction is best effort, as in the line
            files = []
        item = hd.RolloutItem(
            item_id=run_key,
            task_id="unknown",
            run=run,
            files=tuple(files),
            diff_text=view["final"].get("diff"),
        )
        verdict = hd.evaluate_l0_stage(item)
        reasons = list(verdict.reasons)
        decision = verdict.decision
        unrec = len(verdict.details.get("unrecognized_tool_steps") or [])
        evidence = list(verdict.evidence_steps)
        error = None
    except (Exception, _L0Timeout) as exc:  # noqa: BLE001 - record, never drop the row
        reasons, decision, unrec, evidence, error = (
            ["L0_RUNNER_ERROR"],
            "review",
            0,
            [],
            type(exc).__name__,
        )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, old_handler)
    return {
        "run_key": run_key,
        "decision": decision,
        "reasons": reasons,
        "evidence_steps": evidence,
        "unrecognized_steps": unrec,
        "n_tool_steps": sum(1 for s in steps if s.get("tool_calls")),
        "tool_names": dict(names),
        "mapped": dict(mapped),
        "error": error,
    }


def reason_key(reason: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in reason.lower()).strip("_")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--runs", type=Path, default=RUNS)
    ap.add_argument("--view", choices=("full", "actions_only"), default="full")
    ap.add_argument("--sets", nargs="*")
    ap.add_argument(
        "--counterfactual",
        action="store_true",
        help="cf1: map known tool shapes to their true equivalent",
    )
    ap.add_argument(
        "--counterfactual2",
        action="store_true",
        help="cf2: cf1 plus every residual shape (upper bound, zero unrecognized by construction)",
    )
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--signals-dir", type=Path, default=SIGNALS)
    ap.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    args = ap.parse_args(argv)

    rows = read_jsonl(args.runs)
    if args.sets:
        rows = [r for r in rows if r["set"] in set(args.sets)]
    key = "view" if args.view == "full" else "view_actions_only"
    if args.counterfactual and args.counterfactual2:
        raise SystemExit("choose --counterfactual or --counterfactual2, not both")
    mode = "cf2" if args.counterfactual2 else ("cf1" if args.counterfactual else "now")
    jobs = [(r["run_key"], r[key], mode) for r in rows]
    if args.workers <= 1:
        results = [evaluate_one(j) for j in jobs]
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(evaluate_one, jobs, chunksize=8))
    prefix = f"{ {'now': 'l0v', 'cf1': 'l0cf', 'cf2': 'l0cf2'}[mode] }_{args.view}"
    version = f"{prefix}@{hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]}"
    args.raw_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.raw_dir / f"{prefix}_raw.jsonl", results)

    all_reasons = sorted({r for res in results for r in res["reasons"]})
    args.signals_dir.mkdir(parents=True, exist_ok=True)
    for reason in all_reasons:
        name = f"{prefix}_{reason_key(reason)}"
        write_jsonl(
            args.signals_dir / f"{name}.jsonl",
            (
                signal_row(
                    res["run_key"],
                    name,
                    version,
                    1.0 if reason in res["reasons"] else 0.0,
                    evidence_steps=res["evidence_steps"] if reason in res["reasons"] else [],
                )
                for res in results
            ),
        )
    write_jsonl(
        args.signals_dir / f"{prefix}_any_hard.jsonl",
        (
            signal_row(
                res["run_key"],
                f"{prefix}_any_hard",
                version,
                1.0 if any(x.startswith("L0_HARD_") for x in res["reasons"]) else 0.0,
                evidence_steps=res["evidence_steps"],
            )
            for res in results
        ),
    )
    write_jsonl(
        args.signals_dir / f"{prefix}_any_rule.jsonl",
        (
            signal_row(
                res["run_key"],
                f"{prefix}_any_rule",
                version,
                1.0
                if any(
                    x.startswith("L0_")
                    and x
                    not in (
                        "L0_UNRECOGNIZED_TOOL_SHAPE",
                        "L0_PARSE_ERROR",
                        "L0_NO_EVIDENCE",
                        "L0_RUNNER_ERROR",
                    )
                    for x in res["reasons"]
                )
                else 0.0,
                evidence_steps=res["evidence_steps"],
            )
            for res in results
        ),
    )
    write_jsonl(
        args.signals_dir / f"{prefix}_decision.jsonl",
        (
            signal_row(
                res["run_key"],
                f"{prefix}_decision",
                version,
                {"drop": 1.0, "review": 0.5, "pass": 0.0}[res["decision"]],
            )
            for res in results
        ),
    )
    summary = {
        "runs": len(results),
        "version": version,
        "reasons": {r: sum(1 for res in results if r in res["reasons"]) for r in all_reasons},
        "decisions": dict(Counter(res["decision"] for res in results)),
        "errors": dict(Counter(res["error"] for res in results if res["error"])),
        "mapped": dict(sum((Counter(res["mapped"]) for res in results), Counter())),
        "tool_names_top": Counter(
            sum((Counter(res["tool_names"]) for res in results), Counter())
        ).most_common(40),
    }
    (args.raw_dir / f"{prefix}_summary.json").write_text(
        json.dumps(summary, indent=1), encoding="utf-8"
    )
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
