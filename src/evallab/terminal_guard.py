"""Close the terminal grading hook-planting hole (``terminal-guard-extend@1``).

The hole (proven by the investigator on candidate-0109, MEASURED): the
terminal agent runs as root, and the shipped ``anti_hack_guard.py`` scans only
``/app``. A root agent can plant ``sitecustomize.py`` plus a skip-all pytest
plugin in the system site-packages and set ``PYTEST_PLUGINS``;
``PYTHONNOUSERSITE=1`` does not stop system-site hooks, the guard stays
silent, pytest reports "7 skipped", and ``reward.txt`` flips 0 to 1.

The fix has a setup half and a grading half:

* Setup (``environment/setup/setup.sh``) snapshots the interpreter hook
  surface into ``/var/lib/mimo/terminal-guard-snapshot.json`` just before the
  ready sentinel: every ``sitecustomize.py`` / ``usercustomize.py`` / ``*.pth``
  file under the site directories with its sha256, plus every ``pytest11``
  entry point with its providing distribution, version and plugin module file
  hashes.
* Grading (``tests/task_test.sh`` + ``tests/anti_hack_guard.py``):
  ``task_test.sh`` unsets the hook-loading environment (``PYTEST_PLUGINS``,
  ``PYTHONPATH``, ``PYTHONSTARTUP``), runs pytest with
  ``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` plus explicit ``-p`` flags for exactly
  the setup-time plugins and ``--noconftest`` (no task ships
  ``/tests/conftest.py``; only that one file could load since the graders pin
  ``/tests/test_outputs.py``), and invokes the guard itself with ``python3
  -S`` so a planted startup hook cannot run inside the guard process. The
  extended guard quarantines any hook file absent from the snapshot, and fails
  closed (reward 0, like the existing guard) when a snapshotted hook or plugin
  file changed or vanished.

Static census over all 64 terminal graders (see
``research/experiments/terminal-task-ledger/README.md``): no grader needs a
third-party pytest plugin (the only ``pytest11`` entry point in the images is
``ctrf``, MEASURED n=12 cached images; marks are core-only; no asyncio use;
no ``-p`` plugin-enabling flag; no ``conftest.py``/ini under any ``tests/``
dir), and no ``test_outputs.py`` reads hook-loading env from the ambient
environment (1990 sets its own ``PYTHONPATH`` for a child probe explicitly,
unaffected by the scrub).

Residuals (documented, not oversights): an agent that deletes the snapshot
downgrades grading to quarantine-``sitecustomize``-only plus unpinned plugin
autoload (same downgrade class as deleting the upstream pristine manifest);
honest editable installs adding ``*.pth`` files are quarantined at grade time.

Also in this module: ``derive_terminal_prefetch`` for the
``env-prefetch-network@1`` precedent (HAR-146 shape) applied to the terminal
missing-module graders: 0260 installs ``stevedore`` (bandit's dependency),
2376 installs ``cryptography`` (authlib's JWE dependency). The prefetch block
runs before the guard snapshot when the two compose (derive the prefetch
first, then the guard on top of it).
"""

from __future__ import annotations

import hashlib
import inspect
import tomllib
from pathlib import Path
from typing import Any

from evallab.strip_future_history import pack_setup, render_task_toml
from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

#: Transform id recorded in lineage.
TRANSFORM_ID = "terminal-guard-extend@1"

#: Prefetch precedent id (HAR-146 shape), reused for the terminal missing modules.
PREFETCH_TRANSFORM_ID = "env-prefetch-network@1"

#: Marker comment identifying the inserted blocks (also the idempotence guard).
MARKER = "terminal-guard-extend@1"

#: Package-relative paths this transform rewrites.
SETUP_REL = "environment/setup/setup.sh"
GUARD_REL = "tests/anti_hack_guard.py"
TEST_REL = "tests/task_test.sh"

#: Ready sentinel; the setup snapshot runs just before it, at the end of setup.
SETUP_ANCHOR = 'touch "$M/ready"'

#: Universal anchors in every terminal tests/task_test.sh (verified 64/64).
TEST_ENV_ANCHOR = "export PYTHONDONTWRITEBYTECODE=1"
TEST_PYTEST_ANCHOR = "python3 -m pytest"
TEST_GUARD_CALL = "python3 /tests/anti_hack_guard.py"
TEST_GUARD_CALL_HARDENED = "python3 -S /tests/anti_hack_guard.py"

