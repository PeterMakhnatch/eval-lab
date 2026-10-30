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

``INDEX.md`` and ``INDEX-all.md`` are derived views of the home.
``write_index`` renders both from every published job (the repair path);
``update_index``, which publishing uses, splices one job's rows into the
published files, so publish time does not grow with the number of published
jobs (HAR-237).
"""

from __future__ import annotations

import datetime as _datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from glob import escape as glob_escape
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


def _explicit_linear_card(metadata: dict[str, Any], spec: dict[str, Any]) -> str | None:
    """Explicit ``linear_card`` from run metadata or the frozen spec, if any.

    Both bindings must agree; disagreement or a malformed value raises
    ``ValueError`` fail-closed. Blank/missing stays ``None``. Task, model,
    and harness names are never consulted.
    """
    from evallab.schemas import normalize_linear_card

    found: dict[str, str] = {}
    experiment = metadata.get("experiment")
    if isinstance(experiment, dict) and experiment.get("linear_card") is not None:
        normalized = normalize_linear_card(experiment.get("linear_card"))
        if normalized is not None:
            found["lab-metadata experiment.linear_card"] = normalized
    if spec.get("linear_card") is not None:
        normalized = normalize_linear_card(spec.get("linear_card"))
        if normalized is not None:
            found["experiment-spec linear_card"] = normalized
    if len(set(found.values())) > 1:
        detail = "; ".join(f"{source}={card}" for source, card in sorted(found.items()))
        raise ValueError(f"Explicit linear_card bindings disagree: {detail}")
    return next(iter(found.values()), None)


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
    explicit_card = _explicit_linear_card(metadata, spec)
    if explicit_card is not None:
        if card is not None and card != explicit_card:
            raise ValueError(
                f"Explicit linear_card {explicit_card} conflicts with recorded card {card}"
            )
        card = explicit_card
    if card is None:
        unknown.append("no card in job name, spec question_ref, or explicit linear_card")

    commit = repository.get("commit")
    pr = _pr_number(commit if isinstance(commit, str) else None, pr_lookup)

    provenance: dict[str, Any] = {
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
    if explicit_card is not None:
        provenance["card_assignment"] = {
            "source": "experiment_linear_card",
            "card": explicit_card,
        }
    return provenance


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
    """Byte-copy a job tree, empty directories included. Returns the number of files copied.

    Harbor records an empty collected directory (``/logs/artifacts``) as present;
    ``harbor trial regrade`` refuses a copy that lost it as incomplete.
    """
    copied = 0
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIR_NAMES]
        if skip_processed and current == source:
            dirnames[:] = [name for name in dirnames if name != "processed"]
        relative = current.relative_to(source)
        (dest / relative).mkdir(parents=True, exist_ok=True)
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
    publication_card: str | None = None,
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

    ``publication_card`` explicitly annotates an otherwise unattributed job;
    it never rewrites the frozen spec or overrides a recorded card. The
    source-bound annotation survives republication without the argument.

    The published INDEX is updated in place for this one job (``update_index``):
    a publish costs the same whether the home holds ten jobs or ten thousand,
    and the files it writes are byte-identical to a full ``write_index``.
    """
    source = Path(job_dir).resolve()
    if not source.is_dir():
        raise ValueError(f"Not a job directory: {job_dir}")
    home = (Path(root) if root is not None else results_root()).resolve()
    report_root = (
        Path(processed_report_root).resolve() if processed_report_root is not None else None
    )
    if report_root is not None and not report_root.is_dir():
        raise ValueError(f"Not a processed report directory: {processed_report_root}")
    if report_root is not None and report_root.is_relative_to(home):
        raise ValueError("Processed report input must be outside the results home when publishing")
    custom_reports = report_root is not None and report_root != source / "processed"
    provenance = build_provenance(source, repo_root=repo_root, pr_lookup=pr_lookup, capture=capture)
    recorded_card = provenance["card"]
    if publication_card is not None:
        if not CARD_RE.fullmatch(publication_card):
            raise ValueError("Publication card must be a HAR issue identifier, e.g. HAR-126")
        publication_card = card_from(publication_card)
        if recorded_card is not None and publication_card != recorded_card:
            raise ValueError(f"Publication card conflicts with recorded card {recorded_card}")
    day = _job_date(source, provenance)
    previous = []
    for candidate in (home / day).glob(f"*-{glob_escape(source.name)}*"):
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        saved = _read_json(candidate / "provenance.json") or {}
        if saved.get("source_path") == str(source):
            previous.append((candidate, saved))
    assigned_cards = {
        saved["card"]
        for _, saved in previous
        if isinstance(saved.get("card"), str) and saved.get("card_assignment")
    }
    if any(not CARD_RE.fullmatch(card) for card in assigned_cards):
        raise ValueError("Invalid existing source-bound publication card")
    requested = publication_card or recorded_card
    if len(assigned_cards) > 1 or (
        assigned_cards and requested and requested not in assigned_cards
    ):
        raise ValueError("Publication card conflicts with the existing source-bound assignment")
    if publication_card is not None and recorded_card is None:
        provenance["card_assignment"] = {"source": "publication_argument", "card": publication_card}
    elif recorded_card is None and assigned_cards:
        publication_card = next(iter(assigned_cards))
        provenance["card_assignment"] = next(
            saved["card_assignment"]
            for _, saved in previous
            if saved.get("card") == publication_card
        )
    provenance["card"] = publication_card or recorded_card
    if provenance["card"] is not None:
        provenance["unknown"] = [
            reason
            for reason in provenance["unknown"]
            if reason != "no card in job name, spec question_ref, or explicit linear_card"
        ]
    card = provenance["card"] or "unknown"
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
    # Rehome only this exact source after its replacement is complete. An
    # unrelated same-name job or failed replacement must keep its snapshot.
    for previous_path, _ in previous:
        if previous_path != dest and previous_path.exists():
            shutil.rmtree(previous_path)
    index = (
        update_index(
            home,
            dest,
            provenance=provenance,
            previous=[path for path, _ in previous if path != dest],
            primary_checkout=primary_checkout,
        )
        if rewrite_index
        else home / "INDEX.md"
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
            f"{key}: {reasons[key]}" for key in sorted(reasons) if isinstance(reasons[key], int)
        ]
        if parts:
            detail = f" ({', '.join(parts)})"
    return (
        f"{counted_pass} counted pass, {counted_fail} counted fail, {n_excluded} excluded{detail}"
    )


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
        return f"~${estimate:.4f} shared GPU, not additive; see `evallab spend day`"
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


