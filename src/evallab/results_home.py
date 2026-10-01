"""One results home for finished Harbor jobs (HAR-117).

``process-job`` publishes each finished job to ``<root>/<date>/<card>-<job>/``
as a byte copy, with the provenance that lets a later reader tell which
code produced it. Sources are only read. The root is
``~/Developer/eval-lab-results`` unless ``EVALLAB_RESULTS_HOME`` overrides it
(tests set that).

Provenance is captured when the job runs, not when it is published: the
runner writes ``repository`` and ``repository-provenance/`` into the job
directory, and publish copies them. A backfilled job that predates that
snapshot says so, with the reason, instead of guessing.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

SCHEMA = "results_home/v1"
ENV_VAR = "EVALLAB_RESULTS_HOME"
DEFAULT_ROOT = Path.home() / "Developer" / "eval-lab-results"
PRIMARY_CHECKOUT = Path.home() / "Developer" / "eval-lab"
CARD_RE = re.compile(r"har-?(\d+)", re.IGNORECASE)
SECRET_RE = re.compile(r"(?i)(key|token|secret|password|authorization)")

#: Agents that run controls, not model capability: their jobs collapse to one
#: INDEX line per card. Anything else with a recorded agent is an agent run.
#: Compared against the agent's short name (after the last ``:``), so both
#: ``nop`` and ``evallab.module:Nop``-style paths classify the same way.
CONTROL_AGENTS = frozenset({"nop", "oracle", "probe"})

#: The only research docs the INDEX links, in preference order. The lookup
#: never descends anywhere else, so deep ``evidence/`` and vendored trees
#: are never touched during publish (HAR-117 publish-slowness fix).
_RESEARCH_DOC_NAMES = ("RESULTS.md", "SUMMARY.md", "README.md")

# Publish refuses to follow links out of the job directory, and never publishes
# the executor's staging area.
_SKIP_DIR_NAMES = frozenset({".executor", ".exec-stage", ".harness-staging"})
_GIT_TIMEOUT_SECONDS = 15


def results_root() -> Path:
    """The results home, overridable so tests never write to the real one."""
    override = os.environ.get(ENV_VAR)
    return Path(override).expanduser() if override else DEFAULT_ROOT


def card_from(*texts: str | None) -> str | None:
    """``HAR-110`` from a job name or spec question_ref, else None."""
    for text in texts:
        if not text:
            continue
        match = CARD_RE.search(text)
        if match:
            return f"HAR-{match.group(1)}"
    return None


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_text(root: Path, *args: str) -> str | None:
    completed = _git(root, *args)
    if completed is None or completed.returncode != 0:
        return None
    return completed.stdout.strip()


def capture_repository(root: Path) -> dict[str, Any]:
    """Git state of the checkout a job is about to run from.

    Called by the runner at run time. The diff and untracked-file list are
    the part publish cannot reconstruct later, so they are saved into the job
    directory alongside this record.
    """
    record: dict[str, Any] = {
        "remote": None,
        "worktree": str(root.resolve()) if root.exists() else str(root),
        "branch": None,
        "commit": None,
        "dirty": None,
        "untracked": [],
        "uncommitted_diff_sha256": None,
        "capture": "run-time",
        "unknown": [],
    }
    if _git(root, "rev-parse", "--is-inside-work-tree") is None:
        record["unknown"].append("git unavailable")
        return record
    record["commit"] = _git_text(root, "rev-parse", "HEAD")
    record["remote"] = _git_text(root, "remote", "get-url", "origin")
    record["branch"] = _git_text(root, "rev-parse", "--abbrev-ref", "HEAD")
    status = _git(root, "status", "--porcelain")
    if status is None or status.returncode != 0:
        record["dirty"] = None
        record["unknown"].append("git status failed")
    else:
        record["dirty"] = bool(status.stdout.strip())
    untracked = _git(root, "ls-files", "--others", "--exclude-standard")
    if untracked is not None and untracked.returncode == 0:
        record["untracked"] = [line for line in untracked.stdout.splitlines() if line]
    else:
        record["unknown"].append("untracked file list unavailable")
    diff = _git(root, "diff", "HEAD")
    if diff is None or diff.returncode != 0:
        record["unknown"].append("uncommitted diff unavailable")
    else:
        payload = diff.stdout.encode()
        record["uncommitted_diff_sha256"] = "sha256:" + hashlib.sha256(payload).hexdigest()
        record["_diff"] = diff.stdout
    if record["commit"] is None:
        record["unknown"].append("commit unavailable")
    return record


def write_run_provenance(job_dir: Path, repository: dict[str, Any]) -> None:
    """Persist the run-time snapshot inside the job directory."""
    record = {key: value for key, value in repository.items() if not key.startswith("_")}
    target = job_dir / "repository-provenance"
    target.mkdir(parents=True, exist_ok=True)
    diff = repository.get("_diff")
    if isinstance(diff, str):
        (target / "uncommitted.diff").write_text(diff, encoding="utf-8")
    (target / "repository.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _endpoint(command: list[Any] | None, metadata: dict[str, Any]) -> str | None:
    """The model endpoint, never a secret.

    The self-hosted route is selected by the model id; its URL carries a
    bearer token, so the endpoint recorded here is the route name. ``--env``
    is the sandbox, not the model endpoint.
    """
    for key in ("endpoint", "api_base", "base_url"):
        value = metadata.get(key)
        if isinstance(value, str) and not SECRET_RE.search(value):
            return value
    if not command:
        return None
    for index, part in enumerate(command[:-1]):
        if part == "--model":
            value = str(command[index + 1])
            return value if not SECRET_RE.search(value) else None
    return None


def _model(metadata: dict[str, Any], command: list[Any] | None) -> str | None:
    identity = metadata.get("model_identity")
    if isinstance(identity, dict) and isinstance(identity.get("requested"), str):
        return identity["requested"]
    experiment = metadata.get("experiment")
    if isinstance(experiment, dict) and isinstance(experiment.get("model"), str):
        return experiment["model"]
    if command:
        for index, part in enumerate(command[:-1]):
            if part == "--model":
                return str(command[index + 1])
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _tasks(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    experiment = _as_dict(metadata.get("experiment"))
    staging = _as_dict(metadata.get("task_staging"))
    task_id = experiment.get("task_id") or staging.get("source_task_basename")
    if task_id is None:
        return []
    return [
        {
            "task_id": task_id,
            "task_path": experiment.get("task_path"),
            "package_digest": experiment.get("package_digest")
            or staging.get("source_package_digest"),
            "variant_digest": experiment.get("variant_digest"),
        }
    ]


def _harness(metadata: dict[str, Any]) -> dict[str, Any]:
    experiment = _as_dict(metadata.get("experiment"))
    tree = _as_dict(metadata.get("harness_tree"))
    tools = _as_dict(metadata.get("tools"))
    return {
        "path": experiment.get("harness_tree_path"),
        "digest": experiment.get("harness_tree_sha256") or tree.get("sha256"),
        "harbor_version": tools.get("harbor"),
    }


def _pr_number(commit: str | None, lookup: Callable[[str], str | None] | None) -> dict[str, Any]:
    if not commit:
        return {"number": None, "reason": "no commit recorded"}
    if lookup is None:
        return {"number": None, "reason": "gh lookup not available"}
    try:
        found = lookup(commit)
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        return {"number": None, "reason": "gh lookup failed"}
    if not found:
        return {"number": None, "reason": "no pull request contains this commit"}
    digits = re.search(r"\d+", found)
    if digits is None:
        return {"number": None, "reason": f"unparseable gh output: {found[:40]}"}
    return {"number": int(digits.group()), "reason": None}


def default_pr_lookup(commit: str) -> str | None:
    """Ask gh which PR contains a commit. None when gh is absent or fails."""
    try:
        completed = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--search",
                commit,
                "--state",
                "all",
                "--json",
                "number",
                "--limit",
                "1",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        rows = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        number = rows[0].get("number")
        return str(number) if isinstance(number, int) else None
    return None


def build_provenance(
    job_dir: Path,
    *,
    repo_root: Path | None = None,
    pr_lookup: Callable[[str], str | None] | None = None,
    capture: str = "run-time",
) -> dict[str, Any]:
    """Provenance for one job, preferring the snapshot taken at run time."""
    metadata = _read_json(job_dir / "lab-metadata.json") or {}
    spec = _read_json(job_dir / "experiment-spec.json") or {}
    saved = _read_json(job_dir / "repository-provenance" / "repository.json")
    command = metadata.get("command") if isinstance(metadata.get("command"), list) else None
    unknown: list[str] = []

    if saved:
        repository = dict(saved)
        repository["capture"] = saved.get("capture", "run-time")
    else:
        repository = {
            "remote": None,
            "worktree": None,
            "branch": None,
            "commit": None,
            "dirty": None,
            "untracked": [],
            "uncommitted_diff_sha256": None,
            "capture": capture,
        }
        recorded = metadata.get("repository")
        if isinstance(recorded, dict):
            repository["commit"] = recorded.get("commit")
            repository["dirty"] = recorded.get("dirty")
        if repo_root is not None and capture == "publish-time":
            live = capture_repository(repo_root)
            for key in ("remote", "worktree", "branch"):
                repository[key] = live.get(key)
            if repository["commit"] is None:
                repository["commit"] = live.get("commit")
                repository["dirty"] = live.get("dirty")
                repository["capture"] = "publish-time"
                repository["unknown_reason"] = (
                    "no commit was saved when the job ran; "
                    "commit, remote, branch and worktree are the checkout's state at publish"
                )
            else:
                repository["capture"] = "run-time-partial"
                repository["unknown_reason"] = (
                    "commit and dirty were saved when the job ran; the diff was not, "
                    "so remote, branch and worktree are the checkout's state at publish"
                )
            unknown.append(repository["unknown_reason"])
        else:
            unknown.append("run-time repository snapshot missing")

    diff_path = job_dir / "repository-provenance" / "uncommitted.diff"
    if diff_path.is_file() and repository.get("uncommitted_diff_sha256") is None:
        repository["uncommitted_diff_sha256"] = _sha256_text(diff_path.read_text(encoding="utf-8"))

    card = card_from(
        job_dir.name,
        spec.get("question_ref") if isinstance(spec.get("question_ref"), str) else None,
    )
    if card is None:
        unknown.append("no card in job name or spec question_ref")

    commit = repository.get("commit")
    pr = _pr_number(commit if isinstance(commit, str) else None, pr_lookup)

    return {
        "schema": SCHEMA,
        "job_name": job_dir.name,
        "card": card,
        "repository": {key: value for key, value in repository.items() if key != "unknown"},
        "pull_request": pr,
        "harness": _harness(metadata),
        "tasks": _tasks(metadata),
        "model": _model(metadata, command),
        "endpoint": _endpoint(command, metadata),
        "command": command,
        "host": metadata.get("host"),
        "unknown": unknown,
    }


def _copy_file(source: Path, dest: Path) -> None:
    """Byte copy. Never a hard link and never a symlink.

    A hard link would share the source inode, so replacing a published tree
    on a later publish (``shutil.rmtree``) would delete the source file too.
    Sources are read-only; the only writes are under the results root.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    shutil.copyfile(source, dest)