#: End of main() in the shipped guard; the extended check hooks in here.
GUARD_MAIN_ANCHOR = (
    "    reason = scan(manifest, app=app)\n"
    "    if reason:\n"
    "        return reject(reason, reward=reward)\n"
    "    return 0"
)

#: Entry-point footer; the new defs are inserted before it so they exist
#: when main() runs (appending after it would leave run_site_check
#: undefined at call time).
GUARD_TAIL_ANCHOR = 'if __name__ == "__main__":'

#: Snapshot file written by setup, read at grade time.
SNAPSHOT_ENV = "TERMINAL_GUARD_SNAPSHOT"
SNAPSHOT_DEFAULT = "/var/lib/mimo/terminal-guard-snapshot.json"
QUARANTINE_ENV = "TERMINAL_GUARD_QUARANTINE"
QUARANTINE_DEFAULT = "/logs/verifier/quarantine"

SNAPSHOT_SCHEMA = "terminal-guard-snapshot/v1"


def guard_site_dirs() -> list[str]:
    """Site directories to police, without importing the ``site`` module.

    Runs under ``python3 -S`` (no ``site`` import, so a planted
    ``sitecustomize`` cannot execute inside the guard/snapshot process).
    """
    import os
    import sys

    version = f"python{sys.version_info[0]}.{sys.version_info[1]}"
    found: list[str] = []
    for prefix in (sys.prefix, sys.exec_prefix):
        for layout in ("site-packages", "dist-packages"):
            candidate = os.path.join(prefix, "lib", version, layout)
            if os.path.isdir(candidate) and candidate not in found:
                found.append(candidate)
    return found


def collect_snapshot(site_dirs: list[str]) -> dict[str, Any]:
    """Snapshot hook files and pytest11 plugins under ``site_dirs``.

    Returns a JSON-serializable doc with absolute-path keys. Hook files are
    ``sitecustomize.py`` / ``usercustomize.py`` / ``*.pth`` with sha256.
    Plugins map entry-point name to value, providing dist name/version and
    the sha256 of the plugin module files the value resolves to.
    """
    import hashlib
    import os

    hooks: dict[str, str] = {}
    for site_dir in site_dirs:
        try:
            names = sorted(os.listdir(site_dir))
        except OSError:
            continue
        for name in names:
            path = os.path.join(site_dir, name)
            if not os.path.isfile(path):
                continue
            if name in ("sitecustomize.py", "usercustomize.py") or name.endswith(".pth"):
                with open(path, "rb") as handle:
                    hooks[path] = "sha256:" + hashlib.sha256(handle.read()).hexdigest()
    plugins: dict[str, dict[str, Any]] = {}
    for site_dir in site_dirs:
        try:
            names = sorted(os.listdir(site_dir))
        except OSError:
            continue
        for name in names:
            if not (name.endswith(".dist-info") or name.endswith(".egg-info")):
                continue
            dist_dir = os.path.join(site_dir, name)
            entry_points = _read_entry_points(dist_dir)
            if "pytest11" not in entry_points:
                continue
            version = _read_dist_version(dist_dir)
            for ep_name, value in sorted(entry_points["pytest11"].items()):
                files: dict[str, str] = {}
                for module_file in _plugin_module_files(site_dir, value):
                    with open(module_file, "rb") as handle:
                        files[module_file] = "sha256:" + hashlib.sha256(handle.read()).hexdigest()
                plugins[ep_name] = {
                    "value": value,
                    "dist": name,
                    "version": version,
                    "files": files,
                }
    return {"site_dirs": list(site_dirs), "hooks": hooks, "plugins": plugins}


def _read_entry_points(dist_dir: str) -> dict[str, dict[str, str]]:
    """Parse ``entry_points.txt`` of one installed distribution."""
    import os

    path = os.path.join(dist_dir, "entry_points.txt")
    groups: dict[str, dict[str, str]] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return groups
    group = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1].strip()
            groups.setdefault(group, {})
        elif "=" in line and group:
            ep_name, _, value = line.partition("=")
            ep_name = ep_name.strip()
            value = value.split(";")[0].strip()
            if ep_name and value:
                groups[group][ep_name] = value
    return groups


