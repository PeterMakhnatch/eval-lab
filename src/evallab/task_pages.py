"""One ``harbor view`` page per task: every trusted trial plus every reader's verdict.

A task page is a merged viewer job named ``task-<id>`` (e.g. ``task-000792``)
holding every trial of that task from the trusted jobs. On top of the usual
overlay it adds, per trial:

- **flag dims** in ``verifier_result.rewards`` (``1`` = flagged, ``0`` = clean,
  absent = not decided), so the trial list and Outcomes can show them:
  ``copied.copy_check``, ``copied.rewardkit``, ``copied.laminar``,
  ``reward_hacking.analyze``, ``stuck_loop.laminar``, ``false_completion.laminar``,
  ``task_spec.analyze``;
- an ``analysis.md`` that the viewer's trial **Analysis** tab renders as Markdown:
  raw and gated reward, stop reason, the copied verdict from every source, the
  other reader checks, the task's ``harbor check``, our watch alerts, and a
  clickable Laminar link.

The page's job-level **Analysis** tab (``analysis.json``, Harbor's analyze schema)
lists every trial with one pass/fail badge per source and check, linking each
trial. Task health and solve tags come from the task's latest
``task-health-tags@1`` variant record.

Reader verdicts are read from a store laid out as
``<store>/<job>/<trial>/<reader>.json`` (``evallab.reader_verdict/v1``). Sources
are never written; the page is rebuilt whenever a member trial, a verdict or a
health record changes.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from evallab.harbor_view import (
    _iter_trial_dirs,
    _read_json,
    build_merged_job,
    compile_arm_pattern,
)

PAGE_PREFIX = "task-"
PAGE_RECORD = ".evallab-task-page.json"
PAGE_SCHEMA = "evallab.task_page/v1"
STORE_ENV = "EVALLAB_READERS_STORE"
#: The primary checkout is kept on main by the sync-primary agent; only read here.
DEFAULT_VARIANTS_DIR = Path.home() / "Developer" / "eval-lab" / "library" / "task-variants"
#: Jobs on the trusted Harbor 0.24 setup (native integrity in the reward).
DEFAULT_TRUSTED_GLOBS = ("HAR-168-*",)
#: Attempt label from the job name (``...-a1``, ``...-a2-infra-r1``).
ATTEMPT_REGEX = r"-(?P<arm>a\d+(?:-[a-z0-9-]+)?)$"
READERS = ("laminar_signals", "harbor_analyze")
#: Only analyzer verdicts produced from blinded inputs (no Eval Lab labels or
#: integrity machinery in the copy) are independent enough to show.
ANALYZE_INPUT_POLICY = "evallab.reader_input/blind-v2"
#: (reader, check) -> flag dim name. Copy-type checks first: the page lists them in this order.
READER_DIMS = {
    ("laminar_signals", "copied"): "copied.laminar",
    ("harbor_analyze", "reward_hacking"): "reward_hacking.analyze",
    ("laminar_signals", "stuck_loop"): "stuck_loop.laminar",
    ("laminar_signals", "false_completion"): "false_completion.laminar",
    ("harbor_analyze", "task_specification"): "task_spec.analyze",
}
READER_LABELS = {
    "laminar_signals": "Laminar Signal",
    "harbor_analyze": "harbor analyze",
}
READER_SLUGS = {
    "laminar_signals": "laminar_signal",
    "harbor_analyze": "harbor_analyze",
}
#: Checks whose ``true`` means "this trial copied / gamed its pass".
COPY_CHECKS = (
    ("laminar_signals", "copied"),
    ("harbor_analyze", "reward_hacking"),
)


def page_name(task_name: str) -> str:
    """``task-000792`` for ``.../format-code-task-000792``; else the last path part."""
    tail = task_name.rsplit("/", 1)[-1]
    digits = re.search(r"(\d+)$", tail)
    return PAGE_PREFIX + (digits.group(1) if digits else tail)


def trusted_trials(
    jobs: Iterable[Path], globs: Iterable[str] = DEFAULT_TRUSTED_GLOBS
) -> dict[str, list[tuple[Path, Path]]]:
    """``task_name -> [(job_dir, trial_dir)]`` for trials of trusted jobs."""
    patterns = tuple(globs)
    by_task: dict[str, list[tuple[Path, Path]]] = {}
    for job in jobs:
        if not any(fnmatch.fnmatchcase(job.name, pattern) for pattern in patterns):
            continue
        for trial in _iter_trial_dirs(job):
            result = _read_json(trial / "result.json") or {}
            task = result.get("task_name")
            if isinstance(task, str) and task:
                by_task.setdefault(task, []).append((job, trial))
    return by_task


def default_store() -> Path:
    """Reader verdict store: ``$EVALLAB_READERS_STORE`` or beside the viewer state."""
    configured = os.environ.get(STORE_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Library" / "Application Support" / "evallab" / "readers"


# ---------------------------------------------------------------------------
# Evidence per trial
# ---------------------------------------------------------------------------


def _rewards(result: dict[str, Any]) -> dict[str, Any]:
    verifier = result.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    return rewards if isinstance(rewards, dict) else {}


def _rewardkit_fired(trial: Path) -> list[dict[str, str]]:
    """Integrity rules RewardKit fired inside the verifier (name + reasoning)."""
    details = _read_json(trial / "verifier" / "reward-details.json") or {}
    integrity = details.get("integrity")
    fired: list[dict[str, str]] = []
    for component in (integrity or {}).get("components") or []:
        for criterion in ((component.get("detail") or {}).get("criteria")) or []:
            if criterion.get("value") == 0:
                fired.append(
                    {
                        "rule": str(criterion.get("name")),
                        "reason": str(criterion.get("reasoning") or "")[:300],
                    }
                )
    return fired


def _processed(job: Path, trial: Path) -> dict[str, Any]:
    return _read_json(job / "processed" / f"trial-{trial.name}.json") or {}


def _watch_rules(job: Path, trial: Path) -> list[str]:
    status = _read_json(job / "watch" / "status.json") or {}
    for row in status.get("trials") or []:
        if isinstance(row, dict) and row.get("trial") == trial.name:
            return sorted({str(a.get("rule")) for a in row.get("open_alerts") or []})
    return []


def _verdicts(store: Path, job: Path, trial: Path) -> dict[str, dict[str, Any]]:
    found = {}
    for reader in READERS:
        verdict = _read_json(store / job.name / trial.name / f"{reader}.json")
        if verdict is None:
            continue
        if reader == "harbor_analyze" and verdict.get("input_policy") != ANALYZE_INPUT_POLICY:
            continue
        found[reader] = verdict
    return found


def trial_evidence(store: Path, job: Path, trial: Path) -> dict[str, Any]:
    """Everything the page shows for one trial, from files only."""
    result = _read_json(trial / "result.json") or {}
    rewards = _rewards(result)
    processed = _processed(job, trial)
    copied_ours = any(
        isinstance(flag, dict) and flag.get("kind") == "copied_code"
        for flag in processed.get("taint") or []
    )
    exception = result.get("exception_info") or {}
    verdicts = _verdicts(store, job, trial)
    laminar = verdicts.get("laminar_signals") or {}
    task_name = str(result.get("task_name") or "")
    task_check = (
        _read_json(store / "_tasks" / task_name / "harbor_check.json") if task_name else None
    )
    return {
        "job": job.name,
        "trial": trial.name,
        "reward": rewards.get("reward"),
        "integrity": rewards.get("integrity"),
        "reward_gated": rewards.get("reward_gated"),
        "stop_reason": processed.get("stop_reason")
        or (exception.get("exception_type") if isinstance(exception, dict) else None),
        "exception": exception.get("exception_type") if isinstance(exception, dict) else None,
        "copy_check": copied_ours if processed else None,
        "copy_check_note": next(
            (
                f"{flag.get('matched_lines')} of {flag.get('added_lines')} added lines match "
                "code read from outside the base checkout"
                for flag in processed.get("taint") or []
                if isinstance(flag, dict) and flag.get("kind") == "copied_code"
            ),
            "",
        ),
        "rewardkit_fired": _rewardkit_fired(trial),
        "watch_rules": _watch_rules(job, trial),
        "verdicts": verdicts,
        "laminar_url": laminar.get("trace_url"),
        "task_check": task_check,
    }


def flag_dims(evidence: dict[str, Any]) -> dict[str, int]:
    """Numeric flag dims (1 = flagged) for every decided source."""
    dims: dict[str, int] = {}
    if evidence["copy_check"] is not None:
        dims["copied.copy_check"] = int(bool(evidence["copy_check"]))
    if evidence["integrity"] is not None:
        dims["copied.rewardkit"] = int(
            any(f["rule"].startswith("copy_check") for f in evidence["rewardkit_fired"])
        )
    for (reader, check), dim in READER_DIMS.items():
        value = ((evidence["verdicts"].get(reader) or {}).get("checks") or {}).get(check)
        if value is not None:
            dims[dim] = int(bool(value))
    return dims


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _verdict_word(value: Any, *, flagged: str = "flagged") -> str:
    if value is None:
        return "not decided"
    return f"**{flagged}**" if value else "clean"


def _cell(text: Any) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")[:300]


def render_trial_markdown(evidence: dict[str, Any], health: dict[str, Any]) -> str:
    lines = [f"## {evidence['trial']}", ""]
    if evidence["laminar_url"]:
        lines += [f"**[Open this trial in Laminar]({evidence['laminar_url']})**", ""]
    else:
        lines += ["Laminar: no trace recorded for this trial yet.", ""]
    lines += [
        "| | |",
        "|---|---|",
        f"| raw reward | {evidence['reward']} |",
        f"| integrity | {evidence['integrity']} |",
        f"| gated reward | {evidence['reward_gated']} |",
        f"| stop reason | {_cell(evidence['stop_reason'])} |",
        f"| task health | {_cell(', '.join(health.get('tags') or []) or 'no record')} |",
        "",
        "### Copied? (every source)",
        "",
        "| source | verdict | evidence |",
        "|---|---|---|",
        f"| copy check v1 (Eval Lab) | {_verdict_word(evidence['copy_check'], flagged='copied')} "
        f"| {_cell(evidence['copy_check_note'])} |",
    ]
    fired = evidence["rewardkit_fired"]
    lines.append(
        "| RewardKit integrity (verifier) | "
        + (
            _verdict_word(bool(fired), flagged="copied")
            if evidence["integrity"] is not None
            else "not decided"
        )
        + " | "
        + _cell("; ".join(f"{f['rule']}: {f['reason']}" for f in fired))
        + " |"
    )
    for reader, check in COPY_CHECKS:
        verdict = evidence["verdicts"].get(reader) or {}
        value = (verdict.get("checks") or {}).get(check)
        label = f"{READER_LABELS[reader]} `{check}`"
        lines.append(
            f"| {label} | {_verdict_word(value, flagged='copied')} | "
            f"{_cell((verdict.get('explanations') or {}).get(check))} |"
        )
    lines += [
        "",
        "### Other reader checks",
        "",
        "| reader | check | verdict | why |",
        "|---|---|---|---|",
    ]
    for (reader, check), _dim in READER_DIMS.items():
        if (reader, check) in COPY_CHECKS:
            continue
        verdict = evidence["verdicts"].get(reader) or {}
        value = (verdict.get("checks") or {}).get(check)
        lines.append(
            f"| {READER_LABELS[reader]} | `{check}` | {_verdict_word(value)} | "
            f"{_cell((verdict.get('explanations') or {}).get(check))} |"
        )
    check = evidence.get("task_check") or {}
    lines += ["", "### Task quality (`harbor check`, once per task)", ""]
    if check.get("checks"):
        lines += ["| check | problem? | why |", "|---|---|---|"]
        for name, value in check["checks"].items():
            lines.append(
                f"| `{name}` | {_verdict_word(value)} | "
                f"{_cell((check.get('explanations') or {}).get(name))} |"
            )
    else:
        lines.append(
            "not run for this task" + (f" ({check['error']})" if check.get("error") else "")
        )
    lines += [
        "",
        "### Eval Lab watch alerts",
        "",
        ", ".join(f"`{rule}`" for rule in evidence["watch_rules"]) or "none",
        "",
    ]
    return "\n".join(lines)


def _check_counts(evidence: dict[str, Any]) -> str:
    checks = (evidence.get("task_check") or {}).get("checks") or {}
    if not checks:
        return "not run"
    flagged = sorted(name for name, value in checks.items() if value)
    return f"{len(flagged)} of {len(checks)} flagged" + (
        f" ({', '.join(flagged)})" if flagged else ""
    )


def _outcome(value: Any) -> str:
    return "not_applicable" if value is None else ("fail" if value else "pass")


def render_job_analysis(
    rows: list[tuple[str, dict[str, Any]]], health: dict[str, Any]
) -> dict[str, Any]:
    """Harbor's analyze schema: one entry per trial, a badge per source/check."""
    results = []
    tags = ", ".join(health.get("tags") or []) or "no record"
    for trial_name, evidence in rows:
        checks: dict[str, dict[str, str]] = {
            "copied_copy_check_v1": {
                "outcome": _outcome(evidence["copy_check"]),
                "explanation": evidence["copy_check_note"] or "Eval Lab copy check v1",
            },
            "copied_rewardkit_integrity": {
                "outcome": _outcome(
                    None if evidence["integrity"] is None else bool(evidence["rewardkit_fired"])
                ),
                "explanation": "; ".join(
                    f"{f['rule']}: {f['reason']}" for f in evidence["rewardkit_fired"]
                )
                or "RewardKit integrity in the verifier",
            },
        }
        for (reader, check), _dim in READER_DIMS.items():
            verdict = evidence["verdicts"].get(reader) or {}
            value = (verdict.get("checks") or {}).get(check)
            checks[f"{check}_{READER_SLUGS[reader]}"] = {
                "outcome": _outcome(value),
                "explanation": (verdict.get("explanations") or {}).get(check)
                or f"{READER_LABELS[reader]} {check}: "
                + ("not run yet" if not verdict else "nothing found"),
            }
        summary = (
            f"raw {evidence['reward']} | integrity {evidence['integrity']} | "
            f"gated {evidence['reward_gated']} | stop: {evidence['stop_reason']} | "
            f"task health: {tags} | harbor check: {_check_counts(evidence)} | "
            f"watch: {', '.join(evidence['watch_rules']) or 'none'}\n"
            f"Laminar: {evidence['laminar_url'] or 'no trace yet'}\n"
            "Open the trial, then its Analysis tab, for the clickable Laminar link and evidence."
        )
        results.append({"trial_name": trial_name, "summary": summary, "checks": checks})
    return {"results": results}


