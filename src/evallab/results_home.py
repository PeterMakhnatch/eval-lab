from __future__ import annotations

import datetime as _datetime
import fcntl
import json
import os
import re
import subprocess
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "results_home/v1"
ENV_VAR = "EVALLAB_RESULTS_HOME"
DEFAULT_ROOT = Path.home() / "Developer" / "eval-lab-results"
PRIMARY_CHECKOUT = Path.home() / "Developer" / "eval-lab"
CARD_RE = re.compile(r"har-?(\d+)", re.IGNORECASE)
SECRET_RE = re.compile(r"(?i)(key|token|secret|password|authorization)")

CONTROL_AGENTS = frozenset({"nop", "oracle", "probe"})

_RESEARCH_DOC_NAMES = ("RESULTS.md", "SUMMARY.md", "README.md")

_SKIP_DIR_NAMES = frozenset({".executor", ".exec-stage", ".harness-staging"})
_GIT_TIMEOUT_SECONDS = 15


def results_root() -> Path:
    override = os.environ.get(ENV_VAR)
    return Path(override).expanduser() if override else DEFAULT_ROOT


def card_from(*texts: str | None) -> str | None:
    for text in texts:
        if not text:
            continue
        match = CARD_RE.search(text)
        if match:
            return f"HAR-{match.group(1)}"
    return None


def _explicit_linear_card(metadata: dict[str, Any], spec: dict[str, Any]) -> str | None:
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
    import hashlib

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
    import hashlib

    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _endpoint(command: list[Any] | None, metadata: dict[str, Any]) -> str | None:
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


def build_provenance(
    job_dir: Path,
    *,
    repo_root: Path | None = None,
    pr_lookup: Callable[[str], str | None] | None = None,
    capture: str = "run-time",
) -> dict[str, Any]:
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
    import hashlib
    import shutil

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    with source.open("rb") as original, dest.open("rb") as copied:
        if hashlib.file_digest(original, "sha256").digest() != hashlib.file_digest(
            copied, "sha256"
        ).digest():
            raise OSError(f"Published copy differs from source: {source}")


def _copy_tree(source: Path, dest: Path, *, skip_processed: bool = False) -> int:
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
    parts = source.resolve().parts
    if ".worktrees" in parts:
        return parts[parts.index(".worktrees") + 1]
    if "wt-archive" in parts:
        return "archive-" + parts[parts.index("wt-archive") + 2]
    return source.parent.name


def _destination(
    home: Path, day: str, card: str, source: Path
) -> tuple[Path, str | None, tuple[Path, Path] | None]:
    base = home / day / f"{card}-{source.name}"
    existing = _read_json(base / "provenance.json") or {}
    other = existing.get("source_path")
    if not isinstance(other, str) or other == str(source):
        return base, None, None
    renamed = home / day / f"{card}-{source.name}~{_origin_key(Path(other))}"
    own = home / day / f"{card}-{source.name}~{_origin_key(source)}"
    return own, other, (base, renamed) if not renamed.exists() else None


