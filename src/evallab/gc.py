"""Disk discipline for unpromoted Harbor job directories.

Completed, ingested, projected, unpromoted jobs under ``runs/`` are compressed
after 14 days and pruned after 60. Each action leaves a tombstone. Default
mode is a dry-run plan; ``--apply`` mutates. Time and catalog I/O are injected.
"""

from __future__ import annotations

import json
import shutil
import tarfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from evallab.queue import DirectoryQueue, new_ulid
from evallab.results import JobRecord, load_job, sha256_file
from evallab.schemas import QueueEvent

COMPRESS_AFTER = timedelta(days=14)
PRUNE_AFTER = timedelta(days=60)
TOMBSTONE_DIRNAME = ".tombstones"
ARCHIVE_DIRNAME = Path(".gc") / "archives"
EVIDENCE_DIR = Path("research") / "evidence"
DISCOVERIES = Path("digests") / "DISCOVERIES.md"

Clock = Callable[[], datetime]
EventAppender = Callable[[QueueEvent], None]
ActionKind = Literal["compress", "prune"]


class CatalogStore(Protocol):
    def get(self, job_id: str) -> CatalogEntry | None: ...

    def set_path(self, job_id: str, evidence_path: str) -> None: ...


@dataclass
class CatalogEntry:
    job_id: str
    evidence_path: str
    ingested: bool = False
    projected: bool = False


class MemoryCatalog:
    """In-memory catalog used by tests and as the default injectable store."""

    def __init__(self, entries: Iterable[CatalogEntry] = ()) -> None:
        self._entries = {item.job_id: item for item in entries}

    def get(self, job_id: str) -> CatalogEntry | None:
        return self._entries.get(job_id)

    def set_path(self, job_id: str, evidence_path: str) -> None:
        existing = self._entries.get(job_id)
        if existing is None:
            self._entries[job_id] = CatalogEntry(
                job_id=job_id, evidence_path=evidence_path, ingested=True, projected=True
            )
            return
        existing.evidence_path = evidence_path

    def lookup_path(self, job_id: str) -> str | None:
        entry = self.get(job_id)
        return None if entry is None else entry.evidence_path


@dataclass(frozen=True)
class GcAction:
    action: ActionKind
    job_id: str
    job_name: str
    spec_id: str | None
    path: Path
    age_days: float
    size_bytes: int
    reason: str
    why: str
    rewards: dict[str, float]
    digests: dict[str, str]


@dataclass(frozen=True)
class GcSkip:
    path: Path
    job_id: str | None
    job_name: str
    reason: str


@dataclass
class GcPlan:
    actions: list[GcAction] = field(default_factory=list)
    skipped: list[GcSkip] = field(default_factory=list)

    @property
    def reclaim_bytes(self) -> int:
        return sum(item.size_bytes for item in self.actions)

    @property
    def empty_reason(self) -> str | None:
        if self.actions:
            return None
        if not self.skipped:
            return "no completed jobs under runs/"
        return "no completed+ingested+unpromoted candidates"


@dataclass
class GcApplyResult:
    plan: GcPlan
    tombstones: list[Path]
    events: list[QueueEvent]


def utcnow() -> datetime:
    return datetime.now(UTC)


def tombstone_dir(runs_dir: Path) -> Path:
    return runs_dir / TOMBSTONE_DIRNAME


def archive_dir(runs_dir: Path) -> Path:
    return runs_dir / ARCHIVE_DIRNAME


def tombstone_path(runs_dir: Path, job_id: str) -> Path:
    return tombstone_dir(runs_dir) / f"{job_id}.json"


def archive_path(runs_dir: Path, job_id: str) -> Path:
    return archive_dir(runs_dir) / f"{job_id}.tar.gz"


def parse_finished_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def directory_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def job_digests(job: JobRecord) -> dict[str, str]:
    digests: dict[str, str] = {}
    for name in ("config.json", "lock.json", "result.json"):
        path = job.path / name
        if path.is_file():
            digests[name] = f"sha256:{sha256_file(path)}"
    return digests


def reward_summary(job: JobRecord) -> dict[str, float]:
    summary: dict[str, float] = {}
    for trial in job.trials:
        for name, value in trial.rewards.items():
            summary[name] = summary.get(name, 0.0) + float(value)
    if job.trials:
        for name in list(summary):
            summary[name] = summary[name] / len(job.trials)
    return summary


