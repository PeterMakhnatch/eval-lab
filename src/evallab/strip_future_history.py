from __future__ import annotations

import base64
import hashlib
import io
import re
import struct
import tarfile
import tomllib
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

TRANSFORM_ID = "strip-future-history@1"

STRIP_MARKER = "strip-future-history@1"

SETUP_REL = "environment/setup/setup.sh"

BASE_ANCHOR = 'echo "$BASE" > "$M/base"'

_LATER_OLD = (
    "# Images are built with history truncated at the base. If one is not, the fix could be read out of git log,\n"
    "# so .git is hidden while the agent works and put back for grading.\n"
    'LATER=$(git rev-list --all --not "$BASE" 2>/dev/null | head -n 5 | grep -c . || true)'
)
_HIDE_OLD = (
    'if [ "$LATER" -gt 0 ]; then\n'
    '  mv "$CWD/.git" "$M/git-hidden"\n'
    '  echo "history not truncated at ${BASE:0:12}: .git hidden while the agent works"\n'
    "fi\n"
)

STRIP_BLOCK = """\
# strip-future-history@1: keep BASE and its ancestors, destroy all other history.
[ -d "$CWD/.git" ] && [ ! -L "$CWD/.git" ] || fail "unsupported git directory"
_strip_common=$(git rev-parse --git-common-dir) || fail "cannot resolve git directory"
[ "$_strip_common" = ".git" ] || [ "$_strip_common" = "$CWD/.git" ] || fail "linked git directory"
[ ! -e "$CWD/.git/objects/info/alternates" ] || fail "alternate git object store"
git worktree list --porcelain > "$M/strip-worktrees" || fail "cannot list git worktrees"
[ "$(grep -c '^worktree ' "$M/strip-worktrees")" -eq 1 ] || fail "multiple git worktrees"
_strip_dir=$(mktemp -d "$M/strip-git.XXXXXX") || fail "cannot create clean git directory"
git -c init.templateDir= init --bare -q "$_strip_dir" || fail "cannot initialize clean git directory"
git --no-replace-objects pack-objects --revs --stdout --no-reuse-delta --no-reuse-object > "$M/strip-history.pack" <<EOF_STRIP_BASE || fail "cannot export base history"
$BASE
EOF_STRIP_BASE
git --git-dir="$_strip_dir" index-pack --stdin < "$M/strip-history.pack" >/dev/null || fail "cannot import base history"
git --git-dir="$_strip_dir" -c core.logAllRefUpdates=false update-ref --no-deref HEAD "$BASE" || fail "cannot preserve base HEAD"
git config --file "$_strip_dir/config" core.bare false || fail "cannot configure clean git directory"
git config --file "$_strip_dir/config" core.logAllRefUpdates false || fail "cannot disable reflogs"
git --git-dir="$_strip_dir" read-tree "$BASE" || fail "cannot preserve base index"
rm -rf "$CWD/.git" "$M/git-hidden" || fail "cannot remove original git history"
mv "$_strip_dir" "$CWD/.git" || fail "cannot install clean git directory"
rm -f "$M/strip-history.pack" "$M/strip-worktrees" || fail "cannot remove strip temporaries"
"""

STRIP_VERIFY = """\
# strip-future-history@1: accept the rebuilt history only with no future objects.
_strip_unreach=$(git --git-dir="$CWD/.git" fsck --unreachable --no-reflogs 2>/dev/null | grep -c -E '^(unreachable|dangling) commit ' || true)
[ "$_strip_unreach" -eq 0 ] || fail "unexpected future commits in clean git directory"
_strip_later=$(git --git-dir="$CWD/.git" rev-list --all --not "$BASE" 2>/dev/null | head -n 5 | grep -c . || true)
[ "$_strip_later" -eq 0 ] || fail "unexpected refs beyond base in clean git directory"
"""

_BLOB_PATTERN = re.compile(r"echo ([A-Za-z0-9+/=]+) \| base64 -d")


def _gzip_bytes(data: bytes) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS, 8, 0)
    body = compressor.compress(data) + compressor.flush()
    return (
        b"\x1f\x8b\x08\x00"
        + struct.pack("<L", 0)
        + b"\x02\xff"
        + body
        + struct.pack("<L", zlib.crc32(data) & 0xFFFFFFFF)
        + struct.pack("<L", len(data) & 0xFFFFFFFF)
    )


@dataclass(frozen=True)
class ParentInfo:

    task_name: str
    workdir: str
    docker_image: str
    setup_sh: str
    task_toml_text: str
    healthcheck_command: str


