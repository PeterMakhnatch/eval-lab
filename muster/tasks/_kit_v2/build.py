from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.dont_write_bytecode = True

KIT = Path(__file__).resolve().parent
SEAT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
RESERVED_SEATS = frozenset({"main", "world", "tests", "solution"})
FORBIDDEN_WORKSPACE_HEADS = frozenset({"tests", "solution", "world", "__pycache__"})
RESERVED_WORLD_FILES = frozenset({"codec.py", "worldkit.py"})
MAX_SCAN_ENTRIES = 500_000

DOCKERIGNORE = "**/__pycache__\n*.pyc\n*.pyo\n.DS_Store\n"

MAIN_DOCKERFILE = """FROM python:3.13-slim

COPY world/ /world/
"""

SEAT_DOCKERFILE = """FROM python:3.13-slim

RUN apt-get update \\
    && apt-get install -y --no-install-recommends bash ca-certificates curl git jq tmux \\
    && rm -rf /var/lib/apt/lists/* \\
    && useradd --create-home --shell /bin/bash agent
"""

TEST_SH = """#!/bin/bash
python3 /tests/verify.py
"""


def seat_dockerfile(has_workspace_files: bool = True) -> str:
    base = (
        "FROM python:3.13-slim\n\n"
        "RUN apt-get update \\\n"
        "    && apt-get install -y --no-install-recommends bash ca-certificates curl git jq tmux \\\n"
        "    && rm -rf /var/lib/apt/lists/* \\\n"
        "    && useradd --create-home --shell /bin/bash agent \\\n"
        "    && mkdir -p /home/agent/workspace \\\n"
        "    && chown -R agent:agent /home/agent/workspace\n"
    )
    if not has_workspace_files:
        return base
    return (
        base
        + "\nCOPY --chown=agent:agent workspace/ /home/agent/workspace/\n"
        + "RUN chown -R agent:agent /home/agent/workspace\n"
    )


def _validate_seat_names(seats: list[str]) -> None:
    if not seats:
        raise ValueError("at least one seat is required")
    for seat in seats:
        if not isinstance(seat, str) or not SEAT_NAME_RE.match(seat) or seat.lower() in RESERVED_SEATS:
            raise ValueError(f"invalid or reserved seat name: {seat!r}")
    if len({s.lower() for s in seats}) != len(seats):
        raise ValueError("duplicate or case-colliding seat names")


def _validate_rel_workspace_path(rel_path: str) -> str:
    if not isinstance(rel_path, str) or not rel_path or "\\" in rel_path or rel_path.startswith("/"):
        raise ValueError(f"invalid workspace path: {rel_path!r}")
    parts = rel_path.split("/")
    if any(not p or p in (".", "..") for p in parts):
        raise ValueError(f"unsafe workspace path segments: {rel_path!r}")
    if parts[0].lower() in FORBIDDEN_WORKSPACE_HEADS or any(p.lower() == "__pycache__" for p in parts):
        raise ValueError(f"forbidden workspace path segment: {rel_path!r}")
    if rel_path.lower().endswith((".pyc", ".pyo")):
        raise ValueError(f"bytecode forbidden in workspace: {rel_path!r}")
    return "/".join(parts)


def _validate_emit_file_path(rel_path: str) -> str:
    if not isinstance(rel_path, str) or not rel_path or "\\" in rel_path or rel_path.startswith("/"):
        raise ValueError(f"invalid task file path: {rel_path!r}")
    parts = rel_path.split("/")
    if any(not p or p in (".", "..") or p.lower() == "__pycache__" for p in parts):
        raise ValueError(f"unsafe task file path: {rel_path!r}")
    if rel_path.lower().endswith((".pyc", ".pyo")):
        raise ValueError(f"bytecode forbidden in task files: {rel_path!r}")
    return "/".join(parts)


def _validate_world_env(world_env: dict[str, str]) -> None:
    for key in world_env:
        if not isinstance(key, str) or key.upper() == "SEAT_TOKENS" or not ENV_KEY_RE.match(key):
            raise ValueError(f"invalid or reserved world_env key: {key!r}")