def _read_dist_version(dist_dir: str) -> str:
    """Read the installed version from METADATA / PKG-INFO."""
    import os

    for filename in ("METADATA", "PKG-INFO"):
        try:
            with open(os.path.join(dist_dir, filename), encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("Version:"):
                return line.partition(":")[2].strip()
    return ""


def _plugin_module_files(site_dir: str, value: str) -> list[str]:
    """Files an entry-point value resolves to (``mod`` or ``mod:attr``)."""
    import os

    dotted = value.split(":")[0].strip()
    if not dotted or not all(part.isidentifier() for part in dotted.split(".")):
        return []
    parts = dotted.split(".")
    candidates = [os.path.join(site_dir, *parts[:-1], parts[-1] + ".py")]
    candidates.append(os.path.join(site_dir, *parts, "__init__.py"))
    for depth in range(1, len(parts)):
        candidates.append(os.path.join(site_dir, *parts[:depth], "__init__.py"))
    seen: list[str] = []
    for candidate in candidates:
        if os.path.isfile(candidate) and candidate not in seen:
            seen.append(candidate)
    return seen


def check_snapshot(
    snapshot: dict[str, Any], site_dirs: list[str], quarantine_dir: str
) -> tuple[list[str], str | None]:
    """Enforce a snapshot: quarantine plants, fail closed on anchor tamper.

    Returns ``(quarantined, reason)``: absolute paths moved aside, and a
    reject reason (``None`` when grading may continue). Hook files absent
    from the snapshot are quarantined (moved, not deleted). Snapshotted hook
    or plugin files whose bytes changed or vanished are a reject. Newly
    appeared ``pytest11`` entry points are only logged:
    ``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` keeps them out of grading.
    """
    import hashlib
    import os
    import shutil

    quarantined: list[str] = []
    expected_hooks = dict(snapshot.get("hooks") or {})
    expected_plugins = dict(snapshot.get("plugins") or {})

    def _quarantine(path: str) -> None:
        os.makedirs(quarantine_dir, exist_ok=True)
        target = os.path.join(quarantine_dir, path.lstrip("/").replace("/", "__"))
        suffix = 0
        while os.path.exists(target if suffix == 0 else f"{target}.{suffix}"):
            suffix += 1
        if suffix:
            target = f"{target}.{suffix}"
        shutil.move(path, target)
        quarantined.append(path)

    current_hooks: set[str] = set()
    for site_dir in site_dirs:
        try:
            names = sorted(os.listdir(site_dir))
        except OSError:
            continue
        for name in names:
            path = os.path.join(site_dir, name)
            if not os.path.isfile(path):
                continue
            if name in ("sitecustomize.py", "usercustomize.py") or name.endswith(".pth"):
                current_hooks.add(path)
    for path in sorted(current_hooks):
        if path not in expected_hooks:
            _quarantine(path)
    for path in sorted(expected_hooks):
        if path in quarantined:
            continue
        if not os.path.isfile(path):
            return quarantined, f"protected_interpreter_hook_deleted:{path}"
        with open(path, "rb") as handle:
            digest = "sha256:" + hashlib.sha256(handle.read()).hexdigest()
        if digest != expected_hooks[path]:
            return quarantined, f"protected_interpreter_hook_mutated:{path}"
    current_plugins = collect_snapshot(site_dirs)["plugins"]
    for ep_name in sorted(current_plugins):
        if ep_name not in expected_plugins:
            os.makedirs(quarantine_dir, exist_ok=True)
            with open(os.path.join(quarantine_dir, "new_pytest_plugins.log"), "a", encoding="utf-8") as handle:
                handle.write(f"unpinned pytest11 entry point (autoload disabled): {ep_name}\n")
    for ep_name in sorted(expected_plugins):
        expected = expected_plugins[ep_name]
        current = current_plugins.get(ep_name)
        if current is None:
            return quarantined, f"protected_pytest_plugin_deleted:{ep_name}"
        if current.get("value") != expected.get("value"):
            return quarantined, f"protected_pytest_plugin_mutated:{ep_name}"
        expected_files = dict(expected.get("files") or {})
        current_files = dict(current.get("files") or {})
        for module_file in sorted(expected_files):
            if current_files.get(module_file) != expected_files[module_file]:
                return quarantined, f"protected_pytest_plugin_mutated:{ep_name}"
    return quarantined, None


def _embedded_snippet() -> str:
    """The stdlib-only sources embedded into setup.sh and the guard file.

    Container-side behaviour is exactly what the unit tests exercise, since
    the embedded text is produced from these functions.
    """
    parts = [
        "import hashlib",
        "import json",
        "import os",
        "import shutil",
        "import sys",
        "import sysconfig",
        "from typing import Any",
        "",
    ]
    for func in (
        guard_site_dirs,
        collect_snapshot,
        _read_entry_points,
        _read_dist_version,
        _plugin_module_files,
        check_snapshot,
    ):
        parts.append(inspect.getsource(func))
    return "\n".join(parts)


SNAPSHOT_WRITER = """\
_snapshot_out = sys.argv[1]
_snapshot_dirs = guard_site_dirs()
_snapshot_doc = collect_snapshot(_snapshot_dirs)
_snapshot_doc["schema"] = "terminal-guard-snapshot/v1"
with open(_snapshot_out, "w", encoding="utf-8") as _snapshot_handle:
    json.dump(_snapshot_doc, _snapshot_handle, indent=2, sort_keys=True)
    _snapshot_handle.write("\\n")
print(f"terminal-guard-extend@1: snapshotted {len(_snapshot_doc['hooks'])} hook(s), "
      f"{len(_snapshot_doc['plugins'])} pytest plugin(s) in {len(_snapshot_dirs)} site dir(s)")
"""

SNAPSHOT_BLOCK = """\
# terminal-guard-extend@1: snapshot interpreter/plugin hooks before the agent starts.
_terminal_guard_snapshot="$M/terminal-guard-snapshot.json"
python3 -S - "$_terminal_guard_snapshot" <<'TERMINAL_GUARD_SNAPSHOT_EOF' || fail "terminal-guard-extend@1 cannot snapshot interpreter hooks"
SNIPPET_PLACEHOLDER
SNAPSHOT_WRITER_PLACEHOLDER
TERMINAL_GUARD_SNAPSHOT_EOF
[ -s "$_terminal_guard_snapshot" ] || fail "terminal-guard-extend@1 snapshot is empty"
"""

GUARD_CHECK_GLUE = """\
TERMINAL_GUARD_SNAPSHOT_DEFAULT = "/var/lib/mimo/terminal-guard-snapshot.json"
TERMINAL_GUARD_QUARANTINE_DEFAULT = "/logs/verifier/quarantine"


def run_site_check(snapshot_path=None, quarantine_dir=None):
    snapshot_path = snapshot_path or os.environ.get("TERMINAL_GUARD_SNAPSHOT", TERMINAL_GUARD_SNAPSHOT_DEFAULT)
    quarantine_dir = quarantine_dir or os.environ.get("TERMINAL_GUARD_QUARANTINE", TERMINAL_GUARD_QUARANTINE_DEFAULT)
    try:
        with open(snapshot_path, encoding="utf-8") as handle:
            snapshot = json.load(handle)
    except OSError:
        print(f"terminal-guard-extend@1: no snapshot at {snapshot_path}; grading without hook pinning", file=sys.stderr)
        return None
    except ValueError as exc:
        return f"guard_snapshot_unreadable:{exc}"
    if not isinstance(snapshot, dict) or snapshot.get("schema") != "terminal-guard-snapshot/v1":
        return "guard_snapshot_unreadable:unexpected schema"
    try:
        os.makedirs(quarantine_dir, exist_ok=True)
    except OSError as exc:
        return f"guard_quarantine_unwritable:{exc}"
    quarantined, reason = check_snapshot(snapshot, guard_site_dirs(), quarantine_dir)
    for path in quarantined:
        print(f"terminal-guard-extend@1: quarantined planted interpreter hook: {path}", file=sys.stderr)
    return reason
"""

TEST_ENV_BLOCK = """\
# terminal-guard-extend@1: scrub hook-loading env; pin pytest plugins to the setup snapshot.
unset PYTEST_PLUGINS PYTHONPATH PYTHONSTARTUP
_terminal_guard_snapshot="${TERMINAL_GUARD_SNAPSHOT:-/var/lib/mimo/terminal-guard-snapshot.json}"
_terminal_guard_plugins=""
if [ -f "$_terminal_guard_snapshot" ]; then
  export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
  _terminal_guard_plugins=$(python3 -S - "$_terminal_guard_snapshot" <<'TERMINAL_GUARD_PLUGINS_EOF'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as _snap_handle:
    _snap = json.load(_snap_handle)
_opts = []
for _name in sorted(_snap.get("plugins", {})):
    _module = str(_snap["plugins"][_name].get("value", "")).split(":")[0].strip()
    if _module:
        _opts += ["-p", _module]
print(" ".join(_opts))
TERMINAL_GUARD_PLUGINS_EOF
) || { echo "terminal-guard-extend@1: cannot read plugin snapshot (testbed problem, not scored)" >&2; exit 1; }
else
  echo "terminal-guard-extend@1: no snapshot at $_terminal_guard_snapshot; grading without plugin pinning" >&2
fi
"""


def build_setup_sh(parent_setup_sh: str) -> str:
    """Parent ``setup.sh`` plus the snapshot block. Idempotent marker check first."""
    if MARKER in parent_setup_sh:
        return parent_setup_sh
    if parent_setup_sh.count(SETUP_ANCHOR) != 1:
        raise VariantInvalid("setup.sh has no unique ready sentinel; refusing to extend the guard")
    snippet = _embedded_snippet()
    block = SNAPSHOT_BLOCK.replace("SNIPPET_PLACEHOLDER", snippet.rstrip("\n")).replace(
        "SNAPSHOT_WRITER_PLACEHOLDER", SNAPSHOT_WRITER.rstrip("\n")
    )
    return parent_setup_sh.replace(SETUP_ANCHOR, block.rstrip("\n") + "\n" + SETUP_ANCHOR, 1)


def build_guard_py(parent_guard_py: str) -> str:
    """Parent ``anti_hack_guard.py`` plus the site-hook check. Idempotent marker check first."""
    if MARKER in parent_guard_py:
        return parent_guard_py
    if parent_guard_py.count(GUARD_MAIN_ANCHOR) != 1:
        raise VariantInvalid("anti_hack_guard.py has no unique main anchor; refusing to extend the guard")
    if parent_guard_py.count(GUARD_TAIL_ANCHOR) != 1:
        raise VariantInvalid("anti_hack_guard.py has no unique entry-point anchor; refusing to extend the guard")
    extended_main = GUARD_MAIN_ANCHOR.replace(
        "    return 0",
        "    extended = run_site_check()\n    if extended:\n        return reject(extended, reward=reward)\n    return 0",
        1,
    )
    updated = parent_guard_py.replace(GUARD_MAIN_ANCHOR, extended_main, 1)
    addition = (
        "# terminal-guard-extend@1: site-hook/plugin check (stdlib-only; runs under python3 -S).\n"
        + _embedded_snippet().rstrip("\n")
        + "\n"
        + GUARD_CHECK_GLUE.rstrip("\n")
        + "\n\n\n"
    )
    return updated.replace(GUARD_TAIL_ANCHOR, addition + GUARD_TAIL_ANCHOR, 1)


def build_task_test_sh(parent_test_sh: str) -> str:
    """Parent ``tests/task_test.sh`` plus env scrub, plugin pinning and ``-S`` guard call."""
    if MARKER in parent_test_sh:
        return parent_test_sh
    if parent_test_sh.count(TEST_ENV_ANCHOR) != 1:
        raise VariantInvalid("task_test.sh has no unique env anchor; refusing to extend the guard")
    if parent_test_sh.count(TEST_PYTEST_ANCHOR) != 1:
        raise VariantInvalid("task_test.sh has no unique pytest anchor; refusing to extend the guard")
    if parent_test_sh.count(TEST_GUARD_CALL) != 1:
        raise VariantInvalid("task_test.sh has no unique guard call; refusing to extend the guard")
    updated = parent_test_sh.replace(
        TEST_ENV_ANCHOR,
        TEST_ENV_ANCHOR + "\n" + TEST_ENV_BLOCK.rstrip("\n"),
        1,
    )
    updated = updated.replace(TEST_GUARD_CALL, TEST_GUARD_CALL_HARDENED, 1)
    updated = updated.replace(
        TEST_PYTEST_ANCHOR,
        TEST_PYTEST_ANCHOR + " $_terminal_guard_plugins --noconftest",
        1,
    )
    return updated


def build_setup_prefetch(parent_setup_sh: str, pip_spec: str) -> str:
    """Parent ``setup.sh`` plus a setup-time pip fetch (``env-prefetch-network@1``)."""
    if PREFETCH_TRANSFORM_ID in parent_setup_sh:
        return parent_setup_sh
    if parent_setup_sh.count(SETUP_ANCHOR) != 1:
        raise VariantInvalid("setup.sh has no unique ready sentinel; refusing to prefetch")
    block = (
        "# env-prefetch-network@1: grading imports this module; fetch while the network is open.\n"
        f'python3 -m pip install {pip_spec} > "$M/env-prefetch-network.log" 2>&1 '
        f'|| fail "env-prefetch-network@1 cannot install {pip_spec}"'
    )
    return parent_setup_sh.replace(SETUP_ANCHOR, block + "\n" + SETUP_ANCHOR, 1)


def _read_parent(parent_dir: Path) -> tuple[str, str, str, str, str, str]:
    try:
        task_toml = tomllib.loads((parent_dir / "task.toml").read_text(encoding="utf-8"))
        task_name = task_toml["task"]["name"]
        workdir = task_toml["environment"]["workdir"]
        image = task_toml["environment"]["docker_image"]
    except Exception as exc:
        raise VariantInvalid(f"parent task.toml is missing or invalid: {exc}") from exc
    try:
        setup_sh = (parent_dir / SETUP_REL).read_text(encoding="utf-8")
        guard_py = (parent_dir / GUARD_REL).read_text(encoding="utf-8")
        task_test_sh = (parent_dir / TEST_REL).read_text(encoding="utf-8")
    except OSError as exc:
        raise VariantInvalid(f"parent grading/setup file is missing: {exc}") from exc
    if not task_name or not workdir or not image:
        raise VariantInvalid("parent task.toml names no task, workdir or image")
    return task_name, workdir, image, setup_sh, guard_py, task_test_sh


def build_guard_changes(
    parent_dir: Path | str,
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``derive_task`` changes mapping plus lineage inputs.

    Only setup, the guard, ``task_test.sh`` and the re-embedded ``task.toml``
    healthcheck change; the hidden tests are byte-identical by construction.
    """
    parent = Path(parent_dir)
    task_name, workdir, image, setup_sh, guard_py, task_test_sh = _read_parent(parent)
    new_setup = build_setup_sh(setup_sh)
    new_guard = build_guard_py(guard_py)
    new_task_test = build_task_test_sh(task_test_sh)
    if new_setup == setup_sh:
        raise VariantInvalid("parent setup.sh already carries terminal-guard-extend@1")
    if new_guard == guard_py or new_task_test == task_test_sh:
        raise VariantInvalid("parent grading already carries terminal-guard-extend@1")
    new_blob = pack_setup(parent / "environment" / "setup", setup_sh=new_setup.encode("utf-8"))
    parent_toml = (parent / "task.toml").read_text(encoding="utf-8")
    changes: dict[str, bytes | None] = {
        SETUP_REL: new_setup.encode("utf-8"),
        GUARD_REL: new_guard.encode("utf-8"),
        TEST_REL: new_task_test.encode("utf-8"),
        "task.toml": render_task_toml(parent_toml, new_blob=new_blob).encode("utf-8"),
    }
    inputs: dict[str, Any] = {
        "parent_task": task_name,
        "workdir": workdir,
        "docker_image": image,
        "snapshot_path": SNAPSHOT_DEFAULT,
        "setup_before_sha256": f"sha256:{hashlib.sha256(setup_sh.encode()).hexdigest()}",
        "setup_after_sha256": f"sha256:{hashlib.sha256(new_setup.encode()).hexdigest()}",
        "guard_before_sha256": f"sha256:{hashlib.sha256(guard_py.encode()).hexdigest()}",
        "guard_after_sha256": f"sha256:{hashlib.sha256(new_guard.encode()).hexdigest()}",
    }
    return changes, inputs


def derive_terminal_guard(
    parent_dir: Path | str,
    *,
    rationale: str = (
        "Close the proven site-packages hook-planting hole (sitecustomize / "
        ".pth / pytest entry-point plugins / PYTEST_PLUGINS / PYTHONPATH "
        "outside /app flipped 0109 reward 0 to 1): snapshot the interpreter "
        "hook surface at end of setup, quarantine additions and fail closed "
        "on anchor tamper at grade time, scrub hook-loading env, pin pytest "
        "to the setup-time plugins with autoload off, and run the guard "
        "itself without site imports. No grader needs a third-party plugin, "
        "so honest grading is unchanged."
    ),
    created_by: str = "terminal-guard",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``terminal-guard-extend@1`` variant of a terminal task package.

    The parent may be the snapshot package or a prefetch variant of it; both
    keep the ready sentinel, so the snapshot block always lands after any
    setup-time fetch.
    """
    parent = Path(parent_dir)
    changes, inputs = build_guard_changes(parent)
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


def build_prefetch_changes(
    parent_dir: Path | str, pip_spec: str
) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    """Build the ``env-prefetch-network@1`` changes mapping plus lineage inputs."""
    parent = Path(parent_dir)
    try:
        task_toml = tomllib.loads((parent / "task.toml").read_text(encoding="utf-8"))
        task_name = task_toml["task"]["name"]
        workdir = task_toml["environment"]["workdir"]
        image = task_toml["environment"]["docker_image"]
    except Exception as exc:
        raise VariantInvalid(f"parent task.toml is missing or invalid: {exc}") from exc
    try:
        setup_sh = (parent / SETUP_REL).read_text(encoding="utf-8")
    except OSError as exc:
        raise VariantInvalid(f"parent {SETUP_REL} is missing: {exc}") from exc
    new_setup = build_setup_prefetch(setup_sh, pip_spec)
    if new_setup == setup_sh:
        raise VariantInvalid("parent setup.sh already carries env-prefetch-network@1")
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
        "pip_spec": pip_spec,
        "setup_before_sha256": f"sha256:{hashlib.sha256(setup_sh.encode()).hexdigest()}",
        "setup_after_sha256": f"sha256:{hashlib.sha256(new_setup.encode()).hexdigest()}",
    }
    return changes, inputs


def derive_terminal_prefetch(
    parent_dir: Path | str,
    pip_spec: str,
    *,
    rationale: str = "",
    created_by: str = "terminal-prefetch",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive an ``env-prefetch-network@1`` variant fetching ``pip_spec`` at setup."""
    parent = Path(parent_dir)
    changes, inputs = build_prefetch_changes(parent, pip_spec)
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
        transform=PREFETCH_TRANSFORM_ID,
        rationale=rationale
        or (
            f"Grading imports a module missing from the image; install {pip_spec} "
            "at setup while the network is open."
        ),
        created_by=created_by,
        inputs=inputs,
        **kwargs,
    )


__all__ = [
    "GUARD_MAIN_ANCHOR",
    "GUARD_REL",
    "GUARD_TAIL_ANCHOR",
    "MARKER",
    "PREFETCH_TRANSFORM_ID",
    "QUARANTINE_DEFAULT",
    "QUARANTINE_ENV",
    "SETUP_REL",
    "SNAPSHOT_DEFAULT",
    "SNAPSHOT_ENV",
    "SNAPSHOT_SCHEMA",
    "TEST_ENV_ANCHOR",
    "TEST_ENV_BLOCK",
    "TEST_GUARD_CALL",
    "TEST_GUARD_CALL_HARDENED",
    "TEST_PYTEST_ANCHOR",
    "TEST_REL",
    "TRANSFORM_ID",
    "_plugin_module_files",
    "_read_dist_version",
    "_read_entry_points",
    "build_guard_changes",
    "build_guard_py",
    "build_prefetch_changes",
    "build_setup_prefetch",
    "build_setup_sh",
    "build_task_test_sh",
    "check_snapshot",
    "collect_snapshot",
    "derive_terminal_guard",
    "derive_terminal_prefetch",
    "guard_site_dirs",
]
