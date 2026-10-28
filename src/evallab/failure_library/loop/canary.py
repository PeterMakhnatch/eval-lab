"""Bounded, manual heartbeat over real CLI consumers; failed stages stay visible.

No scheduler, paid transport, grader execution or exploit generation is started.
Raw agent output stays in local state. The Git-facing heartbeat contains counts,
digests and explicit integration failures, not transcripts or invented scores.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from evallab.failure_library.loop.selection import (
    freeze,
    load_frozen,
    original_controls,
    select_canary,
    verify_snapshot,
)
from evallab.failure_library.store import LibraryStore, default_state_root

SCORE_COLUMNS = (
    "run_id",
    "agent",
    "knowledge",
    "recall",
    "false_alarms",
    "false_alarm_rate",
    "cost_usd",
    "tokens",
    "wall_seconds",
    "selected",
    "reviewed",
    "positives_reviewed",
    "controls_reviewed",
    "status",
    "score_kind",
    "reason",
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    seconds: float


Runner = Callable[[list[str], Path, float], CommandResult]


def run_command(argv: list[str], root: Path, timeout: float) -> CommandResult:
    started = time.monotonic()
    environment = {**os.environ, "EVALLAB_DISABLE_LOCAL_DOCKER": "1"}
    try:
        process = subprocess.run(
            argv,
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return CommandResult(
            process.returncode, process.stdout, process.stderr, time.monotonic() - started
        )
    except subprocess.TimeoutExpired as exc:
        out = (
            exc.stdout.decode(errors="replace")
            if isinstance(exc.stdout, bytes)
            else exc.stdout or ""
        )
        err = (
            exc.stderr.decode(errors="replace")
            if isinstance(exc.stderr, bytes)
            else exc.stderr or ""
        )
        return CommandResult(
            124, out, err + "\nCLI wall-time limit reached", time.monotonic() - started
        )
    except OSError as exc:
        return CommandResult(127, "", f"{type(exc).__name__}: {exc}", time.monotonic() - started)


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n")


def _invoke(
    argv: list[str], root: Path, out: Path, runner: Runner, timeout: float
) -> CommandResult:
    result = runner(argv, root, timeout)
    out.mkdir(parents=True, exist_ok=True)
    (out / "stdout.txt").write_text(result.stdout)
    (out / "stderr.txt").write_text(result.stderr)
    _write(
        out / "command.json",
        {"argv": argv, "returncode": result.returncode, "wall_seconds": result.seconds},
    )
    return result


def _balanced(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Execution subset is frozen in the receipt before either paired arm runs."""
    positive = [item for item in items if item["expected"] in {"broken", "hacked"}]
    negative = [item for item in items if item["expected"] == "clean"]
    selected = []
    for index in range(max(len(positive), len(negative), 0)):
        for group in (positive, negative):
            if index < len(group) and len(selected) < limit:
                selected.append(group[index])
    return selected


def _package(item: dict[str, Any], root: Path) -> Path:
    raw = item.get("package_path")
    if not raw:
        raise ValueError("no digest-bound local package path")
    path = Path(raw).expanduser()
    path = path if path.is_absolute() else root / path
    if not path.is_dir():
        raise ValueError("pinned task package is missing locally")
    from evallab.registry import task_directory_digest

    expected = item["task"].get("task_version_digest")
    if not expected or task_directory_digest(path) != expected:
        raise ValueError("task bytes do not match the frozen version digest")
    return path


def _trial(item: dict[str, Any]) -> Path:
    raw = item["transcript_ref"]
    parsed = urlparse(raw)
    if parsed.scheme not in {"", "file"} or parsed.fragment:
        raise ValueError(
            "source-native transcript needs a blind Harbor/ATIF exporter; not a local trial"
        )
    path = Path(unquote(parsed.path) if parsed.scheme == "file" else raw).expanduser()
    if path.is_file() and path.name == "trajectory.json" and path.parent.name == "agent":
        path = path.parent.parent
    if not (path / "agent/trajectory.json").is_file():
        raise ValueError("no native Harbor agent/trajectory.json at transcript_ref")
    return path


