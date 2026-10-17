"""Release-local setup repair for the retained ntfy Python 3.8 import failure.

Derive this root *before* the normal clean chain: separate-verifier must bundle
its repaired setup. A Dockerfile-only change would not repair the default path:
Harbor uses the pinned ``environment.docker_image`` without building that file,
and the verifier independently starts from the same upstream image. The existing
public setup phase therefore installs one hash-locked, pure-Python wheel; no
network policy, image/resource declaration, test, or project source is changed.

The historical ``ruamel.yaml>=0.11.11,<0.12.0`` predates Python 3.8 wheels.
0.16.13 explicitly supports 3.8 and retains ``from ruamel import yaml`` and
``yaml.safe_load``. Its wheel's ``main.py`` tolerates an absent C extension;
``safe_load`` uses the Python ``SafeLoader``. Install with ``--no-deps`` rather
than introducing the unused ``ruamel.yaml.clib`` binary dependency. These are
source/metadata observations, not a claim that a repaired native control passed.
"""

from __future__ import annotations

import hashlib
import shlex
import tomllib
from pathlib import Path
from typing import Any

from evallab.registry import harbor_task_digest, task_directory_digest
from evallab.strip_future_history import pack_setup, render_task_toml
from evallab.task_variants import (
    RECORDS_DIRNAME,
    VariantInvalid,
    VariantRecord,
    derive_task,
    resolve_record,
)

TRANSFORM_ID = "mimo-v6-environment-repair@1"
TASK_ID = "format-code-task-000838"
SETUP_REL = "environment/setup/setup.sh"
ANCHOR = "\nwrite_blocklist\n"
# A runtime-checked candidate, NOT an executable path observed in the old log.
# Absolute lookup survives the helper-only PATH pin in purge/separate-verifier.
RUNNER_PYTHON_CANDIDATE = "/usr/local/bin/python"
RUNNER_STDLIB = "/usr/local/lib/python3.8"
RUNNER_UNITTEST_LOADER = RUNNER_STDLIB + "/unittest/loader.py"
YAML_METADATA_URL = "https://pypi.org/pypi/ruamel.yaml/0.16.13/json"
YAML_WHEEL_URL = (
    "https://files.pythonhosted.org/packages/ed/c3/"
    "4c823dac2949a6baf36a4987d04c50d30184147393ba6f4bfb4c67d15a13/"
    "ruamel.yaml-0.16.13-py2.py3-none-any.whl"
)
YAML_WHEEL_SHA256 = "64b06e7873eb8e1125525ecef7345447d786368cadca92a7cd9b59eae62e95a3"
YAML_LOCK = f"ruamel.yaml @ {YAML_WHEEL_URL} --hash=sha256:{YAML_WHEEL_SHA256}\n"
FAILURE_LOG = (
    "2026-10-16/unknown-heldout-control-oracle-format-code-task-000838/"
    "heldout-control-oracle-format-co__cygoqWo/verifier/test_output.log"
)


def _repair_block() -> str:
    return f"""# {TRANSFORM_ID}: restore ntfy's missing legacy YAML dependency.
# Select the existing /usr/local Python 3.8 environment, never setup's python3.
# The retained log proves its stdlib path, not this candidate's historical alias.
_mimo_v6_runner=$(
  {shlex.quote(RUNNER_PYTHON_CANDIDATE)} - <<'MIMO_V6_RUNNER'
import os
import sys
import sysconfig
from unittest import loader
loader_path = os.path.realpath(loader.__file__)
if (sys.version_info[:2] != (3, 8)
        or sysconfig.get_path("stdlib") != {RUNNER_STDLIB!r}
        or loader_path != {RUNNER_UNITTEST_LOADER!r}):
    raise SystemExit("{TRANSFORM_ID}: expected native /usr/local Python 3.8, got "
                     + sys.version + " stdlib=" + sysconfig.get_path("stdlib")
                     + " unittest_loader=" + loader_path)
print(os.path.realpath(sys.executable))
MIMO_V6_RUNNER
) || fail "{TRANSFORM_ID}: cannot resolve the native Python 3.8 runner"
echo "{TRANSFORM_ID}: runner_python=$_mimo_v6_runner"
# safe_load uses the pure-Python loader; the optional clib is not needed.
"$_mimo_v6_runner" -m pip --isolated --disable-pip-version-check install \\
  --no-input --no-cache-dir --no-index --no-deps --require-hashes --only-binary=:all: \\
  --retries 0 --requirement /dev/stdin <<'MIMO_V6_YAML_LOCK' || fail "{TRANSFORM_ID}: YAML install failed"
{YAML_LOCK}MIMO_V6_YAML_LOCK
unset _mimo_v6_runner

"""


def _parent_source(parent: Path, task_name: str, repo_root: Path) -> dict[str, str]:
    """Retain an existing record edge when available; otherwise name real bytes."""
    digest = task_directory_digest(parent)
    relative = (
        RECORDS_DIRNAME / task_name.replace("/", "__")
        / f"{digest.removeprefix('sha256:')[:12]}.json"
    )
    if not (repo_root / relative).exists():
        return {"kind": "local", "path": str(parent)}
    record = resolve_record(relative, repo_root=repo_root)
    if (
        record.task_name != task_name
        or record.variant_digest != digest
        or record.variant_harbor_digest != harbor_task_digest(parent)
    ):
        raise VariantInvalid(f"{TRANSFORM_ID}: parent record does not bind supplied package")
    return {"kind": "variant", "record": relative.as_posix()}


