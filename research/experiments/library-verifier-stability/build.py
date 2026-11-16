#!/usr/bin/env python3
"""Render the six HAR-457 jobs from published native evidence, without changing it."""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import io
import json
import math
import os
import re
import sys
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
PACKAGES = {
    "query-optimize": "library/tasks/query-optimize",
    "html-js-filter": "library/tasks/terminal-bench-html-js-filter",
    "event-summary": "library/tasks/event-summary",
    "release-branch-rescue": "library/tasks/release-branch-rescue",
    "transaction-reconciliation": "library/tasks/transaction-reconciliation",
    "syn-funcdag-medium": "library/tasks/experimental/syn-funcdag-medium",
}
MANIFEST_SCHEMA = "evallab.library_verifier_stability_jobs/v1"
STABILITY_SCHEMA = "evallab.verifier_stability/v1"
UNKNOWN = "unknown"
BASE_COLUMNS = (
    "package", "classification", "evidence_errors", "task_path", "task_digest",
    "source_package_digest", "staged_harbor_digest", "lock_task_digest", "host_arch", "docker_arch", "job_path", "trial",
    "command_exit_code", "dispatcher_exit_code", "native_exception", "verifier_environment_mode",
)
RUN_COLUMNS = ("reward", "error", "exit_code", "stdout_tail_sha256", "stdout_error")
COLUMNS = BASE_COLUMNS + tuple(f"{key}_{i}" for i in range(3) for key in RUN_COLUMNS)
RUNTIME_COLUMNS = (
    "package", "job_path", "trial", "repeat_index", "golden_median_s",
    "solution_median_s", "ratio", "limit", "comparison", "error",
)
HTML_COLUMNS = ("repeat_index", "batches_run", "timed_out_batches", "error")


class BuildError(Exception):
    """The manifest cannot safely identify its published evidence."""


def read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BuildError(f"{path}: expected a JSON object")
    return value


def optional_object(path: Path, errors: list[str]) -> dict:
    try:
        return read_object(path)
    except BuildError as exc:
        errors.append(str(exc))
        return {}


