"""Run ordinary native coding agents on owned snapshots, with external behavioral grades.

This is an exploratory local patch probe, not a hermetic Harbor evaluation.
Only explicit --run-subscription executes the included ChatGPT subscription.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import sys
from pathlib import Path

from run_pairs import (
    CODEX_EXECUTABLE,
    EFFORT,
    MODEL,
    _argv,
    _interrupt,
    _logged_process,
    _private_json,
    _scrub_env,
    file_sha256,
    read_events,
    strict_json,
    utc_now,
    weekly_quota,
)

SCRATCH = Path("/tmp/agent-efficiency/code-cases").resolve()


def schedule(manifest: dict) -> list[dict]:
    cases = {(c["task"], c["repeat"], c["arm"]): c for c in manifest["cases"]}
    if len(cases) != len(manifest["cases"]):
        raise ValueError("duplicate coding cases")
    result = []
    for repeat in range(1, manifest["repeats"] + 1):
        for index, task in enumerate(sorted(manifest["tasks"])):
            arms = ("baseline", "candidate") if (repeat + index) % 2 else ("candidate", "baseline")
            result.extend(cases[(task, repeat, arm)] for arm in arms)
    if len(result) != len(cases):
        raise ValueError("incomplete or extra coding cases")
    return result


def validate(manifest: dict, path: Path, cases: list[dict]) -> None:
    if manifest["schema"] != "agent-efficiency-native-code-cases/v1":
        raise ValueError("unsupported coding manifest")
    if not path.resolve().is_relative_to(SCRATCH):
        raise ValueError("coding cases must be owned scratch snapshots")
    inventory_path = Path(manifest["baseline_files"])
    if file_sha256(inventory_path) != manifest["baseline_files_sha256"]:
        raise ValueError("baseline inventory changed")
    inventory = {row["path"]: row for row in strict_json(inventory_path.read_text())}
    for name, digest in manifest["tooling_sha256"].items():
        if file_sha256(path.parent / "control/tooling" / name) != digest:
            raise ValueError("frozen grading/preparation tooling changed")
    for case in cases:
        workspace = Path(case["workspace"]).resolve()
        if not workspace.is_relative_to(path.parent / "cases") or workspace.is_symlink():
            raise ValueError("workspace escapes prepared coding cases")
        if file_sha256(Path(case["prompt_file"])) != case["prompt_sha256"]:
            raise ValueError("coding prompt changed")
        for relative in case["allowed_paths"]:
            source = (workspace / relative).resolve()
            if (
                not source.is_relative_to(workspace)
                or file_sha256(source) != inventory[relative]["sha256"]
            ):
                raise ValueError("solver source is not the fresh pinned baseline")


def totals(rows: list[dict]) -> dict:
    arms = {}
    for arm in ("baseline", "candidate"):
        selected = [row for row in rows if row["arm"] == arm]
        counters = {}
        for name in ("input_tokens", "output_tokens", "cached_input_tokens"):
            values = [row.get("events", {}).get("tokens", {}).get(name) for row in selected]
            known = [value for value in values if type(value) is int]
            counters[name] = {
                "total": sum(known) if len(known) == len(values) else None,
                "reported_subtotal": sum(known) if known else None,
                "unknown_attempts": len(values) - len(known),
            }
        arms[arm] = {
            "scheduled_attempts": len(selected),
            "attempted": sum(row["status"] != "not_attempted" for row in selected),
            "behavioral_passes": sum(row.get("accepted", False) for row in selected),
            "tokens": counters,
        }
    return arms


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-subscription", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--hard-max-calls", type=int, default=12)
    for arm in ("baseline", "candidate"):
        parser.add_argument(f"--{arm}-effort", choices=("medium", "high"), default=EFFORT)
    args = parser.parse_args()
    efforts = {arm: getattr(args, f"{arm}_effort") for arm in ("baseline", "candidate")}
    if not 1 <= args.timeout_seconds <= 3600 or not 1 <= args.hard_max_calls <= 64:
        parser.error("timeout must be 1..3600 seconds and hard max calls 1..64")
    path = args.manifest.resolve()
    manifest = strict_json(path.read_text())
    cases = schedule(manifest)
    if len(cases) > args.hard_max_calls:
        parser.error("complete planned cases exceed hard max calls")
    validate(manifest, path, cases)
    output = args.output.resolve()
    if not output.is_relative_to(path.parent) or any(
        output.is_relative_to(Path(c["workspace"])) for c in cases
    ):
        parser.error(
            "controller output must be owned case-root artifacts, outside solver workspaces"
        )
    plan = {
        "schema": "agent-efficiency-native-code-run/v2",
        "exploratory": True,
        "manifest_sha256": file_sha256(path),
        "launcher_sha256": file_sha256(Path(__file__)),
        "model": MODEL,
        "effort_by_arm": efforts,
        "planned_calls": len(cases),
        "native_executable": str(CODEX_EXECUTABLE),
        "native_executable_sha256": file_sha256(CODEX_EXECUTABLE),
        "controller_python": sys.version,
        "max_calls_unit": "native_exec_invocations_not_physical_upstream_requests",
        "counterbalance": "alternating first arm by original task and nested repeat",
        "context_treatment": manifest["only_experimental_change"],
        "coding_grader": "external frozen public-contract behavioral checks plus source-only scope guard",
        "limitations": manifest["caveats"],
        "schedule": [
            {k: case[k] for k in ("case_id", "task", "arm", "repeat", "prompt_sha256")}
            for case in cases
        ],
    }
    if not args.run_subscription:
        print(json.dumps({**plan, "mode": "dry-run", "model_calls": 0}, indent=2))
        return 0
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(output, 0o700)
    _private_json(output / "plan.json", plan)
    env = _scrub_env(dict(os.environ))
    rows = [
        {
            "case_id": c["case_id"],
            "task": c["task"],
            "arm": c["arm"],
            "repeat": c["repeat"],
            "requested_effort": efforts[c["arm"]],
            "status": "not_attempted",
            "accepted": False,
        }
        for c in cases
    ]
    previous = signal.signal(signal.SIGTERM, _interrupt)
    stop = None
    try:
        preflight = _logged_process(
            [str(CODEX_EXECUTABLE), "login", "status"],
            output / "authentication",
            cwd=output,
            env=env,
            timeout=30,
        )
        login = (output / "authentication/stdout.jsonl").read_text() + (
            output / "authentication/stderr.log"
        ).read_text()
        if preflight.get("returncode") != 0 or "logged in using chatgpt" not in login.casefold():
            raise ValueError(
                "only authenticated included ChatGPT subscription execution is permitted"
            )
        for index, (case, row) in enumerate(zip(cases, rows, strict=True)):
            quota = _logged_process(
                ["omp", "usage", "--provider", "openai-codex", "--json", "--redact"],
                output / f"quota-{index:03d}",
                cwd=output,
                env=dict(os.environ),
                timeout=45,
            )
            if quota.get("returncode") != 0:
                raise ValueError("quota unavailable; fail closed")
            row["quota"] = weekly_quota(
                strict_json((output / f"quota-{index:03d}/stdout.jsonl").read_text())
            )
            if file_sha256(path) != plan["manifest_sha256"]:
                raise ValueError("coding manifest changed")
            workspace = Path(case["workspace"])
            artifacts = Path(case["output_dir"])
            argv = _argv(workspace, artifacts / "last-message.txt", effort=efforts[case["arm"]])
            sandbox_index = argv.index("--sandbox") + 1
            if argv[sandbox_index] != "read-only":
                raise ValueError("unexpected native transport sandbox contract")
            argv[sandbox_index] = "workspace-write"
            row["status"] = "running"
            row["started_at_utc"] = utc_now()
            row["process"] = _logged_process(
                argv,
                artifacts / "native-cli",
                cwd=workspace,
                env=env,
                timeout=args.timeout_seconds,
                prompt=Path(case["prompt_file"]),
            )
            row["events"] = read_events(artifacts / "native-cli/stdout.jsonl")
            if row["process"]["status"] == "cleanup_failed":
                raise RuntimeError("native process group could not be cleaned up")
            if row["process"]["status"] == "interrupted":
                raise KeyboardInterrupt
            if row["events"]["model_reroute_reported"]:
                raise RuntimeError("native model substitution reported; do not continue")
            grade = _logged_process(
                shlex.split(case["grading_command"]),
                artifacts / "external-grader",
                cwd=workspace,
                env=dict(os.environ),
                timeout=900,
            )
            row["grader_process"] = grade
            grade_file = artifacts / "grade.json"
            row["grade"] = strict_json(grade_file.read_text()) if grade_file.is_file() else None
            completed = (
                row["process"].get("returncode") == 0
                and row["process"]["status"] == "exited"
                and row["events"]["turn_completions"] == 1
                and not row["events"]["turn_failed"]
                and not row["events"]["invalid_event_lines"]
            )
            row["accepted"] = bool(
                completed
                and grade.get("returncode") == 0
                and isinstance(row["grade"], dict)
                and row["grade"].get("passed") is True
            )
            row["status"] = "completed" if completed else "failed_execution"
            row["finished_at_utc"] = utc_now()
            _private_json(output / f"attempt-{index:03d}.json", row)
    except (Exception, KeyboardInterrupt) as exc:
        stop = f"{type(exc).__name__}: {exc}"
    finally:
        signal.signal(signal.SIGTERM, previous)
        for row in rows:
            if row["status"] == "running":
                row["status"] = "interrupted"
        summary = {
            "plan": plan,
            "stop_reason": stop,
            "monetary_cost_usd": None,
            "arms": totals(rows),
            "attempts": rows,
        }
        _private_json(output / "summary.json", summary)
    print(
        json.dumps({"output": str(output), "stop_reason": stop, "arms": summary["arms"]}, indent=2)
    )
    return 1 if stop else 0


if __name__ == "__main__":
    raise SystemExit(main())