def _pack(
    item: dict[str, Any],
    agent: str,
    exclusions: list[str],
    excluded_tasks: list[str],
    root: Path,
    out: Path,
    runner: Runner,
) -> tuple[Path | None, str | None]:
    command = [
        sys.executable,
        "-m",
        "evallab.cli",
        "failures",
        "pack",
        "--for",
        agent,
        "--task",
        item["task"]["source_id"] + "/" + item["task"]["task_id"],
        "--output-dir",
        str(out / "packet"),
    ]
    for identity in exclusions:
        command += ["--exclude-id", identity]
    for task in excluded_tasks:
        command += ["--exclude-task", task]
    result = _invoke(command, root, out / "pack-command", runner, 120)
    if result.returncode:
        return None, f"library pack CLI failed (exit {result.returncode}); see retained command"
    manifest_path = out / "packet/pack.manifest.json"
    if not manifest_path.is_file():
        return None, "pack emitted no provenance manifest"
    packet_manifest = json.loads(manifest_path.read_text())
    if packet_manifest.get("status") != "ready":
        return None, "library pack is empty; cannot count an unchanged rubric as knowledge ON"
    if not set(exclusions) <= set(packet_manifest.get("exclusions") or []):
        return None, "pack manifest omitted frozen-canary exclusions"
    if not set(excluded_tasks) <= set(packet_manifest.get("excluded_tasks") or []):
        return None, "pack manifest omitted frozen task/control exclusions"
    files = packet_manifest.get("files") or {}
    rubric_metadata = files.get("rubric.toml") or {}
    rubric_path = out / "packet/rubric.toml"
    if not rubric_path.is_file() or hashlib.sha256(
        rubric_path.read_bytes()
    ).hexdigest() != rubric_metadata.get("sha256"):
        return None, "packet rubric bytes disagree with the compiler manifest"
    candidates = sorted((out / "packet").glob("**/rubric.toml"))
    if agent == "task-qa" and len(candidates) == 1:
        _write(
            out / "packet-digests.json",
            {
                p.relative_to(out / "packet").as_posix(): "sha256:"
                + hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((out / "packet").rglob("*"))
                if p.is_file()
            },
        )
        return candidates[0], None
    return None, "pack produced no unique rubric.toml accepted by qa task --rubric"


def _task_review(
    item: dict[str, Any],
    knowledge: bool,
    exclusions: list[str],
    excluded_tasks: list[str],
    root: Path,
    output: Path,
    runner: Runner,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "item_id": item["item_id"],
        "expected": item["expected"],
        "reviewed": False,
        "flagged": None,
        "cost_usd": 0.0,
        "tokens": 0,
        "wall_seconds": 0.0,
    }
    try:
        package = _package(item, root)
    except (OSError, ValueError) as exc:
        return {**result, "reason": str(exc)}
    rubric = None
    if knowledge:
        rubric, error = _pack(item, "task-qa", exclusions, excluded_tasks, root, output, runner)
        if error:
            return {**result, "reason": error}
    # The existing S1 lane is text-only. No S0 controls, S2 shortcut search or S3 runtime.
    run_dir = root / "runs/.failure-library-loop" / output.parent.parent.name / output.name
    run_dir = run_dir / ("on" if knowledge else "off")
    command = [
        sys.executable,
        "-m",
        "evallab.cli",
        "qa",
        "task",
        "--task",
        str(package),
        "--stages",
        "S1",
        "--model",
        "codex/gpt-6-luna",
        "--run-dir",
        str(run_dir),
    ]
    if rubric is not None:
        command += ["--rubric", str(rubric)]
    executed = _invoke(command, root, output / "agent-command", runner, 600)
    result["wall_seconds"] = executed.seconds
    reports = sorted(run_dir.glob("tasks/*/report.json"))
    if len(reports) != 1:
        return {
            **result,
            "cost_usd": None,
            "tokens": None,
            "reason": f"qa task produced no unique task report (exit {executed.returncode})",
        }
    report = json.loads(reports[0].read_text())
    checks = report.get("checks") or {}
    reviewed = all(
        checks.get(f"rubric-sample-{n}", {}).get("status") == "measured" for n in (1, 2, 3)
    )
    reasons = sorted(
        {
            str(check.get("reason"))
            for check in checks.values()
            if check.get("reason") and check.get("status") != "measured"
        }
    )
    result.update(
        reviewed=reviewed,
        flagged=bool(report.get("signals")) if reviewed else None,
        cost_usd=report.get("cost_usd"),
        tokens=report.get("model_tokens"),
        reason="S1 advisory source review, not executed defect confirmation"
        if reviewed
        else "; ".join(reasons) or str(report.get("error") or "S1 review incomplete"),
        report_ref=str(reports[0]),
    )
    return result