def spec_id_of(job: JobRecord) -> str | None:
    metadata = job.metadata
    if isinstance(metadata.get("spec_id"), str):
        return metadata["spec_id"]
    experiment = metadata.get("experiment")
    if isinstance(experiment, dict) and isinstance(experiment.get("spec_id"), str):
        return experiment["spec_id"]
    config = job.config
    if isinstance(config.get("spec_id"), str):
        return config["spec_id"]
    return None


def is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def is_protected_layout(path: Path, *, repo_root: Path, runs_dir: Path) -> bool:
    resolved = path.resolve()
    if is_under(resolved, repo_root / EVIDENCE_DIR):
        return True
    if is_under(resolved, tombstone_dir(runs_dir)):
        return True
    return is_under(resolved, archive_dir(runs_dir))


def collect_reference_tokens(texts: Iterable[str]) -> set[str]:
    tokens: set[str] = set()
    for text in texts:
        split_text = text.replace("`", " ").replace("|", " ")
        split_text = split_text.replace("(", " ").replace(")", " ")
        for raw in split_text.split():
            cleaned = raw.strip(".,;:[]()\"'")
            if cleaned:
                tokens.add(cleaned)
            if "runs/" in cleaned:
                tokens.add(cleaned.rsplit("/", maxsplit=1)[-1])
            if "evidence/runs/" in cleaned:
                tokens.add(Path(cleaned).name)
    return tokens


def load_reference_texts(repo_root: Path) -> list[str]:
    texts: list[str] = []
    digest_root = repo_root / "digests"
    if digest_root.is_dir():
        for path in sorted(digest_root.rglob("*")):
            if path.is_file() and path.suffix in {".md", ".txt", ".json"}:
                texts.append(path.read_text(errors="replace"))
    discoveries = repo_root / DISCOVERIES
    if discoveries.is_file() and discoveries.read_text(errors="replace") not in texts:
        texts.append(discoveries.read_text(errors="replace"))
    return texts


def is_referenced(job: JobRecord, tokens: set[str]) -> bool:
    names = {
        job.id,
        job.name,
        job.path.name,
        job.path.as_posix(),
        spec_id_of(job) or "",
    }
    names.discard("")
    return any(name in tokens for name in names)


