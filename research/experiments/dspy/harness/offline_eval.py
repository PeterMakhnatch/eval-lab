"""Offline evaluation of the typed harness modules; no Harbor, no agent runs.

    D=research/experiments/dspy
    PYTHONPATH=$D uv run python -m harness.offline_eval extract     # build recovery fixture from retained ATIF traces
    PYTHONPATH=$D uv run python -m harness.offline_eval recovery    # run ErrorRecovery over the fixture
    PYTHONPATH=$D uv run python -m harness.offline_eval decompose   # run TaskDecomposer over Lab task instructions

What is measured:
- typed validity: every call must parse into the Pydantic schema (DSPy raises otherwise)
- recovery: the module's repair kind versus a heuristic class of what the agent
  actually did next in the retained trace, plus whether the proposed command
  starts with the same program the agent actually used
- decomposition: fraction of target/required paths grounded in the instruction or
  the environment listing (paths not mentioned anywhere are hallucinated)

These are consistency checks against retained behaviour, not proof that the
modules improve an agent. That needs a Harbor comparison through Eval Lab.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import shlex
import subprocess
import time
from collections import Counter
from pathlib import Path

import dspy
from lm import DEFAULT_MODEL, configure

from .plan import TaskDecomposer
from .recover import ErrorRecovery

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
PRIMARY_CHECKOUT = Path("/Users/petermakhnatch/Developer/eval-lab")
FIXTURES = HERE / "fixtures"
ARTIFACTS = HERE / "artifacts"
FAIL = re.compile(
    r"(command not found|No such file|Permission denied|exit code [1-9]|Traceback|SyntaxError|error:|Error:|failed|cannot)",
    re.I,
)
INSPECT_PROGRAMS = {
    "ls",
    "cat",
    "find",
    "which",
    "head",
    "tail",
    "pwd",
    "type",
    "file",
    "stat",
    "grep",
    "sed",
    "wc",
    "echo",
    "env",
    "printenv",
}


def _cmd_from_call(call: dict) -> str | None:
    args = call.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return args[:800]
    if not isinstance(args, dict):
        return None
    for key in ("cmd", "command", "input"):
        value = args.get(key)
        if isinstance(value, str):
            match = re.search(r'\\"cmd\\":\\"(.*?)\\",\\"workdir', value) or re.search(
                r'"cmd":\s*"(.*?)"\s*,\s*"workdir', value
            )
            return bytes(match.group(1), "utf-8").decode("unicode_escape") if match else value
    return json.dumps(args)[:800]


def cmd_extract(_: argparse.Namespace) -> int:
    patterns = [
        str(PRIMARY_CHECKOUT / "runs/*/*/agent/trajectory.json"),
        str(PRIMARY_CHECKOUT / "derived/harbor-traces/**/trajectory.json"),
    ]
    paths = sorted({p for pattern in patterns for p in glob.glob(pattern, recursive=True)})
    steps_out = []
    for path in paths:
        try:
            trajectory = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        steps = trajectory.get("steps", [])
        agent = trajectory.get("agent", {})
        history: list[str] = []
        for index, step in enumerate(steps):
            command = next((_cmd_from_call(c) for c in (step.get("tool_calls") or [])), None)
            observation = step.get("observation")
            texts = []
            if isinstance(observation, dict):
                texts = [
                    r.get("content")
                    for r in observation.get("results", [])
                    if isinstance(r, dict) and isinstance(r.get("content"), str)
                ]
            failed = command is not None and any(FAIL.search(t) for t in texts)
            if failed:
                next_command = next(
                    (
                        _cmd_from_call(s2["tool_calls"][0])
                        for s2 in steps[index + 1 :]
                        if s2.get("tool_calls")
                    ),
                    None,
                )
                steps_out.append(
                    {
                        "trial": "/".join(path.split("/")[-4:-2]),
                        "agent": f"{agent.get('name')} {agent.get('model_name')}",
                        "step_id": step.get("step_id"),
                        "goal": (step.get("message") or "").strip()[:600],
                        "last_action": command,
                        "error_output": "\n".join(texts)[:2500],
                        "recent_history": "\n".join(history[-3:]),
                        "actual_next_command": next_command,
                    }
                )
            if command:
                history.append(command)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    out = FIXTURES / "recovery-steps.json"
    out.write_text(
        json.dumps({"source_trajectories": len(paths), "steps": steps_out}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"{len(steps_out)} failure steps from {len(paths)} trajectories -> {out.relative_to(REPO_ROOT)}"
    )
    return 0


def _program(command: str | None) -> str | None:
    if not command:
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        if "=" in token and not token.startswith("-"):
            continue  # env assignment prefix
        return token
    return None


def _actual_class(last: str, nxt: str | None) -> str:
    if nxt is None:
        return "stop"
    if nxt.strip() == last.strip():
        return "retry_same"
    program_last, program_next = _program(last), _program(nxt)
    if program_next in INSPECT_PROGRAMS and program_last not in INSPECT_PROGRAMS:
        return "inspect"
    if program_last == program_next:
        return "same_program_fixed"
    return "different_program"


KIND_CLASS = {
    "retry_same": "retry_same",
    "fix_command": "same_program_fixed",
    "fix_path": "same_program_fixed",
    "fix_environment": "same_program_fixed",
    "inspect_state": "inspect",
    "change_approach": "different_program",
    "install_or_substitute_tool": "different_program",
    "stop_and_report": "stop",
}


def cmd_recovery(args: argparse.Namespace) -> int:
    lm = configure(args.model)
    fixture = json.loads((FIXTURES / "recovery-steps.json").read_text(encoding="utf-8"))
    module = ErrorRecovery()
    rows = []
    started = time.time()
    for step in fixture["steps"]:
        exit_code = "non-zero" if "Script failed" in step["error_output"] else "unknown"
        t0 = time.time()
        try:
            prediction = module(
                goal=step["goal"] or "(agent message not retained)",
                last_action=step["last_action"],
                exit_code=exit_code,
                error_output=step["error_output"],
                recent_history=step["recent_history"],
            )
            strategy = prediction.strategy
            parsed = True
            kind = strategy.kind
            proposed = strategy.next_command
            diagnosis = strategy.diagnosis
        except Exception as exc:  # noqa: BLE001 - recorded as a typed-validity failure
            parsed, kind, proposed, diagnosis = (
                False,
                None,
                None,
                f"ERROR {type(exc).__name__}: {exc}"[:300],
            )
        actual = _actual_class(step["last_action"], step["actual_next_command"])
        rows.append(
            {
                "trial": step["trial"],
                "step_id": step["step_id"],
                "typed_valid": parsed,
                "kind": kind,
                "kind_class": KIND_CLASS.get(kind) if kind else None,
                "actual_class": actual,
                "class_agrees": (KIND_CLASS.get(kind) == actual) if kind else False,
                "proposed_program": _program(proposed),
                "actual_program": _program(step["actual_next_command"]),
                "program_agrees": (_program(proposed) == _program(step["actual_next_command"]))
                if proposed
                else False,
                "diagnosis": diagnosis,
                "proposed_next_command": (proposed or "")[:400],
                "actual_next_command": (step["actual_next_command"] or "")[:400],
                "error_excerpt": step["error_output"][:200],
                "elapsed_s": round(time.time() - t0, 1),
            }
        )
        print(
            f"{step['trial'].split('/')[-1][:40]:40} step {step['step_id']:>3} kind={kind!s:28} actual={actual:20} class_ok={rows[-1]['class_agrees']} prog_ok={rows[-1]['program_agrees']}"
        )
    n = len(rows)
    summary = {
        "n_steps": n,
        "typed_valid": sum(r["typed_valid"] for r in rows),
        "class_agreement": sum(r["class_agrees"] for r in rows),
        "program_agreement": sum(r["program_agrees"] for r in rows),
        "kind_distribution": Counter(r["kind"] for r in rows),
        "actual_class_distribution": Counter(r["actual_class"] for r in rows),
        "model": args.model,
        "elapsed_s": round(time.time() - started, 1),
        "calls": len(lm.history),
        "litellm_estimated_cost_usd": round(sum(h.get("cost") or 0.0 for h in lm.history), 5),
        "dspy_version": dspy.__version__,
    }
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "recovery-eval.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, indent=2, default=dict) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, default=dict))
    return 0


def _task_inputs() -> list[dict]:
    tasks = []
    for task_dir in sorted(REPO_ROOT.glob("library/tasks/**/task.toml")):
        root = task_dir.parent
        instruction_path = root / "instruction.md"
        env_dir = root / "environment"
        if not instruction_path.exists() or not env_dir.exists():
            continue
        listing = subprocess.run(
            ["find", ".", "-maxdepth", "3", "-not", "-path", "*/.*"],
            cwd=env_dir,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        listing = "\n".join(
            sorted("/app" + line[1:] for line in listing.splitlines() if line != ".")
        )
        tasks.append(
            {
                "task": root.relative_to(REPO_ROOT).as_posix(),
                "instruction": instruction_path.read_text(encoding="utf-8"),
                "listing": listing,
            }
        )
    return tasks


def _grounded(path: str, instruction: str, listing: str) -> bool:
    if path in instruction or path in listing:
        return True
    parent = str(Path(path).parent)
    return parent != "/" and (parent in instruction or parent in listing)


def cmd_decompose(args: argparse.Namespace) -> int:
    lm = configure(args.model)
    module = TaskDecomposer()
    rows = []
    started = time.time()
    for task in _task_inputs():
        t0 = time.time()
        try:
            plan = module(instruction=task["instruction"], environment_listing=task["listing"]).plan
            paths = [p for goal in plan.sub_goals for p in goal.target_files] + list(
                plan.required_outputs
            )
            grounded = [p for p in paths if _grounded(p, task["instruction"], task["listing"])]
            rows.append(
                {
                    "task": task["task"],
                    "typed_valid": True,
                    "sub_goals": len(plan.sub_goals),
                    "paths": len(paths),
                    "grounded_paths": len(grounded),
                    "ungrounded": sorted(set(paths) - set(grounded)),
                    "required_outputs": plan.required_outputs,
                    "assumptions": plan.assumptions,
                    "verification_commands": plan.verification_commands,
                    "plan": plan.model_dump(),
                    "elapsed_s": round(time.time() - t0, 1),
                }
            )
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "task": task["task"],
                    "typed_valid": False,
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }
            )
        r = rows[-1]
        print(
            f"{task['task']:60} valid={r['typed_valid']} goals={r.get('sub_goals')} grounded={r.get('grounded_paths')}/{r.get('paths')} ungrounded={r.get('ungrounded')}"
        )
    summary = {
        "n_tasks": len(rows),
        "typed_valid": sum(r["typed_valid"] for r in rows),
        "paths": sum(r.get("paths", 0) for r in rows),
        "grounded_paths": sum(r.get("grounded_paths", 0) for r in rows),
        "model": args.model,
        "elapsed_s": round(time.time() - started, 1),
        "calls": len(lm.history),
        "litellm_estimated_cost_usd": round(sum(h.get("cost") or 0.0 for h in lm.history), 5),
        "dspy_version": dspy.__version__,
    }
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "decompose-eval.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("extract").set_defaults(func=cmd_extract)
    sub.add_parser("recovery").set_defaults(func=cmd_recovery)
    sub.add_parser("decompose").set_defaults(func=cmd_decompose)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
