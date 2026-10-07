"""Local-only incremental refresh behind ``evallab nightly --refresh`` (HAR-199).

Raw queues/runs are read-only. Reports, projections, replay and task pages live
under this service's state directory. No executor, model, cloud reader or network
publisher is constructed here. The existing paid nightly cycle is separate.
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from evallab.harbor_watch_hooks import HOOK_JOURNAL
from evallab.process_job import process_job
from evallab.results import discover_job_dirs
from evallab.storage.paths import shared_checkout_root

ET = ZoneInfo("America/New_York")
SCHEMA = "evallab.nightly_refresh/v1"
DIGEST_SCHEMA = "evallab.nightly_digest/v1"
VERDICT_FILES = tuple(
    Path(name)
    for name in (
        "research/experiments/har108-python-census/task_health.parquet",
        "research/experiments/har108-python-census/pool.json",
        "research/experiments/python-task-ledger/task_history.csv",
        "research/experiments/python-task-ledger/oracle_pilot.csv",
        "research/experiments/python-task-ledger/oracle_sweep.csv",
        "research/experiments/har122-egress-lock/har146-locked-nop.csv",
        "research/experiments/har161-exploit/probe_verdicts.json",
    )
)


@dataclass(frozen=True)
class RefreshConfig:
    repo_root: Path
    state_dir: Path
    queue_roots: tuple[Path, ...] = ()
    facts_root: Path | None = None
    readers_store: Path | None = None
    data_root: Path | None = None
    verdict_root: Path | None = None


def _json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _key(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:20]


def queue_roots(config: RefreshConfig) -> tuple[Path, ...]:
    if config.queue_roots:
        return tuple(dict.fromkeys(path.expanduser().resolve() for path in config.queue_roots))
    primary = config.data_root or shared_checkout_root(config.repo_root)
    candidates = [primary]
    trees = primary / ".worktrees"
    if trees.is_dir():
        candidates.extend(path for path in sorted(trees.iterdir()) if (path / "queue").is_dir())
    return tuple(dict.fromkeys(path.resolve() for path in candidates))


def _stamp_tree(root: Path, *, raw_job: bool = False) -> list[tuple[str, int, int]]:
    """Stat metadata only; never read logs/trajectories on an unchanged night."""
    stamps = []
    if not root.exists():
        return stamps
    if root.is_file():
        stat = root.lstat()
        return [(str(root), stat.st_mtime_ns, stat.st_size)]
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(
            name
            for name in dirs
            if not name.startswith(".")
            and name != "__pycache__"
            and (not raw_job or name not in {"processed", "watch"})
        )
        links = [name for name in dirs if (Path(base) / name).is_symlink()]
        for name in sorted([*files, *links]):
            if name.startswith("."):
                continue
            path = Path(base) / name
            stat = path.lstat()  # Artifact symlinks belong to the sandbox, not this host.
            stamps.append((str(path), stat.st_mtime_ns, stat.st_size))
    if raw_job:
        journal = root / HOOK_JOURNAL
        try:
            stat = journal.lstat()
        except FileNotFoundError:
            pass
        else:
            stamps.append((str(journal), stat.st_mtime_ns, stat.st_size))
    return stamps


def _accepted_roots(roots: list[Path], refused: set[Path]) -> list[Path]:
    """Prune refused jobs before census discovery or transient projection reads."""
    selected = []
    for root in roots:
        if root in refused:
            continue
        if not any(job.is_relative_to(root) for job in refused):
            selected.append(root)
            continue
        children = [
            child
            for child in sorted(root.iterdir())
            if child.is_dir() and not child.is_symlink() and not child.name.startswith(".")
        ]
        selected.extend(_accepted_roots(children, refused))
    return selected


def _fingerprint(stamps: list[tuple[str, int, int]]) -> str:
    return hashlib.sha256(json.dumps(stamps, separators=(",", ":")).encode()).hexdigest()


def _metadata(config: RefreshConfig, roots: tuple[Path, ...]) -> list[tuple[str, int, int]]:
    from evallab.queue import QUEUE_STATES

    repo = config.verdict_root or config.data_root or shared_checkout_root(config.repo_root)
    paths = [
        repo / "library/task-variants",
        *(repo / relative for relative in VERDICT_FILES),
        config.readers_store or Path.home() / "Library/Application Support/evallab/readers",
    ]
    for root in roots:
        paths.append(root / "runs/campaigns")
        paths.extend(
            path
            for state in QUEUE_STATES
            for path in sorted((root / "queue" / state).glob("*.json"))
        )
    return [stamp for path in paths for stamp in _stamp_tree(path)]


def refresh_trials(config: RefreshConfig, roots: list[Path]) -> dict[str, Any]:
    from evallab.storage.trials import connect_trials

    con, info = connect_trials(
        repo_root=config.data_root or shared_checkout_root(config.repo_root),
        roots=roots,
        derived_root=config.state_dir / "parquet",
        read_only=True,
    )
    try:
        con.execute("SET threads = 1")
        rows = con.execute("SELECT * FROM trials ORDER BY job, trial").to_arrow_table()
        import pyarrow.parquet as pq

        pq.write_table(rows, config.state_dir / "trials.parquet")
        counts = con.execute(
            "SELECT count(*), count(*) FILTER (WHERE legit) FROM trials"
        ).fetchone()
        assert counts is not None  # COUNT always returns one row, even for an empty view.
        total, legit = counts
        return {"total": total, "legit": legit, "projection_errors": len(info["projection_errors"])}
    finally:
        con.close()


def snapshot_verdict_inputs(config: RefreshConfig) -> Path:
    """Persist the real reviewed inputs so retiring their worktree loses no evidence."""
    source = (
        config.verdict_root or config.data_root or shared_checkout_root(config.repo_root)
    ).resolve()
    target = config.state_dir / "evidence"
    manifest_path = config.state_dir / "evidence-sources.json"
    previous = _json(manifest_path)
    if not source.exists():
        if previous.get("source") != str(source) or not previous.get("files"):
            raise FileNotFoundError(f"No reviewed verdict inputs at {source}; set --verdict-root")
        for relative, entry in previous["files"].items():
            data = (target / relative).read_bytes()
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError(f"Cached verdict evidence changed: {relative}")
        return target
    required = (VERDICT_FILES[0], VERDICT_FILES[2], VERDICT_FILES[5])
    for relative in required:
        if not (source / relative).is_file():
            raise FileNotFoundError(
                f"Missing reviewed verdict input: {source / relative}; set --verdict-root"
            )
    inputs = [source / relative for relative in VERDICT_FILES if (source / relative).is_file()]
    inputs.extend(sorted((source / "library/task-variants").glob("*/*.json")))
    files = {}
    for path in inputs:
        relative = path.relative_to(source).as_posix()
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        destination = target / relative
        if (
            not destination.is_file()
            or hashlib.sha256(destination.read_bytes()).hexdigest() != digest
        ):
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
            try:
                temporary.write_bytes(data)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        files[relative] = {"source": str(path), "sha256": digest}
    for relative in previous.get("files", {}).keys() - files.keys():
        (target / relative).unlink(missing_ok=True)
    _atomic(
        manifest_path,
        {"schema": "evallab.nightly_evidence/v1", "source": str(source), "files": files},
    )
    return target


def refresh_verdicts(config: RefreshConfig) -> dict[str, Any]:
    """Re-run HAR-177's deterministic builder, never modify its source ledger."""
    repo = snapshot_verdict_inputs(config)
    builder = config.repo_root / "research/experiments/python-task-ledger/build.py"
    output = config.state_dir / "ledger.csv"
    execution = subprocess.run(
        [sys.executable, str(builder), "--root", str(repo), "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    if execution.returncode:
        raise RuntimeError(
            f"HAR-177 ledger refresh failed: {(execution.stderr or execution.stdout).strip()}"
        )
    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    verdicts = {
        row["task_id"]: {"verdict": row["verdict"], "evidence": row["verdict_evidence"]}
        for row in rows
    }
    # HAR-176's page consumer needs assessment records, not copied task packages.
    # Reuse the exact HAR-177/HAR-172 tag and solve producers with recorded evidence.
    from evallab.task_health_tags import HISTORY, LOCKED_NOP, health_tag, solve_summary, verdict_tag

    def read_csv(path: Path) -> dict[str, dict[str, str]]:
        with path.open(newline="", encoding="utf-8") as handle:
            return {row["task_id"]: row for row in csv.DictReader(handle)}

    history, locked = read_csv(repo / HISTORY), read_csv(repo / LOCKED_NOP)
    variants = config.state_dir / "variant-records"
    for row in rows:
        task = row["task_id"]
        assessment = {
            "task_id": task,
            "tags": [
                health_tag(row, locked.get(task)),
                solve_summary(history.get(task))["tag"],
                verdict_tag(row),
            ],
            "ledger_status": row["status"],
            "ledger_reason": row["reason"],
            "ledger_verdict": row["verdict"],
            "ledger_verdict_evidence": row["verdict_evidence"],
        }
        _atomic(
            variants / f"format-code__{task}" / "nightly.json",
            {
                "transform": "task-health-tags@1",
                "created_at": "1970-01-01T00:00:00+00:00",
                "task_name": task,
                "inputs": {"assessment": assessment},
            },
        )
    _atomic(config.state_dir / "verdicts.json", verdicts)
    return verdicts


def replay_history(config: RefreshConfig, changed_jobs: list[Path]) -> dict[str, Any]:
    from evallab.live_watch import run_watch

    total = 0
    for job in changed_jobs:
        outcome = run_watch(runs_dirs=[job], out_dir=config.state_dir / "history" / _key(job))
        total += sum(
            alert["rule"] == "history_mining"
            for status in outcome["statuses"]
            for alert in status["open_alerts"]
        )
    return {"history_mining": total, "jobs": len(changed_jobs)}


def refresh_pages(config: RefreshConfig, jobs: list[Path]) -> dict[str, Any]:
    from evallab.task_pages import TaskPages, default_store

    mirrors = []
    for job in jobs:
        mirror = config.state_dir / "jobs" / _key(job) / job.name
        mirror.mkdir(parents=True, exist_ok=True)
        wanted = {}
        for child in job.iterdir():
            if child.name not in {"processed", "watch"}:
                wanted[child.name] = child.resolve()
        wanted["processed"] = (config.state_dir / "processed" / _key(job)).resolve()
        wanted["watch"] = (config.state_dir / "history" / _key(job)).resolve()
        for child in mirror.iterdir():
            if child.name not in wanted:
                child.unlink()
        for name, target in wanted.items():
            link = mirror / name
            if link.is_symlink() and link.readlink() == target:
                continue
            link.unlink(missing_ok=True)
            link.symlink_to(target, target_is_directory=target.is_dir())
        mirrors.append(mirror)
    outcome = TaskPages(
        config.state_dir / "task-pages",
        store=config.readers_store or default_store(),
        variants_dir=config.state_dir / "variant-records",
        trusted_globs=("*",),
        laminar_api_key=None,
    ).sync(mirrors)
    if outcome["failed"]:
        raise RuntimeError(f"Task pages failed: {outcome['failed']}")
    return outcome


def campaign_spend(config: RefreshConfig, roots: tuple[Path, ...]) -> dict[str, Any]:
    from evallab.campaign_approval import campaign_spend_breakdown, campaign_spend_usd

    campaigns = {}
    for root in roots:
        directory = root / "runs/campaigns"
        if not directory.is_dir():
            continue
        for campaign in sorted(directory.iterdir()):
            if not (campaign / "campaign.json").is_file():
                continue
            reserved, settled, total = campaign_spend_usd(root, campaign.name)
            # Scope each meter to its queue; cloned roots cannot be added as if
            # they were independent bills. The source path remains explicit.
            campaigns[f"{campaign.name}@{root.name}"] = {
                "queue_root": str(root),
                "reserved": reserved,
                "settled": settled,
                "total": total,
                "sources": campaign_spend_breakdown(root, campaign.name),
            }
    return campaigns


def write_facts(config: RefreshConfig, digest: dict[str, Any], now: datetime) -> Path:
    facts_root = config.facts_root or Path.home() / ".local/state/daily-report"
    path = facts_root / "inputs/evallab-nightly.json"
    _atomic(path, digest)
    # The collector reads the durable input afresh before the reporter. Also
    # expose it in the next report's existing facts format now, without running
    # collection or the paid generation path. Share the report's actual lock.
    with (facts_root / "run.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return path
        local = now.astimezone(ET)
        day = local.date() if local.hour < 7 else local.date() + timedelta(days=1)
        target = facts_root / "runs" / day.isoformat()
        target.mkdir(parents=True, exist_ok=True)
        facts = _json(target / "facts.json")
        facts["evallab_nightly"] = digest
        _atomic(target / "facts.json", facts)
        markdown = target / "facts.md"
        text = markdown.read_text(encoding="utf-8") if markdown.exists() else ""
        marker = "## Eval Lab nightly\n"
        if marker in text:
            before, _, after = text.partition(marker)
            _, separator, rest = after.partition("\n## ")
            text = before + ("## " + rest if separator else "")
        markdown.write_text(
            text.rstrip() + "\n\n" + "\n".join(digest["lines"]) + "\n", encoding="utf-8"
        )
    return path


def run_refresh(config: RefreshConfig, *, now: datetime | None = None) -> dict[str, Any]:
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("Refresh time needs a timezone")
    config.state_dir.mkdir(parents=True, exist_ok=True)
    with (config.state_dir / "refresh.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "locked"}
        state_path = config.state_dir / "state.json"
        previous = _json(state_path)
        checkouts = queue_roots(config)
        roots = [
            root / name for root in checkouts for name in ("runs", "jobs") if (root / name).is_dir()
        ]
        from evallab.live_watch import discover_trials

        jobs = discover_job_dirs(roots)
        completed = set(jobs)
        observed = sorted(completed | {job for job, _ in discover_trials([*roots, *jobs])})
        stamps = {str(job): _stamp_tree(job, raw_job=True) for job in observed}
        fingerprints = {job: _fingerprint(value) for job, value in stamps.items()}
        metadata = _metadata(config, checkouts)
        selection = {
            "schema": SCHEMA,
            "producer": _fingerprint(_stamp_tree(config.repo_root / "src")),
            "roots": [str(root) for root in roots],
            "facts_root": str(config.facts_root),
            "verdict_root": str(config.verdict_root),
            "metadata": _fingerprint(metadata),
            "jobs": fingerprints,
        }
        if previous.get("selection") == selection:
            return {
                "status": "noop",
                "jobs": sum(str(job) not in previous.get("failed_jobs", {}) for job in observed),
                "refused_jobs": len(previous.get("failed_jobs", {})),
                "high_water_mark": previous["high_water_mark"],
            }
        old_jobs = (previous.get("selection") or {}).get("jobs", {})
        changed = [job for job in observed if fingerprints[str(job)] != old_jobs.get(str(job))]
        # Fail on missing or mismatched reviewed inputs before walking every run.
        verdicts = refresh_verdicts(config)
        # Completed jobs can be projected; ongoing trials are still replayed and
        # included in the HAR-178 census and HAR-176 pages as evidence arrives.
        failed_jobs = {
            job: failure
            for job, failure in previous.get("failed_jobs", {}).items()
            if job in fingerprints
        }
        processed_jobs = {
            job: stamp
            for job, stamp in previous.get("processed_jobs", {}).items()
            if job in fingerprints
        }
        for job in (job for job in changed if job in completed):
            output = config.state_dir / "processed" / _key(job)
            try:
                process_job(
                    job,
                    root=config.repo_root,
                    ingest=False,
                    publish=False,
                    output_dir=output,
                    parquet_root=config.state_dir / "parquet",
                )
            except Exception as exc:
                failed_jobs[str(job)] = {
                    "reason_class": type(exc).__name__,
                    "reason": str(exc),
                    "input_fingerprint": fingerprints[str(job)],
                }
                processed_jobs.pop(str(job), None)
                if output.exists():
                    shutil.rmtree(output)  # Only this service's partial job reports.
            else:
                failed_jobs.pop(str(job), None)
                processed_jobs[str(job)] = fingerprints[str(job)]
        accepted = [job for job in observed if str(job) not in failed_jobs]
        changed = [job for job in changed if str(job) not in failed_jobs]
        new_runs = sum(
            str(job) not in old_jobs or str(job) in previous.get("failed_jobs", {})
            for job in changed
        )
        trials = refresh_trials(config, _accepted_roots(roots, {Path(job) for job in failed_jobs}))
        replay = replay_history(config, changed)
        pages = refresh_pages(config, accepted)
        campaigns = campaign_spend(config, checkouts)
        old_verdicts = previous.get("verdicts", {})
        changes = sum(old_verdicts.get(task) != verdict for task, verdict in verdicts.items())
        new_trials = sum(
            1 for job in changed for child in job.iterdir() if (child / "result.json").is_file()
        )
        spending = {
            name: {k: v for k, v in values.items() if k != "sources"}
            for name, values in campaigns.items()
        }
        sources = {name: values["sources"] for name, values in campaigns.items()}
        reasons = Counter(failure["reason_class"] for failure in failed_jobs.values())
        refusal = (
            ", ".join(f"{reason} ×{count}" for reason, count in sorted(reasons.items())) or "none"
        )
        digest = {
            "schema": DIGEST_SCHEMA,
            "generated_at": moment.isoformat(),
            "campaigns": campaigns,
            "failed_jobs": failed_jobs,
            "lines": [
                "## Eval Lab nightly",
                f"- Refreshed: {moment.astimezone(ET).isoformat(timespec='seconds')} ET; refresh spend $0.",
                f"- Queue roots: {len(checkouts)}; outputs: {config.state_dir}; "
                f"{len(failed_jobs)} jobs refused: {refusal}.",
                f"- New runs: {new_runs}; changed runs: {len(changed)}.",
                f"- New/changed trial results: {new_trials}.",
                f"- Legit trials: {trials['legit']} / {trials['total']} (HAR-178).",
                f"- New verdict changes: {changes} (HAR-177; initial values count as new).",
                f"- History-mining alerts on refreshed runs: {replay.get('history_mining', 0)} (HAR-184).",
                "- Campaign spend to date (USD; each queue scoped separately): "
                + json.dumps(spending, sort_keys=True),
                "- Spend sources (USD): " + json.dumps(sources, sort_keys=True),
            ],
        }
        digest_path = write_facts(config, digest, moment)
        high_water = max(
            [previous.get("high_water_mark", 0)]
            + [stamp[1] for value in stamps.values() for stamp in value]
            + [stamp[1] for stamp in metadata]
        )
        _atomic(
            state_path,
            {
                "schema": SCHEMA,
                "selection": selection,
                "verdicts": verdicts,
                "failed_jobs": failed_jobs,
                "processed_jobs": processed_jobs,
                "high_water_mark": high_water,
                "refreshed_at": moment.isoformat(),
            },
        )
        return {
            "status": "refreshed",
            "new_runs": new_runs,
            "changed_runs": len(changed),
            "refused_jobs": len(failed_jobs),
            "trials": trials,
            "verdict_changes": changes,
            "history": replay,
            "pages": pages,
            "digest": str(digest_path),
            "high_water_mark": high_water,
        }
