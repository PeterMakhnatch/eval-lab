"""Stratified, local coverage-guided hunts over a digest-bound MiMo manifest.

The entire known-fix cohort is inventoried, including unsupported languages and
missing inputs. Only positively traced survivors enter the repair ranking. This
is a sample of verifier sensitivity, not a fleet-wide weak-test prevalence rate.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from evallab.registry import compute_task_digests
from evallab.storage.paths import shared_checkout_root
from evallab.task_admission import _patch_container_targets, _task_workdir
from evallab.verifier_mutation import MutationReport, run_mutation_audit

SCHEMA = "evallab.verifier_hunt/v1"
DEFAULT_SEEDS = ("format-code-task-002402", "format-code-task-002391")
DEFAULT_MANIFEST = Path("research/experiments/mimo-clean-v5/manifest.csv")


@dataclass(frozen=True)
class HuntTask:
    task_id: str
    package: str
    package_digest: str
    reference_sha256: str
    language: str
    targets: tuple[str, ...]
    stratum: str
    disposition: str


def _source_target(path: str) -> bool:
    member = PurePosixPath(path)
    return (
        member.suffix == ".py"
        and not {"test", "tests", "testing", "docs", "doc", "examples"}.intersection(member.parts)
        and not member.name.startswith("test_")
        and not member.name.endswith("_test.py")
        and member.name not in {"conftest.py", "setup.py"}
    )


def inventory(manifest: Path, package_root: Path) -> list[HuntTask]:
    """All known-fix rows, with explicit reasons for every ineligible member."""
    with manifest.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    tasks: list[HuntTask] = []
    seen: set[str] = set()
    for row in rows:
        if not row.get("reference_fix") or row["reference_fix"] == "none":
            continue
        task_id = row["task_id"]
        if task_id in seen:
            raise ValueError(f"duplicate manifest task: {task_id}")
        seen.add(task_id)
        package = package_root / row["package_path"]
        targets: tuple[str, ...] = ()
        stratum, reference_sha = "unavailable", ""
        if row["status"] != "built":
            disposition = "not-built"
        elif row["language"] != "python":
            disposition = "unsupported-coverage-language"
        elif not (package / "solution/solve.sh").is_file():
            disposition = "package-or-reference-unavailable"
        elif not Path(row["reference_fix"]).is_file():
            disposition = "reference-patch-unavailable"
        else:
            patch = Path(row["reference_fix"]).read_bytes()
            reference_sha = hashlib.sha256(patch).hexdigest()
            targets = tuple(
                target
                for target in _patch_container_targets(patch, _task_workdir(package))
                if _source_target(target)
            )
            changes = sum(
                line.startswith((b"+", b"-")) and not line.startswith((b"+++", b"---"))
                for line in patch.splitlines()
            )
            size = "small" if changes <= 20 else "medium" if changes <= 100 else "large"
            stratum = f"{'single' if len(targets) == 1 else 'multi'}:{size}"
            disposition = "eligible" if targets else "no-python-source-targets"
        tasks.append(
            HuntTask(
                task_id,
                str(package),
                row["final_digest"],
                reference_sha,
                row["language"],
                targets,
                stratum,
                disposition,
            )
        )
    return tasks


def select_tasks(
    tasks: Sequence[HuntTask], size: int, *, seed: str, first: Sequence[str] = DEFAULT_SEEDS
) -> list[HuntTask]:
    """Seed cases first, then round-robin fix breadth × patch-size strata."""
    if size < 1:
        raise ValueError("sample size must be positive")
    eligible = {task.task_id: task for task in tasks if task.disposition == "eligible"}
    chosen = [eligible[name] for name in dict.fromkeys(first) if name in eligible][:size]
    selected = {task.task_id for task in chosen}
    groups: dict[str, list[HuntTask]] = defaultdict(list)
    for task in eligible.values():
        if task.task_id not in selected:
            groups[task.stratum].append(task)
    for group in groups.values():
        group.sort(key=lambda task: hashlib.sha256(f"{seed}:{task.task_id}".encode()).digest())
    while len(chosen) < size and groups:
        for key in sorted(tuple(groups)):
            chosen.append(groups[key].pop(0))
            if not groups[key]:
                del groups[key]
            if len(chosen) == size:
                break
    return chosen


def rank_report(report: MutationReport) -> dict[str, Any]:
    """Refuse unknown coverage/controls rather than rank them as negative findings."""
    controls = {
        run.role: run for run in report.runs if run.role in {"capture-oracle", "capture-base"}
    }
    ready = (
        report.executed
        and report.coverage == "guided"
        and report.verdict in {"survivors_found", "no_survivors", "no_mutants"}
        and all(
            role in controls
            and controls[role].status in {"ok", "mismatch"}
            and controls[role].reward == reward
            for role, reward in (("capture-oracle", 1.0), ("capture-base", 0.0))
        )
    )
    traced = {mutant.id for mutant in report.mutants if mutant.executed is True}
    scored = [
        run
        for run in report.runs
        if run.role == "mutant"
        and run.mutant in traced
        and run.outcome in {"survived", "killed", "partial"}
    ]
    ready = ready and bool(scored)
    survivors = [run.mutant for run in scored if run.outcome == "survived"]
    return {
        "task": report.task,
        "package_digest": report.package_digest,
        "report": str(Path(report.run_dir) / "mutation-report.json"),
        "coverage": report.coverage,
        "verdict": report.verdict,
        "rankable": ready,
        "executed_survivors": survivors if ready else None,
        "survived": len(survivors) if ready else None,
        "scored_executed": len(scored) if ready else None,
        "unscored_mutants": sum(
            run.role == "mutant" and run.outcome == "unscored" for run in report.runs
        ),
    }


def run_hunt(
    manifest: Path,
    *,
    repo_root: Path,
    output_dir: Path,
    sample_size: int = 32,
    max_mutants: int = 12,
    seed: str = "envcheck-2026-10-13",
    execute: bool = False,
    timeout_seconds: int = 900,
    audit: Callable[..., tuple[MutationReport, Path]] = run_mutation_audit,
) -> dict[str, Any]:
    """Run one audit at a time; auxiliary observer containers are operator-controlled.

    Reuse only finished, digest-matching reports from this frozen plan. A partial
    run directory is retained and refused, never overwritten or retried blindly.
    """
    repo_root, output_dir = repo_root.resolve(), output_dir.resolve()
    if not output_dir.is_relative_to(repo_root):
        raise ValueError("hunt output must be inside the owned repository")
    if max_mutants < 1 or timeout_seconds < 1:
        raise ValueError("mutant and timeout bounds must be positive")
    tasks = inventory(manifest, shared_checkout_root(repo_root))
    selected = select_tasks(tasks, sample_size, seed=seed)
    plan = {
        "schema": SCHEMA,
        "manifest": str(manifest.resolve()),
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "seed": seed,
        "sample_size": sample_size,
        "max_mutants": max_mutants,
        "timeout_seconds": timeout_seconds,
        "backend": "docker",
        "workers": 1,
        "stratification": "Python source fixes; single/multi source file × <=20/<=100/>100 patch lines",
        "inventory": [asdict(task) for task in tasks],
        "selected": [task.task_id for task in selected],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_path = output_dir / "hunt-plan.json"
    encoded_plan = json.loads(json.dumps(plan))
    if plan_path.exists() and json.loads(plan_path.read_text()) != encoded_plan:
        raise ValueError(f"frozen hunt plan differs: {plan_path}; choose a new output directory")
    plan_path.write_text(json.dumps(plan, indent=2) + "\n")
    results: list[dict[str, Any]] = []
    for task in selected:
        report_dir = output_dir / "mutations" / task.task_id.rsplit("-", 1)[-1]
        report_path = report_dir / "mutation-report.json"
        if report_path.is_file():
            report = MutationReport.model_validate_json(report_path.read_text())
            if report.package_digest != task.package_digest or report.targets != list(task.targets):
                raise ValueError(f"existing mutation report has different inputs: {report_path}")
            if not report.executed:
                raise ValueError(f"existing report is only a plan: {report_path}")
            results.append(rank_report(report))
        elif execute:
            if report_dir.exists():
                raise ValueError(f"partial audit directory retained, not overwritten: {report_dir}")
            try:
                digest = compute_task_digests(Path(task.package)).package
                if digest != task.package_digest:
                    raise ValueError(
                        f"manifest package drift: expected {task.package_digest}, got {digest}"
                    )
                report, _ = audit(
                    task.package,
                    repo_root=repo_root,
                    targets=task.targets,
                    max_mutants=max_mutants,
                    seed=seed,
                    execute=True,
                    workers=1,
                    timeout_seconds=timeout_seconds,
                    output_dir=report_dir,
                    coverage=True,
                )
                results.append(rank_report(report))
            except (ValueError, OSError) as exc:
                results.append({"task": task.task_id, "rankable": False, "error": str(exc)})
        else:
            results.append({"task": task.task_id, "rankable": False, "status": "planned"})
        _save_results(output_dir, plan, results)
    return _save_results(output_dir, plan, results)


def _save_results(
    output_dir: Path, plan: dict[str, Any], results: list[dict[str, Any]]
) -> dict[str, Any]:
    ranking = sorted(
        (row for row in results if row["rankable"] and row["survived"] > 0),
        key=lambda row: (-row["survived"], -row["survived"] / row["scored_executed"], row["task"]),
    )
    payload = {
        "schema": SCHEMA,
        "plan": str(output_dir / "hunt-plan.json"),
        "cohort_size": len(plan["inventory"]),
        "selected": len(plan["selected"]),
        "results": results,
        "ranking": ranking,
        "top20": ranking[:20],
        "limits": "Stratified local sample; Python-only coverage. Survivors are hypotheses, not confirmed task defects.",
    }
    path = output_dir / "hunt-results.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)
    return payload


def _hunt_command(args: argparse.Namespace, root: Path, *, harbor: Any | None = None) -> int:
    del harbor
    try:
        result = run_hunt(
            root / args.manifest,
            repo_root=root,
            output_dir=root / args.output_dir,
            sample_size=args.sample_size,
            max_mutants=args.max_mutants,
            seed=args.seed,
            execute=args.execute,
            timeout_seconds=args.timeout_seconds,
        )
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


def add_hunt_parser(hack_commands: Any) -> None:
    parser = hack_commands.add_parser(
        "hunt", help="Stratify and rank known-fix tasks by traced survivors"
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=32)
    parser.add_argument("--max-mutants", type=int, default=12)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--seed", default="envcheck-2026-10-13")
    parser.add_argument("--execute", action="store_true")
    parser.set_defaults(func=_hunt_command)