def iter_run_jobs(runs_dir: Path) -> list[Path]:
    if not runs_dir.is_dir():
        return []
    jobs: list[Path] = []
    for child in sorted(runs_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        result = child / "result.json"
        if not result.is_file():
            continue
        try:
            payload = json.loads(result.read_text())
        except json.JSONDecodeError:
            continue
        if "n_total_trials" in payload and "stats" in payload:
            jobs.append(child)
    return jobs


GC_CATALOG_RELATIVE = Path("derived") / "gc-catalog.json"
INGEST_LEDGER_RELATIVE = Path("derived") / "ingest-ledger.jsonl"


def retarget_postgres(database_url: str, job_id: str, evidence_path: str) -> None:
    """Point jobs/trials.evidence_path at the tombstone. Best-effort."""
    import psycopg

    with psycopg.connect(database_url) as connection:
        connection.execute(
            "UPDATE jobs SET evidence_path = %s, updated_at = now() WHERE id = %s",
            (evidence_path, job_id),
        )
        connection.execute(
            "UPDATE trials SET evidence_path = %s, updated_at = now() WHERE job_id = %s",
            (evidence_path, job_id),
        )


def _optional_postgres_retarget(job_id: str, evidence_path: str) -> None:
    try:
        from evallab.runner import database_url_from_environment

        retarget_postgres(database_url_from_environment(), job_id, evidence_path)
    except Exception:
        return


class FilesystemCatalog:
    """Durable catalog: parquet + ingest ledger + derived/gc-catalog.json.

    ``set_path`` rewrites ``derived/gc-catalog.json`` so a new process load
    sees the tombstone. Optional SQL retarget updates Postgres when available.
    """

    def __init__(
        self,
        repo_root: Path,
        *,
        sql_retarget: Callable[[str, str], None] | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.catalog_path = self.repo_root / GC_CATALOG_RELATIVE
        self.sql_retarget = sql_retarget
        self._entries: dict[str, CatalogEntry] = {}
        self._load()

    def _put(self, entry: CatalogEntry) -> None:
        self._entries[entry.job_id] = entry

    def _load(self) -> None:
        self._entries = {}
        parquet_root = self.repo_root / "derived" / "parquet"
        if parquet_root.is_dir():
            for child in parquet_root.iterdir():
                if child.is_dir() and child.name.startswith("job_id="):
                    job_id = child.name.removeprefix("job_id=")
                    self._put(
                        CatalogEntry(
                            job_id=job_id,
                            evidence_path=f"runs/{job_id}",
                            ingested=True,
                            projected=True,
                        )
                    )
        ledger = self.repo_root / INGEST_LEDGER_RELATIVE
        if ledger.is_file():
            for line in ledger.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                job_id = str(row.get("job_id") or "")
                if not job_id:
                    continue
                self._put(
                    CatalogEntry(
                        job_id=job_id,
                        evidence_path=str(row.get("evidence_path") or f"runs/{job_id}"),
                        ingested=bool(row.get("ingested", True)),
                        projected=bool(row.get("projected", True)),
                    )
                )
        if self.catalog_path.is_file():
            try:
                payload = json.loads(self.catalog_path.read_text())
            except json.JSONDecodeError:
                payload = {}
            jobs = payload.get("jobs") if isinstance(payload, dict) else None
            if isinstance(jobs, dict):
                for job_id, raw in jobs.items():
                    if not isinstance(raw, dict):
                        continue
                    existing = self._entries.get(str(job_id))
                    ingested_default = existing.ingested if existing else True
                    projected_default = existing.projected if existing else True
                    self._put(
                        CatalogEntry(
                            job_id=str(job_id),
                            evidence_path=str(raw.get("evidence_path") or ""),
                            ingested=bool(raw.get("ingested", ingested_default)),
                            projected=bool(raw.get("projected", projected_default)),
                        )
                    )

    def get(self, job_id: str) -> CatalogEntry | None:
        return self._entries.get(job_id)

    def set_path(self, job_id: str, evidence_path: str) -> None:
        existing = self._entries.get(job_id)
        if existing is None:
            self._entries[job_id] = CatalogEntry(
                job_id=job_id,
                evidence_path=evidence_path,
                ingested=True,
                projected=True,
            )
        else:
            existing.evidence_path = evidence_path
        self._write()
        if self.sql_retarget is not None:
            self.sql_retarget(job_id, evidence_path)

    def _write(self) -> None:
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "jobs": {
                job_id: {
                    "job_id": entry.job_id,
                    "evidence_path": entry.evidence_path,
                    "ingested": entry.ingested,
                    "projected": entry.projected,
                }
                for job_id, entry in sorted(self._entries.items())
            }
        }
        temporary = self.catalog_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        temporary.replace(self.catalog_path)


def filesystem_catalog(
    repo_root: Path,
    *,
    sql_retarget: Callable[[str, str], None] | None = None,
) -> FilesystemCatalog:
    return FilesystemCatalog(repo_root, sql_retarget=sql_retarget)


def classify_job(
    job: JobRecord,
    *,
    now: datetime,
    repo_root: Path,
    runs_dir: Path,
    catalog: CatalogStore,
    tokens: set[str],
) -> GcAction | GcSkip:
    if is_protected_layout(job.path, repo_root=repo_root, runs_dir=runs_dir):
        return GcSkip(job.path, job.id, job.name, "protected: research/evidence or gc layout")
    if is_referenced(job, tokens):
        return GcSkip(job.path, job.id, job.name, "referenced by digest or DISCOVERIES")
    entry = catalog.get(job.id)
    if entry is None or not entry.ingested or not entry.projected:
        return GcSkip(job.path, job.id, job.name, "not ingested+projected")
    finished = parse_finished_at(str(job.result.get("finished_at") or ""))
    if finished is None:
        return GcSkip(job.path, job.id, job.name, "missing finished_at")
    age = now - finished
    size = directory_size(job.path)
    archive = archive_path(runs_dir, job.id)
    if age >= PRUNE_AFTER:
        target = archive if archive.is_file() else job.path
        return GcAction(
            action="prune",
            job_id=job.id,
            job_name=job.name,
            spec_id=spec_id_of(job),
            path=target,
            age_days=age.total_seconds() / 86400,
            size_bytes=directory_size(target),
            reason="completed+ingested+unpromoted age>=60d",
            why="prune_60d",
            rewards=reward_summary(job),
            digests=(
                job_digests(job)
                if job.path.is_dir() and (job.path / "result.json").is_file()
                else {}
            ),
        )
    if age >= COMPRESS_AFTER:
        return GcAction(
            action="compress",
            job_id=job.id,
            job_name=job.name,
            spec_id=spec_id_of(job),
            path=job.path,
            age_days=age.total_seconds() / 86400,
            size_bytes=size,
            reason="completed+ingested+unpromoted age>=14d",
            why="compress_14d",
            rewards=reward_summary(job),
            digests=job_digests(job),
        )
    return GcSkip(job.path, job.id, job.name, f"too young ({age.days}d)")


def plan_gc(
    *,
    repo_root: Path,
    runs_dir: Path | None = None,
    clock: Clock = utcnow,
    catalog: CatalogStore | None = None,
    reference_texts: Iterable[str] | None = None,
) -> GcPlan:
    root = repo_root.resolve()
    runs = (runs_dir or root / "runs").resolve()
    store = catalog if catalog is not None else filesystem_catalog(root)
    tokens = collect_reference_tokens(
        reference_texts if reference_texts is not None else load_reference_texts(root)
    )
    now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    plan = GcPlan()
    for job_dir in iter_run_jobs(runs):
        try:
            job = load_job(job_dir)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            plan.skipped.append(GcSkip(job_dir, None, job_dir.name, f"not a completed job: {exc}"))
            continue
        decision = classify_job(
            job,
            now=now,
            repo_root=root,
            runs_dir=runs,
            catalog=store,
            tokens=tokens,
        )
        if isinstance(decision, GcAction):
            plan.actions.append(decision)
        else:
            plan.skipped.append(decision)
    seen = {item.job_id for item in plan.actions}
    for stone in sorted(tombstone_dir(runs).glob("*.json")):
        try:
            payload = json.loads(stone.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        job_id = str(payload.get("job_id") or stone.stem)
        if job_id in seen:
            continue
        if payload.get("action") == "prune" or payload.get("why") == "prune_60d":
            continue
        if payload.get("why") == "referenced by digest or DISCOVERIES":
            continue
        job_name = str(payload.get("job_name") or job_id)
        if job_name in tokens or job_id in tokens:
            continue
        anchor = parse_finished_at(str(payload.get("finished_age_anchor") or ""))
        if anchor is None:
            age_days = float(payload.get("age_days") or 0)
            anchor = now - timedelta(days=age_days)
        age = now - anchor
        if age < PRUNE_AFTER:
            continue
        archive = archive_path(runs, job_id)
        target = archive if archive.is_file() else stone
        if not archive.is_file() and not (runs / job_name).exists():
            # Already pruned except tombstone — do not treat the tombstone as reclaim.
            continue
        plan.actions.append(
            GcAction(
                action="prune",
                job_id=job_id,
                job_name=job_name,
                spec_id=payload.get("spec_id") if isinstance(payload.get("spec_id"), str) else None,
                path=target,
                age_days=age.total_seconds() / 86400,
                size_bytes=directory_size(target) if target != stone else 0,
                reason="completed+ingested+unpromoted age>=60d",
                why="prune_60d",
                rewards=payload.get("reward_summary")
                if isinstance(payload.get("reward_summary"), dict)
                else {},
                digests=payload.get("digests") if isinstance(payload.get("digests"), dict) else {},
            )
        )
    return plan


def write_tombstone(
    runs_dir: Path,
    action: GcAction,
    *,
    clock: Clock,
) -> Path:
    path = tombstone_path(runs_dir, action.job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": action.job_id,
        "spec_id": action.spec_id,
        "job_name": action.job_name,
        "digests": action.digests,
        "reward_summary": action.rewards,
        "why": action.why,
        "action": action.action,
        "removed_at": clock().isoformat(),
        "source_path": action.path.as_posix(),
        "age_days": action.age_days,
        "finished_age_anchor": (clock() - timedelta(days=action.age_days)).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def _compress_job(action: GcAction, runs_dir: Path) -> Path:
    dest = archive_path(runs_dir, action.job_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as archive:
        archive.add(action.path, arcname=action.job_name)
    shutil.rmtree(action.path)
    return dest


def _prune_job(action: GcAction, runs_dir: Path) -> None:
    if action.path.exists():
        if action.path.is_dir():
            shutil.rmtree(action.path)
        else:
            action.path.unlink()
    archive = archive_path(runs_dir, action.job_id)
    if archive.is_file():
        archive.unlink()


def apply_gc(
    plan: GcPlan,
    *,
    repo_root: Path,
    runs_dir: Path | None = None,
    catalog: CatalogStore | None = None,
    clock: Clock = utcnow,
    append_event: EventAppender | None = None,
) -> GcApplyResult:
    root = repo_root.resolve()
    runs = (runs_dir or root / "runs").resolve()
    store = (
        catalog
        if catalog is not None
        else filesystem_catalog(root, sql_retarget=_optional_postgres_retarget)
    )
    events: list[QueueEvent] = []
    tombstones: list[Path] = []
    writer = append_event or DirectoryQueue(root / "queue").append_event
    for action in plan.actions:
        if action.action == "compress":
            _compress_job(action, runs)
        else:
            _prune_job(action, runs)
        stone = write_tombstone(runs, action, clock=clock)
        tombstones.append(stone)
        relative = stone.relative_to(root).as_posix() if is_under(stone, root) else stone.as_posix()
        store.set_path(action.job_id, relative)
        event = QueueEvent(
            event_id=new_ulid(),
            spec_id=action.spec_id or f"job-{action.job_id}",
            occurred_at=clock(),
            event="gc_compressed" if action.action == "compress" else "gc_pruned",
            actor="gc",
            reason_code=action.why,
            job_name=action.job_name,
        )
        writer(event)
        events.append(event)
    return GcApplyResult(plan=plan, tombstones=tombstones, events=events)


def format_plan(plan: GcPlan) -> str:
    lines = [
        f"gc plan: {len(plan.actions)} action(s), "
        f"{len(plan.skipped)} skipped, reclaim={plan.reclaim_bytes} bytes"
    ]
    if plan.empty_reason:
        lines.append(f"empty: {plan.empty_reason}")
    for action in plan.actions:
        lines.append(
            f"  {action.action:8} {action.job_name}  id={action.job_id}  "
            f"age={action.age_days:.1f}d  bytes={action.size_bytes}  {action.reason}"
        )
    for skip in plan.skipped:
        lines.append(f"  skip     {skip.job_name}  {skip.reason}")
    return "\n".join(lines)


def append_gc_plan_to_digest(digest_path: Path, plan: GcPlan) -> None:
    section = [
        "",
        "## GC would reclaim",
        "",
        "Nightly runs `evallab gc` in plan-only mode. Apply stays human-triggered.",
        "",
        f"- Actions: {len(plan.actions)}",
        f"- Skipped: {len(plan.skipped)}",
        f"- Would reclaim: {plan.reclaim_bytes} bytes",
    ]
    if plan.empty_reason:
        section.append(f"- {plan.empty_reason}")
    for action in plan.actions:
        section.append(
            f"- WOULD {action.action} `{action.job_name}` "
            f"({action.age_days:.1f}d, {action.size_bytes} bytes) — {action.why}"
        )
    section.append("")
    existing = digest_path.read_text() if digest_path.is_file() else ""
    if "## GC would reclaim" in existing:
        prefix = existing.split("## GC would reclaim", 1)[0].rstrip() + "\n"
        digest_path.write_text(prefix + "\n".join(section[1:]))
        return
    digest_path.write_text(existing.rstrip() + "\n" + "\n".join(section))


def nightly_gc_plan(
    repo_root: Path,
    *,
    clock: Clock = utcnow,
    catalog: CatalogStore | None = None,
    runs_dir: Path | None = None,
) -> GcPlan:
    return plan_gc(repo_root=repo_root, runs_dir=runs_dir, clock=clock, catalog=catalog)


def doctor_disk_line(
    repo_root: Path,
    *,
    clock: Clock = utcnow,
    catalog: CatalogStore | None = None,
    runs_dir: Path | None = None,
    usage: Mapping[str, int] | None = None,
) -> str:
    runs = (runs_dir or repo_root / "runs").resolve()
    plan = plan_gc(repo_root=repo_root, runs_dir=runs, clock=clock, catalog=catalog)
    used = directory_size(runs)
    if usage is not None:
        used = int(usage.get("used", used))
    compress_n = sum(1 for item in plan.actions if item.action == "compress")
    prune_n = sum(1 for item in plan.actions if item.action == "prune")
    return (
        f"disk  runs={used}B  compress-candidates={compress_n}  "
        f"prune-candidates={prune_n}  would-reclaim={plan.reclaim_bytes}B"
    )


def run_gc(
    repo_root: Path,
    *,
    apply: bool = False,
    clock: Clock = utcnow,
    catalog: CatalogStore | None = None,
    runs_dir: Path | None = None,
    append_event: EventAppender | None = None,
) -> tuple[GcPlan, GcApplyResult | None]:
    plan = plan_gc(repo_root=repo_root, runs_dir=runs_dir, clock=clock, catalog=catalog)
    if not apply:
        return plan, None
    return plan, apply_gc(
        plan,
        repo_root=repo_root,
        runs_dir=runs_dir,
        catalog=catalog,
        clock=clock,
        append_event=append_event,
    )


def catalog_path_exists(repo_root: Path, catalog: CatalogStore, job_id: str) -> bool:
    entry = catalog.get(job_id)
    if entry is None:
        return True
    path = Path(entry.evidence_path)
    if not path.is_absolute():
        path = repo_root / path
    return path.exists()