# ---------------------------------------------------------------------------
# Task health (Data's task-health-tags@1 variant records)
# ---------------------------------------------------------------------------


def task_health(variants_dir: Path | None, task_name: str) -> dict[str, Any]:
    """Tags and reasons from the newest ``task-health-tags@1`` record of the task."""
    if variants_dir is None:
        return {}
    folder = variants_dir / task_name.replace("/", "__")
    best: tuple[float, dict[str, Any]] | None = None
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        record = _read_json(path) or {}
        if record.get("transform") != "task-health-tags@1":
            continue
        assessment = (record.get("inputs") or {}).get("assessment") or {}
        stamp = path.stat().st_mtime
        if best is None or stamp > best[0]:
            best = (stamp, {"tags": list(assessment.get("tags") or []), "record": path.name})
    return best[1] if best else {}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _member_signature(
    members: list[tuple[Path, Path]], store: Path, health: dict[str, Any], task_name: str
) -> str:
    digest = hashlib.sha256()
    paths = [store / "_tasks" / task_name / "harbor_check.json"]
    for job, trial in sorted(members, key=lambda m: str(m[1])):
        paths += [
            trial / "result.json",
            job / "processed" / f"trial-{trial.name}.json",
            job / "watch" / "status.json",
            *(store / job.name / trial.name / f"{r}.json" for r in READERS),
        ]
    for path in paths:
        try:
            stat = path.stat()
            digest.update(f"{path}:{stat.st_mtime_ns}:{stat.st_size}\n".encode())
        except OSError:
            digest.update(f"{path}:-\n".encode())
    digest.update(json.dumps(health, sort_keys=True).encode())
    return digest.hexdigest()