def _copy_tree(source: Path, dest: Path, *, skip_processed: bool = False) -> int:
    """Byte-copy a job tree. Returns the number of files copied."""
    copied = 0
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIR_NAMES]
        if skip_processed and current == source:
            dirnames[:] = [name for name in dirnames if name != "processed"]
        relative = current.relative_to(source)
        for name in filenames:
            origin = current / name
            if origin.is_symlink():
                continue
            _copy_file(origin, dest / relative / name)
            copied += 1
    return copied


def _job_date(job_dir: Path, provenance: dict[str, Any]) -> str:
    metadata = _read_json(job_dir / "lab-metadata.json") or {}
    started = metadata.get("started_at") or metadata.get("finished_at")
    if isinstance(started, str) and len(started) >= 10:
        return started[:10]
    mtime = _datetime.datetime.fromtimestamp(job_dir.stat().st_mtime, _datetime.UTC)
    return mtime.date().isoformat()


def _origin_key(source: Path) -> str:
    """A short, stable label for where a job was found, so two copies coexist."""
    parts = source.resolve().parts
    if ".worktrees" in parts:
        return parts[parts.index(".worktrees") + 1]
    if "wt-archive" in parts:
        return "archive-" + parts[parts.index("wt-archive") + 2]
    return source.parent.name


