import ast
import base64
import hashlib
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

LAB = Path.home() / "Developer" / "bespoke-macbench-lab"
RESULTS = Path.home() / "Developer" / "eval-lab-results" / "multiagent-campaign-2026-11-23"
LIBRARY = Path.home() / "Developer" / "research-context"
EVAL_LAB = Path.home() / "Developer" / "eval-lab"
REPO = Path(__file__).resolve().parents[4]
DEFAULT_BUILD = Path.home() / "Developer" / "muster-runs" / "day-2026-11-24" / "mac-run" / "build"
UPSTREAM_COMMIT = "dbec43ff757c86c17077e61b37c7f584c880bdee"
UPSTREAM_TASK = "upstream/tasks/holdout/task"
VARIANTS = ("team", "silent")
MANIFEST = "manifest.json"
ORCHESTRATION = "orchestrations/holdout_keepalive_fixed_horizon.py"
SCHEMA = "mac-matched-build/v1"
MAX_FILES = 4000
MAX_FILE_BYTES = 64 * 1024 * 1024

KEEPALIVE = "environment/hub/keepalive.py"
KEEPALIVE_SHA256 = "2cc55b3651aa8ea91982d4282234be58807a3ce343b9a6fb93825409ece229ec"
KEEPALIVE_REMOVED = (
    (271, "        elif not team.running and self.idle_since and (self._blocked(team) or "
          "(FINISH_IS_FINAL and not self._crashed(team)\n"),
    (272, "                                                                                "
          "and not self._coming_back(team))):\n"),
    (273, '            await self._freeze(team, "run_end")\n'),
)
KEEPALIVE_KEPT = ((269, "        if self._left(team) <= FREEZE_LEFT_SEC:\n"),
                  (270, '            await self._freeze(team, "time_up")\n'),
                  (274, "        ended = False\n"))

MANAGED = "environment/claude-managed.json"
MANAGED_SHA256 = "3e72f65e28e96cef71d6f0858c293f6713dc29a8df24fd902468dc1883c858e6"
MANAGED_LINE = 4
MANAGED_OLD = '    "deny": ["Agent", "Task", "Workflow", "Skill", "RemoteTrigger", "Bash(claude *)"]\n'
MANAGED_NEW = ('    "deny": ["Agent", "Task", "Workflow", "Skill", "RemoteTrigger", "Bash(claude *)", '
               '"WebSearch", "WebFetch"]\n')

CONFIG = "tests/hidden/holdout/config.py"
CONFIG_SHA256 = "c0b2cab079c7354a9515b6c196564075a42499eaf0a13aa9695692f51a52c5b8"
COMMS_ON = '"comms": True,'
COMMS_OFF = '"comms": False,'
HUB_PLUGIN = "build/holdout_hub_plugin.py"
HUB_BUNDLE = "solution/hub_bundle.tar.gz"
MAKE_HUB_PLUGIN = "environment/hub/make_hub_plugin.py"
BUNDLE_ROOT = "opt/holdout/"
BUILD_ONLY = frozenset({HUB_PLUGIN, HUB_BUNDLE})
RUNTIME_FILES = ("run_hardened.py", "run_study4_trial.py", "agents/scripted_team_agent.py",
                 "agents/scripted_seat.py", "upstream/harness/pyproject.toml")
RUNTIME_TREES = ("upstream/harness/src/multi_agent_harness",)


def sha256_bytes(data: bytes) -> str:
    assert isinstance(data, bytes)
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    assert path.is_file(), path
    assert path.stat().st_size <= MAX_FILE_BYTES, path
    return sha256_bytes(path.read_bytes())


def tree_files(root: Path) -> list[str]:
    assert root.is_dir(), root
    found: list[str] = []
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(names):
            if name.endswith(".pyc"):
                continue
            found.append(Path(directory, name).relative_to(root).as_posix())
            assert len(found) <= MAX_FILES, f"more than {MAX_FILES} files under {root}"
    return sorted(found)


def inside(child: Path, parent: Path) -> bool:
    child_real, parent_real = Path(os.path.realpath(child)), Path(os.path.realpath(parent))
    return child_real == parent_real or parent_real in child_real.parents