def _doc_cell(docs: dict[str, str], card: str) -> str:
    """The INDEX Research cell for a card: one link, or ``no research doc``."""
    doc = docs.get(card)
    return f"[research]({doc})" if doc else "no research doc"


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
        "doc": _doc_cell(docs, card),
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


# --- INDEX model -----------------------------------------------------------
#
# Both files are rendered from one model: a ``_Group`` per card holding
# already-rendered row lines, newest row first. ``write_index`` builds that
# model from every published job; ``update_index`` parses it back out of the
# published files, splices one job's rows in, and re-renders. Publishing a job
# therefore reads that job's records and writes two files, instead of
# re-reading the provenance and report of every published job (HAR-237).
#
# The parser accepts only what these renderers emit and checks every count in
# the files against the rows it counts. Anything else -- a hand-edited file, a
# format change, a research doc that appeared since the last publish -- makes
# ``update_index`` fall back to the full rebuild, so a splice never publishes a
# file a rebuild would not have written.

_AGENT_PREAMBLE = (
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
)
_AGENT_HEADER = (
    "| Date | Job | Tasks | Agent | Model | Reward (raw) | Counted | Spend "
    "| Run | Report | Research | Commit |"
)
_AGENT_SEPARATOR = "|---|---|---|---|---|---|---|---|---|---|---|---|"
_ALL_TITLE = "# Eval Lab results: all jobs"
_ALL_SUBTITLE = " jobs, newest first. Agent runs are listed up front in"
_ALL_LINK = "[INDEX.md](INDEX.md); this file is the complete per-job list."
_ALL_HEADER = (
    "| Card | Date | Tasks | Reward (raw) | Counted | Spend | Run | Report "
    "| Research | Commit |"
)
_ALL_SEPARATOR = "|---|---|---|---|---|---|---|---|---|---|"
_ROUTINE_HEADER = ("| Card | Jobs | List |", "|---|---|---|")
_ROUTINE_TITLE = re.compile(r"## Routine control jobs \((?P<rows>\d+) jobs, (?P<cards>\d+) cards\)")
_ROUTINE_ROW = re.compile(
    r"\| (?P<shown>[^|]+) \| (?P<count>\d+) \| \[all (?P<label>[^|]+) routine jobs\]"
    r"\(INDEX-all\.md#(?P<anchor>[^)]*)\) \|"
)
_SECTION_HEADER = re.compile(r"(?P<card>.+) \((?P<count>\d+)\)")
_RUN_LINK = re.compile(r"\[run\]\(([^)]+)\)")
_INDEX_LOCK_NAME = ".index.lock"
_INDEX_THREAD_LOCK = threading.Lock()