def token(seed: str, instance: int, seat: str) -> str:
    assert seed and isinstance(instance, int) and not isinstance(instance, bool)
    return hashlib.sha256(f"{seed}/{instance}/{seat}".encode()).hexdigest()[:32]


def compose(
    seats: list[str],
    tokens: dict[str, str],
    world_env: dict[str, str],
    internet: bool = False,
    *,
    per_seat_build: bool = False,
) -> str:
    _validate_seat_names(seats)
    _validate_world_env(world_env)
    seat_tokens = json.dumps({tokens[seat]: seat for seat in seats}, sort_keys=True)
    lines = ["x-seat: &seat"]
    if not per_seat_build:
        lines.append("  build: ./seat")
    lines += [
        "  user: agent",
        "  working_dir: /home/agent",
        '  command: ["sleep", "infinity"]',
        "  init: true",
        "  depends_on:",
        "    main:",
        "      condition: service_healthy",
        "",
        "services:",
        "  main:",
        '    command: ["python3", "/world/world.py"]',
        "    environment:",
        f"      SEAT_TOKENS: {json.dumps(seat_tokens)}",
    ]
    for key in sorted(world_env):
        lines.append(f"      {key}: {json.dumps(str(world_env[key]))}")
    lines.append("    networks:")
    for seat in seats:
        lines += [f"      {seat}-world:", "        aliases: [world]"]
    lines += [
        "    healthcheck:",
        '      test: ["CMD", "python3", "-c", "import urllib.request; '
        "urllib.request.urlopen('http://localhost:8080/health', timeout=2)\"]",
        "      interval: 2s",
        "      timeout: 5s",
        "      retries: 15",
        "      start_period: 2s",
    ]
    for seat in seats:
        lines += [f"  {seat}:", "    <<: *seat"]
        if per_seat_build:
            lines.append(f"    build: ./seat_{seat}")
        lines += [
            f"    networks: [{seat}-world]",
            "    environment:",
            f"      SEAT: {json.dumps(seat)}",
            f"      SEAT_TOKEN: {json.dumps(tokens[seat])}",
            '      WORLD: "http://world:8080"',
        ]
    lines += ["", "networks:"]
    for seat in seats:
        lines.append(f"  {seat}-world:")
        if internet:
            lines.append("    internal: false")
        else:
            lines += ["    internal: true", "    enable_ipv4: false", "    enable_ipv6: true"]
    lines.append("")
    return "\n".join(lines)


def _toml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    raise TypeError(f"no TOML form for {value!r}")


def _toml_value(value: object) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    return _toml_scalar(value)


def task_toml(
    *,
    name: str,
    description: str,
    seats: dict[str, bool],
    metadata: dict[str, object],
    agent_timeout_sec: float = 600.0,
    verifier_timeout_sec: float = 120.0,
) -> str:
    _validate_seat_names(list(seats))
    lines = [
        'schema_version = "1.4"',
        'artifacts = ["/logs/world/events.jsonl"]',
        "",
        "[task]",
        f"name = {json.dumps(name)}",
        f"description = {json.dumps(description)}",
        'keywords = ["seats", "multi-agent", "muster"]',
        "",
        "[metadata]",
    ]
    lines += [f"{key} = {_toml_value(metadata[key])}" for key in metadata]
    lines += [
        "",
        "[verifier]",
        f"timeout_sec = {float(verifier_timeout_sec)!r}",
        "",
        "[agent]",
        f"timeout_sec = {float(agent_timeout_sec)!r}",
        "",
        "[environment]",
        "build_timeout_sec = 600.0",
        "",
        "[environment.env]",
        'COMPOSE_BAKE = "false"',
    ]
    for seat, under_test in seats.items():
        lines += ["", f"[agents.{seat}]"]
        if not under_test:
            lines.append("under_test = false")
    return "\n".join(lines) + "\n"


