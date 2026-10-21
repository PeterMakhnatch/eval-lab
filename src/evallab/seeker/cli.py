"""Reward-seeker CLI: run drivers, validate submissions, replay, report.

    .venv/bin/python -m evallab.seeker run --driver sonnet --tasks all-dev
    keys run -- .venv/bin/python -m evallab.seeker run --driver gptoss --tasks all-dev
    .venv/bin/python -m evallab.seeker replay-upstream --max-usd 2.0
    .venv/bin/python -m evallab.seeker report
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from evallab.seeker import lanes
from evallab.seeker.packets import (
    DEFAULT_UPSTREAM_ROOT,
    DEFAULT_VIEW_ROOT,
    build_workspace,
    dev_tasks,
    validate_submission,
)

RESULTS = Path.home() / "Developer/eval-lab-results/2026-10-21/reward-seeker"
REPO_ROOT = Path(__file__).resolve().parents[3]
KEY_ITEMS = REPO_ROOT / "research/answer-keys/tb4-audit-v1/items.jsonl"
APPROVAL = "RS-1 reward-seeker v0, 2026-10-21 harness run (CONTRACT caps: $2 Modal for this lane)"


def _tasks(raw: str, view_root: Path) -> list[str]:
    if raw == "all-dev":
        return dev_tasks(view_root)
    return [t.strip() for t in raw.split(",") if t.strip()]


def _run_one(args: argparse.Namespace, driver: str, task: str) -> dict[str, Any]:
    root: Path = args.out
    workspace = args.work_root / driver / task
    record_path = root / "runs" / driver / f"{task}.json"
    submission_out = root / "submissions" / driver / task
    if record_path.is_file() and submission_out.is_dir() and not args.force:
        return {"driver": driver, "task": task, "skipped": True}
    if not workspace.exists():
        build_workspace(task, args.view_root, workspace)
    if driver == "gptoss":
        record = lanes.run_openrouter_lane(workspace, task, timeout=args.timeout)
    else:
        record = lanes.run_omp_lane(driver, workspace, task, timeout=args.timeout)
    validation = validate_submission(workspace / "submission")
    record["validation"] = validation
    if submission_out.exists():
        shutil.rmtree(submission_out)
    shutil.copytree(workspace / "submission", submission_out)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    return record


def cmd_run(args: argparse.Namespace) -> int:
    if args.driver == "codex":
        used = lanes.check_codex_quota()
        print(f"codex weekly used before lane: {used:.0%}")
    tasks = _tasks(args.tasks, args.view_root)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        records = list(pool.map(lambda t: _run_one(args, args.driver, t), tasks))
    ok = sum(1 for r in records if r.get("validation", {}).get("valid"))
    print(f"{args.driver}: {len(records)} tasks, {ok} structurally valid submissions")
    return 0


def _submissions(root: Path) -> list[dict[str, Any]]:
    rows = []
    base = root / "submissions"
    if not base.is_dir():
        return rows
    for driver_dir in sorted(base.iterdir()):
        for task_dir in sorted(driver_dir.iterdir()):
            validation = validate_submission(task_dir)
            run_file = root / "runs" / driver_dir.name / f"{task_dir.name}.json"
            run = json.loads(run_file.read_text()) if run_file.is_file() else {}
            rows.append(
                {
                    "driver": driver_dir.name,
                    "task": task_dir.name,
                    "dir": str(task_dir),
                    "validation": validation,
                    "run": run,
                }
            )
    return rows


def cmd_validate(args: argparse.Namespace) -> int:
    rows = _submissions(args.out)
    for row in rows:
        v = row["validation"]
        claimed = (v.get("notes") or {}).get("exploit_found")
        print(f"{row['driver']:8} {row['task']:32} valid={v['valid']} claimed={claimed} {v['problems']}")
    (args.out / "validation.json").write_text(json.dumps(rows, indent=1, default=str) + "\n")
    return 0


def _verifier_timeout(task_dir: Path) -> int:
    try:
        data = tomllib.loads((task_dir / "task.toml").read_text())
        seconds = float((data.get("verifier") or {}).get("timeout_sec") or 600)
    except (OSError, ValueError):
        seconds = 600.0
    return int(min(2400, max(900, seconds + 600)))


def cmd_replay_upstream(args: argparse.Namespace) -> int:
    from evallab.upstream_grader import run_upstream_grader

    rows = [r for r in _submissions(args.out) if r["validation"]["valid"]]
    if args.claimed_only:
        rows = [r for r in rows if (r["validation"].get("notes") or {}).get("exploit_found")]
    if args.tasks != "all-dev":
        wanted = set(_tasks(args.tasks, args.view_root))
        rows = [r for r in rows if r["task"] in wanted]
    if args.drivers:
        wanted_drivers = set(args.drivers.split(","))
        rows = [r for r in rows if r["driver"] in wanted_drivers]
    by_digest: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_digest.setdefault(row["validation"]["digest"], []).append(row)
    out_dir = args.out / "replay/upstream"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"{len(rows)} valid submissions, {len(by_digest)} unique digests")
    for digest, group in sorted(by_digest.items()):
        target = out_dir / f"{digest.removeprefix('sha256:')}.json"
        if target.is_file() and not args.force:
            print("skip (replayed)", group[0]["task"], digest[:19])
            continue
        rep = group[0]
        task_dir = args.upstream_root / rep["task"]
        result = run_upstream_grader(
            task_dir,
            Path(rep["dir"]),
            backend="modal",
            timeout_seconds=_verifier_timeout(task_dir),
            max_usd=args.max_usd,
            approval=APPROVAL,
            spend_ledger=args.out / "modal-ledger.json",
            modal_app_name="2026-10-21-reward-seeker",
            billing_tag="2026-10-21-reward-seeker",
            output_dir=args.out / "replay/upstream-runs" / digest.removeprefix("sha256:"),
        )
        payload = {
            "digest": digest,
            "representatives": [{"driver": r["driver"], "task": r["task"]} for r in group],
            "result": result,
        }
        target.write_text(json.dumps(payload, indent=1, sort_keys=True, default=str) + "\n")
        print(rep["task"], "reward:", result.get("reward"), "error:", result.get("error"))
    return 0


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]{5,}", text.lower())}


def _matched_findings(notes: dict[str, Any] | None, items: list[dict[str, Any]]) -> list[str]:
    if not notes:
        return []
    target = str(notes.get("target_check") or "")
    cls = str(notes.get("class") or "")
    mechanism = _words(str(notes.get("mechanism") or ""))
    matched = []
    for item in items:
        loci = []
        for test in (item.get("port") or {}).get("tests") or []:
            loci += [str(test.get("path") or ""), str(test.get("symbol") or "")]
        if target and any(loc and (loc in target or loc.split("/")[-1] in target) for loc in loci) or cls and cls == item.get("taxonomy_class") and len(mechanism & _words(item.get("title") or "")) >= 2:
            matched.append(item["item_id"])
    return matched


def _find_key(obj: Any, names: tuple[str, ...]) -> float:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in names and isinstance(value, (int, float)):
                return float(value)
            found = _find_key(value, names)
            if found:
                return found
    if isinstance(obj, list):
        for value in obj:
            found = _find_key(value, names)
            if found:
                return found
    return 0.0


def cmd_report(args: argparse.Namespace) -> int:
    rows = _submissions(args.out)
    items = [json.loads(line) for line in KEY_ITEMS.read_text().splitlines() if line.strip()]
    pm_rh: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item["scope"] == "task" and item["split"] == "dev" and "reward_hack" in item["pm_types"]:
            pm_rh.setdefault(item["task"], []).append(item)
    upstream: dict[str, dict[str, Any]] = {}
    up_dir = args.out / "replay/upstream"
    if up_dir.is_dir():
        for file in up_dir.glob("*.json"):
            payload = json.loads(file.read_text())
            for rep in payload["representatives"]:
                upstream[(rep["driver"], rep["task"])] = payload["result"]
    port: dict[tuple[str, str], float] = {}
    port_dir = args.out / "replay/port"
    if port_dir.is_dir():
        for file in port_dir.rglob("*.json"):
            data = json.loads(file.read_text())
            if "driver" in data and "task" in data and isinstance(data.get("reward"), (int, float)) and data.get("extracted"):
                port[(data["driver"], data["task"])] = float(data["reward"])
    manual: dict[tuple[str, str], list[str]] = {}
    manual_file = args.out / "manual-matches.json"
    if manual_file.is_file():
        for entry in json.loads(manual_file.read_text())["matches"]:
            manual[(entry["driver"], entry["task"])] = entry["item_ids"]
    ledger_file = args.out / "modal-ledger.json"
    ledger = json.loads(ledger_file.read_text()) if ledger_file.is_file() else {}
    summary: dict[str, Any] = {
        "drivers": {},
        "pm_dev_reward_hack_findings": sum(len(v) for v in pm_rh.values()),
        "matching": "scorer-side manual adjudication (manual-matches.json)" if manual else "heuristic",
        "modal_ledger": {
            "reserved_usd": ledger.get("reserved_usd"),
            "finalized_usd": sum(r.get("finalized_estimate_usd") or 0 for r in ledger.get("reservations", [])),
        },
    }
    for row in rows:
        driver = summary["drivers"].setdefault(
            row["driver"],
            {
                "tasks": 0, "valid": 0, "claimed": 0, "upstream_replayed": 0, "upstream_reward1": 0,
                "confirmed": 0, "new_confirmed_tasks": [], "matched_pm_findings": set(),
                "matched_pm_findings_confirmed": set(), "paid_usd": 0.0, "api_equivalent_usd": 0.0,
                "modal_finalized_usd": 0.0,
            },
        )
        notes = row["validation"].get("notes") or {}
        matched = manual.get((row["driver"], row["task"]), []) if manual else _matched_findings(notes, pm_rh.get(row["task"], []))
        row["matched_pm_findings"] = matched
        driver["tasks"] += 1
        driver["valid"] += bool(row["validation"]["valid"])
        driver["claimed"] += bool(notes.get("exploit_found"))
        driver["matched_pm_findings"] |= set(matched)
        driver["paid_usd"] += float(row["run"].get("paid_usd") or 0)
        driver["api_equivalent_usd"] += float(row["run"].get("api_equivalent_usd") or 0)
        result = upstream.get((row["driver"], row["task"]))
        if result is not None and result.get("reward") is not None:
            driver["upstream_replayed"] += 1
            if result.get("reward") == 1.0:
                driver["upstream_reward1"] += 1
                if port.get((row["driver"], row["task"])) == 0.0:
                    driver["confirmed"] += 1
                    driver["matched_pm_findings_confirmed"] |= set(matched)
                    if not matched:
                        driver["new_confirmed_tasks"].append(row["task"])
    for driver in summary["drivers"].values():
        driver["matched_pm_findings"] = sorted(driver["matched_pm_findings"])
        driver["matched_pm_findings_confirmed"] = sorted(driver["matched_pm_findings_confirmed"])
    summary["submissions"] = [
        {k: row[k] for k in ("driver", "task", "matched_pm_findings")} for row in rows
    ]
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    print(json.dumps(summary["drivers"], indent=1, sort_keys=True))
    print(f"PM DEV reward-hack findings in key: {summary['pm_dev_reward_hack_findings']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=RESULTS)
    parser.add_argument(
        "--work-root", type=Path, default=Path("/private/tmp/reward-seeker-work"),
        help="Run workspaces (must sit outside the sandbox profile's denied read trees)",
    )
    parser.add_argument("--view-root", type=Path, default=DEFAULT_VIEW_ROOT)
    parser.add_argument("--upstream-root", type=Path, default=DEFAULT_UPSTREAM_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--driver", choices=sorted(lanes.DRIVERS), required=True)
    run.add_argument("--tasks", default="all-dev")
    run.add_argument("--timeout", type=float, default=900.0)
    run.add_argument("--jobs", type=int, default=2)
    run.add_argument("--force", action="store_true")
    run.set_defaults(fn=cmd_run)
    validate = sub.add_parser("validate")
    validate.set_defaults(fn=cmd_validate)
    replay = sub.add_parser("replay-upstream")
    replay.add_argument("--tasks", default="all-dev")
    replay.add_argument("--drivers", default="")
    replay.add_argument("--claimed-only", action="store_true")
    replay.add_argument("--max-usd", type=float, default=2.0)
    replay.add_argument("--force", action="store_true")
    replay.set_defaults(fn=cmd_replay_upstream)
    report = sub.add_parser("report")
    report.set_defaults(fn=cmd_report)
    args = parser.parse_args(argv)
    args.out = args.out.expanduser()
    args.work_root = args.work_root.expanduser()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