@dataclass
class _Group:
    """One card's rows in one INDEX file, already rendered, newest first."""

    card: str
    rows: list[str]


class _IndexFormatError(ValueError):
    """A published INDEX file is not what ``write_index`` renders."""


def _row_sort(row: str) -> str:
    """``<day>-<job>`` from a rendered row's run link: its ``sort`` key."""
    match = _RUN_LINK.search(row)
    if match is None:
        raise _IndexFormatError(f"INDEX row without a run link: {row[:80]}")
    run = Path(match.group(1))
    return f"{run.parent.name}-{run.name}"


def _models_from_entries(
    entries: list[dict[str, Any]],
) -> tuple[list[_Group], list[_Group], dict[str, int]]:
    """The INDEX model for every entry: agent groups, all groups, routine counts."""
    agent_rows: dict[str, list[str]] = {}
    every_rows: dict[str, list[str]] = {}
    routine: dict[str, int] = {}
    for entry in sorted(entries, key=lambda item: item["sort"], reverse=True):
        every_rows.setdefault(entry["card"], []).append(_routine_row(entry))
        if entry["is_agent_run"]:
            agent_rows.setdefault(entry["card"], []).append(_agent_row(entry))
        else:
            routine[entry["card"]] = routine.get(entry["card"], 0) + 1
    return (
        [_Group(card, rows) for card, rows in agent_rows.items()],
        [_Group(card, rows) for card, rows in every_rows.items()],
        routine,
    )


def _agent_index(agent: list[_Group], every: list[_Group], routine: dict[str, int]) -> str:
    """Render ``INDEX.md``: agent runs by card, then the routine control table.

    The routine table lists cards in ``INDEX-all.md`` order (each card's newest
    entry) rather than by its newest control job: that order is then readable
    back out of the published files, which is what lets one publish update one
    card without re-reading every published job (HAR-237).
    """
    lines = [
        *_AGENT_PREAMBLE,
        f"## Agent runs ({sum(len(group.rows) for group in agent)})",
        "",
    ]
    if agent:
        for group in agent:
            lines.append(f"### {group.card} ({len(group.rows)})")
            lines.extend(("", _AGENT_HEADER, _AGENT_SEPARATOR, *group.rows, ""))
    else:
        lines.extend(("No agent runs published yet.", ""))
    lines.append(f"## Routine control jobs ({sum(routine.values())} jobs, {len(routine)} cards)")
    lines.append("")
    lines.extend(_ROUTINE_HEADER)
    for group in every:
        count = routine.get(group.card, 0)
        if count:
            anchor = group.card.lower().replace(" ", "-")
            lines.append(
                f"| {group.card} | {count} | [all {group.card} routine jobs](INDEX-all.md#{anchor}) |"
            )
    lines.append("")
    return "\n".join(lines)


def _all_index(every: list[_Group]) -> str:
    """Render ``INDEX-all.md``: every job, grouped by card, newest first."""
    lines = [
        _ALL_TITLE,
        "",
        f"{sum(len(group.rows) for group in every)}{_ALL_SUBTITLE}",
        _ALL_LINK,
        "",
    ]
    for group in every:
        lines.append(f"## {group.card} ({len(group.rows)})")
        lines.extend(("", _ALL_HEADER, _ALL_SEPARATOR, *group.rows, ""))
    return "\n".join(lines)


