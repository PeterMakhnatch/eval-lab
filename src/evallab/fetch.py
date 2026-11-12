"""Pinned Harbor Hub / adapter-lane acquisition for library/benchmarks/."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

UNPINNED_VERSIONS = frozenset({"latest", "head", "main", "master"})
PROTECTED_INGESTS = frozenset(
    {"aime", "gpqa-diamond", "humanevalfix", "terminal-bench-sample"}
)
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


@dataclass(frozen=True)
class PublicAtifSource:
    """Anonymous, immutable Hugging Face ATIF file."""

    item_id: str
    repo_id: str
    revision: str
    filename: str
    sha256: str
    license: str

    @property
    def url(self) -> str:
        path = quote(self.filename, safe="/")
        return f"https://huggingface.co/datasets/{self.repo_id}/resolve/{self.revision}/{path}"


PROOFJUDGE_ATIF_SAMPLE = PublicAtifSource(
    item_id="proofjudge-eval-traces@qwen3-32b",
    repo_id="SJCaldwell/proofjudge-eval-traces",
    revision="aac1f0f4c96e8394da6315a04778e4b7f13ac900",
    filename="data/traces_qwen3-32b.jsonl",
    sha256="79b7d3e71d28af6dc1630cb135d697c035a4e74de5eb9226db6e1c0cd3ee17fb",
    license="mit",
)


