from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from muster import __version__, attribution, render, roster, runner
from muster.commands import add_run_options, attribute, emit, names, now, positive_int
from muster.model import InputError

DESCRIPTION = """Run every roster of a control matrix on one task, then print the attribution report.

A roster gives every seat one policy. `oracle` and `nop` are Harbor's agents. Any other policy
name x is the script x.sh, from <task>/policies or a --policy-dir, run by muster's ScriptAgent.
Each roster is one Harbor job in <out>/<roster>/. The same command on the same --out runs only
the trials that are missing. An interrupted trial stays (CancelledError) until its roster
directory is removed."""

NULL_HELP = 'the policy that stands for "does nothing" in the rules (default: nop)'


def configure(parser: argparse.ArgumentParser) -> None:
    add = parser.add_argument
    add("task", type=Path, help="a seat task directory")
    add("--policies", default="oracle,nop", metavar="NAMES", help="comma list (default: oracle,nop)")
    add(
        "--policy-env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="environment for every oracle and scripted seat, for example WAIT_SEC=5 (repeatable; no secrets)",
    )
    add("--policy-dir", type=Path, action="append", default=[], metavar="DIR", help="more <policy>.sh files")
    add("--exploits", metavar="NAMES", help="comma list of adversary policies, added to [metadata] exploits")
    add("--null", metavar="POLICY", help=NULL_HELP)
    add("--limit", type=positive_int, default=64, metavar="N", help="refuse more rosters (default: 64)")
    add_run_options(parser)


def build(args: argparse.Namespace) -> dict[str, Any]:
    task = roster.read_task(args.task)
    policies, extra = names(args.policies), names(args.exploits)
    rosters = roster.matrix(task.seats, policies, args.limit)
    policy_dirs = [task.path / "policies", *args.policy_dir]
    env = roster.policy_env(args.policy_env)
    plans = {r.roster_key(): roster.participants(r, policy_dirs, env) for r in rosters}
    for policy in [*extra, *([args.null] if args.null else [])]:
        if policy not in policies:
            raise InputError(f"{policy!r} is not one of the policies {', '.join(policies)}")
    out = Path(os.path.abspath(args.out or Path("runs") / f"{task.path.name}-matrix"))
    harbor = None if args.dry_run else runner.find_harbor(args.harbor)
    configs = _job_configs(args, task.path, out, plans)
    seats = [{"seat": s, "under_test": u} for s, u in zip(task.seats, task.under_test, strict=True)]
    roster_entries: list[dict[str, Any]] = [{"roster": key} for key in configs]
    manifest: dict[str, Any] = {
        "muster": __version__,
        "task": {"path": str(task.path), "name": task.name, "checksum": None, "seats": seats}
        | {"oracle_reward": task.oracle_reward},
        "policies": policies,
        "null_policy": args.null or roster.NOP,
        "exploits": [*task.exploits, *(policy for policy in extra if policy not in task.exploits)],
        "expected_findings": list(task.expected_findings),
        "policy_env": env,
        "attempts": args.attempts,
        "concurrency": args.concurrency,
        "agent_timeout_multiplier": args.timeout_multiplier,
        "harbor": str(harbor) if harbor else None,
        "harbor_version": runner.harbor_version(harbor) if harbor else None,
        "dry_run": args.dry_run,
        "rosters": roster_entries,
        "started_at": now(),
        "finished_at": None,
    }
    for key, config in configs.items():
        runner.write_config(config, out / key)
    _write(out, manifest)
    if args.dry_run:
        return {"dry_run": True, "out": str(out), "task": task.name, "rosters": list(configs)} | {
            "trials": len(configs) * args.attempts
        }
    assert harbor is not None
    timeout = roster.job_timeout_sec([task], args.attempts, args.concurrency, args.timeout_multiplier)
    for index, (key, config) in enumerate(configs.items()):
        print(f"muster: roster {index + 1} of {len(configs)}: {key}", file=sys.stderr, flush=True)
        outcome = runner.run_job(config, out / key, harbor, timeout_sec=timeout)
        roster_entries[index].update(
            returncode=outcome.returncode, timed_out=outcome.timed_out, seconds=round(outcome.seconds, 1)
        )
        _write(out, manifest)
    manifest["finished_at"] = now()
    _write(out, manifest)
    try:
        report = attribute.report(out)
    except InputError as error:
        raise InputError(f"{error}; harbor's output is in {out}/<roster>/{runner.LOG_NAME}") from None
    checksums = report["task_checksums"]
    manifest["task"]["checksum"] = checksums[0] if len(checksums) == 1 else None
    _write(out, manifest)
    return report


def _job_configs(
    args: argparse.Namespace, task: Path, out: Path, plans: dict[str, list[dict[str, Any]]]
) -> dict[str, dict[str, Any]]:
    tasks = [task]
    return {
        key: roster.job_config(
            name=key,
            jobs_dir=out / key / "jobs",
            tasks=tasks,
            participants=listed,
            attempts=args.attempts,
            concurrency=args.concurrency,
            timeout_multiplier=args.timeout_multiplier,
        )
        for key, listed in plans.items()
    }


def text(report: dict[str, Any]) -> str:
    if not report.get("dry_run"):
        return attribution.text(report)
    lines = [
        f"dry run: {len(report['rosters'])} rosters and {report['trials']} trials for task {report['task']}",
        f"configs and {attribute.MANIFEST} are in {report['out']}",
        *(f"  {key}" for key in report["rosters"]),
        "Nothing was started.",
    ]
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> int:
    report = build(args)
    emit(report, args.json, text)
    if report.get("dry_run"):
        return 0
    return 1 if report["errors"] or report["harbor_failures"] else 0


def _write(out: Path, manifest: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / attribute.MANIFEST).write_text(render.to_json(manifest), encoding="utf-8")