def write_index(home: Path, *, primary_checkout: Path | None = None) -> Path:
    """Regenerate ``INDEX.md`` (agent runs first) and ``INDEX-all.md`` (every job).

    Agent runs are grouped by card, newest group first, newest job first
    inside each group. Control jobs (``nop``/``oracle``/``probe`` agents,
    plus jobs with no recorded agent) collapse to one line per card with a
    count and a link to the full per-card list. Provenance columns are
    unchanged. The research-doc lookup is built once per call and never
    descends outside ``research/experiments`` and ``research/explorations``.

    This is the repair path: publishing one job goes through ``update_index``,
    which splices the same model into the published files, and ``backfill``
    rebuilds once after publishing many. A rebuild is the authority those two
    are measured against.
    """
    checkout = primary_checkout if primary_checkout is not None else PRIMARY_CHECKOUT
    docs = _research_docs(checkout)
    entries = [
        _entry(published, provenance, docs) for published, provenance in _published_jobs(home)
    ]
    agent, every, routine = _models_from_entries(entries)
    with _index_lock(home):
        _write_index_files(home, _agent_index(agent, every, routine), _all_index(every))
    return home / "INDEX.md"


def _split_index(text: str) -> list[str]:
    """Lines of an INDEX file, including the empty string after its final newline."""
    lines = text.split("\n")
    if lines[-1] != "":
        raise _IndexFormatError("INDEX file does not end with a newline")
    return lines


def _counted(line: str, prefix: str, suffix: str) -> int:
    """The decimal count between ``prefix`` and ``suffix``."""
    if not line.startswith(prefix) or not line.endswith(suffix):
        raise _IndexFormatError(f"unexpected INDEX line: {line[:80]}")
    digits = line[len(prefix) : len(line) - len(suffix)]
    if not digits.isdigit():
        raise _IndexFormatError(f"unexpected INDEX count: {line[:80]}")
    return int(digits)


def _sections(
    lines: list[str], start: int, *, level: int, header: str, separator: str
) -> tuple[list[_Group], int]:
    """Parse ``level``-headed card sections starting at ``lines[start]``."""
    mark = "#" * level + " "
    groups: list[_Group] = []
    index = start
    while index < len(lines) and lines[index].startswith(mark):
        match = _SECTION_HEADER.fullmatch(lines[index][len(mark) :])
        if match is None:
            raise _IndexFormatError(f"unexpected INDEX section: {lines[index][:80]}")
        card = match.group("card")
        count = int(match.group("count"))
        if lines[index + 1 : index + 4] != ["", header, separator]:
            raise _IndexFormatError(f"unexpected INDEX section body for {card}")
        index += 4
        rows: list[str] = []
        while index < len(lines) and lines[index].startswith("| "):
            rows.append(lines[index])
            index += 1
        if len(rows) != count:
            raise _IndexFormatError(f"INDEX {card} says {count} rows, holds {len(rows)}")
        if lines[index : index + 1] == [""]:
            index += 1
        elif index != len(lines):
            raise _IndexFormatError(f"INDEX {card} section is not blank-terminated")
        groups.append(_Group(card, rows))
    _check_order(groups)
    return groups, index


def _trailing_ok(lines: list[str], index: int) -> bool:
    """A document may end after its last section, with at most that blank line left."""
    return lines[index:] in ([], [""])


def _check_order(groups: list[_Group]) -> None:
    """Cards and rows are strictly newest-first, with no repeated card."""
    if len({group.card for group in groups}) != len(groups):
        raise _IndexFormatError("INDEX repeats a card")
    keys = [_row_sort(group.rows[0]) for group in groups if group.rows]
    if len(keys) != len(groups):
        raise _IndexFormatError("INDEX has an empty card section")
    if any(earlier <= later for earlier, later in zip(keys, keys[1:], strict=False)):
        raise _IndexFormatError("INDEX cards are not newest-first")
    for group in groups:
        rows = [_row_sort(row) for row in group.rows]
        if any(earlier <= later for earlier, later in zip(rows, rows[1:], strict=False)):
            raise _IndexFormatError(f"INDEX {group.card} rows are not newest-first")