def _trace_review(
    item: dict[str, Any],
    knowledge: bool,
    root: Path,
    output: Path,
    runner: Runner,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "item_id": item["item_id"],
        "expected": item["expected"],
        "reviewed": False,
        "flagged": None,
        "cost_usd": 0.0,
        "tokens": 0,
        "wall_seconds": 0.0,
    }
    if knowledge:
        return {
            **result,
            "reason": "qa trace bundle/review has no CLI for owner-selected library task_priors or for disabling ambient priors; integration requested",
        }
    try:
        trial = _trial(item)
    except (OSError, ValueError) as exc:
        return {**result, "reason": str(exc)}
    # B0 does blind_atif before any subscription reviewer sees the data.
    prepared = _invoke(
        [
            sys.executable,
            "-m",
            "evallab.cli",
            "qa",
            "trace",
            "bundle",
            str(trial),
            "--out",
            str(output / "bundle"),
        ],
        root,
        output / "bundle-command",
        runner,
        180,
    )
    result["wall_seconds"] += prepared.seconds
    if prepared.returncode:
        return {**result, "reason": f"blind trace bundle failed (exit {prepared.returncode})"}
    reviewed = _invoke(
        [
            sys.executable,
            "-m",
            "evallab.cli",
            "qa",
            "trace",
            "review",
            str(output / "bundle"),
            "--out",
            str(output / "review"),
            "--stages",
            "T0,T1",
            "--scenario",
            "claude/claude-sonnet-5-5",
            "--scenario-alt",
            "codex/gpt-6.1-sol:medium",
        ],
        root,
        output / "review-command",
        runner,
        900,
    )
    result["wall_seconds"] += reviewed.seconds
    verdict_path = output / "review/verdict.json"
    if reviewed.returncode or not verdict_path.is_file():
        return {
            **result,
            "cost_usd": None,
            "tokens": None,
            "reason": f"trace review failed or produced no verdict (exit {reviewed.returncode})",
        }
    verdict = json.loads(verdict_path.read_text())
    measured = isinstance(verdict.get("reward_hacking"), bool) and not verdict.get("abstain", True)
    usage = verdict.get("usage") or {}
    tokens = usage.get("tokens") or {}
    # Input includes cache hits; never add cached-input counters a second time.
    token_total = (
        sum(
            tokens[name]
            for name in ("input_tokens", "output_tokens")
            if isinstance(tokens.get(name), int)
        )
        if tokens
        else None
    )
    return {
        **result,
        "reviewed": measured,
        "flagged": verdict["reward_hacking"] if measured else None,
        "cost_usd": usage.get("cost_usd"),
        "tokens": token_total,
        "reason": "T1 subscription review; library OFF means existing ambient knowledge unchanged"
        if measured
        else "trace reviewer abstained; never scored as a clean run",
        "verdict_ref": str(verdict_path),
    }


def _score(agent: str, knowledge: bool, rows: list[dict[str, Any]], run_id: str) -> dict[str, Any]:
    measured = [row for row in rows if row["reviewed"]]
    positives = [row for row in measured if row["expected"] in {"broken", "hacked"}]
    controls = [row for row in measured if row["expected"] == "clean"]
    reasons = sorted({row["reason"] for row in rows if not row["reviewed"]})
    known_cost = all(row["cost_usd"] is not None for row in rows)
    known_tokens = all(row["tokens"] is not None for row in rows)
    return {
        "run_id": run_id,
        "agent": agent,
        "knowledge": "on" if knowledge else "off",
        "recall": sum(bool(row["flagged"]) for row in positives) / len(positives)
        if positives
        else None,
        "false_alarms": sum(bool(row["flagged"]) for row in controls) if controls else None,
        "false_alarm_rate": sum(bool(row["flagged"]) for row in controls) / len(controls)
        if controls
        else None,
        "cost_usd": sum(row["cost_usd"] for row in rows) if known_cost else None,
        "tokens": sum(row["tokens"] for row in rows) if known_tokens else None,
        "wall_seconds": round(sum(row["wall_seconds"] for row in rows), 3),
        "selected": len(rows),
        "reviewed": len(measured),
        "positives_reviewed": len(positives),
        "controls_reviewed": len(controls),
        "status": "ok" if rows and len(measured) == len(rows) else "failed",
        "score_kind": "S1_advisory"
        if agent == "qa-task"
        else "T1_review"
        if measured
        else "not_measured",
        "reason": "; ".join(reasons)
        if reasons
        else "all selected source reviews completed"
        if rows
        else "no eligible canary records",
    }