def build_changes(
    parent_dir: Path | str, *, task_id: str,
) -> tuple[dict[str, bytes], dict[str, Any]] | None:
    """Plan the actual setup changes and complete cache inputs without mutation."""
    if task_id != TASK_ID:
        return None
    parent = Path(parent_dir).resolve()
    parent_text = (parent / "task.toml").read_text(encoding="utf-8")
    config = tomllib.loads(parent_text)
    task_name = f"mimo-v2.6-rl/{task_id}"
    if config.get("task", {}).get("name") != task_name:
        raise VariantInvalid(f"{TRANSFORM_ID}: parent task identity does not match {task_id}")
    verifier = config.get("verifier", {})
    if (
        verifier.get("environment_mode") == "separate"
        or "environment" in verifier
        or (parent / "tests/Dockerfile").exists()
        or (parent / "tests/_verifier-setup").exists()
    ):
        raise VariantInvalid(
            f"{TRANSFORM_ID}: derive before separate-verifier so it bundles the repaired setup"
        )
    environment = config.get("environment", {})
    if environment.get("network_mode", "public") != "public":
        raise VariantInvalid(
            f"{TRANSFORM_ID}: existing startup policy does not permit the pinned PyPI wheel"
        )
    if verifier.get("network_mode", "public") != "public":
        raise VariantInvalid(
            f"{TRANSFORM_ID}: existing verifier policy does not permit the pinned PyPI wheel"
        )
    setup_path = parent / SETUP_REL
    setup = setup_path.read_text(encoding="utf-8")
    if TRANSFORM_ID in setup:
        raise VariantInvalid(f"{TRANSFORM_ID}: parent setup already carries this repair")
    if setup.count(ANCHOR) != 1:
        raise VariantInvalid(f"{TRANSFORM_ID}: expected one write_blocklist setup call")
    repaired = setup.replace(ANCHOR, "\n" + _repair_block() + "write_blocklist\n", 1)
    repaired_bytes = repaired.encode("utf-8")
    blob = pack_setup(setup_path.parent, setup_sh=repaired_bytes)
    changes = {
        SETUP_REL: repaired_bytes,
        "task.toml": render_task_toml(parent_text, new_blob=blob).encode("utf-8"),
    }
    inputs: dict[str, Any] = {
        "parent_task": task_name,
        "docker_image": environment.get("docker_image"),
        "workdir": environment.get("workdir"),
        "failure_log": FAILURE_LOG,
        "missing_import": "from ruamel import yaml",
        "historical_requirement": "ruamel.yaml>=0.11.11,<0.12.0",
        "dependency": {
            "name": "ruamel.yaml", "version": "0.16.13",
            "metadata_url": YAML_METADATA_URL,
            "wheel_url": YAML_WHEEL_URL,
            "wheel_sha256": f"sha256:{YAML_WHEEL_SHA256}",
            "requirements_lock": YAML_LOCK,
            "omitted_dependency": "ruamel.yaml.clib (unused by legacy pure-Python safe_load)",
        },
        "runner_selection": {
            "candidate": RUNNER_PYTHON_CANDIDATE,
            "expected_python": "3.8", "observed_stdlib": RUNNER_STDLIB,
            "observed_unittest_loader": RUNNER_UNITTEST_LOADER,
            "executable_observed_in_retained_log": False,
            "runtime_receipt": f"{TRANSFORM_ID}: runner_python=<resolved sys.executable>",
        },
        "installation_phase": "existing public clean setup before agent; replayed by verifier",
        "setup_before_sha256": "sha256:" + hashlib.sha256(setup.encode()).hexdigest(),
        "setup_after_sha256": "sha256:" + hashlib.sha256(repaired_bytes).hexdigest(),
    }
    return changes, inputs


def derive_environment_repair(
    parent_dir: Path | str,
    *,
    task_id: str,
    repo_root: Path | str,
    variants_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    created_by: str = "mimo-v6-full-fleet",
) -> VariantRecord | None:
    """Derive a real setup repair, or return None for an unaffected task.

    Derive before separation so the verifier bundles the repaired setup. The
    runtime-checked interpreter candidate and dependency install are not run
    during derivation. Standard builder caching supplies truthful parent_source.
    """
    planned = build_changes(parent_dir, task_id=task_id)
    if planned is None:
        return None
    parent = Path(parent_dir).resolve()
    root = Path(repo_root).resolve()
    changes, inputs = planned
    task_name = f"mimo-v2.6-rl/{task_id}"
    return derive_task(
        parent,
        changes=changes,
        transform=TRANSFORM_ID,
        rationale=(
            "Repair the retained native Python 3.8 ntfy import failure by installing "
            "the hash-locked ruamel.yaml 0.16.13 universal wheel into its runtime-checked "
            "existing /usr/local interpreter during clean setup. This version retains "
            "the legacy safe_load API and explicitly supports Python 3.8; historical "
            "0.11.x predates its wheels. Omit the unused C extension. Preserve the "
            "pinned upstream image, all resource/network declarations, project and "
            "test bytes, test commands and grading. Native controls remain unverified."
        ),
        created_by=created_by,
        inputs=inputs,
        parent_source=parent_source if parent_source is not None else _parent_source(parent, task_name, root),
        repo_root=root,
        variants_root=variants_root,
    )
