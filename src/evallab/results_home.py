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
from collections.abc import Callable
from pathlib import Path
from typing import Any

SCHEMA = "results_home/v1"
ENV_VAR = "EVALLAB_RESULTS_HOME"
DEFAULT_ROOT = Path.home() / "Developer" / "eval-lab-results"
PRIMARY_CHECKOUT = Path.home() / "Developer" / "eval-lab"
CARD_RE = re.compile(r"har-?(\d+)", re.IGNORECASE)
SECRET_RE = re.compile(r"(?i)(key|token|secret|password|authorization)")

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


def _copy_tree(source: Path, dest: Path) -> int:
    """Byte-copy a job tree. Returns the number of files copied."""
    copied = 0
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIR_NAMES]
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


def publish_job(
    job_dir: str | Path,
    *,
    root: str | Path | None = None,
    repo_root: Path | None = None,
    pr_lookup: Callable[[str], str | None] | None = None,
    capture: str = "run-time",
    primary_checkout: Path | None = None,
    rewrite_index: bool = True,
) -> dict[str, Any]:
    """Publish one job into the results home. Idempotent.

    A second call with the same provenance replaces the published tree with
    an identical one and rewrites the index, so a retried process-job does
    not duplicate or drift.
    """
    source = Path(job_dir).resolve()
    if not source.is_dir():
        raise ValueError(f"Not a job directory: {job_dir}")
    home = Path(root) if root is not None else results_root()
    provenance = build_provenance(source, repo_root=repo_root, pr_lookup=pr_lookup, capture=capture)
    card = provenance["card"] or "unknown"
    day = _job_date(source, provenance)
    dest, collided_with = _destination(home, day, card, source)
    provenance["source_path"] = str(source)
    provenance["collided_with"] = collided_with
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    copied = _copy_tree(source, dest)
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


def _spend(published: Path) -> str:
    report = _read_json(published / "processed" / "job.json")
    if report is None:
        return "None (no processed report)"
    summary = _as_dict(report.get("summary"))
    cost = summary.get("cost_usd")
    if isinstance(cost, (int, float)):
        return f"${cost:.4f}"
    estimate = summary.get("cost_estimate_usd")
    if isinstance(estimate, (int, float)):
        return f"~${estimate:.4f} estimated"
    reason = summary.get("cost_reason")
    if isinstance(reason, str) and reason:
        return "None (unknown)"
    return "None (unknown)"


def _tasks_cell(provenance: dict[str, Any]) -> str:
    tasks = provenance.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return "unknown"
    names = [str(task.get("task_id")) for task in tasks if isinstance(task, dict)]
    return ", ".join(names) if names else "unknown"


def _research_doc(card: str | None, checkout: Path) -> str | None:
    if card is None or not checkout.is_dir():
        return None
    slug = card.lower().replace("-", "")
    research = checkout / "research"
    if not research.is_dir():
        return None
    for path in sorted(research.rglob("*.md")):
        if slug in path.relative_to(checkout).as_posix().lower().replace("-", ""):
            return str(path)
    return None


def _index_rows(home: Path, checkout: Path) -> list[str]:
    rows: list[tuple[str, str]] = []
    if not home.is_dir():
        return []
    for provenance_path in home.rglob("provenance.json"):
        published = provenance_path.parent
        provenance = _read_json(provenance_path) or {}
        repository = _as_dict(provenance.get("repository"))
        day = published.parent.name
        card = provenance.get("card") or "unknown"
        dirty = repository.get("dirty")
        mark = " **uncommitted code**" if dirty is True else ""
        commit = repository.get("commit") or "unknown"
        short = commit[:12] if isinstance(commit, str) else "unknown"
        doc = _research_doc(card if isinstance(card, str) else None, checkout)
        doc_cell = f"[research]({doc})" if doc else "no research doc"
        line = (
            f"| {card}{mark} | {day} | {_tasks_cell(provenance)} | {_reward_summary(published)} "
            f"| {_spend(published)} | [run]({published}) | [report]({published / 'processed' / 'job.md'}) "
            f"| {doc_cell} | `{short}` |"
        )
        rows.append((f"{day}-{published.name}", line))
    rows.sort(reverse=True)
    return [line for _, line in rows]


def write_index(home: Path, *, primary_checkout: Path | None = None) -> Path:
    """Regenerate ``INDEX.md``, newest job first."""
    checkout = primary_checkout if primary_checkout is not None else PRIMARY_CHECKOUT
    rows = _index_rows(home, checkout)
    lines = [
        "# Eval Lab results",
        "",
        "Published by `evallab process-job`. Newest first. A job marked",
        "**uncommitted code** ran from a dirty checkout; its `uncommitted.diff`",
        "is the change.",
        "",
        "| Card | Date | Tasks | Reward | Spend | Run | Report | Research | Commit |",
        "|---|---|---|---|---|---|---|---|---|",
        *rows,
        "",
    ]
    target = home / "INDEX.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
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
