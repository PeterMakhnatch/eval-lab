from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path
from typing import Any

from evallab.strip_future_history import pack_setup, render_task_toml
from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

TRANSFORM_ID = "purge-installed-copies@1"
SETUP_REL = "environment/setup/setup.sh"
ANCHOR = "\nwrite_blocklist\n"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SCRIPT = Path(__file__).with_name("purge_container.py")
PURGE_PY = _SCRIPT.read_text(encoding="utf-8")
if "def remove_copies" not in PURGE_PY or "def main" not in PURGE_PY:
    raise RuntimeError("purge container script is missing remove_copies or main")
if "def v2_neutralize_matches" not in PURGE_PY or "def v2_action_verify" not in PURGE_PY:
    raise RuntimeError("purge container script is missing the @2 neutralize entry points")

TRANSFORM_ID_V2 = "purge-installed-copies@2"
SETUP_PYTHON_PATH = "/usr/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/bin"
MARKER_V2 = "# purge-installed-copies@2: neutralize installed copies of this project's\n"
V1_BLOCK_START = "# purge-installed-copies@1: remove this project's build and installed copies,\n"
_HEREDOC_END = "\nPY\n"


def _read_parent(parent_dir: Path) -> tuple[str, str, str, str]:
    try:
        text = (parent_dir / "task.toml").read_text(encoding="utf-8")
        data = tomllib.loads(text)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise VariantInvalid(f"parent task.toml is missing or invalid: {exc}") from exc
    environment = data.get("environment") if isinstance(data, dict) else None
    if not isinstance(environment, dict):
        raise VariantInvalid("parent task.toml has no [environment]")
    workdir = environment.get("workdir")
    image = environment.get("docker_image")
    task_raw = data.get("task")
    task = task_raw if isinstance(task_raw, dict) else {}
    task_name = task.get("name")
    if not isinstance(workdir, str) or not isinstance(image, str) or not isinstance(task_name, str):
        raise VariantInvalid("parent task.toml is missing name, workdir or docker_image")
    setup_path = parent_dir / SETUP_REL
    if not setup_path.is_file():
        raise VariantInvalid(f"parent has no {SETUP_REL}")
    return task_name, workdir, image, setup_path.read_text(encoding="utf-8")


def shell_block_v2() -> str:
    return (
        MARKER_V2 + "# tracked sources in place (BASE bytes overwrite, fix-added files deleted,\n"
        "# adjacent pyc dropped). Install metadata and other distributions stay.\n"
        'python3 - "$CWD" neutralize <<\'PY\' || fail "purge-installed-copies@2 failed"\n'
        + PURGE_PY
        + "PY\n"
        'python3 - "$CWD" verify2 <<\'PY\' || fail "purge-installed-copies@2 left a non-base copy"\n'
        + PURGE_PY
        + "PY\n"
    )


def _excise_v1_block(parent_setup_sh: str) -> str:
    start = parent_setup_sh.find(V1_BLOCK_START)
    if start < 0:
        return parent_setup_sh
    end = start
    for _ in range(3):
        end = parent_setup_sh.find(_HEREDOC_END, end + len(V1_BLOCK_START))
        if end < 0:
            raise VariantInvalid("parent @1 block has an unexpected shape; refusing to purge")
        end += len(_HEREDOC_END)
    excised = parent_setup_sh[:start] + parent_setup_sh[end:]
    if V1_BLOCK_START in excised:
        raise VariantInvalid("parent @1 block has an unexpected shape; refusing to purge")
    return excised


def build_setup_sh_v2(parent_setup_sh: str) -> tuple[str, bool]:
    if MARKER_V2 in parent_setup_sh:
        raise VariantInvalid("parent setup.sh already carries purge-installed-copies@2")
    if ANCHOR not in parent_setup_sh:
        raise VariantInvalid("setup.sh has no write_blocklist call; refusing to purge")
    superseded = V1_BLOCK_START in parent_setup_sh
    without_v1 = _excise_v1_block(parent_setup_sh)
    setup_path = (
        "# Setup helpers need system Python; do not change the project test runner.\n"
        f"export PATH='{SETUP_PYTHON_PATH}'\n"
    )
    if without_v1.startswith("#!"):
        shebang, _, body = without_v1.partition("\n")
        without_v1 = shebang + "\n" + setup_path + body
    else:
        without_v1 = setup_path + without_v1
    return (
        without_v1.replace(ANCHOR, "\n" + shell_block_v2() + "write_blocklist\n", 1),
        superseded,
    )


def derive_purge_installed_copies_v2(
    parent_dir: Path | str,
    *,
    repairs_digest: str,
    rationale: str = (
        "Neutralize installed copies of this project's tracked sources in "
        "place (BASE bytes overwrite, fix-added files deleted, adjacent pyc "
        "dropped). Fail setup if a non-base copy remains or the top-level "
        "package no longer imports. No-op when nothing matches."
    ),
    created_by: str = "purge-installed-copies-v2",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    if _DIGEST.fullmatch(repairs_digest) is None:
        raise VariantInvalid(f"repairs_digest is not a sha256 digest: {repairs_digest}")
    parent = Path(parent_dir)
    task_name, workdir, image, setup_sh = _read_parent(parent)
    new_setup, superseded = build_setup_sh_v2(setup_sh)
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
        "repairs_digest": repairs_digest,
        "removed": ["neutralized-in-place"],
        "superseded_v1": superseded,
        "setup_python_path": SETUP_PYTHON_PATH,
        "setup_before_sha256": f"sha256:{hashlib.sha256(setup_sh.encode()).hexdigest()}",
        "setup_after_sha256": f"sha256:{hashlib.sha256(new_setup.encode()).hexdigest()}",
    }
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
        transform=TRANSFORM_ID_V2,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        **kwargs,
    )