def build_task_page(
    dest: Path,
    task_name: str,
    members: list[tuple[Path, Path]],
    *,
    store: Path,
    variants_dir: Path | None = None,
) -> dict[str, Any]:
    """Write one ``task-<id>`` viewer job at ``dest`` (which must not exist)."""
    health = task_health(variants_dir, task_name)
    jobs = sorted({job for job, _ in members}, key=str)
    selected = {job: sorted(t for j, t in members if j == job) for job in jobs}
    counts = build_merged_job(
        dest,
        page_name(task_name),
        jobs,
        arm_pattern=compile_arm_pattern(ATTEMPT_REGEX),
        selected_trials=selected,
    )
    by_source = {(job.name, trial.name): (job, trial) for job, trial in members}
    rows: list[tuple[str, dict[str, Any]]] = []
    for trial_dest in sorted(p for p in dest.iterdir() if p.is_dir()):
        record = _read_json(trial_dest / ".evallab-source.json") or {}
        source_job = str(record.get("source_job"))
        match = by_source.get((source_job, trial_dest.name)) or next(
            (
                pair
                for (job_name, trial_name), pair in by_source.items()
                if job_name == source_job and trial_dest.name.endswith(trial_name)
            ),
            None,
        )
        if match is None:
            continue
        evidence = trial_evidence(store, *match)
        doc_path = trial_dest / "result.json"
        doc = _read_json(doc_path)
        if doc is not None:
            verifier = doc.setdefault("verifier_result", {}) or {}
            doc["verifier_result"] = verifier
            rewards = verifier.setdefault("rewards", {}) or {}
            verifier["rewards"] = rewards
            if isinstance(rewards, dict) and rewards:
                rewards.update(flag_dims(evidence))
            doc_path.unlink()  # merged result.json is a fresh file; never write through a link
            doc_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        for stale in ("analysis.json", "analysis.md"):
            (trial_dest / stale).unlink(missing_ok=True)  # never write through a hard link
        (trial_dest / "analysis.md").write_text(
            render_trial_markdown(evidence, health), encoding="utf-8"
        )
        rows.append((trial_dest.name, evidence))
    (dest / "analysis.json").unlink(missing_ok=True)
    (dest / "analysis.json").write_text(
        # Insertion order is the display order (copy sources first): no sort_keys.
        json.dumps(render_job_analysis(rows, health), indent=2) + "\n",
        encoding="utf-8",
    )
    counts["task"] = task_name
    counts["health"] = health
    return counts


