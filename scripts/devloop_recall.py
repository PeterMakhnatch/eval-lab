#!/usr/bin/env python3
"""Replay failed PR quality runs against two standalone devloop implementations.

Requires Python 3.12+, git, tar, and an authenticated gh CLI. No project install
is needed. The replay loads planner code, but never imports historical source
modules or executes historical tests.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import ModuleType

FAILURE = re.compile(r"\b(?:FAILED|ERROR)\s+(\S+?\.py)(?=::|\s|$)")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# plan_devloop caches per-file import statements by content hash under this
# directory, so all archived run trees share one warm import graph.
CACHE_DIR = Path(tempfile.mkdtemp(prefix="devloop-recall-cache-"))
os.environ.setdefault("EVALLAB_DEVLOOP_CACHE_DIR", str(CACHE_DIR))


def command(repo: Path, *args: str) -> str:
    return subprocess.run(
        args, cwd=repo, check=True, capture_output=True, text=True, timeout=180
    ).stdout


def error_detail(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return (exc.stderr or exc.stdout or str(exc)).strip()[-1500:]
    return str(exc)


def normalize(path: str) -> str:
    return str(PurePosixPath(path.split("::", 1)[0].replace("\\", "/")))


def failing_files(log: str) -> list[str]:
    return sorted({normalize(match[1]) for match in FAILURE.finditer(ANSI.sub("", log))})


def load_planner(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load devloop from {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through the registered module.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def fetch_head(repo: Path, sha: str, branch: str) -> None:
    try:
        # GitHub requires the full headSha here, not an abbreviated SHA.
        command(repo, "git", "fetch", "origin", sha)
    except (subprocess.SubprocessError, OSError):
        prs = json.loads(
            command(
                repo, "gh", "pr", "list", "--head", branch, "--state", "all",
                "--json", "number", "--limit", "1",
            )
        )
        if not prs:
            raise ValueError(f"No PR found for branch {branch}") from None
        command(repo, "git", "fetch", "origin", f"refs/pull/{prs[0]['number']}/head")
    # A branch may have moved since this run. Never replay its newer tree.
    command(repo, "git", "cat-file", "-e", f"{sha}^{{commit}}")


def materialize(repo: Path, sha: str, root: Path, archive: Path) -> list[str]:
    config = tomllib.loads(command(repo, "git", "show", f"{sha}:pyproject.toml"))
    roots = config.get("tool", {}).get("pytest", {}).get("ini_options", {}).get(
        "testpaths", ["tests"]
    )
    paths = sorted({"src", "tests", "pyproject.toml", *roots})
    try:
        command(repo, "git", "archive", "--output", str(archive), sha, *paths)
    except subprocess.CalledProcessError:
        # Some historical configurations name test roots absent from that tree.
        command(repo, "git", "archive", "--output", str(archive), sha)
    command(repo, "tar", "-xf", str(archive), "-C", str(root))
    return roots


def test_inventory(root: Path, roots: list[str], planner: ModuleType) -> set[str]:
    files: set[str] = set()
    for test_root in roots:
        path = root / test_root
        candidates = [path] if path.is_file() else path.rglob("*.py")
        files.update(
            candidate.relative_to(root).as_posix()
            for candidate in candidates
            if planner.is_test_path(candidate.relative_to(root).as_posix(), roots)
        )
    return files


def score(plan, failures: list[str], inventory: set[str]) -> dict:
    modules = sorted({normalize(path) for path in plan.modules})
    fallback = "__all__" in modules
    hits = [path for path in failures if fallback or path in modules]
    misses = [path for path in failures if path not in hits]
    return {
        "selected_modules": modules,
        "selected_count": len(inventory) if fallback else len(modules),
        "fallback": fallback,
        "hits": hits,
        "misses": misses,
        "recall": len(hits) / len(failures),
    }


def explain_misses(run_id: int, failures: list[str], changed: list[str],
                   root: Path, roots: list[str], planner: ModuleType,
                   selected: list[str]) -> list[dict]:
    changed_modules = sorted({
        planner.src_path_to_module(path) for path in changed if planner.is_src_path(path)
    })
    all_modules = {
        planner.src_path_to_module(path.relative_to(root).as_posix())
        for path in (root / "src" / "evallab").rglob("*.py")
    } | set(changed_modules)
    misses = []
    for path in failures:
        test = root / path
        referenced = sorted(planner._referenced_modules(test, all_modules))
        if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            cause = "path normalization issue: failure path is not repository-relative"
        elif not test.is_file():
            cause = "test file absent at head"
        elif any(normalize(candidate) == path for candidate in selected):
            cause = "path normalization issue: selected path differs from failure path"
        elif not planner.is_test_path(path, roots):
            cause = "test file outside configured test roots"
        elif not changed_modules:
            cause = "no evallab source changed — selection empty or fallback"
        else:
            cause = (
                f"no import path: changed modules {changed_modules} not imported "
                f"directly or transitively by {path} under the planner's static rules"
            )
        misses.append({
            "run_id": run_id, "file": path, "cause": cause,
            "changed_src_modules": changed_modules,
            "referenced_modules": referenced,
        })
    return misses


def evaluate(repo: Path, run: dict, failures: list[str], before: ModuleType,
             after: ModuleType) -> tuple[dict, list[dict]]:
    sha = run["headSha"]
    merge_base = command(repo, "git", "merge-base", "origin/main", sha).strip()
    changed = command(repo, "git", "diff", "--name-only", merge_base, sha).splitlines()
    with tempfile.TemporaryDirectory(prefix="devloop-recall-tree-") as directory:
        temp = Path(directory)
        root = temp / "tree"
        root.mkdir()
        roots = materialize(repo, sha, root, temp / "tree.tar")
        inventory = test_inventory(root, roots, after)
        before_score = score(before.plan_devloop(changed, root), failures, inventory)
        after_score = score(after.plan_devloop(changed, root), failures, inventory)
        misses = explain_misses(
            run["databaseId"], after_score["misses"], changed, root, roots,
            after, after_score["selected_modules"],
        )
    return {
        "run_id": run["databaseId"], "headSha": sha, "branch": run["headBranch"],
        "created_at": run["createdAt"], "merge_base": merge_base,
        "failing_test_files": failures, "changed_files_count": len(changed),
        "changed_files": changed, "test_file_count": len(inventory),
        "before": before_score, "after": after_score,
    }, misses


def aggregate(runs: list[dict]) -> dict:
    failing_count = sum(len(run["failing_test_files"]) for run in runs)
    metrics = {"failing_file_observations": failing_count}
    for version in ("before", "after"):
        scores = [run[version] for run in runs]
        hits = sum(len(score["hits"]) for score in scores)
        metrics[version] = {
            "hits": hits,
            "misses": failing_count - hits,
            "recall": hits / failing_count if failing_count else None,
            "mean_per_run_recall": statistics.mean(
                score["recall"] for score in scores
            ) if scores else None,
            "fallback_rate": sum(score["fallback"] for score in scores) / len(scores)
            if scores else None,
            "median_selected_file_count": statistics.median(
                score["selected_count"] for score in scores
            ) if scores else None,
        }
    return metrics


def markdown(report: dict) -> str:
    def cell(value) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    def rate(value) -> str:
        return "n/a" if value is None else f"{value:.1%}"

    lines = [
        "# Devloop selection recall", "", f"Run date (UTC): {report['generated_at']}", "",
        "## Method", "",
        "Replay the most recent failed `quality` PR runs, in descending creation order.",
        "Extract unique failing Python files from pytest `FAILED` and `ERROR` log lines.",
        "Compare both planners on each run's archived source, test roots, and configuration.",
        "Changed files use the merge base between the run head and the recorded `origin/main`.",
        f"Before: `{report['before_rev']}` (`{report['before_sha']}`).",
        f"After: working-tree `{report['after_path']}`.",
        "No historical tests run. A full-suite fallback counts every failing file as a hit.",
        "Selected counts expand a fallback to the archived Python test-consumer inventory.",
        "", f"Usable runs: {len(report['runs'])}; attempts: {report['counts']['attempted']}; "
        f"skipped: {len(report['skipped_runs'])}.", "",
        "## Aggregate metrics", "",
        "File recall = total hits / total failing-file observations (weighted across runs).",
        "Mean per-run recall = the mean of each run's hits / failing files (equal run weights).",
        "Fallback rate = full-suite fallback runs / usable runs.", "",
        "| Metric | Before | After |", "|---|---:|---:|",
    ]
    metrics = report["aggregate"]
    for label, key, is_rate in (
        ("File recall", "recall", True),
        ("Mean per-run recall", "mean_per_run_recall", True),
        ("Fallback rate", "fallback_rate", True),
        ("Median selected file count", "median_selected_file_count", False),
        ("Hits", "hits", False), ("Misses", "misses", False),
    ):
        values = [metrics[version][key] for version in ("before", "after")]
        rendered = [rate(value) if is_rate else cell(value) for value in values]
        lines.append(f"| {label} | {rendered[0]} | {rendered[1]} |")
    lines += [
        "", "## Per-run results", "",
        "| Run | Branch | Failing files | Hits before / after | Recall before / after | Fallback before / after |",
        "|---|---|---|---:|---:|---|",
    ]
    for run in report["runs"]:
        before, after = run["before"], run["after"]
        files = ", ".join(f"`{cell(path)}`" for path in run["failing_test_files"])
        lines.append(
            f"| {run['run_id']} | {cell(run['branch'])} | {files} | "
            f"{len(before['hits'])} / {len(after['hits'])} | "
            f"{rate(before['recall'])} / {rate(after['recall'])} | "
            f"{before['fallback']} / {after['fallback']} |"
        )
    lines += ["", "## Remaining misses", ""]
    for miss in report["misses"]:
        lines.append(f"- Run {miss['run_id']}, `{miss['file']}`: {miss['cause']}.")
        lines.append(f"  Referenced evallab modules: {miss['referenced_modules']}.")
    if not report["misses"]:
        lines.append("No post-change misses occur in the usable runs.")
    lines += ["", "## Skipped runs", ""]
    for skip in report["skipped_runs"]:
        lines.append(f"- Run {skip['run_id']}: {skip['cause']}. {skip.get('detail', '')}")
    if not report["skipped_runs"]:
        lines.append("None.")
    lines += [
        "", "## Limits", "",
        "- Failed-run sampling is not a random sample of all PRs. Repeat heads remain separate runs.",
        "- The denominator counts each unique failing file once per run, not each failing test case.",
        "- Logs without pytest FAILED/ERROR lines, unavailable heads, and invalid trees are skipped.",
        "- Recall measures file selection only. It does not establish test success or root cause.",
        "- Full-suite fallback can raise recall while selecting more files; inspect its rate and size.",
        "- Fallback size counts Python consumers under test roots, excluding conftest.py. It is not pytest collection.",
        "- Static import and string matching cannot prove dynamic runtime dependencies.",
        "- The current origin/main merge base can differ from the original CI merge base.",
        "- The working-tree after planner can contain uncommitted edits; preserve it with the evidence.",
        "- A bounded smoke run does not satisfy the default 30-run measurement requirement.",
    ]
    if report.get("error"):
        lines += ["", f"Setup error: {report['error']}"]
    return "\n".join(lines) + "\n"


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--limit", type=positive, default=200)
    parser.add_argument("--min-runs", type=positive, default=30)
    parser.add_argument("--max-runs", type=positive)
    parser.add_argument("--before-rev", default="origin/main")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    report = {
        "schema": "evallab.devloop_recall/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "before_rev": args.before_rev, "before_sha": None,
        "after_path": "src/evallab/devloop.py", "origin_main_sha": None,
        "parameters": {"limit": args.limit, "min_runs": args.min_runs, "max_runs": args.max_runs},
        "counts": {"enumerated": 0, "attempted": 0, "usable": 0, "skipped": 0},
        "runs": [], "skipped_runs": [], "misses": [],
    }
    try:
        report["before_sha"] = command(repo, "git", "rev-parse", args.before_rev).strip()
        report["origin_main_sha"] = command(repo, "git", "rev-parse", "origin/main").strip()
        with tempfile.TemporaryDirectory(prefix="devloop-recall-code-") as directory:
            before_path = Path(directory) / "before.py"
            before_path.write_text(
                command(repo, "git", "show", f"{report['before_sha']}:src/evallab/devloop.py"),
                encoding="utf-8",
            )
            before = load_planner(before_path, "_devloop_recall_before")
            after = load_planner(repo / report["after_path"], "_devloop_recall_after")
            runs = json.loads(command(
                repo, "gh", "run", "list", "--status", "failure", "--workflow", "quality",
                "--event", "pull_request", "--limit", str(args.limit), "--json",
                "databaseId,headSha,headBranch,createdAt",
            ))
            runs.sort(key=lambda run: (run["createdAt"], run["databaseId"]), reverse=True)
            report["counts"]["enumerated"] = len(runs)
            print(f"Found {len(runs)} failed quality PR runs", file=sys.stderr)
            for run in runs:
                if len(report["runs"]) >= args.min_runs:
                    break
                if args.max_runs is not None and report["counts"]["attempted"] >= args.max_runs:
                    break
                report["counts"]["attempted"] += 1
                run_id = run["databaseId"]
                print(f"Run {run_id}: reading failed logs", file=sys.stderr)
                stage = "failed logs unavailable"
                try:
                    failures = failing_files(command(repo, "gh", "run", "view", str(run_id), "--log-failed"))
                    if not failures:
                        report["skipped_runs"].append({
                            "run_id": run_id, "cause": "no pytest FAILED/ERROR lines",
                        })
                        print(f"Run {run_id}: skip — no pytest FAILED/ERROR lines", file=sys.stderr)
                        continue
                    print(f"Run {run_id}: extracted {len(failures)} failing files; fetching head", file=sys.stderr)
                    stage = "head not fetchable"
                    fetch_head(repo, run["headSha"], run["headBranch"])
                    stage = "tree or planner evaluation failed"
                    result, misses = evaluate(repo, run, failures, before, after)
                    report["runs"].append(result)
                    report["misses"].extend(misses)
                    print(
                        f"Run {run_id}: usable; hits before/after "
                        f"{len(result['before']['hits'])}/{len(result['after']['hits'])} "
                        f"of {len(failures)}; selected before/after "
                        f"{result['before']['selected_count']}/{result['after']['selected_count']}",
                        file=sys.stderr,
                    )
                except Exception as exc:
                    detail = error_detail(exc)
                    report["skipped_runs"].append({
                        "run_id": run_id, "headSha": run["headSha"], "branch": run["headBranch"],
                        "cause": stage, "detail": detail,
                    })
                    print(f"Run {run_id}: skip — {stage}: {detail}", file=sys.stderr)
    except Exception as exc:
        report["error"] = error_detail(exc)
        print(f"Setup failed: {report['error']}", file=sys.stderr)
    report["counts"]["usable"] = len(report["runs"])
    report["counts"]["skipped"] = len(report["skipped_runs"])
    report["aggregate"] = aggregate(report["runs"])
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (args.out_dir / "README.md").write_text(markdown(report), encoding="utf-8")
    success = len(report["runs"]) >= args.min_runs
    print(
        f"Wrote results.json and README.md to {args.out_dir}; "
        f"{len(report['runs'])}/{args.min_runs} required usable runs",
        file=sys.stderr,
    )
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
