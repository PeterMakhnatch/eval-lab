"""Prefetch abcmidi into MiMo music setups (``music-prefetch-abcmidi@1``).

All 1,000 music tasks share one image and one scorer. Grading
(``tests/test.sh``) installs ``abcmidi`` with ``apt-get`` at grade time, so
grading fails once the egress lock blocks the network (exit 1, unscored).
The grader already skips that install when ``abc2midi`` is on PATH
(``command -v`` guard), so the repair is setup-side only: install the same
pinned ``abcmidi`` version while setup still has the network, fail setup
when it cannot, and re-embed the setup in the existing task.toml
healthcheck payload, which is what actually executes. Grading (``tests/``),
the instruction and the image are unchanged.

The install names a version, never an architecture or a ``.deb`` URL, so
``apt`` resolves the image-arch build on its own (amd64 on Daytona, arm64
locally) and the record stays text-only. Embedding the ``.deb`` itself is
rejected by the variant rules (binary, near the size ceiling) and would pin
one architecture.
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Any

from evallab.strip_future_history import pack_setup, render_task_toml
from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

#: Transform id recorded in lineage.
TRANSFORM_ID = "music-prefetch-abcmidi@1"

#: Marker comment identifying the inserted block (also the idempotence guard).
MARKER = "music-prefetch-abcmidi@1"

#: Package-relative path of the setup script this transform rewrites.
SETUP_REL = "environment/setup/setup.sh"

#: Package-relative path of the grading script, left byte-identical.
TEST_REL = "tests/test.sh"

#: Ready sentinel; the block runs just before it, at the end of setup.
ANCHOR = 'touch "$M/ready"'

#: Pinned abcmidi version, matching the grade-time install in tests/test.sh.
ABCMIDI_VERSION = "20250216+ds-1"

#: Grade-time guard this transform relies on: the grader must already skip
#: its own apt install when abc2midi is present, otherwise prefetched setup
#: would not remove the grading-time network touch. Parents without it are
#: refused, not rewritten (grader logic is never deleted here).
GRADER_GUARD = "command -v abc2midi"

#: Setup-time prefetch. Runs as root before the agent starts, while the
#: network is still open; any failure stops setup before the ready sentinel.
PREFETCH_BLOCK = """\
# music-prefetch-abcmidi@1: grading needs abc2midi; install it now while the network is open.
if ! command -v abc2midi >/dev/null 2>&1; then
  apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends abcmidi=20250216+ds-1 >/dev/null \\
    || fail "music-prefetch-abcmidi@1 could not install abcmidi"
fi
command -v abc2midi >/dev/null 2>&1 || fail "music-prefetch-abcmidi@1: abc2midi still missing after install"
"""


def build_setup_sh(parent_setup_sh: str) -> str:
    """Parent ``setup.sh`` plus the prefetch block. Returns input unchanged when present."""
    if MARKER in parent_setup_sh:
        return parent_setup_sh
    if parent_setup_sh.count(ANCHOR) != 1:
        raise VariantInvalid("setup.sh has no unique ready sentinel; refusing to prefetch abcmidi")
    return parent_setup_sh.replace(ANCHOR, PREFETCH_BLOCK.rstrip("\n") + "\n" + ANCHOR, 1)


def _read_parent(parent_dir: Path) -> tuple[str, str, str, str, str]:
    try:
        task_toml = tomllib.loads((parent_dir / "task.toml").read_text(encoding="utf-8"))
        task_name = task_toml["task"]["name"]
        workdir = task_toml["environment"]["workdir"]
        image = task_toml["environment"]["docker_image"]
    except Exception as exc:
        raise VariantInvalid(f"parent task.toml is missing or invalid: {exc}") from exc
    try:
        setup_sh = (parent_dir / SETUP_REL).read_text(encoding="utf-8")
    except OSError as exc:
        raise VariantInvalid(f"parent {SETUP_REL} is missing: {exc}") from exc
    try:
        test_sh = (parent_dir / TEST_REL).read_text(encoding="utf-8")
    except OSError as exc:
        raise VariantInvalid(f"parent {TEST_REL} is missing: {exc}") from exc
    if not task_name or not workdir or not image:
        raise VariantInvalid("parent task.toml names no task, workdir or image")
    if GRADER_GUARD not in test_sh:
        raise VariantInvalid(
            "parent tests/test.sh has no abc2midi presence guard; "
            "prefetch alone would not remove the grading-time network touch"
        )
    return task_name, workdir, image, setup_sh, test_sh


def build_changes(
    parent_dir: Path | str,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs.

    Only ``environment/setup/setup.sh`` and the ``task.toml`` healthcheck
    payload change; grading (``tests/``) is byte-identical by construction.
    """
    parent = Path(parent_dir)
    task_name, workdir, image, setup_sh, _ = _read_parent(parent)
    new_setup = build_setup_sh(setup_sh)
    if new_setup == setup_sh:
        raise VariantInvalid("parent setup.sh already carries music-prefetch-abcmidi@1")
    new_blob = pack_setup(parent / "environment" / "setup", setup_sh=new_setup.encode("utf-8"))
    parent_toml = (parent / "task.toml").read_text(encoding="utf-8")
    changes: dict[str, bytes | None] = {
        SETUP_REL: new_setup.encode("utf-8"),
        "task.toml": render_task_toml(parent_toml, new_blob=new_blob).encode("utf-8"),
    }
    inputs: dict[str, Any] = {
        "parent_task": task_name,
        "workdir": workdir,
        "docker_image": image,
        "abcmidi_version": ABCMIDI_VERSION,
        "arch_note": (
            "version-pinned apt install with no architecture qualifier; "
            "apt resolves the image-arch build (amd64 on Daytona, arm64 locally)"
        ),
        "grader_guard": GRADER_GUARD,
        "setup_before_sha256": f"sha256:{hashlib.sha256(setup_sh.encode()).hexdigest()}",
        "setup_after_sha256": f"sha256:{hashlib.sha256(new_setup.encode()).hexdigest()}",
    }
    return changes, inputs


def derive_music_prefetch(
    parent_dir: Path | str,
    *,
    rationale: str = (
        "Grading apt-installs abcmidi at grade time and fails unscored once "
        "the egress lock blocks the network; setup installs the same pinned "
        "abcmidi version before the agent starts, while the network is open, "
        "failing setup if it cannot. The grader already skips its install "
        "when abc2midi is present, so grading never touches the network. "
        "Grading, instruction and image are unchanged."
    ),
    created_by: str = "music-prefetch",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``music-prefetch-abcmidi@1`` variant of a MiMo music package."""
    parent = Path(parent_dir)
    changes, inputs = build_changes(parent)
    kwargs: dict[str, Any] = {}
    if repo_root is not None:
        kwargs["repo_root"] = repo_root
    if parent_source is not None:
        kwargs["parent_source"] = parent_source
    if variants_root is not None:
        kwargs["variants_root"] = variants_root
    return derive_task(
        parent,
        changes=changes,
        transform=TRANSFORM_ID,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        **kwargs,
    )


__all__ = [
    "ABCMIDI_VERSION",
    "ANCHOR",
    "GRADER_GUARD",
    "MARKER",
    "PREFETCH_BLOCK",
    "SETUP_REL",
    "TEST_REL",
    "TRANSFORM_ID",
    "build_changes",
    "build_setup_sh",
    "derive_music_prefetch",
]