def read_parent_info(parent_dir: Path | str) -> ParentInfo:
    parent = Path(parent_dir)
    try:
        config = tomllib.loads((parent / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise VariantInvalid(f"parent task.toml is missing or invalid: {exc}") from exc
    task = config.get("task")
    if not isinstance(task, dict) or not task.get("name"):
        raise VariantInvalid("parent task.toml has no [task].name")
    environment = config.get("environment")
    if not isinstance(environment, dict):
        raise VariantInvalid("parent task.toml has no [environment] table")
    workdir = environment.get("workdir")
    docker_image = environment.get("docker_image")
    if not isinstance(workdir, str) or not workdir.startswith("/"):
        raise VariantInvalid("parent [environment].workdir must be an absolute path")
    if not isinstance(docker_image, str) or not docker_image:
        raise VariantInvalid("parent [environment].docker_image must be set")
    healthcheck = environment.get("healthcheck")
    command = healthcheck.get("command") if isinstance(healthcheck, dict) else None
    if not isinstance(command, str) or _BLOB_PATTERN.search(command) is None:
        raise VariantInvalid(
            "parent [environment.healthcheck].command has no embedded setup payload"
        )
    try:
        setup_sh = (parent / SETUP_REL).read_text(encoding="utf-8")
    except OSError as exc:
        raise VariantInvalid(f"parent {SETUP_REL} is missing: {exc}") from exc
    if BASE_ANCHOR not in setup_sh:
        raise VariantInvalid(
            "parent setup.sh records no base commit "
            f"({BASE_ANCHOR!r} absent; not a git-based Code setup)"
        )
    if STRIP_MARKER in setup_sh:
        raise VariantInvalid("parent setup.sh already carries strip-future-history@1")
    if _LATER_OLD not in setup_sh or _HIDE_OLD not in setup_sh:
        raise VariantInvalid("parent setup.sh is not the expected mimo_harbor shape")
    return ParentInfo(
        task_name=str(task["name"]),
        workdir=workdir,
        docker_image=docker_image,
        setup_sh=setup_sh,
        task_toml_text=(parent / "task.toml").read_text(encoding="utf-8"),
        healthcheck_command=command,
    )


def build_setup_sh(parent_setup_sh: str) -> str:
    if STRIP_MARKER in parent_setup_sh:
        return parent_setup_sh
    if BASE_ANCHOR not in parent_setup_sh:
        raise VariantInvalid("setup.sh records no base commit; refusing to strip")
    if _LATER_OLD not in parent_setup_sh:
        raise VariantInvalid("setup.sh is not the expected mimo_harbor shape")
    if _HIDE_OLD not in parent_setup_sh:
        raise VariantInvalid("setup.sh hide block is not the expected shape")
    text = parent_setup_sh.replace(_LATER_OLD, (STRIP_BLOCK + STRIP_VERIFY).rstrip("\n"), 1)
    return text.replace(_HIDE_OLD, "", 1)


def pack_setup(setup_dir: Path | str, *, setup_sh: bytes | None = None) -> str:
    root = Path(setup_dir)
    members: list[tuple[str, bytes]] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(root).as_posix()
        data = setup_sh if (rel == "setup.sh" and setup_sh is not None) else None
        if data is None:
            data = path.read_bytes()
        members.append((rel, data))
    if "setup.sh" not in {rel for rel, _ in members}:
        raise VariantInvalid(f"setup dir has no setup.sh: {root}")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for rel, data in members:
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o700
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(_gzip_bytes(buffer.getvalue())).decode("ascii")


def render_task_toml(parent_text: str, *, new_blob: str) -> str:
    match = _BLOB_PATTERN.search(parent_text)
    if match is None:
        raise VariantInvalid("task.toml healthcheck command has no embedded payload")
    return parent_text[: match.start(1)] + new_blob + parent_text[match.end(1) :]


def build_changes(
    parent_dir: Path | str,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    parent = Path(parent_dir)
    info = read_parent_info(parent)
    new_setup = build_setup_sh(info.setup_sh)
    if new_setup == info.setup_sh:
        raise VariantInvalid("parent setup.sh already carries strip-future-history@1")
    new_blob = pack_setup(
        parent / "environment" / "setup", setup_sh=new_setup.encode("utf-8")
    )
    changes: dict[str, bytes | None] = {
        SETUP_REL: new_setup.encode("utf-8"),
        "task.toml": render_task_toml(info.task_toml_text, new_blob=new_blob).encode("utf-8"),
    }
    inputs: dict[str, Any] = {
        "parent_task": info.task_name,
        "workdir": info.workdir,
        "docker_image": info.docker_image,
        "leak_patterns": ["refs-beyond-base", "unreachable-commits"],
        "setup_before_sha256": f"sha256:{hashlib.sha256(info.setup_sh.encode()).hexdigest()}",
        "setup_after_sha256": f"sha256:{hashlib.sha256(new_setup.encode()).hexdigest()}",
    }
    return changes, inputs


def derive_strip_future_history(
    parent_dir: Path | str,
    *,
    rationale: str = (
        "Rebuild agent-visible git storage from exactly BASE and its ancestors; "
        "remove the original refs, metadata and objects. Fail setup on errors "
        "instead of hiding a root-readable copy; grading is unchanged."
    ),
    created_by: str = "har177-strip-future-history",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    changes, inputs = build_changes(parent_dir)
    kwargs: dict[str, Any] = {}
    if repo_root is not None:
        kwargs["repo_root"] = repo_root
    if parent_source is not None:
        kwargs["parent_source"] = parent_source
    if variants_root is not None:
        kwargs["variants_root"] = variants_root
    return derive_task(
        parent_dir,
        changes=changes,
        transform=TRANSFORM_ID,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        **kwargs,
    )