def _populate_seat_images(
    files: dict[str, str],
    seat_list: list[str],
    seat_files: dict[str, dict[str, str]] | None,
) -> None:
    if seat_files is None:
        files["environment/seat/Dockerfile"] = SEAT_DOCKERFILE
        files["environment/seat/.dockerignore"] = DOCKERIGNORE
        return
    unknown = set(seat_files) - set(seat_list)
    if unknown:
        raise ValueError(f"seat_files names unknown seats: {sorted(unknown)}")
    for seat in seat_list:
        ws_entries = seat_files.get(seat, {})
        if not isinstance(ws_entries, dict):
            raise ValueError(f"seat_files[{seat!r}] must be a dict")
        files[f"environment/seat_{seat}/Dockerfile"] = seat_dockerfile(has_workspace_files=bool(ws_entries))
        files[f"environment/seat_{seat}/.dockerignore"] = DOCKERIGNORE
        for raw_rel, content in sorted(ws_entries.items()):
            clean_rel = _validate_rel_workspace_path(raw_rel)
            if not isinstance(content, str):
                raise ValueError(f"workspace file content must be str: {seat}/{clean_rel}")
            files[f"environment/seat_{seat}/workspace/{clean_rel}"] = content


def _validate_policies_and_world(policies: dict[str, str], world: dict[str, str]) -> None:
    if "oracle" not in policies:
        raise ValueError("every task needs the oracle policy")
    for policy, text in policies.items():
        if not isinstance(policy, str) or not SEAT_NAME_RE.match(policy):
            raise ValueError(f"invalid policy name: {policy!r}")
        if not isinstance(text, str):
            raise ValueError(f"policy script must be str: {policy!r}")
    if len({p.lower() for p in policies}) != len(policies):
        raise ValueError("duplicate or case-colliding policy names")
    for path, text in world.items():
        clean_w = _validate_rel_workspace_path(path)
        if clean_w.lower() in RESERVED_WORLD_FILES or Path(clean_w).name.lower() in RESERVED_WORLD_FILES:
            raise ValueError(f"world cannot override kit module: {path!r}")
        if not isinstance(text, str):
            raise ValueError(f"world file content must be str: {clean_w!r}")


def task_files(
    *,
    name: str,
    description: str,
    seats: dict[str, bool],
    tokens: dict[str, str],
    instructions: dict[str, str],
    world: dict[str, str],
    verify: str,
    policies: dict[str, str],
    metadata: dict[str, object],
    world_env: dict[str, str] | None = None,
    seat_files: dict[str, dict[str, str]] | None = None,
    agent_timeout_sec: float = 600.0,
    verifier_timeout_sec: float = 120.0,
    internet: bool = False,
) -> dict[str, str]:
    seat_list = list(seats)
    _validate_seat_names(seat_list)
    _validate_policies_and_world(policies, world)
    _validate_world_env(world_env or {})
    if set(instructions) != set(seats) or set(tokens) != set(seats):
        raise ValueError("instructions and tokens must name exactly the seats")
    files: dict[str, str] = {
        "task.toml": task_toml(
            name=name, description=description, seats=seats, metadata=metadata,
            agent_timeout_sec=agent_timeout_sec, verifier_timeout_sec=verifier_timeout_sec,
        ),
        "environment/docker-compose.yaml": compose(
            seat_list, tokens, world_env or {}, internet, per_seat_build=seat_files is not None,
        ),
        "environment/Dockerfile": MAIN_DOCKERFILE,
        "environment/.dockerignore": DOCKERIGNORE,
        "environment/world/codec.py": (KIT / "codec.py").read_text(encoding="utf-8"),
        "environment/world/worldkit.py": (KIT / "worldkit.py").read_text(encoding="utf-8"),
        "tests/test.sh": TEST_SH,
        "tests/verify.py": verify,
        "solution/solve.sh": policies["oracle"],
    }
    _populate_seat_images(files, seat_list, seat_files)
    for path, text in world.items():
        files[f"environment/world/{_validate_rel_workspace_path(path)}"] = text
    for seat, text in instructions.items():
        files[f"instructions/{seat}.md"] = text if text.endswith("\n") else text + "\n"
    for policy, text in policies.items():
        files[f"policies/{policy}.sh"] = text
    return files