def _destination(home: Path, day: str, card: str, source: Path) -> tuple[Path, str | None]:
    """``<day>/<card>-<job>/``, plus the other source when the name collides.

    The same source republished replaces its own copy. A different source
    with the same job name is kept alongside it, with ``~<origin>`` appended
    to both, and the collision is named so provenance can record it.
    """
    base = home / day / f"{card}-{source.name}"
    existing = _read_json(base / "provenance.json") or {}
    other = existing.get("source_path")
    if not isinstance(other, str) or other == str(source):
        return base, None
    renamed = home / day / f"{card}-{source.name}~{_origin_key(Path(other))}"
    if not renamed.exists():
        base.rename(renamed)
    own = home / day / f"{card}-{source.name}~{_origin_key(source)}"
    return own, other


def _is_processed_report_file(name: str) -> bool:
    """Report pages process-job writes, never unrelated out-dir contents."""
    if name in ("job.json", "job.md"):
        return True
    return name.startswith("trial-") and (name.endswith(".json") or name.endswith(".md"))


def publish_job(
    job_dir: str | Path,
    *,
    root: str | Path | None = None,
    repo_root: Path | None = None,
    pr_lookup: Callable[[str], str | None] | None = None,
    capture: str = "run-time",
    primary_checkout: Path | None = None,
    rewrite_index: bool = True,
    processed_report_root: str | Path | None = None,
) -> dict[str, Any]:
    """Publish one job into the results home. Idempotent.

    A second call with the same provenance replaces the published tree with
    an identical one and rewrites the index, so a retried process-job does
    not duplicate or drift.

    ``processed_report_root`` points at the freshly written report directory
    when process-job used a custom ``output_dir``: those report pages
    replace the snapshotted ``processed/`` copy, so the published tree shows
    the new outcome instead of the stale source ``processed/``. Only
    ``job.json``/``job.md`` and ``trial-*.json``/``trial-*.md`` pages are
    overlaid; nothing else is taken from that directory. Raw job inputs and
    provenance still come from ``job_dir``.
    """
    source = Path(job_dir).resolve()
    if not source.is_dir():
        raise ValueError(f"Not a job directory: {job_dir}")
    home = (Path(root) if root is not None else results_root()).resolve()
    report_root = Path(processed_report_root).resolve() if processed_report_root is not None else None
    if report_root is not None and not report_root.is_dir():
        raise ValueError(f"Not a processed report directory: {processed_report_root}")
    if report_root is not None and report_root.is_relative_to(home):
        raise ValueError("Processed report input must be outside the results home when publishing")
    custom_reports = report_root is not None and report_root != source / "processed"
    provenance = build_provenance(source, repo_root=repo_root, pr_lookup=pr_lookup, capture=capture)
    card = provenance["card"] or "unknown"
    day = _job_date(source, provenance)
    dest, collided_with = _destination(home, day, card, source)
    provenance["source_path"] = str(source)
    provenance["collided_with"] = collided_with
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    copied = _copy_tree(source, dest, skip_processed=custom_reports)
    if custom_reports:
        assert report_root is not None
        processed_dest = dest / "processed"
        processed_dest.mkdir(parents=True, exist_ok=True)
        for child in sorted(report_root.iterdir()):
            if child.is_symlink() or not child.is_file():
                continue
            if not _is_processed_report_file(child.name):
                continue
            _copy_file(child, processed_dest / child.name)
            copied += 1
    (dest / "provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    diff_source = source / "repository-provenance" / "uncommitted.diff"
    if diff_source.is_file():
        _copy_file(diff_source, dest / "uncommitted.diff")
    index = (
        write_index(home, primary_checkout=primary_checkout) if rewrite_index else home / "INDEX.md"
    )
    return {
        "published": str(dest),
        "files": copied,
        "card": provenance["card"],
        "collided_with": collided_with,
        "index": str(index),
    }


def _reward_summary(published: Path) -> str:
    report = _read_json(published / "processed" / "job.json")
    if report is None:
        return "unprocessed"
    summary = _as_dict(report.get("summary"))
    passed = summary.get("n_pass")
    failed = summary.get("n_fail")
    unscored = summary.get("n_unscored")
    if not isinstance(passed, int):
        return "unprocessed"
    return f"{passed} pass, {failed} fail, {unscored} unscored"


def _counts_summary(published: Path) -> str:
    """Counted verdicts from ``evallab.counts`` stored in the job report.

    Reads only the stored ``summary`` (``n_counted_pass``/``n_counted_fail``/
    ``n_excluded``/``excluded_reasons``), so the INDEX matches the canonical
    report even for multi-trial jobs. Reports that predate counts have none
    of these fields and render ``counts unknown``, never ``0``.
    """
    report = _read_json(published / "processed" / "job.json")
    if report is None:
        return "counts unknown"
    summary = _as_dict(report.get("summary"))
    counted_pass = summary.get("n_counted_pass")
    counted_fail = summary.get("n_counted_fail")
    n_excluded = summary.get("n_excluded")
    if not (
        isinstance(counted_pass, int)
        and isinstance(counted_fail, int)
        and isinstance(n_excluded, int)
    ):
        return "counts unknown"
    reasons = summary.get("excluded_reasons")
    detail = ""
    if isinstance(reasons, dict) and reasons:
        parts = [
            f"{key}: {reasons[key]}"
            for key in sorted(reasons)
            if isinstance(reasons[key], int)
        ]
        if parts:
            detail = f" ({', '.join(parts)})"
    return f"{counted_pass} counted pass, {counted_fail} counted fail, {n_excluded} excluded{detail}"


def _spend(published: Path) -> str:
    report = _read_json(published / "processed" / "job.json")
    if report is None:
        return "None (no processed report)"
    summary = _as_dict(report.get("summary"))
    allocated = _session_spend_cell(summary.get("session_spend"))
    if allocated is not None:
        return allocated
    cost = summary.get("cost_usd")
    if isinstance(cost, (int, float)):
        return f"${cost:.4f}"
    estimate = summary.get("cost_estimate_usd")
    if isinstance(estimate, (int, float)):
        return (
            f"~${estimate:.4f} shared GPU, not additive; "
            "see `evallab spend day`"
        )
    reason = summary.get("cost_reason")
    if isinstance(reason, str) and reason:
        return "None (unknown)"
    return "None (unknown)"


def _session_spend_cell(allocation: Any) -> str | None:
    """Allocated billed-GPU + Daytona estimate INDEX cell, or None.

    Prefers the billed-session allocation when present. An unknown Daytona
    estimate renders the GPU share plus unknown sandbox: never a false full
    total and never a fallback to the legacy wall-time estimate.
    """
    if not isinstance(allocation, dict):
        return None
    modal = allocation.get("modal_allocated_usd")
    if not isinstance(modal, (int, float)):
        return None
    session = allocation.get("session_id")
    daytona = allocation.get("daytona_estimate_usd")
    total = allocation.get("total_usd")
    if isinstance(total, (int, float)) and isinstance(daytona, (int, float)):
        return (
            f"${total:.4f} session "
            f"(billed GPU ${modal:.4f} + Daytona est ${daytona:.4f}; "
            f"{allocation.get('allocation_basis')}; session {session})"
        )
    return f"${modal:.4f} billed GPU share + sandbox unknown (session {session})"


def _tasks_cell(provenance: dict[str, Any]) -> str:
    tasks = provenance.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return "unknown"
    names = [str(task.get("task_id")) for task in tasks if isinstance(task, dict)]
    return ", ".join(names) if names else "unknown"


def _doc_for_dir(directory: Path) -> Path | None:
    """The preferred research doc directly inside one experiment/exploration dir."""
    for name in _RESEARCH_DOC_NAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _research_docs(checkout: Path) -> dict[str, str]:
    """Card (``HAR-117``) to research doc, from one bounded scan per regeneration.

    Only ``research/experiments/*/`` and ``research/explorations/*/``
    top-level docs are considered, plus one bounded level for nested
    explorations such as ``trace-lab/har109``. The previous implementation
    ran ``research.rglob("*.md")`` once per INDEX row, descending into
    ``research/evidence/runs`` and vendored trees on every publish; this
    never leaves the two buckets above, so its cost does not depend on deep
    trees. Only directories whose own name carries the card match.
    """
    docs: dict[str, str] = {}
    research = checkout / "research"
    for bucket in ("experiments", "explorations"):
        base = research / bucket
        if not base.is_dir():
            continue
        try:
            top = sorted(p for p in base.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            continue
        candidates: list[Path] = list(top)
        for entry in top:
            try:
                children = sorted(p for p in entry.iterdir() if p.is_dir() and not p.is_symlink())
            except OSError:
                continue
            candidates.extend(children)
        for directory in candidates:
            card = card_from(directory.name)
            if card is None or card in docs:
                continue
            doc = _doc_for_dir(directory)
            if doc is not None:
                docs[card] = str(doc)
    return docs


def _command_agent(command: Any) -> str | None:
    """The ``--agent`` value of a recorded harbor command, else None."""
    if not isinstance(command, list):
        return None
    for index, token in enumerate(command):
        if token == "--agent" and index + 1 < len(command):
            value = command[index + 1]
            return str(value) if value else None
    return None


def _agent_model(published: Path, provenance: dict[str, Any]) -> tuple[str | None, str | None]:
    """``(agent, model)`` for one published job, from records, not name heuristics.

    Prefers the provenance command, then the published ``config.json``
    agent entry (old backfilled jobs have no recorded command but keep
    their config), then the ``lab-metadata.json`` command. Either side may
    stay None when nothing recorded it.
    """
    agent = _command_agent(provenance.get("command"))
    model = provenance.get("model")
    if not isinstance(model, str):
        model = None
    if agent is None or model is None:
        config = _read_json(published / "config.json") or {}
        agents = config.get("agents")
        first = (
            agents[0]
            if isinstance(agents, list) and agents and isinstance(agents[0], dict)
            else None
        )
        if agent is None and first is not None:
            name = first.get("name")
            agent = str(name) if name else None
        if model is None and first is not None:
            model_name = first.get("model_name")
            model = str(model_name) if model_name else None
    if agent is None:
        metadata = _read_json(published / "lab-metadata.json") or {}
        agent = _command_agent(metadata.get("command"))
    return agent, model


def _agent_short(agent: str | None) -> str | None:
    """``SecretSafeTerminus2`` from ``evallab.harbor_terminus:SecretSafeTerminus2``."""
    if not agent:
        return None
    return agent.split(":")[-1].strip() or None


def is_agent_run(agent: str | None) -> bool:
    """A recorded agent counts as an agent run unless it is a control agent."""
    short = _agent_short(agent)
    return short is not None and short.lower() not in CONTROL_AGENTS


def _published_jobs(home: Path) -> Iterator[tuple[Path, dict[str, Any]]]:
    """``(published dir, provenance)`` for every job, without descending into jobs.

    Published jobs always live at ``<home>/<day>/<card>-<job>/``. The
    previous ``home.rglob("provenance.json")`` walked every copied run tree
    (gigabytes of trial files) on each regeneration; two ``iterdir`` levels
    find the same entries in time proportional to the job count.
    """
    if not home.is_dir():
        return
    try:
        days = sorted(p for p in home.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return
    for day in days:
        try:
            jobs = sorted(p for p in day.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            continue
        for job in jobs:
            provenance_path = job / "provenance.json"
            if not provenance_path.is_file():
                continue
            yield job, _read_json(provenance_path) or {}


def _entry(published: Path, provenance: dict[str, Any], docs: dict[str, str]) -> dict[str, Any]:
    """All INDEX cells for one published job, computed once per regeneration."""
    repository = _as_dict(provenance.get("repository"))
    day = published.parent.name
    card = provenance.get("card")
    if not isinstance(card, str) or not card:
        card = "unknown"
    commit = repository.get("commit") or "unknown"
    agent, model = _agent_model(published, provenance)
    doc = docs.get(card)
    return {
        "sort": f"{day}-{published.name}",
        "day": day,
        "card": card,
        "job": published.name,
        "tasks": _tasks_cell(provenance),
        "reward": _reward_summary(published),
        "counts": _counts_summary(published),
        "spend": _spend(published),
        "agent": agent or "unknown",
        "agent_short": _agent_short(agent) or "unknown",
        "model": model or "unknown",
        "is_agent_run": is_agent_run(agent),
        "mark": " **uncommitted code**" if repository.get("dirty") is True else "",
        "short_commit": commit[:12] if isinstance(commit, str) else "unknown",
        "run": str(published),
        "report": str(published / "processed" / "job.md"),
        "doc": f"[research]({doc})" if doc else "no research doc",
    }


def _agent_row(entry: dict[str, Any]) -> str:
    return (
        f"| {entry['day']} | {entry['job']}{entry['mark']} | {entry['tasks']} "
        f"| {entry['agent_short']} | {entry['model']} | {entry['reward']} "
        f"| {entry['counts']} | {entry['spend']} | [run]({entry['run']}) | [report]({entry['report']}) "
        f"| {entry['doc']} | `{entry['short_commit']}` |"
    )


def _routine_row(entry: dict[str, Any]) -> str:
    return (
        f"| {entry['card']}{entry['mark']} | {entry['day']} | {entry['tasks']} "
        f"| {entry['reward']} | {entry['counts']} | {entry['spend']} | [run]({entry['run']}) "
        f"| [report]({entry['report']}) | {entry['doc']} | `{entry['short_commit']}` |"
    )


def write_index(home: Path, *, primary_checkout: Path | None = None) -> Path:
    """Regenerate ``INDEX.md`` (agent runs first) and ``INDEX-all.md`` (every job).

    Agent runs are grouped by card, newest group first, newest job first
    inside each group. Control jobs (``nop``/``oracle``/``probe`` agents,
    plus jobs with no recorded agent) collapse to one line per card with a
    count and a link to the full per-card list. Provenance columns are
    unchanged. The research-doc lookup is built once per call and never
    descends outside ``research/experiments`` and ``research/explorations``.
    """
    checkout = primary_checkout if primary_checkout is not None else PRIMARY_CHECKOUT
    docs = _research_docs(checkout)
    entries = [
        _entry(published, provenance, docs) for published, provenance in _published_jobs(home)
    ]
    agent_runs = sorted(
        (entry for entry in entries if entry["is_agent_run"]),
        key=lambda entry: entry["sort"],
        reverse=True,
    )
    routine = sorted(
        (entry for entry in entries if not entry["is_agent_run"]),
        key=lambda entry: entry["sort"],
        reverse=True,
    )
    by_card: dict[str, list[dict[str, Any]]] = {}
    for entry in agent_runs:
        by_card.setdefault(entry["card"], []).append(entry)
    cards = sorted(by_card, key=lambda card: by_card[card][0]["sort"], reverse=True)
    routine_by_card: dict[str, list[dict[str, Any]]] = {}
    for entry in routine:
        routine_by_card.setdefault(entry["card"], []).append(entry)
    routine_cards = sorted(
        routine_by_card, key=lambda card: routine_by_card[card][0]["sort"], reverse=True
    )
    lines = [
        "# Eval Lab results",
        "",
        "Published by `evallab process-job`. Agent runs first, grouped by card,",
        "newest first. Control jobs (`nop`/`oracle`/`probe` agents, or no",
        "recorded agent) collapse to one line per card below; the full list is",
        "[INDEX-all.md](INDEX-all.md). A job marked **uncommitted code** ran",
        "from a dirty checkout; its `uncommitted.diff` is the change.",
        "Reward (raw) is the verifier pass/fail; Counted is the `evallab.counts`",
        "verdict (`counts unknown` when the report predates counts).",
        "",
        f"## Agent runs ({len(agent_runs)})",
        "",
    ]
    if agent_runs:
        for card in cards:
            group = by_card[card]
            lines.append(f"### {card} ({len(group)})")
            lines.append("")
            lines.append(
                "| Date | Job | Tasks | Agent | Model | Reward (raw) | Counted | Spend | Run | Report | Research | Commit |"
            )
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
            lines.extend(_agent_row(entry) for entry in group)
            lines.append("")
    else:
        lines.append("No agent runs published yet.")
        lines.append("")
    lines.append(f"## Routine control jobs ({len(routine)} jobs, {len(routine_cards)} cards)")
    lines.append("")
    lines.append("| Card | Jobs | List |")
    lines.append("|---|---|---|")
    for card in routine_cards:
        group = routine_by_card[card]
        anchor = card.lower().replace(" ", "-")
        lines.append(
            f"| {card} | {len(group)} | [all {card} routine jobs](INDEX-all.md#{anchor}) |"
        )
    lines.append("")
    target = home / "INDEX.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    all_by_card: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        all_by_card.setdefault(entry["card"], []).append(entry)
    for group in all_by_card.values():
        group.sort(key=lambda entry: entry["sort"], reverse=True)
    all_lines = [
        "# Eval Lab results: all jobs",
        "",
        f"{len(entries)} jobs, newest first. Agent runs are listed up front in",
        "[INDEX.md](INDEX.md); this file is the complete per-job list.",
        "",
    ]
    for card in sorted(all_by_card, key=lambda card: all_by_card[card][0]["sort"], reverse=True):
        group = all_by_card[card]
        all_lines.append(f"## {card} ({len(group)})")
        all_lines.append("")
        all_lines.append(
            "| Card | Date | Tasks | Reward (raw) | Counted | Spend | Run | Report | Research | Commit |"
        )
        all_lines.append("|---|---|---|---|---|---|---|---|---|---|")
        all_lines.extend(_routine_row(entry) for entry in group)
        all_lines.append("")
    (home / "INDEX-all.md").write_text("\n".join(all_lines), encoding="utf-8")
    return target


def _is_job_dir(path: Path) -> bool:
    return path.is_dir() and (path / "result.json").is_file() and (path / "config.json").is_file()


def _card_number(name: str) -> int | None:
    match = CARD_RE.search(name)
    return int(match.group(1)) if match else None


def discover_jobs(roots: list[Path]) -> list[Path]:
    """HAR-81+ jobs under a worktree's ``runs/`` or an archived ``runs/``.

    Only ``runs/`` directories are walked. Task packages under
    ``research/`` and the executor's staging dirs are never treated as jobs,
    and nothing here is opened for writing.
    """
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        runs_dirs = [root] if root.name == "runs" else list(root.rglob("runs"))
        for runs_dir in runs_dirs:
            if not runs_dir.is_dir():
                continue
            for dirpath, dirnames, _filenames in os.walk(runs_dir, followlinks=False):
                current = Path(dirpath)
                dirnames[:] = [name for name in dirnames if name not in _SKIP_DIR_NAMES]
                if _is_job_dir(current):
                    number = _card_number(current.name)
                    if number is not None and number >= 81:
                        found.append(current)
                    dirnames.clear()
    return sorted(set(found))


def backfill(
    *,
    home: Path | None = None,
    worktrees: Path | None = None,
    archive: Path | None = None,
    primary_checkout: Path | None = None,
    pr_lookup: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Publish every HAR-81+ job found under worktrees and the wt-archive."""
    root = home if home is not None else results_root()
    trees = worktrees or (PRIMARY_CHECKOUT / ".worktrees")
    archived = archive or (Path.home() / ".local" / "share" / "wt-archive")
    jobs = discover_jobs([trees, archived])
    published: list[str] = []
    skipped: list[dict[str, str]] = []
    collisions: list[dict[str, str]] = []
    by_card: dict[str, int] = {}
    for job in jobs:
        try:
            result = publish_job(
                job,
                root=root,
                repo_root=job.parents[1] if (job.parents[1] / ".git").exists() else None,
                pr_lookup=pr_lookup,
                capture="publish-time",
                primary_checkout=primary_checkout,
                rewrite_index=False,
            )
        except (OSError, ValueError) as exc:
            skipped.append({"job": str(job), "reason": f"{type(exc).__name__}: {exc}"})
            continue
        published.append(result["published"])
        card = result["card"] or "unknown"
        by_card[card] = by_card.get(card, 0) + 1
        if result.get("collided_with"):
            collisions.append({"job": result["published"], "kept_both": result["collided_with"]})
    write_index(root, primary_checkout=primary_checkout)
    return {
        "published": len(published),
        "skipped": skipped,
        "collisions": collisions,
        "by_card": by_card,
        "index": str(root / "INDEX.md"),
    }