def _parse_agent_index(text: str) -> tuple[list[_Group], list[tuple[str, int]]]:
    """Parse ``INDEX.md`` into agent groups and its routine ``(card, count)`` table."""
    lines = _split_index(text)
    head = len(_AGENT_PREAMBLE)
    if lines[:head] != list(_AGENT_PREAMBLE):
        raise _IndexFormatError("unexpected INDEX.md preamble")
    total = _counted(lines[head], "## Agent runs (", ")")
    if lines[head + 1 : head + 2] != [""]:
        raise _IndexFormatError("unexpected INDEX.md agent heading")
    index = head + 2
    agent: list[_Group] = []
    if lines[index : index + 1] == ["No agent runs published yet."]:
        index += 1
        if lines[index : index + 1] != [""]:
            raise _IndexFormatError("unexpected INDEX.md empty-agent line")
        index += 1
    else:
        agent, index = _sections(
            lines, index, level=3, header=_AGENT_HEADER, separator=_AGENT_SEPARATOR
        )
    if sum(len(group.rows) for group in agent) != total:
        raise _IndexFormatError("INDEX.md agent count does not match its rows")
    title = _ROUTINE_TITLE.fullmatch(lines[index]) if index < len(lines) else None
    if title is None or lines[index + 1 : index + 2] != [""]:
        raise _IndexFormatError("unexpected INDEX.md routine heading")
    index += 2
    if lines[index : index + 2] != list(_ROUTINE_HEADER):
        raise _IndexFormatError("unexpected INDEX.md routine table head")
    index += 2
    table: list[tuple[str, int]] = []
    while index < len(lines) and lines[index].startswith("| "):
        row = _ROUTINE_ROW.fullmatch(lines[index])
        if row is None:
            raise _IndexFormatError(f"unexpected INDEX.md routine row: {lines[index][:80]}")
        card = row.group("shown")
        if row.group("label") != card or row.group("anchor") != card.lower().replace(" ", "-"):
            raise _IndexFormatError(f"INDEX.md routine row for {card} does not link itself")
        table.append((card, int(row.group("count"))))
        index += 1
    if not _trailing_ok(lines, index):
        raise _IndexFormatError("unexpected trailing content in INDEX.md")
    if sum(count for _, count in table) != int(title.group("rows")):
        raise _IndexFormatError("INDEX.md routine count does not match its table")
    if len(table) != int(title.group("cards")):
        raise _IndexFormatError("INDEX.md routine card count does not match its table")
    return agent, table


def _parse_all_index(text: str) -> list[_Group]:
    """Parse ``INDEX-all.md`` into card groups, newest card first."""
    lines = _split_index(text)
    if lines[:2] != [_ALL_TITLE, ""]:
        raise _IndexFormatError("unexpected INDEX-all.md preamble")
    total = _counted(lines[2], "", _ALL_SUBTITLE)
    if lines[3:5] != [_ALL_LINK, ""]:
        raise _IndexFormatError("unexpected INDEX-all.md preamble")
    groups, index = _sections(lines, 5, level=2, header=_ALL_HEADER, separator=_ALL_SEPARATOR)
    if not _trailing_ok(lines, index):
        raise _IndexFormatError("unexpected trailing content in INDEX-all.md")
    if sum(len(group.rows) for group in groups) != total:
        raise _IndexFormatError("INDEX-all.md count does not match its rows")
    return groups


def _read_index_model(home: Path) -> tuple[list[_Group], list[_Group], dict[str, int]]:
    """Parse the published pair back into the model ``write_index`` renders from."""
    agent, table = _parse_agent_index((home / "INDEX.md").read_text(encoding="utf-8"))
    every = _parse_all_index((home / "INDEX-all.md").read_text(encoding="utf-8"))
    routine = dict(table)
    if sum(len(group.rows) for group in agent) + sum(routine.values()) != sum(
        len(group.rows) for group in every
    ):
        raise _IndexFormatError("INDEX.md and INDEX-all.md disagree about the job count")
    if not set(routine) <= {group.card for group in every}:
        raise _IndexFormatError("INDEX.md names a routine card INDEX-all.md does not have")
    if [group.card for group in every if group.card in routine] != [card for card, _ in table]:
        raise _IndexFormatError("INDEX.md routine table is not in INDEX-all.md card order")
    return agent, every, routine


def _doc_cells_current(groups: list[_Group], docs: dict[str, str]) -> bool:
    """Every existing row still shows the research doc its card resolves to today."""
    for group in groups:
        cell = _doc_cell(docs, group.card)
        if any(cell not in row for row in group.rows):
            return False
    return True


