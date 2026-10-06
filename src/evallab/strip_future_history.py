"""Remove future git history from MiMo Code setups (``strip-future-history@1``).

The parent hides an untruncated ``.git`` in ``/var/lib/mimo/git-hidden``.
A root agent can still read it; when no ref points past the base, dangling
objects can expose the fix even without that move (HAR-161).

After setup records its base SHA, rebuild ``.git`` from a self-contained pack
containing BASE and every ancestor object present in the image. No refs,
reflogs, pseudo-ref files, kept/cruft packs or future objects are copied. Remove
the original object store, not merely its references. Any failed operation
stops setup before the ready sentinel; there is no hide-only fallback.
Images built with history truncated at the base may already lack some of
BASE's ancestors (``git fsck`` reports them as broken links / missing
commits); that is pre-existing past history, not a strip failure, so the
post-rebuild verification only counts surviving future objects, never the
fsck exit code.

The base SHA and worktree bytes survive unchanged. The grader still diffs
against that SHA, resets test files from it and applies the same hidden patch;
``tests/``, instruction and image are not modified. The setup is also re-embedded
in the existing task.toml healthcheck payload, which is what actually executes.
Linked worktrees and alternate object stores are refused rather than leaving
an unchecked copy of future history elsewhere in the environment.
"""

from __future__ import annotations

import base64
import gzip
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

#: Transform id recorded in lineage.
TRANSFORM_ID = "strip-future-history@1"

#: Marker comment identifying the inserted block (also the idempotence guard).
STRIP_MARKER = "strip-future-history@1"

#: Package-relative path of the setup script this transform rewrites.
SETUP_REL = "environment/setup/setup.sh"

#: Line after which the strip block is inserted (the recorded-base anchor).
BASE_ANCHOR = 'echo "$BASE" > "$M/base"'

#: The generated history-count and hide blocks are replaced, not retained.
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

#: Only raw objects reachable from BASE are exported; no original git metadata
#: or pack storage survives. No delta/object reuse can pull a future delta base
#: into the exported pack. Fail before setup marks the environment ready.
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

#: Post-rebuild acceptance on the clean git directory. Counts only future
#: objects: unreachable/dangling commits (the HAR-161 leak shape) and refs
#: reaching beyond BASE. Deliberately ignores the fsck exit code and its
#: broken-link/missing-object lines: truncated-at-base images already lack
#: some of BASE's ancestors (e.g. 000076: BASE 90915883 names two absent
#: parents), and that pre-existing past-history gap must not fail setup.
#: Import integrity is already guarded by the index-pack exit code above.
STRIP_VERIFY = """\
# strip-future-history@1: accept the rebuilt history only with no future objects.
_strip_unreach=$(git --git-dir="$CWD/.git" fsck --unreachable --no-reflogs 2>/dev/null | grep -c -E '^(unreachable|dangling) commit ' || true)
[ "$_strip_unreach" -eq 0 ] || fail "unexpected future commits in clean git directory"
_strip_later=$(git --git-dir="$CWD/.git" rev-list --all --not "$BASE" 2>/dev/null | head -n 5 | grep -c . || true)
[ "$_strip_later" -eq 0 ] || fail "unexpected refs beyond base in clean git directory"
"""

#: ``echo <blob> | base64 -d`` inside the healthcheck command.
_BLOB_PATTERN = re.compile(r"echo ([A-Za-z0-9+/=]+) \| base64 -d")


def _gzip_bytes(data: bytes) -> bytes:
    """gzip a payload exactly like the mimo_harbor setup packer.

    ``gzip.compress(..., mtime=0)`` emits a host-dependent OS byte on some
    interpreters, so the 10-byte header is fixed explicitly (deflate level 9,
    XFL 2, OS 255) around a raw level-9 deflate body. Re-packing an unchanged
    setup dir is byte-identical to the parent healthcheck payload.
    """
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
    """Validated facts about the parent package."""

    task_name: str
    workdir: str
    docker_image: str
    setup_sh: str
    task_toml_text: str
    healthcheck_command: str


def read_parent_info(parent_dir: Path | str) -> ParentInfo:
    """Read and validate the parent package; raise :class:`VariantInvalid`."""
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
    """Parent ``setup.sh`` plus the strip block (idempotent).

    The recorded-base lines are preserved byte-identical; only the
    history-accounting region changes. Returns the input unchanged when the
    block is already present.
    """
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
    """Pack ``environment/setup/`` into the base64 tarball for task.toml.

    Reproduces the mimo_harbor convention exactly: ``ustar`` members sorted by
    name, no directory entries, mode ``0o700``, ``mtime`` 0, uid/gid 0, gzip
    ``mtime`` 0. ``setup_sh`` overrides the on-disk ``setup.sh`` bytes (the
    repair path); every other file is read from ``setup_dir``.
    """
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
    """Swap the embedded setup payload; everything else stays byte-identical."""
    match = _BLOB_PATTERN.search(parent_text)
    if match is None:
        raise VariantInvalid("task.toml healthcheck command has no embedded payload")
    return parent_text[: match.start(1)] + new_blob + parent_text[match.end(1) :]


def unpack_setup_blob(blob: str) -> dict[str, bytes]:
    """Unpack a healthcheck payload blob into ``{arcname: bytes}`` (tests)."""
    buffer = io.BytesIO(gzip.decompress(base64.b64decode(blob)))
    with tarfile.open(fileobj=buffer, mode="r") as tar:
        out: dict[str, bytes] = {}
        for member in tar.getmembers():
            extracted = tar.extractfile(member)
            if extracted is None:
                raise VariantInvalid(f"setup payload entry {member.name!r} has no file content")
            out[member.name] = extracted.read()
        return out


def build_changes(
    parent_dir: Path | str,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs.

    Only ``environment/setup/setup.sh`` and the ``task.toml`` healthcheck
    payload change; grading (``tests/``) is byte-identical by construction.
    """
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
    """Derive the ``strip-future-history@1`` variant of a MiMo task package."""
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


__all__ = [
    "SETUP_REL",
    "STRIP_VERIFY",
    "STRIP_MARKER",
    "TRANSFORM_ID",
    "ParentInfo",
    "build_changes",
    "build_setup_sh",
    "derive_strip_future_history",
    "pack_setup",
    "read_parent_info",
    "render_task_toml",
    "unpack_setup_blob",
]
