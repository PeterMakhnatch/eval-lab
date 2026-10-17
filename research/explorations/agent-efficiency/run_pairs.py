"""Exploratory native-Codex workflow pairs, not a hermetic Harbor evaluation.

Default: validate an explicit manifest and print the plan without writing or
calling a provider. --run-subscription is the only model-execution switch;
--score-only reads retained attempts without invoking Codex or a model judge.
max_calls bounds Codex exec invocations, not physical upstream requests inside
an agent workflow. The runner never retries, changes models, or falls back.
Raw prompts, scorer keys, CLI events and quota snapshots are PRIVATE artifacts.
A native read-only sandbox prevents writes, not reads of other host files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

WORKTREE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WORKTREE / "src"))

from evallab.gepa_optimizer.codex_transport import (  # noqa: E402
    CODEX_EXECUTABLE,
    DISABLED_FEATURES,
    MODEL_PROVIDER,
    _scrub_env,
)

SCHEMA = "evallab.agent_efficiency_pairs/v1"
MODEL = "gpt-6.1-sol"
EFFORT = "high"
MAX_CALLS = 64
QUOTA_STOP_PERCENT = 60
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ARM = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_TRUNCATION = re.compile(
    r"(?:\b(?:output|response|context) (?:was )?truncated\b|"
    r"\btruncated (?:output|response)\b|\b\d+ (?:tokens|characters|lines) truncated\b|"
    r"\btotal output lines:|\boriginal token count:)",
    re.IGNORECASE,
)
_LIMIT = re.compile(r"rate[ -]limit|usage[ -]limit|quota|context (?:window|length)", re.I)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_digest(files: list[dict[str, Any]]) -> str:
    """Location-independent digest of explicitly pinned source-file identities."""
    records = [
        {key: row[key] for key in ("relative_path", "sha256", "kind")}
        for row in sorted(files, key=lambda row: row["relative_path"])
    ]
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant: {value}")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_unique_keys, parse_constant=_reject_constant)


def _positive(value: Any, name: str, ceiling: int) -> int:
    if type(value) is not int or not 1 <= value <= ceiling:
        raise ValueError(f"{name} must be an integer in 1..{ceiling}")
    return value


def load_manifest(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    data = strict_json(raw.decode("utf-8"))
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError(f"manifest must use {SCHEMA}")
    if data.get("exploratory") is not True:
        raise ValueError("manifest must explicitly label this probe exploratory")
    if data.get("model") != MODEL or data.get("effort") != EFFORT:
        raise ValueError(f"only ChatGPT subscription {MODEL}, {EFFORT} effort is supported")
    repeats = _positive(data.get("repeats"), "repeats", MAX_CALLS // 2)
    cap = _positive(data.get("max_calls"), "max_calls", MAX_CALLS)
    _positive(data.get("timeout_seconds"), "timeout_seconds", 3600)
    if not isinstance(data.get("seed"), str) or not data["seed"]:
        raise ValueError("manifest needs an explicit nonempty seed")
    arms = data.get("arms")
    if (
        not isinstance(arms, list)
        or len(arms) != 2
        or any(not isinstance(arm, str) or not _ARM.fullmatch(arm) for arm in arms)
        or len(set(arms)) != 2
    ):
        raise ValueError("manifest must declare exactly two distinct, filename-safe arms")
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks or len(tasks) * 2 * repeats > cap:
        raise ValueError("nonempty complete pairs must fit within the manifest's hard max_calls")
    identities: set[str] = set()
    source_identities: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("task_id"), str):
            raise ValueError("every task needs a task_id")
        if not task["task_id"] or task["task_id"] in identities:
            raise ValueError(
                "task IDs must be nonempty and unique; repeats use repeats, not new IDs"
            )
        identities.add(task["task_id"])
        digest = task.get("source_digest")
        if not isinstance(digest, str) or not _HEX.fullmatch(digest):
            raise ValueError("every task needs a full SHA-256 source_digest")
        if digest in source_identities:
            raise ValueError("duplicate task source identities must be represented as repeats")
        source_identities.add(digest)
        prompts = task.get("prompts")
        if not isinstance(prompts, dict) or set(prompts) != set(arms):
            raise ValueError("every task must explicitly supply both prompt arms")
        if any(not isinstance(text, str) or not text.strip() for text in prompts.values()):
            raise ValueError("prompt arms must be nonempty strings")
        if not isinstance(task.get("expected_answer"), dict) or not task["expected_answer"]:
            raise ValueError("every task needs an explicit structured expected_answer")
        root_value = task.get("source_root")
        if not isinstance(root_value, str) or not Path(root_value).is_absolute():
            raise ValueError("task source_root must be absolute")
        files = task.get("source_files")
        if not isinstance(files, list) or not files:
            raise ValueError("every task needs explicitly pinned source_files")
        seen_files: set[str] = set()
        for row in files:
            if not isinstance(row, dict):
                raise ValueError("invalid source_files entry")
            if any(
                not isinstance(row.get(key), str)
                for key in ("path", "relative_path", "sha256", "kind")
            ):
                raise ValueError(
                    "source files require path, relative_path, sha256 and kind strings"
                )
            source = Path(row["path"])
            if not source.is_absolute() or not _HEX.fullmatch(row["sha256"]):
                raise ValueError("source files require absolute paths and full SHA-256 digests")
            relative = source.resolve().relative_to(Path(root_value).resolve()).as_posix()
            if relative != row["relative_path"] or relative in seen_files:
                raise ValueError("source file identity is duplicated or escapes the declared root")
            seen_files.add(relative)
        if source_digest(files) != digest:
            raise ValueError(f"source_digest does not match pinned files for {task['task_id']}")
    implementation = data.get("implementation_files", [])
    if not isinstance(implementation, list):
        raise ValueError("implementation_files must be a list")
    for row in implementation:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("path"), str)
            or not Path(row["path"]).is_absolute()
            or not isinstance(row.get("sha256"), str)
            or not _HEX.fullmatch(row["sha256"])
        ):
            raise ValueError("implementation files require absolute paths and SHA-256 digests")
    return data, hashlib.sha256(raw).hexdigest()


def verify_sources(manifest: dict[str, Any]) -> None:
    """Refuse changed evidence, including changes after planning or a prior pair."""
    for task in manifest["tasks"]:
        for row in task["source_files"]:
            path = Path(row["path"])
            if not path.is_file() or file_sha256(path) != row["sha256"]:
                raise ValueError(f"source bytes changed or disappeared: {path}")
    for row in manifest.get("implementation_files", []):
        path = Path(row["path"])
        if not path.is_file() or file_sha256(path) != row["sha256"]:
            raise ValueError(f"implementation bytes changed or disappeared: {path}")


def make_plan(manifest: dict[str, Any], manifest_sha256: str) -> dict[str, Any]:
    # Hash-rank tasks, then alternate the leading arm within each repeat. With
    # an odd cohort, flip the extra leading arm on the next repeat. Pair members
    # remain adjacent; repetition never manufactures a new task identity.
    tasks = sorted(
        manifest["tasks"],
        key=lambda task: hashlib.sha256(
            (manifest["seed"] + "\0" + task["source_digest"] + "\0" + task["task_id"]).encode()
        ).hexdigest(),
    )
    offset = int(hashlib.sha256(manifest["seed"].encode()).hexdigest(), 16) % 2
    calls = []
    for repeat in range(1, manifest["repeats"] + 1):
        for position, task in enumerate(tasks):
            arms = manifest["arms"][:]
            if (position + repeat - 1 + offset) % 2:
                arms.reverse()
            for arm in arms:
                index = len(calls) + 1
                calls.append(
                    {
                        "index": index,
                        "attempt_id": f"{index:04d}-{arm}-r{repeat:03d}",
                        "task_id": task["task_id"],
                        "source_digest": task["source_digest"],
                        "repeat": repeat,
                        "arm": arm,
                        "prompt_sha256": hashlib.sha256(task["prompts"][arm].encode()).hexdigest(),
                    }
                )
    return {
        "schema": SCHEMA,
        "exploratory": True,
        "manifest_sha256": manifest_sha256,
        "model": MODEL,
        "effort": EFFORT,
        "unique_tasks": len(tasks),
        "repeats_per_task": manifest["repeats"],
        "planned_calls": len(calls),
        "max_calls": manifest["max_calls"],
        "max_calls_unit": "codex_exec_invocations_not_physical_upstream_requests",
        "quota_batch_calls": 2,
        "quota_stop_percent": QUOTA_STOP_PERCENT,
        "timeout_seconds_per_call": manifest["timeout_seconds"],
        "calls": calls,
        "caveats": [
            "Exploratory native read-only navigation probe, not a hermetic correctness evaluation.",
            "Scorer keys stay out of prompts, but read-only is not host file-read isolation.",
            "No runner retries or model/API fallbacks; native CLI reconnect notices remain evidence.",
            "Input tokens include reported cached input; cache tokens are reported separately.",
            "Missing usage and monetary cost remain unknown, never zero.",
            "Repeated attempts are nested observations of the same task, not independent tasks.",
        ],
    }


def _private_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def _stop_tree(process: subprocess.Popen[bytes]) -> None:
    # Always signal the group, even if its leader already exited while a shell
    # child survived. start_new_session gives each invocation its own group.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)


def _logged_process(
    argv: list[str],
    directory: Path,
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    prompt: Path | None = None,
) -> dict[str, Any]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    started = time.monotonic()
    result: dict[str, Any] = {"argv": argv, "cwd": str(cwd), "started_at_utc": utc_now()}
    process = None
    with (
        (prompt or Path(os.devnull)).open("rb") as stdin,
        (directory / "stdout.jsonl").open("xb") as stdout,
        (directory / "stderr.log").open("xb") as stderr,
    ):
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            result["pid"] = process.pid
            result["returncode"] = process.wait(timeout=timeout)
            result["status"] = "exited"
        except subprocess.TimeoutExpired:
            result.update(status="timeout", error=f"process-tree timeout after {timeout}s")
        except KeyboardInterrupt:
            result.update(status="interrupted", error="interrupted; process tree terminated")
        except OSError as exc:
            result.update(status="spawn_failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            if process is not None:
                try:
                    _stop_tree(process)
                except (OSError, subprocess.SubprocessError) as exc:
                    result.update(
                        status="cleanup_failed", error=f"process-tree cleanup failed: {exc}"
                    )
                result["returncode"] = process.returncode
    result.update(elapsed_seconds=time.monotonic() - started, finished_at_utc=utc_now())
    _private_json(directory / "process.json", result)
    return result


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def weekly_quota(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("reports"), list):
        raise ValueError("quota report unavailable or malformed")
    unknown = snapshot.get("accountsWithoutUsage", [])
    if not isinstance(unknown, list) or any(
        not isinstance(row, dict) or row.get("provider") == "openai-codex" for row in unknown
    ):
        raise ValueError("an openai-codex account has unavailable usage")
    reports = [
        row
        for row in snapshot["reports"]
        if isinstance(row, dict) and row.get("provider") == "openai-codex"
    ]
    if not reports:
        raise ValueError("no openai-codex quota reports")
    percentages = []
    fetched = []
    for report in reports:
        if report.get("error"):
            raise ValueError("openai-codex quota report contains an error")
        metadata = report.get("metadata") or {}
        if (
            not isinstance(metadata, dict)
            or metadata.get("allowed") is False
            or metadata.get("limitReached") is True
        ):
            raise ValueError("subscription account is not currently allowed")
        limits = report.get("limits")
        if not isinstance(limits, list):
            raise ValueError("subscription weekly limits unavailable")
        weekly = [
            row
            for row in limits
            if isinstance(row, dict)
            and isinstance(row.get("window"), dict)
            and (row["window"].get("id") == "7d" or row["window"].get("durationMs") == 604800000)
        ]
        if not weekly:
            raise ValueError("subscription weekly quota unavailable")
        for limit in weekly:
            if limit.get("status") != "ok":
                raise ValueError("subscription weekly quota status is unavailable or blocked")
            amount = limit.get("amount")
            if not isinstance(amount, dict):
                raise ValueError("subscription quota amount unavailable")
            fraction = amount.get("usedFraction")
            if _number(fraction) and 0 <= fraction <= 1:
                percent = float(fraction) * 100
            elif (
                _number(amount.get("used")) and _number(amount.get("limit")) and amount["limit"] > 0
            ):
                percent = float(amount["used"]) / float(amount["limit"]) * 100
            else:
                raise ValueError("subscription weekly used percentage unavailable")
            if not 0 <= percent <= 100:
                raise ValueError("subscription weekly used percentage invalid")
            percentages.append(percent)
        fetched.append(report.get("fetchedAt"))
    maximum = max(percentages)
    if maximum >= QUOTA_STOP_PERCENT:
        raise ValueError(f"subscription weekly quota at/above {QUOTA_STOP_PERCENT}%: {maximum:g}%")
    return {"allowed": True, "weekly_used_percent_max": maximum, "fetched_at_ms": fetched}


def _event_details(event: dict[str, Any]) -> dict[str, Any]:
    """Keep reported elapsed/limits/truncation fields plus raw-log coordinates."""
    details: dict[str, Any] = {}

    def visit(value: Any, prefix: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                field = f"{prefix}.{key}" if prefix else key
                if any(
                    part in key.casefold()
                    for part in ("elapsed", "duration", "timeout", "limit", "truncat", "omitted")
                ):
                    details[field] = child
                visit(child, field)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{prefix}[{index}]")
        elif isinstance(value, str):
            match = _TRUNCATION.search(value)
            if match:
                details[f"{prefix}.truncation_marker"] = value[
                    max(0, match.start() - 80) : match.end() + 160
                ]
            match = _LIMIT.search(value)
            if match:
                details[f"{prefix}.limit_marker"] = value[
                    max(0, match.start() - 80) : match.end() + 160
                ]

    visit(event, "")
    return details


def read_events(path: Path) -> dict[str, Any]:
    usage_events = []
    details = []
    errors = []
    invalid_lines = []
    retry_notices = []
    tool_events = []
    reported_models = []
    completions = 0
    failed = False
    rerouted = False
    if path.is_file():
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    event = strict_json(line)
                    if not isinstance(event, dict):
                        raise ValueError("event is not an object")
                except ValueError as exc:
                    invalid_lines.append({"line": line_number, "error": str(exc)})
                    continue
                kind = event.get("type")
                completions += int(kind == "turn.completed")
                failed = failed or kind == "turn.failed"
                if "usage" in event:
                    usage_events.append(
                        {"line": line_number, "event_type": kind, "usage": event["usage"]}
                    )
                identity_objects = [("", event)]
                for key in ("response", "metadata"):
                    if isinstance(event.get(key), dict):
                        identity_objects.append((key + ".", event[key]))
                for prefix, record in identity_objects:
                    for key in ("model", "model_name", "served_model"):
                        identity = record.get(key)
                        if isinstance(identity, str) and identity:
                            reported_models.append(
                                {
                                    "line": line_number,
                                    "field": prefix + key,
                                    "value": identity,
                                }
                            )
                            if identity.rsplit("/", 1)[-1] != MODEL:
                                rerouted = True
                item = event.get("item")
                item_kind = item.get("type") if isinstance(item, dict) else None
                if item_kind and item_kind not in {"agent_message", "reasoning", "error"}:
                    tool_events.append(
                        {
                            "line": line_number,
                            "event_type": kind,
                            "item_type": item_kind,
                            "item_id": item.get("id"),
                            "status": item.get("status"),
                            "exit_code": item.get("exit_code"),
                            "reported": _event_details(event),
                        }
                    )
                if kind in {"error", "turn.failed"} or item_kind == "error":
                    errors.append({"line": line_number, "event": event})
                serialized = json.dumps(event, ensure_ascii=False)
                if "model rerouted:" in serialized.casefold():
                    rerouted = True
                if re.search(r"Reconnecting\.\.\.\s*\d+/\d+", serialized):
                    retry_notices.append({"line": line_number, "event": event})
                reported = _event_details(event)
                if reported:
                    details.append(
                        {"log": "stdout.jsonl", "line": line_number, "reported": reported}
                    )
    stderr_path = path.parent / "stderr.log"
    if stderr_path.is_file():
        with stderr_path.open(encoding="utf-8", errors="replace") as stream:
            for line_number, line in enumerate(stream, 1):
                reported = _event_details({"message": line.rstrip("\n")})
                if reported:
                    details.append({"log": "stderr.log", "line": line_number, "reported": reported})
                if "model rerouted:" in line.casefold():
                    rerouted = True
                if re.search(r"Reconnecting\.\.\.\s*\d+/\d+", line):
                    retry_notices.append(
                        {"log": "stderr.log", "line": line_number, "message": line.rstrip("\n")}
                    )
    tokens: dict[str, int | None] = {}
    for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
        values = [
            row["usage"].get(key) if isinstance(row["usage"], dict) else None
            for row in usage_events
        ]
        tokens[key] = (
            sum(values)
            if values and all(type(value) is int and value >= 0 for value in values)
            else None
        )
    return {
        "tokens": tokens,
        "usage_events": usage_events,
        "usage_semantics": "sum_of_reported_event_usage; input_includes_cached; missing_is_unknown",
        "turn_completions": completions,
        "turn_failed": failed,
        "model_reroute_reported": rerouted,
        "reported_model_identities": reported_models,
        "served_model_identity": "reported_in_cli_events"
        if reported_models
        else "not_reported_by_cli",
        "native_retry_notifications": retry_notices,
        "errors_and_warnings": errors,
        "invalid_event_lines": invalid_lines,
        "reported_elapsed_limits_truncation": details,
        "tool_events": tool_events,
        "reported_truncation": any(
            ("truncat" in key or "omitted" in key) and bool(value)
            for row in details
            for key, value in row["reported"].items()
        ),
        "raw_event_log": str(path),
    }


def _argv(cwd: Path, last: Path, *, effort: str = EFFORT) -> list[str]:
    argv = [
        str(CODEX_EXECUTABLE),
        "exec",
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--model",
        MODEL,
        "--json",
        "--output-last-message",
        str(last),
        "-c",
        f'model_provider="{MODEL_PROVIDER}"',
        "-c",
        'forced_login_method="chatgpt"',
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-c",
        'web_search="disabled"',
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        "features.skip_host_skill_discovery=true",
        "-c",
        "analytics.enabled=false",
        "-c",
        "feedback.enabled=false",
        "-C",
        str(cwd),
    ]
    # Retain ordinary shell navigation, but not nested agents, hooks, browser,
    # skills or alternate model-capable tools. No model tool can spawn a pair.
    for feature in DISABLED_FEATURES:
        if feature not in {"shell_tool", "unified_exec"}:
            argv.extend(("--disable", feature))
    argv.append("-")
    return argv


def run_attempt(
    manifest: dict[str, Any],
    task: dict[str, Any],
    call: dict[str, Any],
    output: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    directory = output / "attempts" / call["attempt_id"]
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    prompt = directory / "prompt.txt"
    prompt.write_text(task["prompts"][call["arm"]], encoding="utf-8")
    os.chmod(prompt, 0o600)
    last = directory / "last-message.txt"
    with tempfile.TemporaryDirectory(prefix="evallab-pair-cwd.") as empty:
        process = _logged_process(
            _argv(Path(empty), last),
            directory / "cli",
            cwd=Path(empty),
            env=env,
            timeout=manifest["timeout_seconds"],
            prompt=prompt,
        )
    events = read_events(directory / "cli" / "stdout.jsonl")
    completed = (
        process["status"] == "exited"
        and process.get("returncode") == 0
        and events["turn_completions"] == 1
        and not events["turn_failed"]
        and not events["model_reroute_reported"]
        and not events["invalid_event_lines"]
    )
    receipt = {
        **call,
        "status": "completed" if completed else "failed",
        "process": process,
        "events": events,
        "last_message_present": last.is_file(),
        "monetary_cost_usd": None,
        "sandbox_caveat": "Native read-only sandbox is not hermetic host-read isolation.",
    }
    _private_json(directory / "attempt.json", receipt)
    return receipt


def _exact(expected: Any, actual: Any) -> bool:
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict):
        return expected.keys() == actual.keys() and all(
            _exact(value, actual[key]) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(
            _exact(left, right) for left, right in zip(expected, actual, strict=True)
        )
    return expected == actual


def score_answer(text: str, expected: dict[str, Any], *, completed: bool) -> dict[str, Any]:
    answer = None
    error = None
    try:
        answer = strict_json(text)
        if not isinstance(answer, dict):
            raise ValueError("answer is not a JSON object")
    except ValueError as exc:
        error = f"malformed_or_refusal: {exc}"
    fields = {
        key: {
            "expected": value,
            "actual": answer.get(key) if isinstance(answer, dict) else None,
            "present": isinstance(answer, dict) and key in answer,
            "exact": bool(
                completed
                and isinstance(answer, dict)
                and key in answer
                and _exact(value, answer[key])
            ),
        }
        for key, value in expected.items()
    }
    keys_exact = isinstance(answer, dict) and set(answer) == set(expected)
    return {
        "passed": bool(
            completed
            and error is None
            and keys_exact
            and all(row["exact"] for row in fields.values())
        ),
        "execution_completed": completed,
        "error": error if error else (None if completed else "missing_or_failed_execution"),
        "keys_exact": keys_exact,
        "fields": fields,
    }


def score_run(manifest: dict[str, Any], plan: dict[str, Any], output: Path) -> dict[str, Any]:
    tasks = {task["task_id"]: task for task in manifest["tasks"]}
    attempts = []
    for call in plan["calls"]:
        directory = output / "attempts" / call["attempt_id"]
        receipt_path = directory / "attempt.json"
        try:
            receipt = strict_json(receipt_path.read_text()) if receipt_path.is_file() else {}
            if not isinstance(receipt, dict):
                raise ValueError("receipt is not an object")
        except ValueError as exc:
            receipt = {"status": "invalid_receipt", "receipt_error": str(exc)}
        events = receipt.get("events")
        if not isinstance(events, dict):
            # An interrupted controller can leave raw usage without its final
            # receipt. Count that usage, but never promote the slot to success.
            events = read_events(directory / "cli" / "stdout.jsonl")
        process = receipt.get("process")
        if not isinstance(process, dict):
            process_path = directory / "cli" / "process.json"
            try:
                process = strict_json(process_path.read_text()) if process_path.is_file() else {}
                if not isinstance(process, dict):
                    process = {}
            except ValueError:
                process = {}
        last = directory / "last-message.txt"
        text = last.read_text(encoding="utf-8", errors="replace") if last.is_file() else ""
        completed = receipt.get("status") == "completed"
        attempts.append(
            {
                **call,
                "status": receipt.get("status", "missing"),
                "score": score_answer(
                    text, tasks[call["task_id"]]["expected_answer"], completed=completed
                ),
                "tokens": events.get("tokens", {}),
                "elapsed_seconds": process.get("elapsed_seconds"),
                "reported_truncation": events.get("reported_truncation"),
                "native_retry_notifications": len(events.get("native_retry_notifications", [])),
                "reported_elapsed_limits_truncation": events.get(
                    "reported_elapsed_limits_truncation", []
                ),
                "tool_event_count": len(events.get("tool_events", [])),
                "receipt_error": receipt.get("receipt_error"),
            }
        )
    arms = {}
    for arm in manifest["arms"]:
        rows = [row for row in attempts if row["arm"] == arm]
        totals = {}
        for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
            values = [row["tokens"].get(key) for row in rows]
            known = [value for value in values if type(value) is int]
            totals[key] = {
                "total": sum(known) if len(known) == len(values) else None,
                "reported_sum": sum(known) if known else None,
                "unknown_attempts": len(values) - len(known),
            }
        arms[arm] = {
            "scheduled_attempts": len(rows),
            "completed_attempts": sum(row["status"] == "completed" for row in rows),
            "passed_attempts": sum(row["score"]["passed"] for row in rows),
            "missing_attempts": sum(row["status"] == "missing" for row in rows),
            "truncation_reported_attempts": sum(row["reported_truncation"] is True for row in rows),
            "native_retry_notifications": sum(row["native_retry_notifications"] for row in rows),
            "tokens": totals,
            "field_correct": sum(
                field["exact"] for row in rows for field in row["score"]["fields"].values()
            ),
            "field_denominator": sum(len(row["score"]["fields"]) for row in rows),
        }
    return {
        "schema": SCHEMA,
        "exploratory": True,
        "unique_tasks": len(tasks),
        "scoring": "strict JSON field values and types; malformed/refusal/failed/missing attempts fail",
        "arms": arms,
        "tasks": [
            {
                "task_id": identity,
                "repeats": [
                    {
                        "repeat": repeat,
                        "attempts": [
                            row
                            for row in attempts
                            if row["task_id"] == identity and row["repeat"] == repeat
                        ],
                    }
                    for repeat in range(1, manifest["repeats"] + 1)
                ],
            }
            for identity in tasks
        ],
        "monetary_cost_usd": None,
        "caveats": plan["caveats"],
    }


def run_subscription(
    manifest: dict[str, Any], plan: dict[str, Any], manifest_path: Path, output: Path
) -> int:
    if any(part.is_symlink() for part in (output, *output.parents)):
        raise ValueError("owned output directory must not follow symlinks")
    for task in manifest["tasks"]:
        root = Path(task["source_root"]).resolve()
        if output == root or root in output.parents:
            raise ValueError("never write attempt artifacts inside an immutable source root")
    manifest_bytes = manifest_path.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != plan["manifest_sha256"]:
        raise ValueError("manifest changed after planning")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(output, 0o700)
    with (output / "manifest.json").open("xb") as stream:
        stream.write(manifest_bytes)
    os.chmod(output / "manifest.json", 0o600)
    _private_json(output / "plan.json", plan)
    env = _scrub_env(dict(os.environ))
    tasks = {task["task_id"]: task for task in manifest["tasks"]}
    started = time.monotonic()
    run: dict[str, Any] = {
        "started_at_utc": utc_now(),
        "manifest_path": str(manifest_path),
        "manifest_sha256": plan["manifest_sha256"],
        "exploratory": True,
        "attempted_calls": 0,
        "launched_calls": 0,
        "quota_batches": [],
        "status": "stopped",
        "quota_snapshots_private": True,
    }
    try:
        if not CODEX_EXECUTABLE.is_file():
            raise ValueError(f"subscription Codex executable unavailable: {CODEX_EXECUTABLE}")
        run["codex_executable"] = str(CODEX_EXECUTABLE)
        run["codex_executable_sha256"] = file_sha256(CODEX_EXECUTABLE)
        for name, arguments in (("version", ["--version"]), ("login", ["login", "status"])):
            proc = _logged_process(
                [str(CODEX_EXECUTABLE), *arguments],
                output / "preflight" / name,
                cwd=output,
                env=env,
                timeout=30,
            )
            if proc["status"] != "exited" or proc.get("returncode") != 0:
                raise ValueError(f"subscription {name} preflight failed; inspect private logs")
            stdout = (output / "preflight" / name / "stdout.jsonl").read_text(errors="replace")
            stderr = (output / "preflight" / name / "stderr.log").read_text(errors="replace")
            if name == "login" and "logged in using chatgpt" not in (stdout + stderr).casefold():
                raise ValueError(
                    "Codex must be logged in using ChatGPT; API-key authentication is refused"
                )
            if name == "version":
                run["codex_version"] = stdout.strip()
        for start in range(0, len(plan["calls"]), 2):
            batch_number = start // 2 + 1
            quota_dir = output / "quota" / f"{batch_number:04d}"
            proc = _logged_process(
                ["omp", "usage", "--provider", "openai-codex", "--json", "--redact"],
                quota_dir,
                cwd=output,
                env=dict(os.environ),
                timeout=45,
            )
            if proc["status"] != "exited" or proc.get("returncode") != 0:
                raise ValueError("quota probe unavailable; fail-closed before the next pair")
            quota = weekly_quota(strict_json((quota_dir / "stdout.jsonl").read_text()))
            run["quota_batches"].append({"batch": batch_number, **quota})
            verify_sources(manifest)
            for call in plan["calls"][start : start + 2]:
                if run["attempted_calls"] >= manifest["max_calls"]:
                    raise ValueError("hard max_calls reached")
                run["attempted_calls"] += 1
                receipt = run_attempt(manifest, tasks[call["task_id"]], call, output, env)
                run["launched_calls"] += int("pid" in receipt["process"])
                if receipt["process"]["status"] == "cleanup_failed":
                    raise ValueError("process-tree cleanup failed; do not launch another attempt")
                if receipt["process"]["status"] == "interrupted":
                    raise KeyboardInterrupt
                if receipt["events"]["model_reroute_reported"]:
                    raise ValueError(
                        "native CLI reported a model substitution; no continuation or fallback"
                    )
        run["status"] = "finished"
    except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as exc:
        run["stop_reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        run["finished_at_utc"] = utc_now()
        run["elapsed_seconds"] = time.monotonic() - started
        _private_json(output / "run.json", run)
        scores = score_run(manifest, plan, output)
        _private_json(output / "scores.json", scores)
    print(
        json.dumps(
            {
                "output_dir": str(output),
                "status": run["status"],
                "launched_calls": run["launched_calls"],
                "stop_reason": run.get("stop_reason"),
                "arms": scores["arms"],
            },
            indent=2,
        )
    )
    return 0 if run["status"] == "finished" else 1


def _interrupt(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="Private explicit task/prompt/answer manifest")
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="New owned private directory for a run, or existing directory for --score-only",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--run-subscription",
        action="store_true",
        help="Explicitly execute native Codex on the included ChatGPT subscription",
    )
    mode.add_argument(
        "--score-only",
        action="store_true",
        help="Score all scheduled slots, including missing attempts; never call a model",
    )
    args = parser.parse_args(argv)
    try:
        path = args.manifest.expanduser().resolve()
        output = args.output_dir.expanduser().resolve()
        manifest, digest = load_manifest(path)
        plan = make_plan(manifest, digest)
        if args.score_only:
            saved_plan = strict_json((output / "plan.json").read_text())
            if (
                saved_plan.get("manifest_sha256") != digest
                or saved_plan.get("calls") != plan["calls"]
            ):
                raise ValueError("scoring manifest or schedule differs from the retained run plan")
            print(json.dumps(score_run(manifest, plan, output), indent=2, ensure_ascii=False))
        elif args.run_subscription:
            old_umask = os.umask(0o077)
            previous = signal.signal(signal.SIGTERM, _interrupt)
            try:
                return run_subscription(manifest, plan, path, output)
            finally:
                signal.signal(signal.SIGTERM, previous)
                os.umask(old_umask)
        else:
            verify_sources(manifest)
            print(
                json.dumps(
                    {
                        **plan,
                        "mode": "dry-run",
                        "output_dir": str(output),
                        "would_invoke": _argv(
                            Path("<empty-per-attempt-cwd>"), Path("<private-last-message>")
                        ),
                    },
                    indent=2,
                )
            )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