def _is_processed_report_file(name: str) -> bool:
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
    import shutil
    import tempfile
    from glob import escape as glob_escape

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
    dest, collided_with, relocation = _destination(home, day, card, source)
    provenance["source_path"] = str(source)
    provenance["collided_with"] = collided_with
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{dest.name}-", dir=dest.parent))
    staged = staging / "job"
    previous_copy = staging / "previous"
    installed = False
    relocated = False
    try:
        copied = _copy_tree(source, staged, skip_processed=custom_reports)
        if custom_reports:
            assert report_root is not None
            processed_dest = staged / "processed"
            processed_dest.mkdir(parents=True, exist_ok=True)
            for child in sorted(report_root.iterdir()):
                if child.is_symlink() or not child.is_file():
                    continue
                if not _is_processed_report_file(child.name):
                    continue
                _copy_file(child, processed_dest / child.name)
                copied += 1
        (staged / "provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        diff_source = source / "repository-provenance" / "uncommitted.diff"
        if diff_source.is_file():
            _copy_file(diff_source, staged / "uncommitted.diff")
        try:
            if relocation is not None:
                os.replace(*relocation)
                relocated = True
            if dest.exists():
                os.replace(dest, previous_copy)
            os.replace(staged, dest)
        except BaseException:
            if previous_copy.exists():
                os.replace(previous_copy, dest)
            if relocated:
                assert relocation is not None
                os.replace(relocation[1], relocation[0])
            raise
        installed = True
    finally:
        if installed or not previous_copy.exists():
            shutil.rmtree(staging)
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
    flags_map = summary.get("reward_report_flags")
    if isinstance(flags_map, dict):
        forged = flags_map.get("forged")
        if isinstance(forged, int) and forged > 0:
            return f"{passed} pass, {failed} fail, {unscored} unscored, forged {forged}"
    return f"{passed} pass, {failed} fail, {unscored} unscored"


def _counts_summary(published: Path) -> str:
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
    for name in _RESEARCH_DOC_NAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _research_docs(checkout: Path) -> dict[str, str]:
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
    doc = docs.get(card)
    return f"[research]({doc})" if doc else "no research doc"


def _command_agent(command: Any) -> str | None:
    if not isinstance(command, list):
        return None
    for index, token in enumerate(command):
        if token == "--agent" and index + 1 < len(command):
            value = command[index + 1]
            return str(value) if value else None
    return None


def _agent_model(published: Path, provenance: dict[str, Any]) -> tuple[str | None, str | None]:
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
    if not agent:
        return None
    return agent.split(":")[-1].strip() or None


def is_agent_run(agent: str | None) -> bool:
    short = _agent_short(agent)
    return short is not None and short.lower() not in CONTROL_AGENTS


def _published_jobs(home: Path) -> Iterator[tuple[Path, dict[str, Any]]]:
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
_RUN_LINK = "[run]("
_INDEX_LOCK_NAME = ".index.lock"
_INDEX_THREAD_LOCK = threading.Lock()


@dataclass
class _Group:

    card: str
    rows: list[str]


class _IndexFormatError(ValueError):
    pass


def _path_sort(path: Path | str) -> str:
    parts = str(path).rsplit("/", 2)
    if len(parts) < 3:
        raise _IndexFormatError(f"INDEX run link is not a published job path: {path}")
    return f"{parts[1]}-{parts[2]}"


def _row_sort(row: str) -> str:
    start = row.find(_RUN_LINK)
    if start < 0:
        raise _IndexFormatError(f"INDEX row without a run link: {row[:80]}")
    start += len(_RUN_LINK)
    end = row.find(")", start)
    if end < 0:
        raise _IndexFormatError(f"INDEX row with an unterminated run link: {row[:80]}")
    return _path_sort(row[start:end])


def _models_from_entries(
    entries: list[dict[str, Any]],
) -> tuple[list[_Group], list[_Group], dict[str, int]]:
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
    table = [(group.card, routine[group.card]) for group in every if routine.get(group.card)]
    lines.append(
        f"## Routine control jobs ({sum(count for _, count in table)} jobs, {len(table)} cards)"
    )
    lines.append("")
    lines.extend(_ROUTINE_HEADER)
    for card, count in table:
        anchor = card.lower().replace(" ", "-")
        lines.append(f"| {card} | {count} | [all {card} routine jobs](INDEX-all.md#{anchor}) |")
    lines.append("")
    return "\n".join(lines)


def _all_index(every: list[_Group]) -> str:
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
    home = Path(home).resolve()
    checkout = primary_checkout if primary_checkout is not None else PRIMARY_CHECKOUT
    with _index_lock(home):
        docs = _research_docs(checkout)
        entries = [
            _entry(published, provenance, docs) for published, provenance in _published_jobs(home)
        ]
        agent, every, routine = _models_from_entries(entries)
        _write_index_files(home, _agent_index(agent, every, routine), _all_index(every))
    return home / "INDEX.md"


def _split_index(text: str) -> list[str]:
    lines = text.split("\n")
    if lines[-1] != "":
        raise _IndexFormatError("INDEX file does not end with a newline")
    return lines


def _counted(line: str, prefix: str, suffix: str) -> int:
    if not line.startswith(prefix) or not line.endswith(suffix):
        raise _IndexFormatError(f"unexpected INDEX line: {line[:80]}")
    digits = line[len(prefix) : len(line) - len(suffix)]
    if not digits.isdigit():
        raise _IndexFormatError(f"unexpected INDEX count: {line[:80]}")
    return int(digits)


def _sections(
    lines: list[str], start: int, *, level: int, header: str, separator: str
) -> tuple[list[_Group], int]:
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
        try:
            end = lines.index("", index)
        except ValueError:
            end = len(lines)
        rows = lines[index:end]
        if len(rows) != count:
            raise _IndexFormatError(f"INDEX {card} says {count} rows, holds {len(rows)}")
        if not rows:
            raise _IndexFormatError(f"INDEX {card} section is empty")
        groups.append(_Group(card, rows))
        index = end + 1 if end < len(lines) else end
    _check_order(groups)
    return groups, index


def _trailing_ok(lines: list[str], index: int) -> bool:
    return lines[index:] in ([], [""])


def _check_order(groups: list[_Group]) -> None:
    if len({group.card for group in groups}) != len(groups):
        raise _IndexFormatError("INDEX repeats a card")
    keys = [_row_sort(group.rows[0]) for group in groups]
    if any(earlier <= later for earlier, later in zip(keys, keys[1:], strict=False)):
        raise _IndexFormatError("INDEX cards are not newest-first")


def _parse_agent_index(text: str) -> tuple[list[_Group], list[tuple[str, int]]]:
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
    for group in groups:
        cell = f"{_doc_cell(docs, group.card)} | `"
        if "\n".join(group.rows).count(cell) != len(group.rows):
            return False
    return True


def _locate(rows: list[str], key: str) -> int:
    low, high = 0, len(rows)
    while low < high:
        middle = (low + high) // 2
        if _row_sort(rows[middle]) > key:
            low = middle + 1
        else:
            high = middle
    return low


def _insert_row(groups: list[_Group], card: str, row: str) -> None:
    key = _row_sort(row)
    group = next((existing for existing in groups if existing.card == card), None)
    if group is None:
        group = _Group(card, [])
        groups.append(group)
    position = _locate(group.rows, key)
    if position < len(group.rows) and _row_sort(group.rows[position]) == key:
        del group.rows[position]
    group.rows.insert(position, row)
    groups.remove(group)
    newest = _row_sort(group.rows[0])
    groups.insert(_locate([other.rows[0] for other in groups], newest), group)


def _remove_row(groups: list[_Group], key: str) -> tuple[bool, str | None]:
    for group in list(groups):
        if _row_sort(group.rows[0]) < key or _row_sort(group.rows[-1]) > key:
            continue
        position = _locate(group.rows, key)
        if position == len(group.rows) or _row_sort(group.rows[position]) != key:
            continue
        del group.rows[position]
        if not group.rows:
            groups.remove(group)
        return True, group.card
    return False, None


def _update_models(
    agent: list[_Group],
    every: list[_Group],
    routine: dict[str, int],
    entry: dict[str, Any],
    previous: Sequence[Path] = (),
) -> None:
    for key in (entry["sort"], *(_path_sort(path) for path in previous)):
        was_agent, _ = _remove_row(agent, key)
        was_routine, routine_card = _remove_row(every, key)
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
    home.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(home / _INDEX_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    with _INDEX_THREAD_LOCK, os.fdopen(descriptor, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _write_index_files(home: Path, agent: str, every: str) -> None:
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
    home = Path(home).resolve()
    published = Path(published).resolve()
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
                _update_models(agent, every, routine, entry, previous)
                _write_index_files(home, _agent_index(agent, every, routine), _all_index(every))
                spliced = True
    if not spliced:
        return write_index(home, primary_checkout=checkout)
    return home / "INDEX.md"


