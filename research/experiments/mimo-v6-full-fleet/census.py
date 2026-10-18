"""Inventory the 567 retained MiMo reference tasks against exact v6 bytes.

This is a control-qualification census, not a weak-test or semantic-validity
verdict. Historical package digests never qualify a rebuilt package. Native
execution UUIDs deduplicate receipt copies; setup failures remain unscored.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import math
import os
import re
import shlex
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from evallab.mimo_census import (
    _PYTEST_COLLECTED_NONE_RE,
    _PYTEST_COUNT_RE,
    _PYTEST_SUMMARY_RE,
    _runner_output_executed,
    job_has_trial_errors,
    trial_grade_logs,
    trial_output_logs,
    trial_runner,
)
from evallab.mimo_clean import build_solution_sh
from evallab.registry import harbor_task_digest, task_directory_digest
from evallab.task_health_filter import trial_task_identity

FLEET_LABEL = "oracle:pass+nop:fail"
FLEET_SIZE = 567
COLUMNS = (
    "task_id", "language", "bucket", "reason", "reference_label",
    "reference_patch_path", "reference_patch_sha256", "reference_bytes_match",
    "run_digest", "v6_package_digest", "v6_harbor_digest", "package_path",
    "package_bytes_match", "reference_solution_match", "oracle_execution_ids",
    "nop_execution_ids", "oracle_rewards", "nop_rewards", "reported_issues", "native_evidence",
)


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _custom_framework_output_executed(framework: str, text: str) -> bool:
    """Recognize completed named Django/unittest or Twisted tree-reporter cases."""
    if framework == "twisted":
        counts = r"(?:successes|failures|errors|skips|expectedFailures|unexpectedSuccesses)=\d+"
        terminal = rf"(?:PASSED|FAILED) \({counts}(?:, {counts})*\)"
    else:
        counts = r"(?:failures|errors|skipped|expected failures|unexpected successes)=\d+"
        terminal = (
            rf"OK(?: \({counts}(?:, {counts})*\))?"
            rf"|FAILED \({counts}(?:, {counts})*\)"
        )
    completed = re.search(
        r"(?m)^Ran (?P<cases>[1-9]\d*) tests? in \d+(?:\.\d+)?s[ \t]*\n[ \t]*\n"
        rf"(?P<result>{terminal})[ \t]*$",
        text,
    )
    if completed is None:
        return False
    skipped = re.search(r"\b(?:skipped|skips)=(\d+)\b", completed["result"])
    if skipped and int(skipped[1]) >= int(completed["cases"]):
        return False
    cases = text[:completed.start()]
    if framework == "twisted":
        # Import-error holders have no real test class/method tree. A module
        # name followed by an indented [ERROR] is not a case outcome.
        return re.search(
            r"(?m)^  (?!(?:_FailedTest|_ErrorHolder|ErrorHolder|Failure)\b)[A-Za-z_]\w*[ \t]*\n"
            r"(?:    test\w* \.\.\.[^\n]*\n)*"
            r"    test\w* \.\.\.[ \t]+\[(?:OK|FAIL|ERROR)\][ \t]*$",
            cases,
        ) is not None
    pending = False
    for line in cases.splitlines():
        case = re.fullmatch(
            r"(?P<name>[^()\n]+) \((?P<identity>[^)\n]+)\) \.\.\. (?P<outcome>.*)", line,
        )
        if case:
            pending = (
                re.fullmatch(r"test\S+", case["name"]) is not None
                and not any(
                    loader in case["identity"]
                    for loader in ("_FailedTest", "_ErrorHolder", "nose.failure.Failure")
                )
            )
            line = case["outcome"]
        # Application logging can separate Django's named case from its
        # terminal outcome; a start without an outcome still proves nothing.
        if pending and re.fullmatch(r"(?:ok|FAIL|ERROR)[ \t]*", line):
            return True
        if re.match(r"skipped\b|expected failure\b|unexpected success\b", line):
            pending = False
    return False


def _python_invocations(command: str) -> Iterator[tuple[list[str], str | None]]:
    """Read declared Python commands and stdin bodies, without executing shell code."""
    lines = iter(command.splitlines())
    for line in lines:
        lexer = shlex.shlex(line, posix=True, punctuation_chars="<>")
        lexer.whitespace_split = True
        try:
            argv = list(lexer)
        except ValueError:
            continue
        body = None
        if "<<" in argv:
            index = argv.index("<<")
            if index + 1 >= len(argv):
                continue
            delimiter = argv[index + 1]
            source = []
            for line in lines:
                if line == delimiter:
                    body = "\n".join(source)
                    break
                source.append(line)
            argv = argv[:index]
            if body is None:
                continue
        if argv[:1] == ["exec"]:
            argv = argv[1:]
        if argv and re.fullmatch(r"(?:\S*/)?python(?:\d+(?:\.\d+)*)?", argv[0]):
            yield argv[1:], body


def _inline_django_invocation(command: str) -> bool:
    for argv, body in _python_invocations(command):
        if not argv or argv[0] != "-" or body is None:
            continue
        try:
            statements = ast.parse(body).body
        except SyntaxError:
            continue
        constructors: set[str] = set()
        runners: set[str] = set()
        for statement in statements:
            value = statement.value if isinstance(statement, (ast.Assign, ast.Expr)) else None
            if (
                isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                and value.func.attr == "run_tests" and isinstance(value.func.value, ast.Name)
                and value.func.value.id in runners
            ):
                return True
            constructed = statement.targets[0].id if (
                isinstance(statement, ast.Assign) and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                and value.func.id in constructors
            ) else None
            # Do not mistake a reassigned import/runner, a dormant function,
            # or another object's similarly named method for DiscoverRunner.
            for node in ast.walk(statement):
                if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    constructors.discard(node.id)
                    runners.discard(node.id)
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                constructors.discard(statement.name)
                runners.discard(statement.name)
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                for alias in statement.names:
                    name = alias.asname or alias.name.split(".")[0]
                    constructors.discard(name)
                    runners.discard(name)
                    if (
                        isinstance(statement, ast.ImportFrom)
                        and statement.module == "django.test.runner" and statement.level == 0
                        and alias.name == "DiscoverRunner"
                    ):
                        constructors.add(name)
            if constructed is not None:
                runners.add(constructed)
    return False


def _terminal_pytest_execution(trial: Path, command: str) -> bool:
    """Require a caller-bound producer and consistent, repeat-local native reports."""
    if not any(
        argv[:2] == ["-m", "pytest"] and body is None
        and any(
            argv[index:index + 2] == ["--ctrf", "/logs/verifier/ctrf.json"]
            for index in range(len(argv))
        )
        for argv, body in _python_invocations(command)
    ):
        return False
    for report in sorted(trial.rglob("ctrf.json")):
        if trial_runner(report.parent) != "pytest":
            continue
        try:
            results = json.loads(report.read_bytes())["results"]
            text = report.with_name("test-stdout.txt").read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not isinstance(results, dict) or not isinstance(results.get("tool"), dict):
            continue
        cases, summary = results.get("tests"), results.get("summary")
        if results["tool"].get("name") != "pytest" or not isinstance(cases, list) or not cases:
            continue
        if not isinstance(summary, dict):
            continue
        statuses = ("passed", "failed", "skipped", "pending", "other")
        if any(
            not isinstance(case, dict) or not isinstance(case.get("name"), str)
            or "::" not in case["name"]
            or not all(part.strip() for part in case["name"].rsplit("::", 1))
            or case.get("status") not in statuses
            for case in cases
        ):
            continue
        counts = Counter(case["status"] for case in cases)
        expected = {"tests": len(cases), **{status: counts[status] for status in statuses}}
        if (
            len({case["name"] for case in cases}) != len(cases)
            or any(type(summary.get(key)) is not int or summary[key] != count for key, count in expected.items())
            or counts["passed"] + counts["failed"] == 0
            or _PYTEST_COLLECTED_NONE_RE.search(text)
        ):
            continue
        # A plugin can emit named failed rows for fixture/collection errors.
        # Require the completed stdout's actual passed/failed tallies to agree.
        summaries = _PYTEST_SUMMARY_RE.findall(text)
        if len(summaries) != 1:
            continue
        observed = Counter()
        for count, status in _PYTEST_COUNT_RE.findall(summaries[0]):
            observed[status] += int(count)
        if (
            observed["error"] == 0
            and observed["passed"] == counts["passed"]
            and observed["failed"] == counts["failed"]
        ):
            return True
    return False


def _custom_framework_execution(trial: Path) -> bool:
    for output_path in sorted(trial.rglob("test_output.log")):
        if trial_runner(output_path.parent) != "custom":
            continue
        try:
            command = output_path.with_name("resolved_command.txt").read_text(
                encoding="utf-8", errors="replace",
            )
            text = output_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _inline_django_invocation(command) and _custom_framework_output_executed("django", text):
            return True
        for invocation in re.finditer(
            r"(?m)^[ \t]*(?:exec[ \t]+)?(?:(?:\S*/)?pipenv[ \t]+run[ \t]+)?"
            r"(?:(?P<django>(?:\S*/)?python(?:\d+(?:\.\d+)*)?[ \t]+"
            r"(?:\S*/)?manage\.py[ \t]+test)"
            r"|(?P<twisted>(?:\S*/)?trial|(?:\S*/)?python(?:\d+(?:\.\d+)*)?"
            r"[ \t]+-m[ \t]+twisted\.trial))(?:[ \t]|$)",
            command,
        ):
            framework = "django" if invocation["django"] else "twisted"
            if _custom_framework_output_executed(framework, text):
                return True
    return False


def positive_case_execution(trial: Path, *, pytest_command: str | None = None) -> bool:
    """Read cases; callers must bind optional terminal producer code to its package."""
    if any(grade["cases"] > 0 for grade in trial_grade_logs(trial)):
        return True
    if any(
        re.search(r"\b(?:mvn|gradle)\s+surefire=\d+\s+cases=[1-9]\d*\b", text)
        for text in trial_output_logs(trial, "junit-grade.log")
    ):
        return True
    if pytest_command is not None and _terminal_pytest_execution(trial, pytest_command):
        return True
    runner = trial_runner(trial)
    if runner == "custom" and _custom_framework_execution(trial):
        return True
    custom_python = runner == "custom" and any(
        re.search(
            r"(?m)^[ \t]*(?:\S*/)?python(?:\d+(?:\.\d+)*)?"
            r"[ \t]+-m[ \t]+(?:nose|unittest)(?:[ \t]|$)",
            command,
        )
        for command in trial_output_logs(trial, "resolved_command.txt")
    )
    for text in trial_output_logs(trial, "test_output.log"):
        # These shared recognizers also accept bare status words or file
        # counts. This consumer requires a positive case count instead.
        if runner == "karma":
            ran = re.search(r"\bExecuted\s+[1-9]\d*\s+of\b", text) is not None
        elif runner == "phpunit":
            ran = re.search(
                r"\bTests:\s*[1-9]\d*\b|(?m:^OK\s*\(\s*[1-9]\d*\s+tests?\b)", text,
            ) is not None
        elif runner == "vitest":
            ran = any(
                _runner_output_executed(runner, "Tests " + summary)
                for summary in re.findall(r"(?m)^\s*Tests\s+([^\n]+)", text)
            )
        elif custom_python:
            # Old wrappers label nose as custom. Require the actual command,
            # named non-loader cases and a completed counted Python suite.
            ran = (
                _runner_output_executed("unittest", text)
                and re.search(
                    r"(?m)^Ran [1-9]\d* tests? in \d+(?:\.\d+)?s[ \t]*\n[ \t]*\n"
                    r"(?:OK(?: \([^)\n]*\))?|FAILED \([^)\n]+\))[ \t]*$",
                    text,
                ) is not None
                and re.search(
                    r"(?m)^(?![^\n]*(?:_FailedTest|nose\.failure\.Failure))"
                    r"(?:(?:FAIL|ERROR): test\S+ \([^)\n]+\)"
                    r"|test\S+ \([^)\n]+\) \.\.\. (?:ok|FAIL|ERROR))[ \t]*$",
                    text,
                ) is not None
            )
        else:
            ran = _runner_output_executed(runner, text)
        if ran:
            return True
    return False


def native_task_binding(
    job: Path, trial: Path, native: dict[str, Any], staging: dict[str, Any], *, legacy: bool,
) -> dict[str, Any]:
    """Bind recorded native identities without opening the old staged task tree."""
    identity = trial_task_identity(job, trial)
    config_path = trial / "config.json"
    config = json.loads(config_path.read_bytes()) if config_path.is_file() else {}
    configured_path = (config.get("task") or {}).get("path")
    result_paths = (
        (native.get("config", {}).get("task") or {}).get("path"),
        (native.get("task_id") or {}).get("path"),
    )
    staged_name = staging.get("staged_task_name")
    path_ok = (
        isinstance(configured_path, str) and bool(configured_path)
        and isinstance(staged_name, str) and bool(staged_name)
        and Path(configured_path).name == staged_name
        and all(
            isinstance(path, str) and os.path.normpath(path) == os.path.normpath(configured_path)
            for path in result_paths
        )
    )
    lock_path = job / "lock.json" if identity["via"] == "job-lock" else trial / "lock.json"
    lock = json.loads(lock_path.read_bytes()) if lock_path.is_file() else {}
    locked_tasks = (
        [entry.get("task") for entry in lock.get("trials", []) if isinstance(entry, dict)]
        if identity["via"] == "job-lock" else [lock.get("task")]
    )
    matching_tasks = [
        task for task in locked_tasks
        if isinstance(task, dict) and isinstance(task.get("path"), str)
        and isinstance(configured_path, str)
        and os.path.normpath(task["path"]) == os.path.normpath(configured_path)
    ]
    name = native.get("task_name")
    declared = staging.get("declared_task_name")
    name_ok = (
        isinstance(name, str) and isinstance(declared, str)
        and name.removeprefix("mimo-v2.6-rl/") == declared.removeprefix("mimo-v2.6-rl/")
    )
    bound = (
        identity["task_digest"] is not None
        and identity["task_digest"] == staging.get("staged_harbor_digest")
        and staging.get("staged_harbor_digest") == staging.get("source_harbor_digest")
        and (legacy or identity["source_package_digest"] == staging.get("source_package_digest"))
        and path_ok and name_ok and bool(matching_tasks)
        and all(task.get("name") == staged_name and task.get("type") == "local" for task in matching_tasks)
    )
    return {
        **identity, "verified": bound,
        "config_path": str(config_path),
        "config_sha256": file_sha256(config_path) if config_path.is_file() else None,
        "lock_path": str(lock_path),
        "lock_sha256": file_sha256(lock_path) if lock_path.is_file() else None,
    }


def native_observations(metadata_paths: set[Path]) -> list[dict[str, Any]]:
    """Read native identity, rewards, errors and case execution, not receipt labels."""
    observations: dict[str, dict[str, Any]] = {}
    for metadata_path in sorted(metadata_paths):
        if not metadata_path.is_file():
            continue
        metadata = json.loads(metadata_path.read_bytes())
        job = metadata_path.parent
        legacy = metadata_path.name == "stability-manifest.json"
        job_result_path = job / "result.json"
        job_result = json.loads(job_result_path.read_bytes()) if legacy and job_result_path.is_file() else {}
        process_ok = (
            bool(job_result.get("finished_at"))
            and (job_result.get("stats") or {}).get("n_completed_trials", 0) > 0
            and not job_has_trial_errors(job)
        ) if legacy else metadata.get("exit_code") == 0 and metadata.get("timed_out") is False
        for result_path in sorted(job.glob("*/result.json")):
            if legacy:
                entry = metadata.get("trials", {}).get(result_path.parent.name, {})
                source_slug = Path(entry.get("source", "")).parent.name
                staging = {
                    "declared_task_name": source_slug.removeprefix("mimo-v2.6-rl__"),
                    "source_package_digest": entry.get("task_version_digest"),
                    "source_harbor_digest": entry.get("harbor_digest"),
                    "staged_harbor_digest": entry.get("harbor_digest"),
                    "staged_task_name": entry.get("task_name"),
                }
            else:
                staging = metadata.get("task_staging") or {}
            digest = staging.get("source_package_digest")
            task = str(staging.get("declared_task_name", "")).removeprefix("mimo-v2.6-rl/")
            if not digest or not task.startswith("format-code-task-"):
                continue
            native = json.loads(result_path.read_bytes())
            agent = (native.get("config", {}).get("agent") or {}).get("name")
            if agent not in ("oracle", "nop") or not native.get("id"):
                continue
            trial = result_path.parent
            binding = native_task_binding(job, trial, native, staging, legacy=legacy)
            stability_path = trial / "verifier/stability.json"
            if stability_path.is_file():
                runs = json.loads(stability_path.read_bytes()).get("runs", [])
                rewards = [run.get("reward") for run in runs]
                errors = [run.get("error") for run in runs]
                executed = [
                    positive_case_execution(trial / "verifier/repeat" / str(index))
                    for index in range(len(runs))
                ]
            else:
                reward_map = (native.get("verifier_result") or {}).get("rewards") or {}
                rewards = [reward_map.get("reward")]
                errors = [None]
                executed = [positive_case_execution(trial)]
            repeat_scored = [
                type(reward) in (int, float) and math.isfinite(reward)
                and ran and error is None and binding["verified"]
                and (stability_path.is_file() or native.get("exception_info") is None)
                for reward, ran, error in zip(rewards, executed, errors, strict=True)
            ]
            scored = (
                bool(rewards) and all(repeat_scored)
                and native.get("exception_info") is None and process_ok
            )
            # Only these normalized facts affect qualification. Receipt paths and
            # byte hashes may change when a published copy records a relocation.
            qualification = {
                "task_id": task, "agent": agent,
                "source_package_digest": digest,
                "source_harbor_digest": staging.get("source_harbor_digest"),
                "staged_harbor_digest": staging.get("staged_harbor_digest"),
                "executed_harbor_digest": binding["task_digest"],
                "native_identity_verified": binding["verified"],
                "rewards": rewards, "test_execution": executed,
                "run_error_present": [error is not None for error in errors],
                "exception_present": native.get("exception_info") is not None,
                "process_ok": process_ok,
                "process_failed": metadata.get("exit_code") not in (0, None),
                "timed_out": metadata.get("timed_out") is True,
                "repeat_scored": repeat_scored, "scored": scored,
            }
            observation = {
                "execution_id": native["id"], **qualification,
                "native_task_checksum": native.get("task_checksum"),
                "task_binding": binding, "run_errors": errors,
                "exception_type": (native.get("exception_info") or {}).get("exception_type"),
                "exception_info_sha256": (
                    hashlib.sha256(compact(native["exception_info"]).encode()).hexdigest()
                    if native.get("exception_info") is not None else None
                ),
                "returncode": metadata.get("exit_code"),
                "identity_kind": "stability_manifest" if legacy else "native_executor_staging",
                "job_result_sha256": file_sha256(job_result_path) if legacy and job_result_path.is_file() else None,
                "metadata_path": str(metadata_path), "metadata_sha256": file_sha256(metadata_path),
                "result_path": str(result_path), "result_sha256": file_sha256(result_path),
            }
            previous = observations.get(native["id"])
            if previous is not None:
                if any(previous[key] != value for key, value in qualification.items()):
                    raise ValueError(f"conflicting copies of native execution {native['id']}")
                continue
            observations[native["id"]] = observation
    return sorted(observations.values(), key=lambda item: item["execution_id"])


def census_row(
    reference: dict[str, str], manifest: dict[str, str] | None,
    *, repo: Path, package_root: Path, observations: list[dict[str, Any]],
    reported_issues: list[dict[str, Any]],
) -> dict[str, str]:
    task = reference["task_id"]
    build = manifest or {}
    patch = resolve_path(repo, reference.get("patch_path", ""))
    reference_ok = patch.is_file() and file_sha256(patch) == reference.get("patch_sha256", "").removeprefix("sha256:")
    package = resolve_path(package_root, build.get("package_path", ""))
    package_ok = bool(build.get("final_digest")) and package.is_dir() and task_directory_digest(package) == build["final_digest"]
    harbor_digest = harbor_task_digest(package) if package_ok else ""
    solution_ok = False
    if reference_ok and package_ok:
        solution = package / "solution/solve.sh"
        expected = build_solution_sh(
            task_id=task, label=reference["label"], fix_commit=reference["fix_commit"],
            patch_bytes=patch.read_bytes(),
        )
        solution_ok = solution.is_file() and solution.read_bytes() == expected
    exact = [
        observation for observation in observations
        if observation["task_id"] == task
        and observation["source_package_digest"] == build.get("final_digest")
        and observation["source_harbor_digest"] == harbor_digest
    ]
    controls = {agent: [item for item in exact if item["agent"] == agent] for agent in ("oracle", "nop")}
    good = {
        agent: [item for item in controls[agent] if item["scored"] and all(reward == expected for reward in item["rewards"])]
        for agent, expected in (("oracle", 1), ("nop", 0))
    }
    wrong = any(
        any(scored and reward != expected for reward, scored in zip(item["rewards"], item["repeat_scored"], strict=True))
        for agent, expected in (("oracle", 1), ("nop", 0)) for item in controls[agent]
    )
    if not reference_ok:
        bucket, reason = "missing_ref", "indexed_reference_bytes_missing_or_hash_mismatch"
    elif build.get("status") != "built" or not package_ok:
        bucket, reason = "broken_ref_or_env", "v6_package_missing_or_digest_mismatch"
    elif not solution_ok:
        bucket, reason = "broken_ref_or_env", "embedded_reference_differs_from_index"
    elif wrong:
        bucket, reason = "broken_ref_or_env", "exact_input_control_disagrees_with_reference_1_nop_0"
    elif good["oracle"] and good["nop"]:
        bucket, reason = "v6_verified", "exact_input_reference_1_nop_0_tests_executed"
    elif any(
        issue["reference_patch_sha256"] == reference.get("patch_sha256", "")
        for issue in reported_issues
    ):
        bucket, reason = "broken_ref_or_env", "accepted_reported_reference_failure_not_superseded_by_exact_controls"
    elif any(
        item["exception_present"] or item["process_failed"] or item["timed_out"]
        or not item["rewards"] or any(reward is None for reward in item["rewards"])
        or any(item["run_error_present"])
        for item in exact
    ):
        bucket, reason = "broken_ref_or_env", "exact_input_control_setup_or_execution_failed"
    elif exact:
        bucket, reason = "v6_unverified", "exact_input_control_test_execution_or_pair_unproven"
    else:
        bucket, reason = "v6_unverified", "exact_input_reference_nop_pair_not_retained"
    return {
        "task_id": task, "language": build.get("language", ""), "bucket": bucket,
        "reason": reason, "reference_label": reference["label"],
        "reference_patch_path": reference.get("patch_path", ""),
        "reference_patch_sha256": reference.get("patch_sha256", ""),
        "reference_bytes_match": str(reference_ok).lower(), "run_digest": build.get("run_digest", ""),
        "v6_package_digest": build.get("final_digest", ""), "v6_harbor_digest": harbor_digest,
        "package_path": build.get("package_path", ""), "package_bytes_match": str(package_ok).lower(),
        "reference_solution_match": str(solution_ok).lower(),
        "reported_issues": compact(reported_issues),
        "oracle_execution_ids": compact([item["execution_id"] for item in controls["oracle"]]),
        "nop_execution_ids": compact([item["execution_id"] for item in controls["nop"]]),
        "oracle_rewards": compact([item["rewards"] for item in controls["oracle"]]),
        "nop_rewards": compact([item["rewards"] for item in controls["nop"]]),
        "native_evidence": compact(exact),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--reference-index", type=Path)
    parser.add_argument("--reported-issues", type=Path, default=Path(__file__).with_name("reported-issues.json"))
    parser.add_argument("--native-controls", type=Path, action="append", default=[])
    parser.add_argument("--jobs-root", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reference_path = args.reference_index or args.repo_root / "research/experiments/mimo-reference-fixes/index.csv"
    references = sorted((row for row in csv_rows(reference_path) if row["label"] == FLEET_LABEL), key=lambda row: row["task_id"])
    if len(references) != FLEET_SIZE or len({row["task_id"] for row in references}) != FLEET_SIZE:
        raise ValueError("canonical reference predicate must resolve exactly 567 unique tasks")
    builds = {row["task_id"]: row for row in csv_rows(args.manifest)}
    issues = json.loads(args.reported_issues.read_bytes())["issues"]
    for issue in issues:
        source = resolve_path(args.repo_root, issue["source"])
        if file_sha256(source) != issue["source_sha256"]:
            raise ValueError(f"reported reference-failure source changed: {source}")
    metadata_paths: set[Path] = set()
    for source in args.native_controls:
        document = json.loads(source.read_bytes())
        for observation in document["observations"]:
            binding = observation["native_metadata"]
            path = Path(binding["path"])
            if path.is_file() and file_sha256(path) != binding["sha256"]:
                raise ValueError(f"native metadata changed since retained receipt: {path}")
            metadata_paths.add(path)
    for root in args.jobs_root:
        metadata_paths.update(root.glob("*/lab-metadata.json"))
        metadata_paths.update(root.glob("*/stability-manifest.json"))
    observations = native_observations(metadata_paths)
    rows = [
        census_row(
            reference, builds.get(reference["task_id"]), repo=args.repo_root,
            package_root=args.package_root, observations=observations,
            reported_issues=[issue for issue in issues if issue["task_id"] == reference["task_id"]],
        )
        for reference in references
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(compact({
        "tasks": len(rows), "buckets": dict(sorted(Counter(row["bucket"] for row in rows).items())),
        "native_execution_ids": len(observations), "output": str(args.output),
        "sha256": file_sha256(args.output), "membership_index_sha256": file_sha256(reference_path),
        "manifest_sha256": file_sha256(args.manifest),
        "scope": "reference/nop controls only; not semantic validity or clean-set ladder acceptance",
    }))


if __name__ == "__main__":
    main()