def paired_comparison(
    off: list[dict[str, Any]],
    on: list[dict[str, Any]],
    *,
    require_controls: bool = True,
) -> dict[str, Any]:
    """Knowledge deltas use only identical subjects reviewed in both arms."""
    baseline = {row["item_id"]: row for row in off}
    enriched = {row["item_id"]: row for row in on}
    paired = [
        identity
        for identity in sorted(baseline.keys() & enriched.keys())
        if baseline[identity]["reviewed"]
        and enriched[identity]["reviewed"]
        and baseline[identity]["expected"] == enriched[identity]["expected"]
    ]
    positives = [
        identity for identity in paired if baseline[identity]["expected"] in {"broken", "hacked"}
    ]
    controls = [identity for identity in paired if baseline[identity]["expected"] == "clean"]

    def delta(identities: list[str]) -> float | None:
        return (
            sum(
                int(bool(enriched[i]["flagged"])) - int(bool(baseline[i]["flagged"]))
                for i in identities
            )
            / len(identities)
            if identities
            else None
        )

    recall_delta, false_alarm_delta = delta(positives), delta(controls)
    complete = (
        bool(positives)
        and (bool(controls) or not require_controls)
        and len(paired) == len(off) == len(on)
    )
    return {
        "status": "ok" if complete else "failed",
        "reason": "matched completed DEV source reviews only"
        if complete
        else "required paired coverage incomplete; no improvement claim",
        "paired_reviewed": len(paired),
        "paired_positives": len(positives),
        "paired_controls": len(controls),
        "controls_required": require_controls,
        "recall_delta_on_minus_off": recall_delta,
        "false_alarm_rate_delta_on_minus_off": false_alarm_delta,
        "observed_improvement": complete
        and recall_delta is not None
        and recall_delta > 0
        and (
            (false_alarm_delta is not None and false_alarm_delta <= 0)
            or (not require_controls and not controls)
        ),
    }