def _scan_tree(base: Path) -> tuple[dict[str, Path], list[tuple[str, Path]]]:
    regular: dict[str, Path] = {}
    anomalies: list[tuple[str, Path]] = []
    if os.path.islink(base):
        return regular, [("symlink", base)]
    if not base.exists():
        return regular, anomalies
    stack: list[Path] = [base]
    visited = 0
    while stack:
        cur = stack.pop()
        for entry in sorted(os.scandir(cur), key=lambda e: e.name):
            visited += 1
            assert visited <= MAX_SCAN_ENTRIES
            p, low = Path(entry.path), entry.name.lower()
            if entry.is_symlink():
                anomalies.append(("symlink", p))
                continue
            if low == "__pycache__" or low.endswith((".pyc", ".pyo")):
                anomalies.append(("bytecode", p))
                if entry.is_dir(follow_symlinks=False):
                    stack.append(p)
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(p)
            elif entry.is_file(follow_symlinks=False) and entry.name != ".DS_Store":
                regular[p.relative_to(base).as_posix()] = p
    return regular, sorted(anomalies, key=lambda x: str(x[1]))


def purge_bytecode(root: Path) -> int:
    base = Path(root)
    if os.path.islink(base) or not base.exists():
        return 0
    removed = 0
    for dirpath, dirnames, filenames in os.walk(base, topdown=False):
        cur = Path(dirpath)
        for fname in filenames:
            fp = cur / fname
            if not fp.is_symlink() and fname.lower().endswith((".pyc", ".pyo")):
                fp.unlink(missing_ok=True)
                removed += 1
        for dname in dirnames:
            dp = cur / dname
            if not dp.is_symlink() and dname.lower() == "__pycache__":
                shutil.rmtree(dp, ignore_errors=True)
                removed += 1
    return removed


def check_no_bytecode(root: Path) -> int:
    _, anomalies = _scan_tree(Path(root))
    for kind, item in anomalies[:10]:
        print(f"{kind} leak: {item}", file=sys.stderr)
    return 1 if anomalies else 0


def _build_expected_map(tasks: dict[str, dict[str, str]]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for raw_task, files in tasks.items():
        clean_task = _validate_rel_workspace_path(raw_task)
        if not isinstance(files, dict):
            raise ValueError(f"task files must be a dict: {raw_task!r}")
        for raw_path, text in files.items():
            clean_path = _validate_emit_file_path(raw_path)
            if not isinstance(text, str):
                raise ValueError(f"file content must be str: {clean_task}/{clean_path}")
            expected[f"{clean_task}/{clean_path}"] = text
    return expected


def _assert_contained(target: Path, resolved_root: Path) -> None:
    resolved_target = target.resolve()
    if not resolved_target.is_relative_to(resolved_root):
        raise ValueError(f"path escapes build_dir: {target}")
    assert resolved_target.is_relative_to(resolved_root)


def emit(build_dir: Path, tasks: dict[str, dict[str, str]], check: bool) -> int:
    base = Path(build_dir)
    expected = _build_expected_map(tasks)
    present, anomalies = _scan_tree(base)
    if check:
        anom_rels = {
            (p.relative_to(base).as_posix() if p != base else base.name)
            for _, p in anomalies
        }
        wrong = sorted(
            set(expected) ^ set(present)
            | anom_rels
            | {
                rel
                for rel in set(expected) & set(present)
                if present[rel].read_text(encoding="utf-8") != expected[rel]
            }
        )
        for rel in wrong[:20]:
            print(f"differs: {rel}", file=sys.stderr)
        return 1 if wrong else 0
    if os.path.islink(base):
        raise ValueError(f"build_dir must not be a symlink: {base}")
    if anomalies and any(kind == "symlink" for kind, _ in anomalies):
        raise ValueError(f"refusing to write into build_dir containing symlinks: {anomalies[0][1]}")
    base.mkdir(parents=True, exist_ok=True)
    resolved_root = base.resolve()
    for rel in expected:
        _assert_contained(base / rel, resolved_root)
    purge_bytecode(base)
    for rel in sorted(set(present) - set(expected)):
        _assert_contained(present[rel], resolved_root)
        present[rel].unlink()
    for rel, text in expected.items():
        target = base / rel
        _assert_contained(target, resolved_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        _assert_contained(target, resolved_root)
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8")
        if target.suffix == ".sh":
            target.chmod(0o755)
    return 0