def _insert_row(groups: list[_Group], card: str, row: str) -> None:
    """Insert one rendered row, keeping cards and rows newest-first."""
    key = _row_sort(row)
    group = next((existing for existing in groups if existing.card == card), None)
    if group is None:
        group = _Group(card, [])
        groups.append(group)
    group.rows = [existing for existing in group.rows if _row_sort(existing) != key]
    position = next(
        (index for index, existing in enumerate(group.rows) if _row_sort(existing) < key),
        len(group.rows),
    )
    group.rows.insert(position, row)
    groups.remove(group)
    position = next(
        (index for index, other in enumerate(groups) if _row_sort(other.rows[0]) < key),
        len(groups),
    )
    groups.insert(position, group)


def _remove_row(groups: list[_Group], run: str) -> tuple[bool, str | None]:
    """Drop the row that links ``run``: ``(removed, the card it was filed under)``."""
    link = f"({run})"
    for group in list(groups):
        for index, row in enumerate(group.rows):
            if link in row:
                del group.rows[index]
                if not group.rows:
                    groups.remove(group)
                return True, group.card
    return False, None


def _update_models(
    agent: list[_Group],
    every: list[_Group],
    routine: dict[str, int],
    entry: dict[str, Any],
    published: Path,
    previous: Sequence[Path] = (),
) -> None:
    """Splice one entry into the model, replacing its previous rows in place.

    ``previous`` names the directories a rehome moved the job out of: their
    rows still link the old path and must be dropped, not left beside the new
    one.
    """
    for run in (str(published), *(str(path) for path in previous)):
        was_agent, _ = _remove_row(agent, run)
        was_routine, routine_card = _remove_row(every, run)
        if was_routine and not was_agent and routine_card is not None:
            remaining = routine.get(routine_card, 0) - 1
            if remaining > 0:
                routine[routine_card] = remaining
            else:
                routine.pop(routine_card, None)
    _insert_row(every, entry["card"], _routine_row(entry))
    if entry["is_agent_run"]:
        _insert_row(agent, entry["card"], _agent_row(entry))
    else:
        routine[entry["card"]] = routine.get(entry["card"], 0) + 1


@contextmanager
def _index_lock(home: Path) -> Iterator[None]:
    """Serialize the INDEX read-modify-write across threads and processes."""
    home.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(home / _INDEX_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    with _INDEX_THREAD_LOCK, os.fdopen(descriptor, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _write_index_files(home: Path, agent: str, every: str) -> None:
    """Replace both files; a reader never sees a half-written INDEX."""
    home.mkdir(parents=True, exist_ok=True)
    for name, text in (("INDEX.md", agent), ("INDEX-all.md", every)):
        staging = home / f".{name}.tmp"
        staging.write_text(text, encoding="utf-8")
        os.replace(staging, home / name)


def update_index(
    home: Path,
    published: Path,
    *,
    provenance: dict[str, Any] | None = None,
    previous: Sequence[Path] = (),
    primary_checkout: Path | None = None,
) -> Path:
    """Splice one published job's rows into ``INDEX.md`` and ``INDEX-all.md``.

    Publish time stops growing with the number of published jobs (HAR-237): the
    files already hold every rendered row, so one publish reads that job's
    records and writes the two files it must write anyway. The result is
    byte-identical to ``write_index`` on the same home. Anything the splice
    cannot prove equivalent -- a missing or foreign INDEX file, a count that
    disagrees with its rows, a research doc that appeared since the last
    publish -- falls back to a full rebuild, which is also how a hand-edited
    INDEX is repaired.

    ``previous`` names publications of the same job that this publish replaced
    or moved (a card rehome); their rows are retired with it.
    """
    home = Path(home)
    published = Path(published)
    checkout = primary_checkout if primary_checkout is not None else PRIMARY_CHECKOUT
    docs = _research_docs(checkout)
    spliced = False
    with _index_lock(home):
        model: tuple[list[_Group], list[_Group], dict[str, int]] | None
        try:
            model = _read_index_model(home)
        except (OSError, _IndexFormatError):
            model = None
        if model is not None:
            agent, every, routine = model
            if _doc_cells_current(agent, docs) and _doc_cells_current(every, docs):
                record = (
                    provenance
                    if provenance is not None
                    else _read_json(published / "provenance.json") or {}
                )
                entry = _entry(published, record, docs)
                _update_models(agent, every, routine, entry, published, previous)
                _write_index_files(home, _agent_index(agent, every, routine), _all_index(every))
                spliced = True
    if not spliced:
        return write_index(home, primary_checkout=checkout)
    return home / "INDEX.md"


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