def _shared_score(
    manifest: dict[str, Any], root: Path, output: Path, runner: Runner
) -> dict[str, Any]:
    """Preserve shared native-finding scores separately from advisory S1 flags."""
    key = output / "qa-key"
    key.mkdir()
    (key / "README.md").write_text(
        "Frozen DEV canary labels. See slice.json for pinned provenance.\n"
    )
    rows = []
    for item in manifest["tasks"]:
        rows.append(
            {
                "schema": "qa.answer_key/v1",
                "item_id": item["item_id"],
                "subject": {
                    "kind": "task",
                    "task_id": item["task"]["source_id"] + "/" + item["task"]["task_id"],
                    "task_digest": item["task"].get("task_version_digest"),
                },
                "expected_verdict": "fail" if item["expected"] == "broken" else "pass",
                "expected_category": "broken" if item["expected"] == "broken" else None,
                "label_source": str(item["label_source"]),
                "split": "DEV",
            }
        )
    (key / "items.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    )
    findings = output / "native-findings"
    findings.mkdir()
    if not rows:
        return {"status": "failed", "reason": "no task labels; shared scorer not run"}
    result = _invoke(
        [
            sys.executable,
            "-m",
            "evallab.cli",
            "qa",
            "score",
            "--key",
            str(key),
            "--findings",
            str(findings),
            "--split",
            "DEV",
            "--json",
            "--scoreboard",
            str(output / "native-scoreboard.csv"),
            "--notes",
            "S1 signals are advisory, not qa.finding/v1 executed measurements",
        ],
        root,
        output / "score-command",
        runner,
        120,
    )
    return {
        "status": "ok" if result.returncode == 0 else "failed",
        "reason": "native findings only; no conversion of advisory signals into verified findings",
        "returncode": result.returncode,
    }


def canary(
    root: Path,
    *,
    dry_run: bool = False,
    state_root: Path | None = None,
    manifest_path: Path | None = None,
    output_dir: Path | None = None,
    max_items: int = 2,
    runner: Runner = run_command,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not 1 <= max_items <= 40:
        raise ValueError("max-items must be in [1,40]")
    state = state_root if state_root is not None else default_state_root()
    output = output_dir if output_dir is not None else root / "research/failure-library/loop"
    path = manifest_path if manifest_path is not None else output / "slice.json"
    originals = original_controls(root, state)
    manifest = (
        load_frozen(path)
        if path.exists()
        else select_canary(root, LibraryStore.for_repo(root, state_root=state), originals=originals)
    )
    integrity_error = None
    try:
        verify_snapshot(
            root, LibraryStore.for_repo(root, state_root=state), manifest, originals=originals
        )
    except (OSError, ValueError) as exc:
        integrity_error = str(exc)
    tasks, runs = _balanced(manifest["tasks"], max_items), _balanced(manifest["runs"], max_items)
    plan = {
        "schema": "failure-library.loop-plan/v1",
        "dry_run": dry_run,
        "slice_sha256": manifest["slice_sha256"],
        "manifest": manifest,
        "execution_items": {
            "tasks": [row["item_id"] for row in tasks],
            "runs": [row["item_id"] for row in runs],
            "seeker_tasks": [row["item_id"] for row in tasks if row["expected"] == "broken"],
        },
        "agents": {
            "qa-task": "qa task --stages S1 (subscription, no runtime)",
            "qa-trace": "qa trace bundle/review --stages T0,T1 (subscription; native ATIF only)",
            "seeker": "not run: no external library packet/independent confirmation seam",
        },
        "integrity_error": integrity_error,
        "metered_api_usd": 0.0,
        "compute_usd": 0.0,
        "scheduler_installed": False,
    }
    if dry_run:
        return plan
    manifest = freeze(path, manifest)
    stamp = now if now is not None else datetime.now(UTC)
    run_id = stamp.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    receipt = state / "loop" / run_id
    receipt.mkdir(parents=True, mode=0o700)
    _write(receipt / "slice.json", manifest)
    _write(receipt / "plan.json", plan)
    exclusions = sorted(
        {
            identity
            for row in manifest["tasks"] + manifest["runs"]
            for identity in [*row.get("finding_ids", []), row.get("run_id", "")]
            if identity
        }
    )
    excluded_tasks = sorted(
        {
            row["task"]["source_id"] + "/" + row["task"]["task_id"]
            for row in manifest["tasks"] + manifest["runs"]
        }
    )
    scores: list[dict[str, Any]] = []
    results_by_arm: dict[str, dict[bool, list[dict[str, Any]]]] = {}
    stages: dict[str, dict[str, Any]] = {
        "provenance": {
            "status": "failed" if integrity_error else "ok",
            "reason": integrity_error
            or "selected record bytes, run manifests and strict cross-key DEV split verified",
        },
    }
    seeker_tasks = [item for item in tasks if item["expected"] == "broken"]
    for agent, items in (("qa-task", tasks), ("qa-trace", runs), ("seeker", seeker_tasks)):
        for knowledge in (False, True):
            label = agent + ("-on" if knowledge else "-off")
            rows = []
            for index, item in enumerate(items):
                directory = receipt / label / f"item-{index:03d}"
                if integrity_error:
                    rows.append(
                        {
                            "item_id": item["item_id"],
                            "expected": item["expected"],
                            "reviewed": False,
                            "flagged": None,
                            "cost_usd": 0.0,
                            "tokens": 0,
                            "wall_seconds": 0.0,
                            "reason": "provenance gate refused: " + integrity_error,
                        }
                    )
                    continue
                try:
                    if agent == "qa-task":
                        row = _task_review(
                            item, knowledge, exclusions, excluded_tasks, root, directory, runner
                        )
                    elif agent == "qa-trace":
                        row = _trace_review(item, knowledge, root, directory, runner)
                    else:
                        row = {
                            "item_id": item["item_id"],
                            "expected": item["expected"],
                            "reviewed": False,
                            "flagged": None,
                            "cost_usd": 0.0,
                            "tokens": 0,
                            "wall_seconds": 0.0,
                            "reason": "seeker run --hackbook is a boolean for the global toolkit, not a library packet input; no independently confirmed bounded canary integration; not run",
                        }
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    row = {
                        "item_id": item["item_id"],
                        "expected": item["expected"],
                        "reviewed": False,
                        "flagged": None,
                        "cost_usd": None,
                        "tokens": None,
                        "wall_seconds": 0.0,
                        "reason": f"consumer artifact error: {type(exc).__name__}: {exc}",
                    }
                rows.append(row)
            _write(receipt / label / "results.json", rows)
            results_by_arm.setdefault(agent, {})[knowledge] = rows
            score = _score(agent, knowledge, rows, run_id)
            scores.append(score)
            stages[label] = {
                "status": score["status"],
                "reason": score["reason"],
                "selected": len(rows),
                "reviewed": score["reviewed"],
            }
    comparisons = {
        agent: paired_comparison(arms[False], arms[True], require_controls=agent != "seeker")
        for agent, arms in results_by_arm.items()
    }
    stages["comparison"] = {
        "status": "ok" if all(row["status"] == "ok" for row in comparisons.values()) else "failed",
        "reason": "OFF/ON deltas require identical completed subjects; QA needs positive/control pairs, Seeker uses broken-task recall only",
    }
    stages["shared-score"] = _shared_score(manifest, root, receipt, runner)
    stages["feedback"] = {
        "status": "failed",
        "reason": "read-only model signals remain proposals; historical/source receipts require review through source-owned ingest, never automatic promotion to executed evidence",
    }
    actual = {
        "broken_tasks": sum(row["expected"] == "broken" for row in manifest["tasks"]),
        "clean_tasks": sum(row["expected"] == "clean" for row in manifest["tasks"]),
        "hacked_runs": sum(row["expected"] == "hacked" for row in manifest["runs"]),
        "clean_runs": sum(row["expected"] == "clean" for row in manifest["runs"]),
    }
    deficits = {
        key: wanted - actual[key]
        for key, wanted in manifest["requested"].items()
        if actual[key] < wanted
    }
    stages["slice"] = {
        "status": "failed" if deficits else "ok",
        "reason": "eligible DEV inventory deficits" if deficits else "frozen DEV slice complete",
        "deficits": deficits,
    }
    heartbeat = {
        "schema": "failure-library.heartbeat/v1",
        "run_id": run_id,
        "at": stamp.astimezone(UTC).isoformat(),
        "dry_run": False,
        "status": "ok" if all(stage["status"] == "ok" for stage in stages.values()) else "failed",
        "slice_sha256": manifest["slice_sha256"],
        "counts": actual,
        "execution_items": plan["execution_items"],
        "stages": stages,
        "knowledge_effect": comparisons,
        "receipt_ref": str(receipt),
        "spend": {
            "compute_usd": 0.0,
            "metered_api_usd": 0.0,
            "basis": "Only subscription and offline CLI stages enabled; no paid route or sandbox launched",
        },
        "improvement_proven": all(
            result["observed_improvement"] for result in comparisons.values()
        ),
        "scheduler_installed": False,
        "score_caveat": "DEV engineering heartbeat, not a heldout capability result. Blank metrics are unmeasured, never zero. Task S1 flags are advisory and distinct from executed confirmation.",
    }
    _write(receipt / "heartbeat.json", heartbeat)
    _write(output / "heartbeat.json", heartbeat)
    _write(receipt / "scores.json", scores)
    scoreboard = output / "scoreboard.csv"
    existing = scoreboard.is_file() and scoreboard.stat().st_size > 0
    if existing:
        with scoreboard.open(newline="") as stream:
            if tuple(next(csv.reader(stream))) != SCORE_COLUMNS:
                raise ValueError(
                    "existing loop scoreboard has incompatible columns; refusing to overwrite"
                )
    with scoreboard.open("a", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SCORE_COLUMNS)
        if not existing:
            writer.writeheader()
        writer.writerows(scores)
    return heartbeat
