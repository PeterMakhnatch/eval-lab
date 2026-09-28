"""Pinned MiMo HF snapshot intake and task-catalog Parquet builds.

This module owns the data side of Peter's MiMo RL-environment experiments:

1. ``pull_hf_snapshot`` downloads one pinned ``<org>/<repo>@<40-hex-sha>``
   Hugging Face dataset snapshot anonymously into the shared derived store at
   ``<derived root>/task-store/hf/<org>__<repo>@<rev12>/`` (resolved with
   ``evallab.storage.paths`` so every worktree shares the primary checkout's
   store), makes it read-only, and records a ``ProvenanceMetadata`` sidecar
   (zone ``01-external``). Idempotent: a complete snapshot whose provenance
   and material digest still match is reused; any mismatch is refused, never
   overwritten.
2. ``build_catalog`` scans pulled snapshots (plus git-tracked lineage records
   under ``library/task-variants/``) and writes the four contract Parquet
   tables under ``<derived root>/parquet/external/task_catalog/``.
3. ``show_task`` prints one task version: identity, digests, source pin,
   findings, lineage, and outcome rows.

Per-task bytes are verified against the adapter's ``manifest.json`` with the
exact hashing the adapter uses (``relpath + NUL + bytes + NUL`` per file,
sorted); mismatches become findings, never crashes. Harbor join digests come
from ``registry.harbor_task_digest``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import tomllib
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq

from evallab.fetch import FetchError, parse_pin
from evallab.registry import compute_task_digests, harbor_task_digest, task_directory_digest
from evallab.schemas import ProvenanceMetadata
from evallab.storage.paths import derived_root_from_environment
from evallab.task_lint import Finding, lint_mimo_task, mimo_manifest_sha

MIMO_HF_PINS: dict[str, str] = {
    "code": "5746e2f0c5c61af12d7c5bf15d7efdd77d1f0785",
    "cyber": "763882ade5fc018892f1aa3c559f997138eb92cc",
    "general": "10b732c5079c47244a77402f5759d62763800f20",
    "terminal": "fe1c2b665aae1ba7a09a270d979724d32269ae6a",
    "webdev": "e1a6293376e8910e1fb1f28efabd93dad2f8b475",
    "music": "e1a66d4553ee20b26c571de1bc2f4193d4a32c3c",
}

#: Expected on-disk task counts per domain (registry/manifest/jsonl agree).
MIMO_EXPECTED_COUNTS: dict[str, int] = {
    "code": 2698,
    "cyber": 1000,
    "general": 925,
    "terminal": 64,
    "webdev": 2093,
    "music": 1000,
}

TASK_STORE_DIRNAME = "task-store"
HF_SNAPSHOTS_DIRNAME = "hf"
VARIANTS_DIRNAME = "variants"
CATALOG_RELPATH = Path("external/task_catalog")
CATALOG_TABLES = ("task_sources", "task_versions", "task_findings", "task_lineage")
PROVENANCE_FILENAME = "provenance.json"
SNAPSHOT_SKIP_NAMES = frozenset({PROVENANCE_FILENAME})

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_RL_SUFFIX = re.compile(r"_rl_\d+$")
_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\㐀-\䶿豈-\﫿]")
_GO_TEST_TARGET = re.compile(r"go test\s+(?:-[^\s]+\s+)*(\S+)")
_HOSTED_REPO = re.compile(r"^(github\.com|gitlab\.com|bitbucket\.org)/([^/]+)/([^/]+)")
_TEST_HELPER_MODULES = frozenset({"github.com/stretchr/testify"})


class CatalogError(ValueError):
    """User-facing pull/build/show refusal."""


def mimo_repo_id(domain: str) -> str:
    """Canonical HF repo for one MiMo domain."""
    return f"FineEnvs/MiMo-V2.6-RL-harbor-{domain}"


def snapshot_dir_name(org: str, repo: str, revision: str) -> str:
    """Store-relative snapshot directory for a pinned repo."""
    return f"{org}__{repo}@{revision[:12]}"


def hf_task_store(derived_root: Path) -> Path:
    """Shared snapshot root: ``<derived root>/task-store/hf``."""
    return derived_root / TASK_STORE_DIRNAME / HF_SNAPSHOTS_DIRNAME


def catalog_dir(derived_root: Path) -> Path:
    """Rebuildable catalog root: ``<derived root>/parquet/external/task_catalog``."""
    return derived_root / CATALOG_RELPATH


def task_store_root(repo_root: Path, *, derived_root: Path | None = None) -> Path:
    """Resolve the shared task-store root without touching the worktree."""
    derived = derived_root or derived_root_from_environment(repo_root)
    return derived / TASK_STORE_DIRNAME


def parse_hf_pin(ref: str) -> tuple[str, str, str]:
    """Split ``<org>/<repo>@<40-hex-sha>``; refuse anything unpinned.

    Reuses :func:`fetch.parse_pin` refusal for empty/unpinned refs, then
    requires exactly ``org/repo`` and a full 40-hex commit sha (never a
    branch, tag, or short prefix).
    """
    try:
        pin = parse_pin(ref)
    except FetchError as exc:
        raise CatalogError(str(exc)) from exc
    org, slash, repo = pin.name.partition("/")
    if not slash or "/" in repo or not org or not repo:
        raise CatalogError(
            f"refused {ref!r}: require <org>/<repo>@<40-hex-sha> "
            "(exactly one slash in the repo name)"
        )
    if not _HEX40.match(pin.version):
        raise CatalogError(
            f"refused {ref!r}: require a full 40-hex commit sha "
            "(never a branch, tag, or short prefix)"
        )
    return org, repo, pin.version


SnapshotDownloader = Callable[[str, str, Path], None]
"""Download collaborator: ``(repo_id, revision, dest_dir)``. Tests inject fakes."""


def hub_snapshot_download(repo_id: str, revision: str, dest: Path) -> None:
    """Default downloader: anonymous ``huggingface_hub.snapshot_download``."""
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=repo_id,
        repo_type="dataset",
        revision=revision,
        local_dir=str(dest),
        max_workers=8,
    )


def snapshot_material_digest(snapshot: Path) -> str:
    """Tree digest over snapshot bytes, excluding the provenance sidecar.

    Same aggregate format as :func:`fetch.material_digest`
    (``"<file-sha>  ./<relpath>"`` lines); the sidecar is excluded because it
    records this digest.
    """
    if not snapshot.is_dir():
        raise CatalogError(f"snapshot directory is missing: {snapshot}")
    aggregate = hashlib.sha256()
    files = sorted(
        path
        for path in snapshot.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.name not in SNAPSHOT_SKIP_NAMES
        and ".git" not in path.parts
        and ".cache" not in path.parts
    )
    if not files:
        raise CatalogError(f"no material files under {snapshot}")
    for candidate in files:
        relative = candidate.relative_to(snapshot).as_posix()
        file_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        aggregate.update(f"{file_digest}  ./{relative}\n".encode())
    return f"sha256:{aggregate.hexdigest()}"


def detect_snapshot_license(snapshot: Path) -> str:
    """Best-effort license note from top-level license files."""
    for name in ("LICENSE", "LICENSE.md", "COPYING", "COPYING.md"):
        if (snapshot / name).is_file():
            return f"Upstream {name} in snapshot; lab-internal eval use."
    return "See upstream Harbor dataset card; lab-internal eval use."


def make_snapshot_read_only(snapshot: Path) -> None:
    """Strip write bits (keeping exec on scripts) so snapshots stay immutable."""
    files = [p for p in snapshot.rglob("*") if not p.is_symlink() and p.is_file()]
    dirs = [p for p in snapshot.rglob("*") if not p.is_symlink() and p.is_dir()]
    for path in files:
        mode = path.stat().st_mode
        path.chmod(0o444 | (0o111 if mode & 0o111 else 0))
    for path in dirs:
        path.chmod(0o555)
    snapshot.chmod(0o555)


def read_snapshot_manifest(snapshot: Path) -> dict[str, str]:
    """Adapter ``manifest.json`` task map (task_id -> hex sha256)."""
    manifest_path = snapshot / "manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogError(f"snapshot has no readable manifest.json: {snapshot}") from exc
    tasks = payload.get("tasks")
    if not isinstance(tasks, dict):
        raise CatalogError(f"snapshot manifest.json has no tasks map: {snapshot}")
    return {str(task_id): str(sha) for task_id, sha in tasks.items()}


def list_snapshot_tasks(snapshot: Path) -> list[Path]:
    """Task directories on disk (sorted by directory name)."""
    tasks_root = snapshot / "tasks"
    if not tasks_root.is_dir():
        return []
    return sorted(
        (child for child in tasks_root.iterdir() if (child / "task.toml").is_file()),
        key=lambda child: child.name,
    )


def verify_snapshot_manifest(snapshot: Path) -> tuple[str, ...]:
    """Task ids whose bytes differ from the adapter manifest (or are absent)."""
    expected = read_snapshot_manifest(snapshot)
    mismatched: list[str] = []
    for task_dir in list_snapshot_tasks(snapshot):
        digest = mimo_manifest_sha(task_dir)
        if expected.get(task_dir.name) != digest:
            mismatched.append(task_dir.name)
    return tuple(mismatched)


@dataclass(frozen=True)
class PullResult:
    """Outcome of one pinned snapshot intake."""

    status: Literal["downloaded", "reused"]
    snapshot: Path
    repo: str
    revision: str
    n_tasks: int
    manifest_mismatches: tuple[str, ...] = ()
    provenance_path: Path | None = None


def _write_provenance(
    snapshot: Path,
    *,
    repo_id: str,
    revision: str,
    material_digest: str,
    license: str,
    created_by: str,
) -> Path:
    org, _, repo = repo_id.partition("/")
    item_id = f"hf-{org.lower()}__{repo.lower()}@{revision[:12]}".replace("/", "__")
    provenance = ProvenanceMetadata(
        item_id=item_id[:120],
        zone="01-external",
        source_uri=f"https://huggingface.co/datasets/{repo_id}",
        revision=revision,
        material_digest=material_digest,
        license=license,
        created_at=datetime.now(UTC),
        created_by=created_by,
        notes=f"Pinned snapshot of {repo_id}; adapter conversion output, not lab evidence.",
    )
    path = snapshot / PROVENANCE_FILENAME
    path.write_text(provenance.model_dump_json(indent=2) + "\n", encoding="utf-8")
    path.chmod(0o444)
    return path


def _check_existing_snapshot(
    dest: Path, *, repo_id: str, revision: str
) -> tuple[str, ...] | None:
    """Return manifest mismatches when an existing snapshot is reusable, else None.

    Any staleness (missing/invalid provenance, repo or revision drift,
    material-digest drift) returns None so the caller refuses rather than
    overwriting.
    """
    provenance_path = dest / PROVENANCE_FILENAME
    if not provenance_path.is_file():
        return None
    try:
        recorded = json.loads(provenance_path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if recorded.get("revision") != revision:
        return None
    if recorded.get("source_uri") != f"https://huggingface.co/datasets/{repo_id}":
        return None
    try:
        current = snapshot_material_digest(dest)
    except CatalogError:
        return None
    if recorded.get("material_digest") != current:
        return None
    return verify_snapshot_manifest(dest)


def pull_hf_snapshot(
    ref: str,
    *,
    repo_root: Path,
    derived_root: Path | None = None,
    downloader: SnapshotDownloader | None = None,
    created_by: str = "evallab-tasks-pull-hf",
) -> PullResult:
    """Download (or reuse) one pinned HF snapshot; refuse anything unpinned.

    Raises :class:`CatalogError` on unpinned refs, on an existing directory
    whose provenance does not match, or on download failures. Manifest
    mismatches are reported in the result, never raised.
    """
    org, repo, revision = parse_hf_pin(ref)
    repo_id = f"{org}/{repo}"
    store = hf_task_store(derived_root or derived_root_from_environment(repo_root))
    dest = store / snapshot_dir_name(org, repo, revision)

    if dest.exists():
        if not dest.is_dir():
            raise CatalogError(f"snapshot path exists and is not a directory: {dest}")
        reusable = _check_existing_snapshot(dest, repo_id=repo_id, revision=revision)
        if reusable is None:
            raise CatalogError(
                f"refusing to overwrite {dest}: existing snapshot does not match "
                f"{repo_id}@{revision} (moved pin or edited bytes?). Remove it "
                "manually if a re-pull is intended."
            )
        tasks = list_snapshot_tasks(dest)
        return PullResult(
            status="reused",
            snapshot=dest,
            repo=repo_id,
            revision=revision,
            n_tasks=len(tasks),
            manifest_mismatches=reusable,
            provenance_path=dest / PROVENANCE_FILENAME,
        )

    download = downloader or hub_snapshot_download
    store.mkdir(parents=True, exist_ok=True)
    try:
        download(repo_id, revision, dest)
    except Exception as exc:
        raise CatalogError(
            f"snapshot download failed for {repo_id}@{revision}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    cache_dir = dest / ".cache"
    if cache_dir.is_dir():
        import shutil

        shutil.rmtree(cache_dir)
    digest = snapshot_material_digest(dest)
    provenance_path = _write_provenance(
        dest,
        repo_id=repo_id,
        revision=revision,
        material_digest=digest,
        license=detect_snapshot_license(dest),
        created_by=created_by,
    )
    make_snapshot_read_only(dest)
    mismatches = verify_snapshot_manifest(dest)
    tasks = list_snapshot_tasks(dest)
    return PullResult(
        status="downloaded",
        snapshot=dest,
        repo=repo_id,
        revision=revision,
        n_tasks=len(tasks),
        manifest_mismatches=mismatches,
        provenance_path=provenance_path,
    )

TASK_SOURCES_SCHEMA = pa.schema(
    [
        pa.field("source_repo", pa.string()),
        pa.field("source_revision", pa.string()),
        pa.field("domain", pa.string()),
        pa.field("snapshot_relpath", pa.string()),
        pa.field("n_manifest", pa.int64()),
        pa.field("n_disk", pa.int64()),
        pa.field("n_registry", pa.int64()),
        pa.field("license", pa.string()),
        pa.field("material_digest", pa.string()),
    ]
)

TASK_VERSIONS_SCHEMA = pa.schema(
    [
        pa.field("task_version_digest", pa.string()),
        pa.field("harbor_digest", pa.string()),
        pa.field("task_name", pa.string()),
        pa.field("task_id", pa.string()),
        pa.field("source_id", pa.string()),
        pa.field("origin", pa.string()),
        pa.field("source_repo", pa.string()),
        pa.field("source_revision", pa.string()),
        pa.field("source_path", pa.string()),
        pa.field("upstream_manifest_sha256", pa.string()),
        pa.field("parent_digest", pa.string()),
        pa.field("transform", pa.string()),
        pa.field("digest_task_toml", pa.string()),
        pa.field("digest_instruction", pa.string()),
        pa.field("digest_environment", pa.string()),
        pa.field("digest_verifier", pa.string()),
        pa.field("domain", pa.string()),
        pa.field("category", pa.string()),
        pa.field("docker_image", pa.string()),
        pa.field("grader_kind", pa.string()),
        pa.field("grader_cost", pa.string()),
        pa.field("has_solution", pa.bool_()),
        pa.field("network_mode", pa.string()),
        pa.field("agent_user", pa.string()),
        pa.field("step_limit", pa.int64()),
        pa.field("agent_timeout_sec", pa.float64()),
        pa.field("verifier_timeout_sec", pa.float64()),
        pa.field("cpus", pa.int64()),
        pa.field("memory_mb", pa.int64()),
        pa.field("instruction_chars", pa.int64()),
        pa.field("instruction_lang", pa.string()),
        pa.field("split_group", pa.string()),
    ]
)

TASK_FINDINGS_SCHEMA = pa.schema(
    [
        pa.field("task_version_digest", pa.string()),
        pa.field("task_id", pa.string()),
        pa.field("domain", pa.string()),
        pa.field("rule", pa.string()),
        pa.field("severity", pa.string()),
        pa.field("message", pa.string()),
    ]
)

TASK_LINEAGE_SCHEMA = pa.schema(
    [
        pa.field("child_digest", pa.string()),
        pa.field("child_harbor_digest", pa.string()),
        pa.field("parent_digest", pa.string()),
        pa.field("parent_harbor_digest", pa.string()),
        pa.field("transform", pa.string()),
        pa.field("record_path", pa.string()),
        pa.field("origin", pa.string()),
    ]
)

_CATALOG_SCHEMAS = {
    "task_sources": TASK_SOURCES_SCHEMA,
    "task_versions": TASK_VERSIONS_SCHEMA,
    "task_findings": TASK_FINDINGS_SCHEMA,
    "task_lineage": TASK_LINEAGE_SCHEMA,
}


def instruction_features(text: str) -> tuple[int, str]:
    """Character count plus a cheap zh-vs-en language signal (CJK fraction)."""
    chars = len(text)
    nonspace = [char for char in text if not char.isspace()]
    cjk = len(_CJK.findall(text))
    frac = (cjk / len(nonspace)) if nonspace else 0.0
    lang = "zh" if frac > 0.05 else ("mixed" if frac > 0.005 else "en")
    return chars, lang


def slug_of(source_id: str) -> str:
    """Lowercase alphanumeric fold used to spot id collisions."""
    return "".join(
        char if char.isalnum() or char in "-_." else "-" for char in source_id.lower()
    ).strip("-.")


def derive_grader_kind_cost(task_dir: Path, toml: Mapping[str, Any]) -> tuple[str, str]:
    """Derive (grader_kind, grader_cost) from task files, not the domain name.

    A paid model judge is present when the verifier env wires ``*JUDGE*``
    keys or ``tests/grade.py`` grades via a judge; vision markers select the
    VLM kind. Everything else is a $0 script.
    """
    verifier = toml.get("verifier")
    env = verifier.get("env") if isinstance(verifier, dict) else None
    env_blob = json.dumps(env, sort_keys=True) if isinstance(env, dict) else ""
    grade_path = task_dir / "tests" / "grade.py"
    grade_text = ""
    if grade_path.is_file():
        try:
            grade_text = grade_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            grade_text = ""
    if "JUDGE" in env_blob or "judge" in grade_text.lower():
        lowered = (env_blob + "\n" + grade_text).lower()
        if "webdev_judge" in lowered or "vlm" in lowered or "vision" in lowered:
            return "vlm_judge", "paid"
        return "llm_judge", "paid"
    return "script", "free"


def _go_test_targets(task_dir: Path) -> list[str]:
    """Module-style ``go test`` targets from the visible and hidden test commands."""
    blobs: list[str] = []
    command_path = task_dir / "tests" / "test_command.sh"
    if command_path.is_file():
        with contextlib.suppress(OSError):
            blobs.append(command_path.read_text(encoding="utf-8", errors="replace"))
    patch_path = task_dir / "tests" / "test.patch"
    if patch_path.is_file():
        try:
            patch = patch_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            patch = ""
        hidden: list[str] = []
        for line in patch.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                hidden.append(line[1:])
        blobs.append("\n".join(hidden))
    targets: list[str] = []
    for blob in blobs:
        for match in _GO_TEST_TARGET.finditer(blob):
            target = match.group(1).strip().strip("'\"").rstrip(",;")
            if re.fullmatch(r"[A-Za-z0-9_./~+@-]+", target or "") and (
                "/" in target or target in ("./...", "...")
            ):
                targets.append(target)
    return targets


def _module_repo_root(target: str) -> str | None:
    """Best-effort repository root for a Go module-style test target."""
    if target.startswith(("./", "../", "...")):
        return None
    hosted = _HOSTED_REPO.match(target)
    if hosted:
        return f"{hosted.group(1)}/{hosted.group(2)}/{hosted.group(3)}"
    parts = target.split("/")
    if "." in parts[0] and len(parts) >= 3:
        return "/".join(parts[:3])
    if "." in parts[0]:
        return target
    return None


_REPO_URL = re.compile(
    r"https?://(github\.com|gitlab\.com)/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)"
)


def _repo_urls_in_text(text: str) -> Counter[str]:
    """Repository roots from hosted code URLs (issue text usually links home)."""
    votes: Counter[str] = Counter()
    for match in _REPO_URL.finditer(text or ""):
        votes[f"{match.group(1)}/{match.group(2)}/{match.group(3)}"] += 1
    return votes


def extract_code_repo(task_dir: Path) -> str | None:
    """Repository identity for a code task, derived from task files.

    Tier 1: majority vote over module-style ``go test`` targets (visible
    command plus the hidden command inside ``tests/test.patch``). Tier 2: Go
    import paths in the patch (excluding test-helper modules). Tier 3: hosted
    code URLs in the instruction and task description. Returns None when
    nothing derivable exists; the caller falls back to the task id and emits
    a ``split-group-unresolved`` finding.
    """
    votes: dict[str, int] = {}
    for target in _go_test_targets(task_dir):
        repo = _module_repo_root(target)
        if repo is not None:
            votes[repo] = votes.get(repo, 0) + 1
    if not votes:
        patch_path = task_dir / "tests" / "test.patch"
        if patch_path.is_file():
            try:
                patch = patch_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                patch = ""
            for match in re.finditer(r'"((?:github|gitlab)\.com/[^"]+)"', patch):
                repo = _module_repo_root(match.group(1))
                if repo is not None and repo not in _TEST_HELPER_MODULES:
                    votes[repo] = votes.get(repo, 0) + 1
    if not votes:
        toml = _read_task_toml(task_dir)
        metadata = toml.get("metadata") if isinstance(toml.get("metadata"), dict) else {}
        task_block = toml.get("task") if isinstance(toml.get("task"), dict) else {}
        try:
            instruction = (task_dir / "instruction.md").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            instruction = ""
        text = "\n".join(
            [
                instruction,
                str(metadata.get("title") or ""),
                str(metadata.get("description") or ""),
                str(task_block.get("description") or ""),
            ]
        )
        for repo, count in _repo_urls_in_text(text).items():
            votes[repo] = votes.get(repo, 0) + count
    if not votes:
        return None
    return sorted(votes.items(), key=lambda item: (-item[1], item[0]))[0][0]


def derive_split_group(
    domain: str,
    task_id: str,
    task_dir: Path,
    toml: Mapping[str, Any],
    jsonl_row: Mapping[str, Any] | None,
) -> tuple[str, bool]:
    """Stable family key so sibling tasks never straddle train/held-out.

    Rules (lead-decided): code groups by repository identity derived from
    graded test targets; cyber groups by ARVO project (first component of
    ``expected_crash.file``); general strips the trailing ``_rl_NNN`` sibling
    suffix; terminal/webdev/music group by task id until near-duplicate
    analysis lands. Returns ``(group, unresolved)``; unresolved groups fall
    back to the task id so nothing is ever silently merged.
    """
    if domain == "code":
        repo = extract_code_repo(task_dir)
        if repo is None:
            return f"code:{task_id}", True
        return f"code:{repo}", False
    if domain == "cyber":
        crash: Any = (jsonl_row or {}).get("expected_crash")
        if not isinstance(crash, dict):
            metadata = toml.get("metadata")
            crash = metadata.get("expected_crash") if isinstance(metadata, dict) else None
        crash_file = crash.get("file") if isinstance(crash, dict) else None
        if isinstance(crash_file, str) and crash_file:
            project = crash_file.split("/")[0]
            if project:
                return f"cyber:{project}", False
        return f"cyber:{task_id}", True
    if domain == "general":
        return f"general:{_RL_SUFFIX.sub('', task_id)}", False
    return f"{domain}:{task_id}", False


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _read_task_toml(task_dir: Path) -> dict[str, Any]:
    try:
        payload = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_jsonl_map(snapshot: Path) -> dict[str, dict[str, Any]]:
    jsonl_path = snapshot / "data" / "tasks.jsonl"
    rows: dict[str, dict[str, Any]] = {}
    if not jsonl_path.is_file():
        return rows
    try:
        lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("task_id"):
            rows[str(row["task_id"])] = row
    return rows


def _read_registry_names(snapshot: Path) -> list[str]:
    try:
        payload = json.loads((snapshot / "registry.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    names: list[str] = []
    entries = payload if isinstance(payload, list) else [payload]
    for entry in entries:
        if isinstance(entry, dict):
            for task in entry.get("tasks") or []:
                if isinstance(task, dict) and task.get("name"):
                    names.append(str(task["name"]))
    return names


@dataclass
class _ExternalTask:
    version_row: dict[str, Any]
    findings: list[Finding]


def _build_external_task(
    task_dir: Path,
    *,
    repo_id: str,
    revision: str,
    domain: str,
    expected_manifest_sha: str | None,
    jsonl_row: Mapping[str, Any] | None,
) -> _ExternalTask:
    toml = _read_task_toml(task_dir)
    task_block = toml.get("task") if isinstance(toml.get("task"), dict) else {}
    metadata = toml.get("metadata") if isinstance(toml.get("metadata"), dict) else {}
    agent = toml.get("agent") if isinstance(toml.get("agent"), dict) else {}
    verifier = toml.get("verifier") if isinstance(toml.get("verifier"), dict) else {}
    environment = toml.get("environment") if isinstance(toml.get("environment"), dict) else {}
    task_id = task_dir.name
    source_id = str(metadata.get("source_id") or (jsonl_row or {}).get("source_id") or task_id)
    instruction_path = task_dir / "instruction.md"
    try:
        instruction = instruction_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        instruction = ""
    chars, lang = instruction_features(instruction)
    digests = compute_task_digests(task_dir)
    grader_kind, grader_cost = derive_grader_kind_cost(task_dir, toml)
    split_group, unresolved = derive_split_group(domain, task_id, task_dir, toml, jsonl_row)
    step_limit = _int_or_none(metadata.get("reference_step_limit"))
    if step_limit is None:
        raw_step = (jsonl_row or {}).get("step_limit")
        step_limit = _int_or_none(raw_step)
        if step_limit is None and isinstance(raw_step, str) and raw_step.isdigit():
            step_limit = int(raw_step)
    solution = task_dir / "solution"
    row = {
        "task_version_digest": task_directory_digest(task_dir),
        "harbor_digest": harbor_task_digest(task_dir),
        "task_name": str(task_block.get("name") or f"mimo-v2.6-rl/{task_id}"),
        "task_id": task_id,
        "source_id": source_id,
        "origin": "external",
        "source_repo": repo_id,
        "source_revision": revision,
        "source_path": f"tasks/{task_id}",
        "upstream_manifest_sha256": expected_manifest_sha,
        "parent_digest": None,
        "transform": None,
        "digest_task_toml": digests.task_toml,
        "digest_instruction": digests.instruction,
        "digest_environment": digests.environment,
        "digest_verifier": digests.verifier,
        "domain": domain,
        "category": str(metadata.get("category") or ""),
        "docker_image": str(environment.get("docker_image") or ""),
        "grader_kind": grader_kind,
        "grader_cost": grader_cost,
        "has_solution": solution.is_dir() and any(solution.rglob("*")),
        "network_mode": str(environment.get("network_mode") or ""),
        "agent_user": str(agent.get("user") or "root"),
        "step_limit": step_limit,
        "agent_timeout_sec": _float_or_none(agent.get("timeout_sec")),
        "verifier_timeout_sec": _float_or_none(verifier.get("timeout_sec")),
        "cpus": _int_or_none(environment.get("cpus")),
        "memory_mb": _int_or_none(environment.get("memory_mb")),
        "instruction_chars": chars,
        "instruction_lang": lang,
        "split_group": split_group,
    }
    findings = lint_mimo_task(task_dir, expected_manifest_sha=expected_manifest_sha, domain=domain)
    if expected_manifest_sha is None:
        findings.append(
            Finding(
                "mimo-manifest-digest-mismatch",
                "error",
                str(task_dir),
                f"task {task_id!r} is on disk but absent from the adapter manifest.json",
            )
        )
    if unresolved:
        findings.append(
            Finding(
                "split-group-unresolved",
                "warning",
                str(task_dir / "task.toml"),
                f"no stable family key derivable for {domain}/{task_id}; "
                "split_group falls back to the task id (never merged silently)",
            )
        )
    return _ExternalTask(version_row=row, findings=findings)


def load_lineage_dicts(repo_root: Path) -> tuple[list[tuple[dict[str, Any], str]], int]:
    """Lineage records as ``(dict, record_relpath)`` pairs via TaskVariants.

    Each file under ``library/task-variants/**/*.json`` is parsed with
    ``evallab.task_variants.resolve_record`` (strict
    ``evallab.task_variant/v1`` validation). Returns ``(records, skipped)``;
    invalid records are skipped with a count, never a crash, so catalog
    builds stay rebuildable while lineage evolves.
    """
    from evallab.task_variants import RECORDS_DIRNAME, LineageError, resolve_record

    tree = Path(repo_root) / RECORDS_DIRNAME
    records: list[tuple[dict[str, Any], str]] = []
    skipped = 0
    if tree.is_dir():
        for path in sorted(tree.rglob("*.json")):
            if not path.is_file():
                continue
            try:
                record = resolve_record(path)
            except LineageError:
                skipped += 1
                continue
            try:
                relpath = path.relative_to(repo_root).as_posix()
            except ValueError:
                relpath = path.name
            records.append((record.model_dump(mode="json", by_alias=True), relpath))
    return records, skipped


def _variant_version_row(record: Mapping[str, Any]) -> dict[str, Any]:
    parent = record.get("parent") if isinstance(record.get("parent"), dict) else {}
    source = parent.get("source") if isinstance(parent.get("source"), dict) else {}
    domain: str | None = None
    if source.get("kind") == "hf" and isinstance(source.get("repo"), str):
        domain = source["repo"].rsplit("harbor-", 1)[-1]
    return {
        "task_version_digest": record.get("variant_digest"),
        "harbor_digest": record.get("variant_harbor_digest"),
        "task_name": record.get("task_name"),
        "task_id": str(record.get("task_name") or "").rsplit("/", 1)[-1],
        "source_id": str(record.get("task_name") or "").rsplit("/", 1)[-1],
        "origin": "variant",
        "source_repo": source.get("repo") if source.get("kind") == "hf" else None,
        "source_revision": source.get("revision") if source.get("kind") == "hf" else None,
        "source_path": source.get("path") or source.get("record"),
        "upstream_manifest_sha256": None,
        "parent_digest": parent.get("digest"),
        "transform": record.get("transform"),
        "digest_task_toml": None,
        "digest_instruction": None,
        "digest_environment": None,
        "digest_verifier": None,
        "domain": domain,
        "category": None,
        "docker_image": None,
        "grader_kind": None,
        "grader_cost": None,
        "has_solution": None,
        "network_mode": None,
        "agent_user": None,
        "step_limit": None,
        "agent_timeout_sec": None,
        "verifier_timeout_sec": None,
        "cpus": None,
        "memory_mb": None,
        "instruction_chars": None,
        "instruction_lang": None,
        "split_group": None,
    }


@dataclass
class CatalogBuildReport:
    """Counts and finding tallies from one catalog build."""

    catalog_dir: Path
    snapshots: list[str] = field(default_factory=list)
    tables: dict[str, int] = field(default_factory=dict)
    n_variants: int = 0
    skipped_variant_records: int = 0
    findings_by_rule_domain: dict[str, dict[str, int]] = field(default_factory=dict)
    unresolved_splits: dict[str, int] = field(default_factory=dict)


def _domain_of_snapshot(repo_id: str) -> str:
    return repo_id.rsplit("harbor-", 1)[-1] if "harbor-" in repo_id else repo_id


def build_catalog(
    *,
    repo_root: Path,
    derived_root: Path | None = None,
) -> CatalogBuildReport:
    """Scan pulled snapshots plus lineage records; write the four catalog tables."""
    derived = derived_root or derived_root_from_environment(repo_root)
    store = hf_task_store(derived)
    outdir = catalog_dir(derived)
    outdir.mkdir(parents=True, exist_ok=True)
    report = CatalogBuildReport(catalog_dir=outdir)

    sources: list[dict[str, Any]] = []
    versions: list[dict[str, Any]] = []
    finding_rows: list[dict[str, Any]] = []
    slug_index: dict[str, list[tuple[str, str]]] = {}

    snapshots = sorted(
        (child for child in store.iterdir() if child.is_dir() and (child / PROVENANCE_FILENAME).is_file()),
        key=lambda child: child.name,
    ) if store.is_dir() else []
    for snapshot in snapshots:
        provenance = json.loads((snapshot / PROVENANCE_FILENAME).read_text(encoding="utf-8"))
        repo_id = str(provenance.get("source_uri", "")).rsplit("/datasets/", 1)[-1]
        revision = str(provenance.get("revision") or "")
        domain = _domain_of_snapshot(repo_id)
        try:
            manifest = read_snapshot_manifest(snapshot)
        except CatalogError:
            manifest = {}
        registry_names = _read_registry_names(snapshot)
        jsonl_rows = _read_jsonl_map(snapshot)
        task_dirs = list_snapshot_tasks(snapshot)
        report.snapshots.append(snapshot.name)
        sources.append(
            {
                "source_repo": repo_id,
                "source_revision": revision,
                "domain": domain,
                "snapshot_relpath": snapshot.relative_to(derived).as_posix(),
                "n_manifest": len(manifest),
                "n_disk": len(task_dirs),
                "n_registry": len(registry_names),
                "license": provenance.get("license"),
                "material_digest": provenance.get("material_digest"),
            }
        )
        for task_dir in task_dirs:
            built = _build_external_task(
                task_dir,
                repo_id=repo_id,
                revision=revision,
                domain=domain,
                expected_manifest_sha=manifest.get(task_dir.name),
                jsonl_row=jsonl_rows.get(task_dir.name),
            )
            versions.append(built.version_row)
            slug_index.setdefault(
                slug_of(str(built.version_row["source_id"])), []
            ).append((domain, task_dir.name))
            for finding in built.findings:
                finding_rows.append(
                    {
                        "task_version_digest": built.version_row["task_version_digest"],
                        "task_id": task_dir.name,
                        "domain": domain,
                        "rule": finding.rule,
                        "severity": finding.severity,
                        "message": finding.message,
                    }
                )

    collisions = {
        slug: members for slug, members in slug_index.items() if len(set(members)) > 1
    }
    digest_of = {(row["domain"], row["task_id"]): row["task_version_digest"] for row in versions}
    for slug, members in sorted(collisions.items()):
        involved = sorted({f"{domain}/{task_id}" for domain, task_id in members})
        if len(involved) < 2:
            continue
        for domain, task_id in sorted(set(members)):
            finding_rows.append(
                {
                    "task_version_digest": digest_of[(domain, task_id)],
                    "task_id": task_id,
                    "domain": domain,
                    "rule": "mimo-id-case-collision",
                    "severity": "warning",
                    "message": f"slug {slug!r} collides across tasks: {', '.join(involved)}",
                }
            )

    lineage: list[dict[str, Any]] = []
    records, skipped = load_lineage_dicts(repo_root)
    report.n_variants = len(records)
    report.skipped_variant_records = skipped
    for record, record_path in records:
        versions.append(_variant_version_row(record))
        parent = record.get("parent") if isinstance(record.get("parent"), dict) else {}
        lineage.append(
            {
                "child_digest": record.get("variant_digest"),
                "child_harbor_digest": record.get("variant_harbor_digest"),
                "parent_digest": parent.get("digest"),
                "parent_harbor_digest": parent.get("harbor_digest"),
                "transform": record.get("transform"),
                "record_path": record_path,
                "origin": "variant",
            }
        )

    tables: dict[str, list[dict[str, Any]]] = {
        "task_sources": sources,
        "task_versions": versions,
        "task_findings": finding_rows,
        "task_lineage": lineage,
    }
    for name in CATALOG_TABLES:
        rows = tables[name]
        table = pa.Table.from_pylist(rows, schema=_CATALOG_SCHEMAS[name])
        pq.write_table(table, outdir / f"{name}.parquet")
        report.tables[name] = len(rows)

    tally: dict[str, dict[str, int]] = {}
    for row in finding_rows:
        tally.setdefault(row["rule"], {}).setdefault(row["domain"], 0)
        tally[row["rule"]][row["domain"]] += 1
    report.findings_by_rule_domain = tally
    unresolved: dict[str, int] = {}
    for row in finding_rows:
        if row["rule"] == "split-group-unresolved":
            domain = str(row["domain"])
            unresolved[domain] = unresolved.get(domain, 0) + 1
    report.unresolved_splits = unresolved
    return report


def wilson_interval(n_pass: int, n_scored: int, z: float = 1.96) -> tuple[float | None, float | None]:
    """Wilson score interval for a pass rate (95% by default; null when unscored)."""
    if n_scored <= 0:
        return None, None
    rate = n_pass / n_scored
    denom = 1.0 + z * z / n_scored
    center = (rate + z * z / (2.0 * n_scored)) / denom
    margin = (
        z * math.sqrt(rate * (1.0 - rate) / n_scored + z * z / (4.0 * n_scored * n_scored)) / denom
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def outcome_verdict(n_attempts: int, n_scored: int, n_pass: int) -> str:
    """HAR-83 verdict: infra attempts never count as pass or fail.

    nop/control trials stay distinguishable because the view groups by agent
    name and model; they never merge into model learnability rows.
    """
    if n_attempts == 0:
        return "untested"
    if n_scored == 0:
        return "infra_only"
    rate = n_pass / n_scored
    if rate >= 1.0:
        return "always_pass"
    if rate <= 0.0:
        return "always_fail"
    return "learnable"


def task_outcomes_sql(trials: str = "trial_facts", versions: str = "task_versions") -> str:
    """Per task-version x agent/model outcome rollup (Harbor digest join).

    Infra = missing reward, reward -1, or any exception: counted in n_infra,
    never in the pass rate. Wilson 95% interval is computed on scored
    attempts only.
    """
    inner = f"""
        SELECT
            v.task_version_digest AS task_version_digest,
            v.harbor_digest AS harbor_digest,
            v.task_id AS task_id,
            v.domain AS domain,
            COALESCE(t.agent_name, '') AS agent_name,
            COALESCE(t.model_name, '') AS model_name,
            COUNT(t.trial_id) AS n_attempts,
            COALESCE(SUM(CASE WHEN t.trial_id IS NOT NULL AND (
                t.primary_reward IS NULL OR t.exception_class IS NOT NULL
                OR t.primary_reward < 0) THEN 1 ELSE 0 END), 0) AS n_infra,
            COALESCE(SUM(CASE WHEN t.trial_id IS NOT NULL
                AND t.primary_reward IS NOT NULL AND t.exception_class IS NULL
                AND t.primary_reward >= 0 THEN 1 ELSE 0 END), 0) AS n_scored,
            COALESCE(SUM(CASE WHEN t.trial_id IS NOT NULL
                AND t.primary_reward IS NOT NULL AND t.exception_class IS NULL
                AND t.primary_reward >= 0 AND t.primary_reward >= 1.0
                THEN 1 ELSE 0 END), 0) AS n_pass,
            AVG(CASE WHEN t.trial_id IS NOT NULL
                AND t.primary_reward IS NOT NULL AND t.exception_class IS NULL
                AND t.primary_reward >= 0 THEN t.primary_reward END) AS mean_reward
        FROM {versions} AS v
        LEFT JOIN {trials} AS t ON t.task_digest = v.harbor_digest
        GROUP BY v.task_version_digest, v.harbor_digest, v.task_id, v.domain,
            COALESCE(t.agent_name, ''), COALESCE(t.model_name, '')
    """
    return f"""
    SELECT
        task_version_digest, harbor_digest, task_id, domain,
        agent_name, model_name, n_attempts, n_scored, n_infra, n_pass, mean_reward,
        CASE WHEN n_scored > 0 THEN n_pass * 1.0 / n_scored END AS pass_rate,
        CASE WHEN n_scored > 0 THEN GREATEST(0.0,
            ((n_pass * 1.0 / n_scored + 3.8416 / (2 * n_scored)) / (1 + 3.8416 / n_scored))
            - (1.96 * SQRT((n_pass * 1.0 / n_scored) * (1 - n_pass * 1.0 / n_scored)
                / n_scored + 3.8416 / (4 * n_scored * n_scored))
                / (1 + 3.8416 / n_scored))) END AS pass_rate_lo,
        CASE WHEN n_scored > 0 THEN LEAST(1.0,
            ((n_pass * 1.0 / n_scored + 3.8416 / (2 * n_scored)) / (1 + 3.8416 / n_scored))
            + (1.96 * SQRT((n_pass * 1.0 / n_scored) * (1 - n_pass * 1.0 / n_scored)
                / n_scored + 3.8416 / (4 * n_scored * n_scored))
                / (1 + 3.8416 / n_scored))) END AS pass_rate_hi,
        CASE
            WHEN n_attempts = 0 THEN 'untested'
            WHEN n_scored = 0 THEN 'infra_only'
            WHEN n_pass * 1.0 / n_scored >= 1.0 THEN 'always_pass'
            WHEN n_pass * 1.0 / n_scored <= 0.0 THEN 'always_fail'
            ELSE 'learnable'
        END AS verdict
    FROM ({inner}) AS rollup
    """


def render_build_report(report: CatalogBuildReport) -> str:
    """Human-readable build summary with row counts and finding tallies."""
    lines = [f"catalog: {report.catalog_dir}"]
    lines.append("snapshots: " + (", ".join(report.snapshots) if report.snapshots else "(none)"))
    for name in CATALOG_TABLES:
        lines.append(f"  {name}: {report.tables.get(name, 0)} rows")
    lines.append(
        f"variants: {report.n_variants} indexed, {report.skipped_variant_records} skipped"
    )
    if report.findings_by_rule_domain:
        lines.append("findings (rule x domain):")
        for rule in sorted(report.findings_by_rule_domain):
            per_domain = report.findings_by_rule_domain[rule]
            detail = ", ".join(f"{domain}={per_domain[domain]}" for domain in sorted(per_domain))
            lines.append(f"  {rule}: {detail}")
    if report.unresolved_splits:
        detail = ", ".join(
            f"{domain}={count}" for domain, count in sorted(report.unresolved_splits.items())
        )
        lines.append(f"split_group fallbacks (task_id, unresolved): {detail}")
    return "\n".join(lines) + "\n"


def show_task(
    selector: str,
    *,
    repo_root: Path,
    derived_root: Path | None = None,
) -> str:
    """Render identity, digests, pin, findings, lineage, and outcomes for one task."""
    from evallab.storage.attach import attach

    derived = derived_root or derived_root_from_environment(repo_root)
    result = attach(repo_root=repo_root, explicit_derived=derived)
    conn = result.connection
    rows = conn.execute(
        """
        SELECT * FROM task_versions
        WHERE task_id = ? OR task_version_digest = ?
           OR harbor_digest = ? OR task_version_digest LIKE ?
           OR harbor_digest LIKE ?
        """,
        [selector, selector, selector, f"%{selector}%", f"%{selector}%"],
    ).fetchall()
    columns = [desc[0] for desc in conn.description]
    if not rows:
        result.connection.close()
        raise CatalogError(f"no catalog task matches {selector!r}")
    if len(rows) > 1:
        ids = sorted({str(row[columns.index("task_id")]) for row in rows})
        result.connection.close()
        raise CatalogError(
            f"{len(rows)} catalog tasks match {selector!r}: {', '.join(ids[:10])}"
            + (" ..." if len(ids) > 10 else "")
            + " (use a full digest)"
        )
    row = dict(zip(columns, rows[0], strict=True))
    digest = str(row["task_version_digest"])
    harbor = str(row["harbor_digest"])
    try:
        findings = conn.execute(
            "SELECT rule, severity, message FROM task_findings "
            "WHERE task_version_digest = ? ORDER BY rule",
            [digest],
        ).fetchall()
    except Exception:
        findings = []
    try:
        children = conn.execute(
            "SELECT child_digest, transform FROM task_lineage WHERE parent_digest = ?",
            [digest],
        ).fetchall()
    except Exception:
        children = []
    try:
        outcomes = conn.execute(
            "SELECT agent_name, model_name, n_attempts, n_scored, n_infra, "
            "pass_rate, pass_rate_lo, pass_rate_hi, verdict FROM v_task_outcomes "
            "WHERE task_version_digest = ?",
            [digest],
        ).fetchall()
    except Exception:
        outcomes = []
    result.connection.close()

    lines = [
        f"task: {row.get('task_name')} (task_id={row.get('task_id')}, source_id={row.get('source_id')})",
        f"origin: {row.get('origin')}",
        f"task_version_digest: {digest}",
        f"harbor_digest: {harbor}",
        "components: "
        f"task_toml={row.get('digest_task_toml')} "
        f"instruction={row.get('digest_instruction')} "
        f"environment={row.get('digest_environment')} "
        f"verifier={row.get('digest_verifier')}",
        f"source: {row.get('source_repo')}@{row.get('source_revision')} "
        f"path={row.get('source_path')} manifest_sha={row.get('upstream_manifest_sha256')}",
        f"parent: {row.get('parent_digest')} transform={row.get('transform')}",
        f"domain={row.get('domain')} category={row.get('category')} image={row.get('docker_image')}",
        f"grader: {row.get('grader_kind')}/{row.get('grader_cost')} "
        f"solution={row.get('has_solution')} net={row.get('network_mode')} "
        f"agent_user={row.get('agent_user')}",
        f"limits: steps={row.get('step_limit')} agent_timeout={row.get('agent_timeout_sec')} "
        f"verifier_timeout={row.get('verifier_timeout_sec')} "
        f"cpus={row.get('cpus')} mem_mb={row.get('memory_mb')}",
        f"instruction: {row.get('instruction_chars')} chars lang={row.get('instruction_lang')}",
        f"split_group: {row.get('split_group')}",
        f"findings ({len(findings)}):",
    ]
    for rule, severity, message in findings:
        lines.append(f"  [{severity}] {rule}: {message}")
    lines.append(f"lineage children ({len(children)}):")
    for child_digest, transform in children:
        lines.append(f"  {child_digest} via {transform}")
    lines.append(f"outcomes ({len(outcomes)}):")
    for outcome in outcomes:
        lines.append(
            f"  agent={outcome[0]!r} model={outcome[1]!r} attempts={outcome[2]} "
            f"scored={outcome[3]} infra={outcome[4]} pass_rate={outcome[5]} "
            f"[{outcome[6]}, {outcome[7]}] verdict={outcome[8]}"
        )
    return "\n".join(lines) + "\n"


def task_audit_sql(
    outcomes: str = "v_task_outcomes",
    versions: str = "task_versions",
    stability: str = "task_stability",
    exploits: str = "task_exploits",
    *,
    has_stability: bool = True,
    has_exploits: bool = True,
) -> str:
    """One row per task version x agent/model with verdict, stability, exploit, eligibility.

    Missing stability/exploit tables yield null columns, never guessed
    values; eligibility is then false with a reason. Split assignment
    (train|heldout) is applied at export time, so the view stays
    split-agnostic and ``train_eligible`` here means learnable, stable, and
    free of confirmed exploits.
    """
    stability_source = (
        f"(SELECT task_version_digest, verdict, n_runs, evidence_path FROM {stability})"
        if has_stability
        else "(SELECT CAST(NULL AS VARCHAR) AS task_version_digest, "
        "CAST(NULL AS VARCHAR) AS verdict, CAST(NULL AS INTEGER) AS n_runs, "
        "CAST(NULL AS VARCHAR) AS evidence_path WHERE FALSE)"
    )
    exploits_source = (
        f"(SELECT task_version_digest, exploit_status, evidence_path FROM {exploits})"
        if has_exploits
        else "(SELECT CAST(NULL AS VARCHAR) AS task_version_digest, "
        "CAST(NULL AS VARCHAR) AS exploit_status, "
        "CAST(NULL AS VARCHAR) AS evidence_path WHERE FALSE)"
    )
    return f"""
    WITH stab AS (
        SELECT s.task_version_digest AS digest,
            SUM(s.n_runs) AS stability_runs,
            CASE
                WHEN SUM(CASE WHEN s.verdict = 'flipped' THEN 1 ELSE 0 END) > 0 THEN 'flipped'
                WHEN SUM(CASE WHEN s.verdict = 'errored' THEN 1 ELSE 0 END) > 0 THEN 'errored'
                WHEN COUNT(*) > 0 THEN 'stable'
            END AS stability,
            LIST(s.evidence_path) AS stability_evidence_paths
        FROM {stability_source} AS s
        GROUP BY s.task_version_digest
    ),
    expl AS (
        SELECT e.task_version_digest AS digest,
            CASE
                WHEN SUM(CASE WHEN e.exploit_status = 'confirmed' THEN 1 ELSE 0 END) > 0
                    THEN 'confirmed'
                WHEN SUM(CASE WHEN e.exploit_status = 'suspected' THEN 1 ELSE 0 END) > 0
                    THEN 'suspected'
                WHEN SUM(CASE WHEN e.exploit_status = 'none' THEN 1 ELSE 0 END) > 0 THEN 'none'
                WHEN COUNT(*) > 0 THEN 'not_probed'
            END AS exploit_status,
            LIST(e.evidence_path) AS exploit_evidence_paths
        FROM {exploits_source} AS e
        GROUP BY e.task_version_digest
    )
    SELECT
        v.task_version_digest AS task_version_digest,
        v.harbor_digest AS harbor_digest,
        v.task_id AS task_id,
        v.task_name AS task_name,
        v.domain AS domain,
        v.split_group AS split_group,
        v.grader_kind AS grader_kind,
        v.grader_cost AS grader_cost,
        o.agent_name AS agent_name,
        o.model_name AS model_name,
        o.n_attempts AS n_attempts,
        o.n_scored AS n_scored,
        o.n_infra AS n_infra,
        o.pass_rate AS pass_rate,
        o.pass_rate_lo AS pass_rate_lo,
        o.pass_rate_hi AS pass_rate_hi,
        o.verdict AS verdict,
        stab.stability AS stability,
        stab.stability_runs AS stability_runs,
        stab.stability_evidence_paths AS stability_evidence_paths,
        expl.exploit_status AS exploit_status,
        expl.exploit_evidence_paths AS exploit_evidence_paths,
        COALESCE(o.verdict = 'learnable' AND stab.stability = 'stable'
            AND (expl.exploit_status IS NULL
                OR expl.exploit_status IN ('none', 'not_probed')), false) AS train_eligible,
        CASE
            WHEN o.verdict IS NULL OR o.verdict != 'learnable'
                THEN 'not learnable: ' || COALESCE(o.verdict, 'missing')
            WHEN stab.stability IS NULL THEN 'no stability evidence'
            WHEN stab.stability != 'stable' THEN 'unstable verifier: ' || stab.stability
            WHEN expl.exploit_status = 'confirmed' THEN 'confirmed exploit'
        END AS train_ineligible_reason
    FROM {versions} AS v
    LEFT JOIN {outcomes} AS o ON o.task_version_digest = v.task_version_digest
    LEFT JOIN stab ON stab.digest = v.task_version_digest
    LEFT JOIN expl ON expl.digest = v.task_version_digest
    """


@dataclass(frozen=True)
class ExportResult:
    """Outcome of one train-eligible export."""

    path: Path
    sha256: str
    n_eligible: int
    provisional: bool
    table_digests: dict[str, str | None]


def _sha256_file(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _read_split_map(split_path: Path | None) -> dict[str, str]:
    if split_path is None:
        return {}
    try:
        payload = json.loads(split_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogError(f"unreadable split file {split_path}: {exc}") from exc
    mapping = payload.get("splits") if isinstance(payload, dict) else None
    if mapping is None:
        mapping = payload
    if not isinstance(mapping, dict):
        raise CatalogError(f"split file {split_path} must map digests to train|heldout")
    splits: dict[str, str] = {}
    for digest, split in mapping.items():
        if split not in ("train", "heldout"):
            raise CatalogError(
                f"split file {split_path}: {digest!r} maps to {split!r} (want train|heldout)"
            )
        splits[str(digest)] = str(split)
    return splits


def export_train_eligible(
    out: Path,
    *,
    repo_root: Path,
    derived_root: Path | None = None,
    split_path: Path | None = None,
) -> ExportResult:
    """Write the git-tracked train-eligible JSON with table digests and a self hash.

    Eligibility = view ``train_eligible`` and split != heldout. Without a
    split file the export is marked provisional and splits stay unassigned.
    """
    from evallab.storage.attach import attach

    derived = derived_root or derived_root_from_environment(repo_root)
    catalog = catalog_dir(derived)
    if not (catalog / "task_versions.parquet").is_file():
        raise CatalogError(f"no catalog tables under {catalog}; run catalog build first")
    table_digests: dict[str, str | None] = {}
    for name in (*CATALOG_TABLES, "task_stability", "task_exploits"):
        path = catalog / f"{name}.parquet"
        table_digests[name] = _sha256_file(path) if path.is_file() else None

    splits = _read_split_map(split_path)
    result = attach(repo_root=repo_root, explicit_derived=derived)
    try:
        rows = result.connection.execute(
            "SELECT task_version_digest, harbor_digest, task_name, split_group, "
            "verdict, pass_rate, stability, exploit_status, train_eligible, "
            "train_ineligible_reason FROM v_task_audit"
        ).fetchall()
        columns = [desc[0] for desc in result.connection.description]
    finally:
        result.connection.close()
    provisional = split_path is None
    items: list[dict[str, Any]] = []
    for row in rows:
        record = dict(zip(columns, row, strict=True))
        if not record.get("train_eligible"):
            continue
        digest = str(record["task_version_digest"])
        split = splits.get(digest, "unassigned")
        if split == "heldout":
            continue
        items.append(
            {
                "task_version_digest": digest,
                "harbor_digest": record.get("harbor_digest"),
                "task_name": record.get("task_name"),
                "split_group": record.get("split_group"),
                "split": split,
                "verdict": record.get("verdict"),
                "pass_rate": record.get("pass_rate"),
                "stability": record.get("stability"),
                "exploit_status": record.get("exploit_status"),
            }
        )
    items.sort(key=lambda item: str(item["task_version_digest"]))
    meta = {
        "created_at": datetime.now(UTC).isoformat(),
        "provisional": provisional,
        "split_path": str(split_path) if split_path else None,
        "table_digests": table_digests,
        "n_eligible": len(items),
        "n_considered": len(rows),
    }
    payload = {"schema": "evallab.train_eligible/v1", "items": items, "meta": meta}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    sha = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({**payload, "sha256": sha}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return ExportResult(
        path=out, sha256=sha, n_eligible=len(items),
        provisional=provisional, table_digests=table_digests,
    )
