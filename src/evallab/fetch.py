"""Pinned Harbor Hub / adapter-lane acquisition for library/benchmarks/."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from evallab.runner import subscription_environment

UNPINNED_VERSIONS = frozenset({"latest", "head", "main", "master"})
PROTECTED_INGESTS = frozenset(
    {"aime", "gpqa-diamond", "humanevalfix", "terminal-bench-sample"}
)
REQUIRED_HEADINGS = ("## Source and pin", "## License", "## Counts / subset")
RESOURCE_HEADINGS = ("## Lane / resources", "## Resources")
SAMPLE_HEADING = "## Sample verification"
STATE_NAME = ".fetch.json"
MANIFEST_NAME = "MANIFEST.md"
SKIP_DIGEST_NAMES = frozenset({MANIFEST_NAME, STATE_NAME})
MAX_N_CONCURRENT = 2
HUB_ROW = re.compile(r"│ (\S+) +│ (\S+) +│\s+(\d+) │")
SHA_RE = re.compile(r"\b([0-9a-f]{40})\b")
PIN_IN_TEXT = re.compile(
    r"`([A-Za-z0-9._/-]+)@([A-Za-z0-9._-]+)`|harbor download ([A-Za-z0-9._/-]+)@([A-Za-z0-9._-]+)"
)
TREE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

NAMED_ADAPTER_LANES: tuple[tuple[str, str], ...] = (
    ("harbor/adapters/aime", "AIME contest math (Hub aime@1.0 preferred)"),
    ("harbor/adapters/swebench", "SWE-bench Verified — disk-heavy, not a laptop canary"),
    ("harbor/adapters/swebenchpro", "SWE-bench Pro — disk-heavy"),
    ("harbor/adapters/livecodebench", "LiveCodeBench time-split codegen"),
    ("harbor/adapters/bfcl", "BFCL tool-calling; prefer Hub bfcl_parity@1.0"),
    ("harbor/adapters/gaia", "GAIA — live web; weak frozen oracle"),
    ("harbor/adapters/gaia2", "GAIA2 refresh — live web"),
    ("harbor/adapters/hle", "Humanity's Last Exam — access/answer-key diligence"),
    ("harbor/adapters/osworld", "OSWorld desktop/GUI — skip for local CPU"),
    ("harbor/adapters/ml_dev_bench", "ML-dev — GPU/cloud"),
    ("harbor/adapters/mlgym-bench", "MLgym — GPU/cloud"),
    ("harbor/adapters/arc_agi_2", "ARC-AGI-2 grid puzzles"),
)


class FetchError(ValueError):
    """User-facing fetch/audit refusal."""


@dataclass(frozen=True)
class Pin:
    name: str
    version: str

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"


@dataclass(frozen=True)
class DatasetListing:
    name: str
    version: str
    task_count: int | None
    source: str
    note: str = ""

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"


@dataclass(frozen=True)
class SampleRow:
    task: str
    oracle_job: str
    oracle: float
    nop_job: str
    nop: float


@dataclass(frozen=True)
class FetchState:
    pin: str
    version: str
    task_git_url: str | None
    task_git_sha: str | None
    tree_digest: str
    task_count: int
    lane: str
    license: str
    sample: tuple[SampleRow, ...] = ()


@dataclass(frozen=True)
class FetchResult:
    status: str
    pin: str
    dest: Path
    message: str
    manifest_path: Path | None = None


@dataclass(frozen=True)
class AuditRow:
    name: str
    status: str
    detail: str


@dataclass
class ControlCall:
    task_path: Path
    agent: str
    job_name: str
    jobs_dir: Path
    n_concurrent: int
    n_attempts: int


class HarborBackend(Protocol):
    def list_hub_datasets(self) -> list[DatasetListing]: ...

    def download(self, pin: str, dest: Path) -> None: ...

    def run_control(self, call: ControlCall) -> float: ...


CommandRunner = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]


def parse_pin(ref: str) -> Pin:
    text = ref.strip()
    if not text:
        raise FetchError("unpinned ref: empty; require name@version (never @latest)")
    if "@" not in text:
        raise FetchError(
            f"unpinned ref {text!r}: require name@version (never @latest)"
        )
    name, version = text.rsplit("@", 1)
    name = name.strip()
    version = version.strip()
    if not name or not version:
        raise FetchError(
            f"unpinned ref {text!r}: require name@version (never @latest)"
        )
    if version.lower() in UNPINNED_VERSIONS:
        raise FetchError(
            f"refused unpinned @{version}: fetch requires an immutable pin "
            f"(never @latest / @head / @main / @master)"
        )
    return Pin(name=name, version=version)


def material_digest(dest: Path) -> str:
    if not dest.is_dir():
        raise FetchError(f"benchmark directory is missing: {dest}")
    aggregate = hashlib.sha256()
    files = sorted(
        path
        for path in dest.rglob("*")
        if path.is_file()
        and path.name not in SKIP_DIGEST_NAMES
        and ".git" not in path.parts
    )
    if not files:
        raise FetchError(f"no material files under {dest}")
    for candidate in files:
        relative = candidate.relative_to(dest).as_posix()
        file_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        aggregate.update(f"{file_digest}  ./{relative}\n".encode())
    return f"sha256:{aggregate.hexdigest()}"


def count_tasks(dest: Path) -> int:
    return sum(1 for path in dest.rglob("task.toml") if path.is_file())


def list_task_dirs(dest: Path) -> list[Path]:
    return sorted(path.parent for path in dest.rglob("task.toml") if path.is_file())


def parse_hub_list_table(text: str) -> list[DatasetListing]:
    rows: list[DatasetListing] = []
    seen: set[str] = set()
    for name, version, tasks in HUB_ROW.findall(text):
        if version.lower() in UNPINNED_VERSIONS:
            continue
        ref = f"{name}@{version}"
        if ref in seen:
            continue
        seen.add(ref)
        rows.append(
            DatasetListing(
                name=name, version=version, task_count=int(tasks), source="hub"
            )
        )
    return rows


def detect_git_origin(dest: Path) -> tuple[str | None, str | None]:
    for git_dir in dest.rglob(".git"):
        if not git_dir.is_dir() and not git_dir.is_file():
            continue
        url = _read_git_remote(git_dir)
        sha = _read_git_sha(git_dir)
        if url or sha:
            return url, sha
    return None, None


def _read_git_sha(git_dir: Path) -> str | None:
    if git_dir.is_file():
        return None
    head = git_dir / "HEAD"
    if not head.is_file():
        return None
    text = head.read_text().strip()
    if text.startswith("ref:"):
        ref = text.split(":", 1)[1].strip()
        ref_path = git_dir / ref
        if ref_path.is_file():
            return ref_path.read_text().strip() or None
        packed = git_dir / "packed-refs"
        if packed.is_file():
            for line in packed.read_text().splitlines():
                if line.startswith("#") or " " not in line:
                    continue
                sha, name = line.split(" ", 1)
                if name.strip() == ref:
                    return sha.strip()
        return None
    return text or None


def _read_git_remote(git_dir: Path) -> str | None:
    if git_dir.is_file():
        return None
    config = git_dir / "config"
    if not config.is_file():
        return None
    url: str | None = None
    in_origin = False
    for raw in config.read_text().splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_origin = 'remote "origin"' in line
            continue
        if in_origin and line.startswith("url"):
            _, value = line.split("=", 1)
            url = value.strip()
            break
    return url


def detect_license(dest: Path) -> str:
    for name in ("LICENSE", "LICENSE.md", "COPYING", "COPYING.md"):
        hits = list(dest.rglob(name))
        if hits:
            first = hits[0].read_text(errors="replace").strip().splitlines()
            headline = first[0].strip() if first else hits[0].name
            return f"{headline} (from {hits[0].relative_to(dest).as_posix()})"
    return "See upstream Harbor dataset card; lab-internal eval use."


def byte_size(dest: Path) -> int:
    return sum(
        path.stat().st_size
        for path in dest.rglob("*")
        if path.is_file() and path.name not in SKIP_DIGEST_NAMES
    )


def format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def render_manifest(state: FetchState, *, dest_rel: str, inner_note: str) -> str:
    if state.task_git_url and state.task_git_sha:
        git_line = f"- **Task git:** `{state.task_git_url}` @ `{state.task_git_sha}`"
    elif state.task_git_sha:
        git_line = f"- **Task git:** (export) @ `{state.task_git_sha}`"
    else:
        git_line = "- **Task git:** export (no `.git` after Harbor download)"
    sample_names = ", ".join(row.task for row in state.sample) or "none"
    sample_block = _render_sample_table(state.sample)
    return (
        f"# {state.pin} @ Harbor Hub {state.version}\n"
        "\n"
        "## Source and pin\n"
        "\n"
        f"- **Lane:** `harbor download {state.pin}`\n"
        "- **Never:** `@latest`\n"
        f"{git_line}\n"
        f"- **Harbor sync digest:** `{state.tree_digest}`\n"
        f"- **On-disk:** `{dest_rel}`{inner_note}\n"
        "\n"
        "## License\n"
        "\n"
        f"{state.license}\n"
        "\n"
        "## Counts / subset\n"
        "\n"
        f"- **Full pin:** {state.task_count} tasks\n"
        f"- **Materialized:** full {state.task_count}\n"
        f"- **Verified sample:** {sample_names}\n"
        "\n"
        "## Lane / resources\n"
        "\n"
        "- CPU Harbor Docker task images unless a task.toml says otherwise\n"
        "- No GPU assumed; skip cloud-only content\n"
        f"- Lane: {state.lane}\n"
        "\n"
        f"{SAMPLE_HEADING} (`-n` ≤ {MAX_N_CONCURRENT})\n"
        "\n"
        f"{sample_block}"
    )


def _render_sample_table(rows: Sequence[SampleRow]) -> str:
    if not rows:
        return "Not run.\n"
    lines = [
        "Harbor `-k 1 -n 2`; jobs under this worktree `./runs/`.",
        "",
        "| Task | Oracle job | Oracle | Nop job | Nop |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row.task} | `{row.oracle_job}` | **{row.oracle}** | "
            f"`{row.nop_job}` | **{row.nop}** |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def write_state(dest: Path, state: FetchState) -> None:
    payload = {
        "pin": state.pin,
        "version": state.version,
        "task_git_url": state.task_git_url,
        "task_git_sha": state.task_git_sha,
        "tree_digest": state.tree_digest,
        "task_count": state.task_count,
        "lane": state.lane,
        "license": state.license,
        "sample": [
            {
                "task": row.task,
                "oracle_job": row.oracle_job,
                "oracle": row.oracle,
                "nop_job": row.nop_job,
                "nop": row.nop,
            }
            for row in state.sample
        ],
    }
    (dest / STATE_NAME).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def load_state(dest: Path) -> FetchState | None:
    path = dest / STATE_NAME
    if path.is_file():
        raw = json.loads(path.read_text())
        sample = tuple(
            SampleRow(
                task=item["task"],
                oracle_job=item["oracle_job"],
                oracle=float(item["oracle"]),
                nop_job=item["nop_job"],
                nop=float(item["nop"]),
            )
            for item in raw.get("sample") or []
        )
        return FetchState(
            pin=raw["pin"],
            version=raw["version"],
            task_git_url=raw.get("task_git_url"),
            task_git_sha=raw.get("task_git_sha"),
            tree_digest=raw["tree_digest"],
            task_count=int(raw["task_count"]),
            lane=raw.get("lane") or "hub",
            license=raw.get("license") or "",
            sample=sample,
        )
    manifest = dest / MANIFEST_NAME
    if manifest.is_file():
        return state_from_manifest(manifest.read_text(), dest=dest)
    return None


def state_from_manifest(text: str, *, dest: Path) -> FetchState | None:
    pin_match = PIN_IN_TEXT.search(text)
    if not pin_match:
        return None
    if pin_match.group(1):
        name, version = pin_match.group(1), pin_match.group(2)
    else:
        name, version = pin_match.group(3), pin_match.group(4)
    sha_match = SHA_RE.search(text)
    digest_match = TREE_DIGEST_RE.search(text)
    try:
        digest = digest_match.group(0) if digest_match else material_digest(dest)
    except FetchError:
        digest = digest_match.group(0) if digest_match else "sha256:" + ("0" * 64)
    url = None
    git_line = re.search(r"\*\*Task git:\*\*\s+`([^`]+)`", text)
    if git_line and git_line.group(1).startswith("http"):
        url = git_line.group(1)
    return FetchState(
        pin=f"{name}@{version}",
        version=version,
        task_git_url=url,
        task_git_sha=sha_match.group(1) if sha_match else None,
        tree_digest=digest,
        task_count=count_tasks(dest),
        lane="hub",
        license="",
    )


def required_sections_present(text: str) -> list[str]:
    missing = [heading for heading in REQUIRED_HEADINGS if heading not in text]
    if not any(heading in text for heading in RESOURCE_HEADINGS):
        missing.append("## Lane / resources")
    return missing


def strip_nested_git(dest: Path) -> None:
    for git_dir in list(dest.rglob(".git")):
        if git_dir.is_dir():
            shutil.rmtree(git_dir)
        elif git_dir.is_file():
            git_dir.unlink()


class SubprocessHarbor:
    """Real Harbor CLI collaborator. Tests replace this object entirely."""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or _run_command

    def list_hub_datasets(self) -> list[DatasetListing]:
        completed = self._runner(
            ["harbor", "dataset", "list", "--legacy"],
            None,
        )
        if completed.returncode != 0:
            raise FetchError(
                f"harbor dataset list exited {completed.returncode}: "
                f"{(completed.stderr or completed.stdout).strip()}"
            )
        return parse_hub_list_table(completed.stdout)

    def download(self, pin: str, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        completed = self._runner(
            [
                "harbor",
                "download",
                pin,
                "--output-dir",
                str(dest),
                "--export",
            ],
            dest,
        )
        if completed.returncode != 0:
            raise FetchError(
                f"harbor download {pin} exited {completed.returncode}: "
                f"{(completed.stderr or completed.stdout).strip()}"
            )

    def run_control(self, call: ControlCall) -> float:
        if call.n_concurrent > MAX_N_CONCURRENT:
            raise FetchError(
                f"n-concurrent {call.n_concurrent} exceeds cap {MAX_N_CONCURRENT}"
            )
        call.jobs_dir.mkdir(parents=True, exist_ok=True)
        completed = self._runner(
            [
                "harbor",
                "run",
                "--path",
                str(call.task_path),
                "--agent",
                call.agent,
                "--job-name",
                call.job_name,
                "--jobs-dir",
                str(call.jobs_dir),
                "--n-concurrent",
                str(call.n_concurrent),
                "--n-attempts",
                str(call.n_attempts),
                "-y",
            ],
            None,
        )
        if completed.returncode != 0:
            raise FetchError(
                f"harbor run {call.agent} {call.task_path.name} exited "
                f"{completed.returncode}"
            )
        return _reward_from_job(call.jobs_dir / call.job_name)


def _run_command(
    command: Sequence[str], cwd: Path | None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        env=subscription_environment(),
    )


def _reward_from_job(job_dir: Path) -> float:
    result_path = job_dir / "result.json"
    if not result_path.is_file():
        raise FetchError(f"Harbor job missing result.json: {job_dir}")
    payload = json.loads(result_path.read_text())
    evals = (payload.get("stats") or {}).get("evals") or {}
    for report in evals.values():
        metrics = report.get("metrics") or []
        for metric in metrics:
            if isinstance(metric, dict) and "mean" in metric:
                return float(metric["mean"])
        rewards = (report.get("reward_stats") or {}).get("reward") or {}
        if rewards:
            return float(next(iter(rewards)))
    raise FetchError(f"Harbor job has no reward mean: {job_dir}")


def list_fetchable(harbor: HarborBackend) -> list[str]:
    lines = [
        "Pinned targets only; @latest and unpinned refs are refused.",
        "",
        "Hub:",
    ]
    for item in harbor.list_hub_datasets():
        tasks = "" if item.task_count is None else f"  tasks={item.task_count}"
        lines.append(f"  {item.ref}{tasks}")
    lines.append("")
    lines.append("Adapter lanes (named; fetch still requires a Hub pin or name@adapter):")
    for name, note in NAMED_ADAPTER_LANES:
        lines.append(f"  {name}  — {note}")
    return lines


def is_audit_bench(path: Path) -> bool:
    """True for a materialized ingest: a non-dot directory with MANIFEST.md."""
    return path.is_dir() and not path.name.startswith(".") and (path / MANIFEST_NAME).is_file()


def audit_library(root: Path) -> list[AuditRow]:
    benches = root / "library" / "benchmarks"
    if not benches.is_dir():
        return [AuditRow(name="library/benchmarks", status="fail", detail="directory missing")]
    rows: list[AuditRow] = []
    for dest in sorted(path for path in benches.iterdir() if is_audit_bench(path)):
        rows.append(audit_one(dest))
    return rows


def audit_one(dest: Path) -> AuditRow:
    name = dest.name
    manifest = dest / MANIFEST_NAME
    if not manifest.is_file():
        return AuditRow(name=name, status="fail", detail="MANIFEST.md missing")
    text = manifest.read_text()
    missing = required_sections_present(text)
    if missing:
        return AuditRow(
            name=name,
            status="fail",
            detail="missing sections: " + ", ".join(missing),
        )
    if "@latest" in text.lower() and "never" not in text.lower():
        return AuditRow(name=name, status="fail", detail="manifest mentions @latest as a pin")
    recorded = load_state(dest)
    if recorded is None:
        return AuditRow(name=name, status="fail", detail="cannot parse pin from MANIFEST.md")
    try:
        parse_pin(recorded.pin)
    except FetchError as exc:
        return AuditRow(name=name, status="fail", detail=str(exc))
    try:
        actual = material_digest(dest)
    except FetchError as exc:
        return AuditRow(name=name, status="fail", detail=str(exc))
    state_file = dest / STATE_NAME
    if state_file.is_file() or TREE_DIGEST_RE.search(text):
        if recorded.tree_digest != actual:
            return AuditRow(
                name=name,
                status="fail",
                detail=(
                    f"digest drift: recorded {recorded.tree_digest} "
                    f"on-disk {actual}"
                ),
            )
        sha_note = (
            f" commit {recorded.task_git_sha}" if recorded.task_git_sha else ""
        )
        return AuditRow(
            name=name,
            status="pass",
            detail=f"pin {recorded.pin} digest {actual}{sha_note}",
        )
    if recorded.task_git_sha:
        return AuditRow(
            name=name,
            status="pass",
            detail=(
                f"pin {recorded.pin} recorded commit {recorded.task_git_sha}; "
                f"no recorded tree digest (INGEST handmade); on-disk {actual}"
            ),
        )
    return AuditRow(
        name=name,
        status="fail",
        detail=(
            f"pin {recorded.pin} has neither a 40-char commit SHA nor a "
            f"recorded tree digest; on-disk {actual}"
        ),
    )


def fetch_pin(
    ref: str,
    *,
    root: Path,
    harbor: HarborBackend,
    verify_sample: int = 0,
    jobs_dir: Path | None = None,
) -> FetchResult:
    pin = parse_pin(ref)
    dest = (root / "library" / "benchmarks" / pin.name).resolve()
    benches = (root / "library" / "benchmarks").resolve()
    if benches not in dest.parents and dest != benches / pin.name:
        raise FetchError(f"destination escapes library/benchmarks: {dest}")
    if dest.exists():
        return _refetch_existing(pin, dest=dest, verify_sample=verify_sample)
    if pin.version.lower() == "adapter":
        raise FetchError(
            f"{pin.ref} names an adapter lane; there is no Hub export to "
            "materialize. Use a Hub name@version pin."
        )
    dest.mkdir(parents=True, exist_ok=True)
    try:
        harbor.download(pin.ref, dest)
        url, sha = detect_git_origin(dest)
        strip_nested_git(dest)
        digest = material_digest(dest)
        n_tasks = count_tasks(dest)
        if n_tasks == 0:
            raise FetchError(f"download of {pin.ref} produced zero task.toml files")
        sample: tuple[SampleRow, ...] = ()
        if verify_sample:
            sample = tuple(
                _verify_sample(
                    dest,
                    name=pin.name,
                    n=verify_sample,
                    harbor=harbor,
                    jobs_dir=jobs_dir or (root / "runs"),
                )
            )
        state = FetchState(
            pin=pin.ref,
            version=pin.version,
            task_git_url=url,
            task_git_sha=sha,
            tree_digest=digest,
            task_count=n_tasks,
            lane="hub",
            license=detect_license(dest),
            sample=sample,
        )
        inner = _inner_note(dest, pin.name)
        dest_rel = f"library/benchmarks/{pin.name}/"
        (dest / MANIFEST_NAME).write_text(
            render_manifest(state, dest_rel=dest_rel, inner_note=inner)
        )
        write_state(dest, state)
    except Exception:
        if dest.exists() and not (dest / MANIFEST_NAME).exists():
            shutil.rmtree(dest)
        raise
    return FetchResult(
        status="fetched",
        pin=pin.ref,
        dest=dest,
        message=f"materialized {pin.ref} ({n_tasks} tasks, {format_size(byte_size(dest))})",
        manifest_path=dest / MANIFEST_NAME,
    )


def _inner_note(dest: Path, name: str) -> str:
    nested = dest / name
    if nested.is_dir():
        return f" ({name}/ …)"
    return ""


def _refetch_existing(
    pin: Pin, *, dest: Path, verify_sample: int
) -> FetchResult:
    recorded = load_state(dest)
    if recorded is None:
        raise FetchError(
            f"{dest.name} exists but has no parseable pin; refusing to mutate"
        )
    if recorded.pin != pin.ref:
        raise FetchError(
            f"refusing to mutate existing ingest {dest.name} pinned {recorded.pin} "
            f"with requested {pin.ref}"
        )
    if dest.name in PROTECTED_INGESTS:
        audit = audit_one(dest)
        return FetchResult(
            status="noop",
            pin=pin.ref,
            dest=dest,
            message=(
                f"protected ingest {dest.name}: verified {audit.status} — {audit.detail}"
            ),
            manifest_path=dest / MANIFEST_NAME,
        )
    audit = audit_one(dest)
    if audit.status != "pass":
        raise FetchError(f"re-fetch of {pin.ref} found drift: {audit.detail}")
    if verify_sample:
        return FetchResult(
            status="noop",
            pin=pin.ref,
            dest=dest,
            message=(
                f"already pinned {pin.ref}; digest match; "
                "not rewriting tasks or sample table"
            ),
            manifest_path=dest / MANIFEST_NAME,
        )
    return FetchResult(
        status="noop",
        pin=pin.ref,
        dest=dest,
        message=f"already pinned {pin.ref}; digest match; no-op",
        manifest_path=dest / MANIFEST_NAME,
    )


def _verify_sample(
    dest: Path,
    *,
    name: str,
    n: int,
    harbor: HarborBackend,
    jobs_dir: Path,
) -> list[SampleRow]:
    if n < 1:
        raise FetchError("--verify-sample N must be >= 1")
    tasks = list_task_dirs(dest)[:n]
    if len(tasks) < n:
        raise FetchError(f"only {len(tasks)} tasks on disk; cannot sample {n}")
    rows: list[SampleRow] = []
    for task_dir in tasks:
        slug = task_dir.name
        oracle_job = f"oracle-fetch-{name}-{slug}"
        nop_job = f"nop-fetch-{name}-{slug}"
        oracle = harbor.run_control(
            ControlCall(
                task_path=task_dir,
                agent="oracle",
                job_name=oracle_job,
                jobs_dir=jobs_dir,
                n_concurrent=MAX_N_CONCURRENT,
                n_attempts=1,
            )
        )
        nop = harbor.run_control(
            ControlCall(
                task_path=task_dir,
                agent="nop",
                job_name=nop_job,
                jobs_dir=jobs_dir,
                n_concurrent=MAX_N_CONCURRENT,
                n_attempts=1,
            )
        )
        rows.append(
            SampleRow(
                task=slug,
                oracle_job=oracle_job,
                oracle=oracle,
                nop_job=nop_job,
                nop=nop,
            )
        )
    return rows


def format_audit(rows: Sequence[AuditRow]) -> str:
    lines = []
    for row in rows:
        lines.append(f"{row.status:4}  {row.name}: {row.detail}")
    failed = sum(1 for row in rows if row.status != "pass")
    lines.append(f"{len(rows)} benches, {failed} fail")
    return "\n".join(lines) + "\n"


@dataclass
class FetchService:
    root: Path
    harbor: HarborBackend = field(default_factory=SubprocessHarbor)

    def list_lines(self) -> list[str]:
        return list_fetchable(self.harbor)

    def audit(self) -> list[AuditRow]:
        return audit_library(self.root)

    def fetch(
        self, ref: str, *, verify_sample: int = 0, jobs_dir: Path | None = None
    ) -> FetchResult:
        return fetch_pin(
            ref,
            root=self.root,
            harbor=self.harbor,
            verify_sample=verify_sample,
            jobs_dir=jobs_dir,
        )