class TaskPages:
    """Keeps ``task-<id>`` pages in a viewer root in step with trusted jobs."""

    def __init__(
        self,
        root: Path,
        *,
        store: Path,
        variants_dir: Path | None = None,
        trusted_globs: Iterable[str] = DEFAULT_TRUSTED_GLOBS,
        laminar_api_key: str | None = None,
        collect_every_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = root
        self.store = store
        self.variants_dir = variants_dir
        self.trusted_globs = tuple(trusted_globs)
        self.staging = root.parent / f".{root.name}.task-staging"
        #: With a key, Laminar Signal verdicts are pulled for trusted trials that lack
        #: a complete one (Signals run in Laminar Cloud on every trace by themselves).
        self.laminar_api_key = laminar_api_key
        self.collect_every_seconds = collect_every_seconds
        self.clock = clock
        self._last_collect: float | None = None

    def _collect_laminar(self, by_task: dict[str, list[tuple[Path, Path]]]) -> int:
        """Refresh incomplete Laminar verdicts, at most once per ``collect_every_seconds``."""
        if not self.laminar_api_key:
            return 0
        now = self.clock()
        if self._last_collect is not None and now - self._last_collect < self.collect_every_seconds:
            return 0
        self._last_collect = now
        pending = []
        for members in by_task.values():
            for job, trial in members:
                verdict = _read_json(self.store / job.name / trial.name / "laminar_signals.json")
                checks = (verdict or {}).get("checks") or {}
                if not checks or any(value is None for value in checks.values()):
                    pending.append(trial)
        if not pending:
            return 0
        from evallab.readers import laminar_signals

        verdicts = laminar_signals.collect(pending, api_key=self.laminar_api_key)
        # A verdict keyed by the trial's own job (the results-home job dir).
        return laminar_signals.write(verdicts, self.store)

    def _existing(self) -> dict[str, str]:
        found = {}
        for page in self.root.glob(PAGE_PREFIX + "*"):
            record = _read_json(page / PAGE_RECORD) or {}
            if record.get("schema") == PAGE_SCHEMA:
                found[page.name] = str(record.get("signature"))
        return found

    def sync(self, jobs: list[Path]) -> dict[str, list[str]]:
        """Build new pages, rebuild changed ones, drop pages whose task left the set."""
        report: dict[str, list[str]] = {"built": [], "removed": [], "failed": []}
        self.root.mkdir(parents=True, exist_ok=True)
        existing = self._existing()
        wanted = {}
        by_task = trusted_trials(jobs, self.trusted_globs)
        for task, members in by_task.items():
            wanted[page_name(task)] = (task, members)
        try:
            self._collect_laminar(by_task)
        except Exception as exc:  # Laminar down never blocks the pages
            report["failed"].append(f"laminar collect: {type(exc).__name__}: {exc}")
        for name in set(existing) - set(wanted):
            self._discard(self.root / name)
            report["removed"].append(name)
        for name, (task, members) in sorted(wanted.items()):
            health = task_health(self.variants_dir, task)
            signature = _member_signature(members, self.store, health, task)
            if existing.get(name) == signature:
                continue
            staged = self.staging / f"build-{uuid.uuid4().hex}" / name
            try:
                build_task_page(
                    staged, task, members, store=self.store, variants_dir=self.variants_dir
                )
                (staged / PAGE_RECORD).write_text(
                    json.dumps(
                        {"schema": PAGE_SCHEMA, "task": task, "signature": signature},
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                if (self.root / name).exists():
                    self._discard(self.root / name)
                staged.rename(self.root / name)
                report["built"].append(name)
            except Exception as exc:  # one bad task never stops the pass
                report["failed"].append(f"{name}: {type(exc).__name__}: {exc}")
            finally:
                shutil.rmtree(staged.parent, ignore_errors=True)
        shutil.rmtree(self.staging, ignore_errors=True)
        return report

    def _discard(self, page: Path) -> None:
        trash = self.staging / f"trash-{uuid.uuid4().hex}"
        trash.parent.mkdir(parents=True, exist_ok=True)
        page.rename(trash)
        shutil.rmtree(trash, ignore_errors=True)