def refuse_foreign(path: Path, what: str, lab: Path = LAB) -> None:
    for root, owner in ((LAB, "the Bespoke lab"), (lab, "the Bespoke lab"), (RESULTS, "another lane's results"),
                        (LIBRARY, "the research library"), (EVAL_LAB, "the Eval Lab repository"),
                        (REPO, "the Eval Lab repository")):
        assert not inside(path, root), f"{what} {path} is inside {owner} {root}"


def upstream_state(lab: Path) -> dict:
    upstream = lab / "upstream"
    head = subprocess.run(["git", "-C", str(upstream), "--no-optional-locks", "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True, timeout=60).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(upstream), "--no-optional-locks", "status", "--porcelain",
                            "--untracked-files=no"], capture_output=True, text=True, check=True,
                           timeout=60).stdout.strip()
    assert head == UPSTREAM_COMMIT, f"upstream HEAD {head} is not the pinned {UPSTREAM_COMMIT}"
    assert dirty == "", f"upstream tracked files changed: {dirty[:300]}"
    return {"commit": head, "tracked_clean": True}


def plugin_template(lab: Path) -> str:
    source = (lab / UPSTREAM_TASK / MAKE_HUB_PLUGIN).read_text()
    found = [node.value.value for node in ast.parse(source).body
             if isinstance(node, ast.Assign) and len(node.targets) == 1
             and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "PLUGIN"
             and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
    assert len(found) == 1, "make_hub_plugin.py PLUGIN template not found"
    assert found[0].count("{bundle}") == 1
    return found[0]


def check_bundle(task: Path, template: str) -> dict:
    bundle = (task / HUB_BUNDLE).read_bytes()
    plugin = (task / HUB_PLUGIN).read_text()
    assert plugin == template.replace("{bundle}", base64.b64encode(bundle).decode()), \
        f"{task / HUB_PLUGIN} is not make_hub_plugin.py's template around {HUB_BUNDLE}"
    hidden = task / "tests" / "hidden"
    expected = {BUNDLE_ROOT + rel: (hidden / rel).read_bytes() for rel in tree_files(hidden)}
    seen: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as archive:
        members = archive.getmembers()
        assert len(members) <= MAX_FILES
        for member in members:
            assert member.isfile(), f"bundle member {member.name} is not a regular file"
            handle = archive.extractfile(member)
            assert handle is not None
            seen[member.name] = handle.read()
    assert seen == expected, f"{task / HUB_BUNDLE} differs from {hidden}"
    return {"bundle_sha256": sha256_bytes(bundle), "bundle_files": len(seen),
            "plugin_sha256": sha256_bytes(plugin.encode())}


def check_parity(lab: Path, variant: str, template: str) -> dict:
    assert variant in VARIANTS
    upstream = lab / UPSTREAM_TASK
    task = lab / "build" / f"holdout-{variant}" / "task"
    up_files, build_files = tree_files(upstream), tree_files(task)
    assert set(build_files) == set(up_files) | BUILD_ONLY, \
        f"holdout-{variant} file set differs from upstream: {sorted(set(build_files) ^ set(up_files))}"
    assert sha256_file(upstream / CONFIG) == CONFIG_SHA256, "pinned holdout config.py changed"
    differs = []
    for rel in up_files:
        if (upstream / rel).read_bytes() != (task / rel).read_bytes():
            differs.append(rel)
    config = (upstream / CONFIG).read_text()
    assert config.count(COMMS_ON) == 1
    if variant == "silent":
        assert differs == [CONFIG], f"holdout-silent differs from upstream in {differs}"
        assert (task / CONFIG).read_text() == config.replace(COMMS_ON, COMMS_OFF)
    else:
        assert differs == [], f"holdout-team differs from upstream in {differs}"
    return {"variant": variant, "files": len(build_files), "differs_from_upstream": differs,
            "build_only": sorted(BUILD_ONLY), "comms": variant == "team",
            **check_bundle(task, template)}


def patched_keepalive(source: bytes) -> bytes:
    assert sha256_bytes(source) == KEEPALIVE_SHA256, "pinned keepalive.py changed"
    lines = source.decode().splitlines(keepends=True)
    for number, text in KEEPALIVE_REMOVED + KEEPALIVE_KEPT:
        assert lines[number - 1] == text, f"keepalive.py line {number} is not the pinned text"
    removed = {number for number, _ in KEEPALIVE_REMOVED}
    out = "".join(line for index, line in enumerate(lines, 1) if index not in removed)
    assert out.count('"run_end"') == 0 and out.count('"time_up"') == 2
    assert len(out.splitlines()) == len(lines) - len(KEEPALIVE_REMOVED)
    return out.encode()


def patched_managed(source: bytes) -> bytes:
    assert sha256_bytes(source) == MANAGED_SHA256, "pinned claude-managed.json changed"
    lines = source.decode().splitlines(keepends=True)
    assert lines[MANAGED_LINE - 1] == MANAGED_OLD
    lines[MANAGED_LINE - 1] = MANAGED_NEW
    out = "".join(lines)
    deny = json.loads(out)["permissions"]["deny"]
    assert deny[-2:] == ["WebSearch", "WebFetch"] and len(deny) == 8
    return out.encode()


def planned_outputs(lab: Path) -> tuple[dict[str, tuple[bytes, int]], list[dict]]:
    outputs: dict[str, tuple[bytes, int]] = {}
    patches: list[dict] = []
    for variant in VARIANTS:
        task = lab / "build" / f"holdout-{variant}" / "task"
        for rel in tree_files(task):
            data = (task / rel).read_bytes()
            if rel == MANAGED:
                patches.append({"source": f"build/holdout-{variant}/task/{rel}",
                                "source_sha256": sha256_bytes(data),
                                "output": f"holdout-{variant}/task/{rel}", "line": MANAGED_LINE,
                                "old": MANAGED_OLD.rstrip("\n"), "new": MANAGED_NEW.rstrip("\n")})
                data = patched_managed(data)
            outputs[f"holdout-{variant}/task/{rel}"] = (data, (task / rel).stat().st_mode & 0o755)
    source = (lab / "build" / "holdout-team" / "task" / KEEPALIVE).read_bytes()
    for variant in VARIANTS:
        assert (lab / "build" / f"holdout-{variant}" / "task" / KEEPALIVE).read_bytes() == source
    outputs[ORCHESTRATION] = (patched_keepalive(source), 0o644)
    patches.append({"source": f"build/holdout-team/task/{KEEPALIVE}", "source_sha256": KEEPALIVE_SHA256,
                    "output": ORCHESTRATION,
                    "removed_lines": [{"line": n, "text": t.rstrip("\n")} for n, t in KEEPALIVE_REMOVED]})
    assert len(outputs) <= MAX_FILES
    return outputs, patches


def source_hashes(lab: Path) -> dict[str, str]:
    rels = [f"{UPSTREAM_TASK}/{rel}" for rel in tree_files(lab / UPSTREAM_TASK)]
    for variant in VARIANTS:
        rels += [f"build/holdout-{variant}/task/{rel}"
                 for rel in tree_files(lab / "build" / f"holdout-{variant}" / "task")]
    rels += list(RUNTIME_FILES)
    for tree in RUNTIME_TREES:
        rels += [f"{tree}/{rel}" for rel in tree_files(lab / tree)]
    assert len(rels) <= MAX_FILES
    return {rel: sha256_file(lab / rel) for rel in sorted(set(rels))}


def runtime_hashes(sources: dict[str, str]) -> dict[str, str]:
    keep = {rel: digest for rel, digest in sources.items()
            if rel in RUNTIME_FILES or any(rel.startswith(tree + "/") for tree in RUNTIME_TREES)}
    assert "run_hardened.py" in keep and "agents/scripted_team_agent.py" in keep
    return keep


def raw_tree(root: Path) -> tuple[list[str], list[str]]:
    files: list[str] = []
    dirs: list[str] = []
    for directory, subdirs, names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in sorted(subdirs):
            assert not (base / name).is_symlink(), f"{base / name} is a symlink"
            dirs.append((base / name).relative_to(root).as_posix())
        for name in sorted(names):
            assert not (base / name).is_symlink(), f"{base / name} is a symlink"
            files.append((base / name).relative_to(root).as_posix())
        assert len(files) + len(dirs) <= 2 * MAX_FILES, f"too many entries under {root}"
    return files, dirs


def clear_build(build: Path, lab: Path) -> None:
    refuse_foreign(build, "build dir", lab)
    if not build.exists():
        build.mkdir(parents=True)
        return
    assert build.is_dir() and not build.is_symlink(), build
    files, dirs = raw_tree(build)
    if not files and not dirs:
        return
    try:
        manifest = json.loads((build / MANIFEST).read_text())
    except (OSError, ValueError):
        raise AssertionError(f"{build} is not empty and has no readable {MANIFEST}; refusing to touch it") from None
    assert isinstance(manifest, dict) and manifest.get("schema") == SCHEMA, f"{build / MANIFEST} is not ours"
    owned = set(manifest.get("outputs") or {}) | {MANIFEST}
    owned_dirs = {p.as_posix() for rel in owned for p in Path(rel).parents if p.as_posix() != "."}
    foreign = sorted(set(files) - owned) + sorted(set(dirs) - owned_dirs)
    assert not foreign, f"{build} holds files the manifest does not list: {foreign[:10]}"
    for rel in files:
        (build / rel).unlink()
    for rel in sorted(dirs, key=lambda d: d.count("/"), reverse=True):
        (build / rel).rmdir()
    assert not any(build.iterdir())


def write_outputs(build: Path, outputs: dict[str, tuple[bytes, int]]) -> None:
    for rel in sorted(outputs):
        data, mode = outputs[rel]
        target = build / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.chmod(target, mode)
    assert sorted(tree_files(build)) == sorted(outputs)


def prepare(lab: Path, build: Path) -> dict:
    assert lab.is_dir(), lab
    upstream = upstream_state(lab)
    template = plugin_template(lab)
    parity = [check_parity(lab, variant, template) for variant in VARIANTS]
    sources = source_hashes(lab)
    outputs, patches = planned_outputs(lab)
    clear_build(build, lab)
    write_outputs(build, outputs)
    manifest = {"schema": SCHEMA, "lab": str(lab), "upstream": upstream, "parity": parity,
                "patches": patches, "orchestration": ORCHESTRATION,
                "runtime": runtime_hashes(sources), "sources": sources,
                "outputs": {rel: sha256_bytes(data) for rel, (data, _) in sorted(outputs.items())}}
    blob = (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode()
    (build / MANIFEST).write_bytes(blob)
    return {"build": str(build), "manifest_sha256": sha256_bytes(blob), "outputs": len(outputs),
            "sources": len(sources), "patches": len(patches)}


def verify_build(build: Path, lab: Path) -> dict:
    path = build / MANIFEST
    assert path.is_file(), f"no {path}: run `matched_trial.py prepare --build-dir {build}` first"
    manifest = json.loads(path.read_text())
    assert manifest.get("schema") == SCHEMA, "unknown build manifest schema"
    assert manifest.get("lab") == str(lab), f"build was prepared from {manifest.get('lab')}, not {lab}"
    outputs = manifest["outputs"]
    present = tree_files(build)
    assert sorted(present) == sorted([*outputs, MANIFEST]), \
        f"build dir file set differs from its manifest: {sorted(set(present) ^ set([*outputs, MANIFEST]))}"
    bad = [rel for rel, digest in sorted(outputs.items()) if sha256_file(build / rel) != digest]
    assert not bad, f"build outputs changed since prepare: {bad[:10]}"
    runtime = manifest["runtime"]
    moved = [rel for rel, digest in sorted(runtime.items()) if sha256_file(lab / rel) != digest]
    assert not moved, f"Bespoke runtime files changed since prepare: {moved[:10]}"
    return {"manifest_sha256": sha256_file(path), "outputs": len(outputs), "runtime": len(runtime)}