def finite(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def cell(value: object) -> str:
    if value is None:
        return UNKNOWN
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def contained(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise BuildError(f"expected a nonempty relative evidence path, got {relative!r}")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise BuildError(f"evidence path escapes root: {relative}")
    return path


def parser_module(folder: str) -> ModuleType:
    path = HERE.parent / folder / "build.py"
    module = ModuleType(folder.replace("-", "_"))
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


def validate_manifest(path: Path) -> tuple[dict, list[dict]]:
    manifest = read_object(path)
    for key, expected in (
        ("schema", MANIFEST_SCHEMA), ("card", "HAR-457"),
        ("verifier_repeat_n", 3), ("concurrency", 1), ("attempts", 1),
        ("watchdog_seconds", 5400),
    ):
        if manifest.get(key) != expected or isinstance(manifest.get(key), bool):
            raise BuildError(f"manifest {key}: expected {expected!r}")
    for key in ("run_id", "code_commit", "host_arch", "docker_arch"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            raise BuildError(f"manifest {key}: expected a nonempty string")
    jobs = manifest.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != len(PACKAGES):
        raise BuildError("manifest must contain exactly six jobs")
    by_package = {}
    identities = set()
    for entry in jobs:
        if not isinstance(entry, dict) or entry.get("package") not in PACKAGES:
            raise BuildError("manifest contains an unknown package")
        package = entry["package"]
        if package in by_package or entry.get("task_path") != PACKAGES[package]:
            raise BuildError(f"duplicate package or unexpected task_path: {package}")
        for key in ("task_digest", "job_path", "trial", "started_at", "finished_at"):
            value = entry.get(key)
            if key not in entry or (value is not None and (not isinstance(value, str) or not value)):
                raise BuildError(f"manifest {package}.{key}: expected a string or null")
        if "command_exit_code" not in entry or (
            entry["command_exit_code"] is not None and type(entry["command_exit_code"]) is not int
        ):
            raise BuildError(f"manifest {package}.command_exit_code: expected an integer or null")
        identity = (entry.get("job_path"), entry.get("trial"))
        if identity[0] and identity in identities:
            raise BuildError(f"duplicate native job/trial: {identity}")
        identities.add(identity)
        by_package[package] = entry
    prior_attempts = manifest.get("prior_attempts", [])
    if not isinstance(prior_attempts, list):
        raise BuildError("manifest prior_attempts must be a list")
    for entry in prior_attempts:
        if not isinstance(entry, dict) or entry.get("package") not in PACKAGES:
            raise BuildError("manifest prior_attempts contains an unknown package")
        if entry.get("task_path") != PACKAGES[entry["package"]]:
            raise BuildError("manifest prior_attempts contains an unexpected task_path")
        identity = (entry.get("job_path"), entry.get("trial"))
        if any(value is not None and not isinstance(value, str) for value in identity):
            raise BuildError("manifest prior attempt job_path/trial must be strings or null")
        if identity[0] and identity in identities:
            raise BuildError(f"prior attempt repeats a selected job/trial: {identity}")
        identities.add(identity)
    return manifest, [by_package[package] for package in PACKAGES]


def require_match(label: str, recorded: object, native: object) -> None:
    if recorded is not None and native is not None and recorded != native:
        raise BuildError(f"identity mismatch for {label}: manifest={recorded!r}, native={native!r}")


def trial_evidence(root: Path, entry: dict, errors: list[str]) -> tuple[dict, dict, Path | None]:
    if not entry.get("job_path"):
        errors.append("missing published job_path")
        return {}, {}, None
    job = contained(root, entry["job_path"])
    metadata = optional_object(job / "lab-metadata.json", errors)
    staging = metadata.get("task_staging") or {}
    if not isinstance(staging, dict):
        raise BuildError(f"{job}: task_staging must be an object")
    manifest_staging = entry.get("task_staging") or {}
    if not isinstance(manifest_staging, dict):
        raise BuildError(f"{job}: manifest task_staging must be an object")
    require_match("staging digest", manifest_staging.get("staged_harbor_digest"), staging.get("staged_harbor_digest"))
    require_match("source package digest", entry.get("source_package_digest"), staging.get("source_package_digest"))
    experiment = metadata.get("experiment") or {}
    if not isinstance(experiment, dict):
        raise BuildError(f"{job}: experiment provenance must be an object")
    if not experiment.get("package_digest"):
        errors.append("missing native experiment package_digest")
    if not experiment.get("task_path"):
        errors.append("missing native experiment task_path")
    require_match("native source package provenance", experiment.get("package_digest"), staging.get("source_package_digest"))
    require_match("native source task path", entry["task_path"], experiment.get("task_path"))
    require_match("source task basename", Path(entry["task_path"]).name, staging.get("source_task_basename"))
    for key in ("source_task_basename", "staged_harbor_digest", "staged_task_name"):
        if not staging.get(key):
            errors.append(f"missing native {key}")
    if not entry.get("trial"):
        errors.append("missing native trial basename")
        return metadata, {}, None
    if Path(entry["trial"]).name != entry["trial"]:
        raise BuildError("trial must be a basename")
    trial = contained(job, entry["trial"])
    result = optional_object(trial / "result.json", errors)
    require_match("trial name", entry["trial"], result.get("trial_name"))
    if not result.get("trial_name"):
        errors.append("missing native trial_name")
    require_match("native task digest", entry.get("task_digest"), result.get("task_checksum"))
    if result.get("task_checksum") and not entry.get("task_digest"):
        errors.append("missing manifest native task_digest")
    if not result.get("task_checksum"):
        errors.append("missing native task_checksum")
    if not staging.get("source_package_digest"):
        errors.append("missing native source_package_digest")
    config = result.get("config") or {}
    if not isinstance(config, dict):
        raise BuildError(f"{trial}: config must be an object")
    agent = config.get("agent") or {}
    if not isinstance(agent, dict) or agent.get("name") != "oracle":
        errors.append("native config does not identify oracle agent")
    return metadata, result, trial


def build(manifest: dict, jobs: list[dict], root: Path) -> tuple[list[dict], list[dict], list[dict]]:
    if not root.is_dir():
        raise BuildError(f"published results root is absent: {root}")
    timing_parser = parser_module("query-optimize-runtime-margin")
    html_parser = parser_module("html-js-filter-unexecuted-vectors")
    rows, runtimes, html = [], [], []
    for entry in jobs:
        errors: list[str] = []
        metadata, result, trial = trial_evidence(root, entry, errors)
        staging = metadata.get("task_staging") or {}
        lock = optional_object(trial / "lock.json", errors) if trial else {}
        task_lock = lock.get("task") or {}
        if not isinstance(task_lock, dict):
            raise BuildError(f"{trial}: lock task must be an object")
        for key in ("type", "name", "path", "digest"):
            if not task_lock.get(key):
                errors.append(f"missing native trial lock task.{key}")
        native_task_config = (result.get("config") or {}).get("task") or {}
        if not isinstance(native_task_config, dict):
            raise BuildError(f"{trial}: config task must be an object")
        if not native_task_config.get("path"):
            errors.append("missing native config task.path")
        require_match("native lock task type", "local", task_lock.get("type"))
        require_match("native lock task name", staging.get("staged_task_name"), task_lock.get("name"))
        require_match("native lock task path", native_task_config.get("path"), task_lock.get("path"))
        require_match("native lock task digest", staging.get("staged_harbor_digest"), task_lock.get("digest"))
        exception = result.get("exception_info")
        if exception:
            errors.append("native trial exception")
        if metadata.get("timed_out"):
            errors.append("native Harbor process timed out")
        if "exit_code" in metadata and metadata["exit_code"] != 0:
            errors.append(f"native Harbor process exit: {cell(metadata['exit_code'])}")
        if metadata.get("exit_code") is None:
            errors.append("missing native Harbor process exit code")
        require_match("Harbor subprocess exit code", entry.get("command_exit_code"), metadata.get("exit_code"))
        if entry.get("command_exit_code") != 0:
            errors.append("manifest Harbor subprocess exit is nonzero or unknown")
        if entry.get("dispatcher_exit_code") not in (None, 0):
            errors.append("dispatcher exit is nonzero")
        for key in ("error", "measurement_error"):
            if entry.get(key):
                errors.append(f"manifest {key}: {cell(entry[key])}")
        row = {key: UNKNOWN for key in COLUMNS}
        row.update({
            "package": entry["package"], "task_path": entry["task_path"],
            "task_digest": cell(result.get("task_checksum")),
            "source_package_digest": cell(staging.get("source_package_digest")),
            "staged_harbor_digest": cell(staging.get("staged_harbor_digest")),
            "lock_task_digest": cell(task_lock.get("digest")),
            "host_arch": manifest["host_arch"], "docker_arch": manifest["docker_arch"],
            "job_path": cell(entry.get("job_path")), "trial": cell(entry.get("trial")),
            "command_exit_code": cell(metadata.get("exit_code")),
            "dispatcher_exit_code": cell(entry.get("dispatcher_exit_code")),
            "native_exception": ("none" if exception is None else cell(exception)) if "exception_info" in result else UNKNOWN,
            "verifier_environment_mode": cell(result.get("verifier_environment_mode")),
        })
        stability = optional_object(trial / "verifier/stability.json", errors) if trial else {}
        if stability:
            if stability.get("schema") != STABILITY_SCHEMA:
                raise BuildError(f"{trial}: unsupported stability schema")
            for key in ("repeat_n_requested", "n_runs"):
                if key in stability and (type(stability[key]) is not int or stability[key] != 3):
                    errors.append(f"stability {key}: expected 3, observed {cell(stability[key])}")
                if key not in stability:
                    errors.append(f"missing stability {key}")
            if stability.get("repeat_dir") != "repeat":
                errors.append("missing or unsupported stability repeat_dir")
        runs = stability.get("runs", [])
        if not isinstance(runs, list):
            raise BuildError(f"{trial}: stability runs must be a list")
        indexed = {}
        for run in runs:
            if not isinstance(run, dict) or type(run.get("index")) is not int or run["index"] not in range(3):
                raise BuildError(f"{trial}: invalid repeat index")
            if run["index"] in indexed:
                raise BuildError(f"{trial}: duplicate repeat index")
            indexed[run["index"]] = run
        rewards = []
        for index in range(3):
            run = indexed.get(index, {})
            reward = finite(run.get("reward"))
            rewards.append(reward)
            run_error = run.get("error")
            if not run:
                run_error = "missing repeat record"
            elif reward is None and not run_error:
                run_error = "missing or nonfinite reward"
            stdout_errors = []
            stdout_bytes = None
            if trial:
                try:
                    stdout_bytes = (trial / f"verifier/repeat/{index}/test-stdout.txt").read_bytes()
                except OSError:
                    stdout_errors.append("missing repeat stdout")
            else:
                stdout_errors.append("missing trial")
            actual_digest = hashlib.sha256(stdout_bytes[-8192:]).hexdigest() if stdout_bytes else None
            recorded_digest = run.get("stdout_tail_sha256")
            if stdout_bytes is None:
                stdout_errors.append("unavailable repeat stdout")
            elif not stdout_bytes:
                stdout_errors.append("empty repeat stdout (0 bytes)")
            if actual_digest != recorded_digest:
                stdout_errors.append("stdout tail digest mismatch")
                errors.append(f"repeat {index}: stdout tail digest mismatch")
            if recorded_digest is not None and not re.fullmatch(r"[0-9a-f]{64}", str(recorded_digest)):
                stdout_errors.append("invalid stdout tail digest")
                errors.append(f"repeat {index}: invalid stdout tail digest")
            if run and "error" not in run:
                errors.append(f"repeat {index}: missing canonical error field")
            if run_error:
                errors.append(f"repeat {index}: {cell(run_error)}")
            if run.get("exit_code") is None:
                errors.append(f"repeat {index}: missing verifier exit code")
            elif type(run["exit_code"]) is not int:
                raise BuildError(f"{trial}: invalid verifier exit code")
            elif run["exit_code"] != 0:
                errors.append(f"repeat {index}: nonzero verifier exit code")
            for key, value in (
                ("reward", reward), ("error", run_error), ("exit_code", run.get("exit_code")),
                ("stdout_tail_sha256", recorded_digest),
                ("stdout_error", "; ".join(stdout_errors) or "none"),
            ):
                row[f"{key}_{index}"] = "none" if key == "error" and value is None else cell(value)
            stdout = stdout_bytes.decode("utf-8", "replace") if stdout_bytes is not None else ""
            if entry["package"] == "query-optimize":
                golden, solution = timing_parser.medians(stdout)
                ratio = solution / golden if golden and solution is not None else None
                if ratio is not None and not math.isfinite(ratio):
                    ratio = None
                runtime_errors = list(stdout_errors)
                if golden is None or solution is None or not golden:
                    runtime_errors.append("missing or invalid golden/solution median")
                runtimes.append({
                    "package": entry["package"], "job_path": row["job_path"], "trial": row["trial"],
                    "repeat_index": index, "golden_median_s": cell(golden),
                    "solution_median_s": cell(solution), "ratio": cell(ratio), "limit": "1.05",
                    "comparison": UNKNOWN if ratio is None else ("within" if ratio <= 1.05 else "above"),
                    "error": "; ".join(runtime_errors) or "none",
                })
            if entry["package"] == "html-js-filter":
                batches, timeouts = html_parser.parse_stdout(stdout)
                html.append({
                    "repeat_index": index,
                    "batches_run": batches if stdout_bytes else UNKNOWN,
                    "timed_out_batches": timeouts if stdout_bytes else UNKNOWN,
                    "error": "; ".join(stdout_errors) or ("no browser batches" if not batches else "none"),
                })
        row["classification"] = "error" if errors else ("stable" if len(set(rewards)) == 1 else "flipped")
        row["evidence_errors"] = "; ".join(dict.fromkeys(errors)) or "none"
        rows.append(row)
    return rows, runtimes, html


def render_csv(rows: list[dict], columns: tuple[str, ...]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def markdown_table(columns: tuple[str, ...], rows: list[dict]) -> str:
    def escape(value: object) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")
    return "\n".join([
        "| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |",
        *("| " + " | ".join(escape(row.get(key, UNKNOWN)) for key in columns) + " |" for row in rows),
    ])


def prior_attempt_rows(manifest: dict, root: Path) -> list[dict]:
    """Read only explicitly retained prior jobs; never infer rewards from startup failures."""
    rows = []
    for entry in manifest.get("prior_attempts", []):
        errors: list[str] = []
        _, result, trial = trial_evidence(root, entry, errors)
        exception = result.get("exception_info")
        if isinstance(exception, dict):
            native_error = ": ".join(
                str(exception[key])
                for key in ("exception_type", "exception_message")
                if exception.get(key)
            ) or UNKNOWN
        else:
            native_error = "none" if "exception_info" in result and exception is None else UNKNOWN
        stability_path = trial / "verifier/stability.json" if trial else None
        if stability_path is not None and stability_path.is_file():
            stability = read_object(stability_path)
            if stability.get("schema") != STABILITY_SCHEMA:
                raise BuildError(f"{trial}: unsupported prior stability schema")
            n_runs = cell(stability.get("n_runs"))
        else:
            n_runs = "unknown (no stability.json)"
        rows.append({
            "package": entry["package"],
            "published job": cell(entry.get("job_path")),
            "native error": native_error,
            "n_runs": n_runs,
        })
    return rows


def render_readme(
    manifest: dict, rows: list[dict], runtimes: list[dict], html: list[dict],
    prior_attempts: list[dict] | None = None,
) -> str:
    summary = [{
        "package": row["package"], "classification": row["classification"],
        "rewards (0,1,2)": ", ".join(row[f"reward_{i}"] for i in range(3)),
        "published job": row["job_path"],
    } for row in rows]
    findings = "\n".join(f"- **{row['package']}**: {row['evidence_errors']}" for row in rows)
    prior_attempts = prior_attempts or []
    prior_text = markdown_table(
        ("package", "published job", "native error", "n_runs"), prior_attempts
    ) if prior_attempts else "No prior attempts are retained in the manifest."
    startup_note = ""
    if any(
        row["package"] == "release-branch-rescue"
        and "EnvironmentStartTimeoutError" in row["native error"]
        for row in prior_attempts
    ):
        startup_note = (
            "The initial release job records `EnvironmentStartTimeoutError` before the verifier starts.\n"
            "The error reports `Environment start timed out after 600.0 seconds`.\n"
            "That message names the configured limit, not a measured 600-second wait.\n"
            "The trace includes a daemon-information query; it does not identify the root cause.\n"
            "The root cause is unknown. This finding does not establish a Docker daemon outage.\n"
            "The startup failure, not a reward, triggers the permitted fresh dispatch.\n"
        )
    return f"""# Library verifier stability (HAR-457)

Cost: **$0**. Run: `{manifest['run_id']}`. Launch commit: `{manifest['code_commit']}`.
Host architecture: `{manifest['host_arch']}`. Docker architecture: `{manifest['docker_arch']}`.

## Measured results

{markdown_table(('package', 'classification', 'rewards (0,1,2)', 'published job'), summary)}

Published paths are relative to `~/Developer/eval-lab-results`.
`jobs.json` selects exactly these 6 final jobs and explicitly retains any prior attempts.
The builder never scans other jobs.
[stability.csv](stability.csv) preserves every repeat reward, error, exit code, and stdout digest.

### Retained prior attempts

{prior_text}

{startup_note}
An absent stability record means no repeat reward is measured; it does not mean reward 0 or a flip.
The table retains the initial job even when a fresh dispatch completes.
Any successful fresh dispatch is a separate job, not another attempt inside the initial job.
Each dispatch still requests `attempts=1`. No retry is selected by reward.

### Evidence findings

{findings}

### Query runtime margin

{markdown_table(('repeat_index', 'golden_median_s', 'solution_median_s', 'ratio', 'limit', 'comparison', 'error'), runtimes)}

[runtime.csv](runtime.csv) records all 3 repeats, including absent measurements.
The builder uses the existing [query parser](../query-optimize-runtime-margin/build.py).
It selects the first printed golden and solution `median_s` values.
The ratio is `solution / golden`; `within` means the ratio is at most `1.05`.
A missing, nonfinite, or zero-denominator measurement remains `unknown`, never zero.
The threshold comparison does not prove query correctness or a stable timing margin on other hosts.

### HTML browser caveat

{markdown_table(HTML_COLUMNS, html)}

These counts use the existing [HAR-424 parser](../html-js-filter-unexecuted-vectors/build.py) on each repeat stdout.
It counts exact `RUN BATCH` lines and at most 1 `Failed to load URL:` event per batch.
The [HAR-424 table](../html-js-filter-unexecuted-vectors/trials.csv) separately records 3 oracle browser trials.
Each records 28 batches, 1 timed-out batch, and reward 1.
Those older trials are context, not repeat evidence for this campaign.
The [source explanation](../html-js-filter-unexecuted-vectors/README.md#why-the-verifier-accepts-the-timeout) shows why the verifier can accept a timeout.
A timed-out batch is not independent proof that the filter blocks its vectors.
The printed `contains 0 tests` value is not a vector count.
Full batches contain 16 vectors; the final batch can contain 12.
Zero browser batches does not prove successful browser execution.

## Method and classification

Each job requests `Executor.execute_direct(RunRequest(agent=\"oracle\", environment=\"docker\",
verifier_repeat_n=3, concurrency=1, attempts=1, ...), ingest=False)`.
The campaign runs 1 job at a time through the default host-wide Docker FIFO.
The active Harbor watchdog is 5,400 seconds per job. FIFO waiting is separate.
No model, paid service, source package edit, or harness edit forms part of this experiment.
For `syn-funcdag-medium`, existing Docker runtime staging uses effective verifier networking `public` instead of requested `no-network`.
`jobs.json` records `network_isolation_enforced=false` and reason `darwin-docker-cannot-enforce-no-network`.
These results do not establish behavior under the requested network isolation.

The [repeat verifier](../../../src/evallab/harbor_repeat_verifier.py) invokes the native verifier 3 times.
All invocations use one final oracle state and the same verifier environment.
They are not independent oracle attempts, fresh environments, or clean-state rollbacks.
Earlier verifier side effects can affect later invocations.
Separate mode transfers declared artifacts to one verifier environment.
One native verifier deadline covers the complete repeat call; it does not multiply by 3.
Separate-mode errors remain findings, not permission to change the task or harness.

- **stable**: all 3 finite rewards agree, and the required evidence has no errors.
- **flipped**: all 3 finite rewards exist and differ, with no evidence errors.
- **error**: any repeat error, absent reward, missing evidence, native exception, or nonzero recorded exit code.
- A stable reward 0 is not an oracle pass. Only three rewards of 1 support repeat oracle success.
- Three failed or missing repeats never count as stable.

Published native files are authoritative. Manifest identities must match native identities when both exist.
`source_package_digest` identifies the complete source package in native staging metadata.
The builder checks it against the manifest and native `experiment.package_digest`.
It also checks the source path in native experiment provenance.

The hashes have different algorithms and file selections:

- **Source package:** `registry.task_directory_digest` sorts recursive package files under its ignore policy.
  It hashes concatenated `file_sha256_hex + two spaces + ./relative_path + LF` records.
- **Registry staging:** `registry.harbor_task_digest` selects root `task.toml`, `instruction.md`, `README.md`,
  and recursive files below `environment`, `tests`, `solution`, and `steps`, under its fixed ignore policy.
  It sorts relative paths and hashes `relative_path + NUL + file_sha256_hex + LF` records.
- **Native lock:** Harbor `Packager.compute_content_hash` uses that flat record encoding.
  Its file selection also supports `trajectory.json` and task `.gitignore` rules.
  Therefore, equality with the registry helper is an observed binding, not a universal algorithm equivalence.
  `lock_task_digest` records the authoritative executed identity from trial `lock.json.task.digest`.
- **Legacy result checksum:** `task_digest` records native `result.json.task_checksum`.
  Harbor `Task.checksum` calls `dirhash(task_dir, \"sha256\")` over the whole staged directory.
  It applies no package ignore list and omits empty directories by default.
  File descriptors contain `data:file_sha256_hex` and `name:basename`.
  Directory descriptors contain `dirhash:child_hash` and `name:basename`.
  It sorts properties, joins them with NUL, sorts entry descriptors, and joins entries with 2 NUL bytes.
  Each directory hash is the SHA-256 of that UTF-8 descriptor. The checksum has no `sha256:` prefix.

The runner adds root `run_manifest.json` after validating the copied package.
This file enters the legacy whole-directory checksum, but not the package-lock or registry-helper allowlists.
The native lock and legacy checksum are recorded before environment setup and oracle execution.
The observed query lock digest matches its staging digest; its legacy checksum differs.
This difference does not establish task drift or oracle-time mutation.

Algorithm sources are [`registry.py`](../../../src/evallab/registry.py),
[`runner.py`](../../../src/evallab/runner.py), and the locked Harbor and `dirhash` runtime sources.
Harbor owners are `models/task/task.py`, `models/job/lock.py`, and `publisher/packager.py`.
The `dirhash` owner's `Protocol.get_descriptor` defines its recursive descriptor encoding.

The builder binds lock type to `local`, lock name to the staged task name,
lock path to native `result.config.task.path`, and lock digest to the staging digest.
Missing lock identities produce evidence errors; identity mismatches refuse the report.
It compares manifest `task_digest` only with the legacy result checksum, never with a flat package hash.
It also checks the source basename and native trial name.
It does not recalculate identity from mutable checkout task bytes.
Missing native identity stays `unknown` and produces an evidence error.

## Digest and exit-code semantics

Canonical per-repeat stdout is `verifier/repeat/{{0,1,2}}/test-stdout.txt`.
The verifier restores top-level files from repeat 0; top-level stdout is not repeat 1 or 2.
`stdout_tail_sha256` hashes only the final 8,192 stdout bytes, not the full file.
Missing or empty stdout has a null native digest, rendered as `unknown`.
A null digest with empty or unavailable stdout is allowed by the native v1 schema.
It does not by itself change equal-reward stability into an error.
`stdout_error_i` preserves availability notes as well as digest errors.
Missing query medians remain unknown; absent HTML browser batches remain an evidence caveat.
The builder recomputes each available tail digest and reports mismatches as evidence errors.
The digest does not establish full-output identity or task correctness.

`exit_code_i` records the last verifier shell/script exec, excluding preparation calls.
It does not necessarily equal an inner pytest exit code.
`command_exit_code` records `lab-metadata.json.exit_code`, the native Harbor subprocess status.
It does not record the dispatcher status or an individual verifier invocation.
`dispatcher_exit_code` separately records the manifest's dispatcher status when available.
Harbor can parse a reward after a nonzero verifier exit. A command exit of 0 does not prove a pass.
Run 0 determines the ordinary native trial reward; this table uses all 3 repeat records.
`none` means an observed absent error; `unknown` means an absent measurement.

## Reproduce without executing tasks

From the owned repository worktree:

```bash
.venv/bin/python research/experiments/library-verifier-stability/build.py
.venv/bin/python research/experiments/library-verifier-stability/build.py --check
# Before the parent copies the manifest:
.venv/bin/python research/experiments/library-verifier-stability/build.py --manifest /path/to/jobs.json
# Optional alternate published root and output directory:
.venv/bin/python research/experiments/library-verifier-stability/build.py \\
  --manifest /path/to/jobs.json --root /path/to/eval-lab-results --out-dir /tmp/har457-render
```

`--manifest` defaults to this folder's `jobs.json`.
`--root` defaults to `EVALLAB_RESULTS_ROOT`, then `~/Developer/eval-lab-results`.
`--out-dir` defaults to this folder. Outputs are `stability.csv`, `runtime.csv`, and `README.md`.
`--check` changes no files. Exit 0 means exact reproduction; exit 1 means output drift.
Exit 2 means an invalid manifest, unsupported schema/count, identity mismatch, or unusable results root.
Measured trial failures still render rows with `error`; they do not become synthetic rewards.
The builder never modifies the manifest or published evidence.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=HERE / "jobs.json")
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("EVALLAB_RESULTS_ROOT") or Path.home() / "Developer/eval-lab-results"))
    parser.add_argument("--out-dir", type=Path, default=HERE)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        manifest, jobs = validate_manifest(args.manifest)
        rows, runtimes, html = build(manifest, jobs, args.root.expanduser())
        outputs = {
            "stability.csv": render_csv(rows, COLUMNS),
            "runtime.csv": render_csv(runtimes, RUNTIME_COLUMNS),
            "README.md": render_readme(
                manifest, rows, runtimes, html,
                prior_attempt_rows(manifest, args.root.expanduser()),
            ),
        }
        if args.check:
            different = False
            for name, text in outputs.items():
                path = args.out_dir / name
                before = path.read_bytes() if path.is_file() else b""
                if before != text.encode("utf-8"):
                    different = True
                    print(f"{path}: stale", file=sys.stderr)
                    diff = difflib.unified_diff(before.decode("utf-8", "replace").splitlines(), text.splitlines(), fromfile=str(path), tofile=f"{path} (rebuilt)", lineterm="")
                    for line in list(diff)[:40]:
                        print(line, file=sys.stderr)
            return 1 if different else 0
        output_root = args.out_dir.expanduser().resolve()
        evidence_root = args.root.expanduser().resolve()
        if output_root.is_relative_to(evidence_root) or evidence_root.is_relative_to(output_root):
            raise BuildError("output directory and published evidence root must not overlap")
        if any((output_root / name).resolve() == args.manifest.resolve() for name in outputs):
            raise BuildError("an output would overwrite the manifest")
        if any((output_root / name).resolve().is_relative_to(evidence_root) for name in outputs):
            raise BuildError("an output symlink points into published evidence")
        output_root.mkdir(parents=True, exist_ok=True)
        for name, text in outputs.items():
            (output_root / name).write_text(text, encoding="utf-8")
        print(f"wrote {len(rows)} package rows and {len(runtimes)} query repeat rows to {output_root}")
        return 0
    except (BuildError, OSError, ValueError) as exc:
        print(f"build: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
