"""Build kit: writes one Harbor seat-task directory in the shape every muster family shares.

A family's generate.py computes the instance (layout, instructions, expected oracle reward) and calls
`task_files(...)` for each condition and instance, then `emit(build_dir, files, check)`.
Everything here is deterministic: the same arguments give the same bytes.
"""

import hashlib
import json
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent

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
# The verifier runs in main. It closes the world, then grades from the event log.
python3 /tests/verify.py
"""


def token(seed: str, instance: int, seat: str) -> str:
    """The seat's bearer token for one instance: 32 hex characters, fixed by the seed."""
    return hashlib.sha256(f"{seed}/{instance}/{seat}".encode()).hexdigest()[:32]


def compose(
    seats: list[str], tokens: dict[str, str], world_env: dict[str, str], internet: bool = False
) -> str:
    """docker-compose.yaml: the world in main, one service per seat, one network per seat.

    main shares one network with each seat, and no two seats share a network. So the world is the only
    path between seats: a seat cannot open a socket to another seat. With internet=False the seat networks
    are internal and IPv6 only: a seat reaches the world and nothing else, not even the internet. A harness
    that calls its model from inside the seat container needs internet=True (then the networks use IPv4).
    """
    seat_tokens = json.dumps({tokens[seat]: seat for seat in seats}, sort_keys=True)
    lines = [
        "x-seat: &seat",
        "  build: ./seat",
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
        lines += [
            f"  {seat}:",
            "    <<: *seat",
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
            # IPv6 only: a network without an IPv4 subnet takes nothing from Docker's small IPv4 address
            # pool, which about 30 networks use up. So many trials can run at the same time.
            lines += ["    internal: true", "    enable_ipv4: false", "    enable_ipv6: true"]
    lines.append("")
    return "\n".join(lines)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"no TOML form for {value!r}")


def task_toml(
    *,
    name: str,
    description: str,
    seats: dict[str, bool],
    metadata: dict[str, object],
    agent_timeout_sec: float = 600.0,
    verifier_timeout_sec: float = 120.0,
) -> str:
    """task.toml. `seats` maps a seat to its under_test flag, in declaration order."""
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
    ]
    for seat, under_test in seats.items():
        lines += ["", f"[agents.{seat}]"]
        if not under_test:
            lines.append("under_test = false")
    return "\n".join(lines) + "\n"


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
    agent_timeout_sec: float = 600.0,
    internet: bool = False,
) -> dict[str, str]:
    """All files of one task directory as {relative path: text}.

    `world` holds the files for /world in main (world.py and the instance data); worldkit.py is added.
    `policies` maps a policy name to its script; `oracle` is required and becomes solution/solve.sh.
    """
    if "oracle" not in policies:
        raise ValueError("every task needs the oracle policy")
    if set(instructions) != set(seats) or set(tokens) != set(seats):
        raise ValueError("instructions and tokens must name exactly the seats")
    files = {
        "task.toml": task_toml(
            name=name,
            description=description,
            seats=seats,
            metadata=metadata,
            agent_timeout_sec=agent_timeout_sec,
        ),
        "environment/docker-compose.yaml": compose(list(seats), tokens, world_env or {}, internet),
        "environment/Dockerfile": MAIN_DOCKERFILE,
        "environment/seat/Dockerfile": SEAT_DOCKERFILE,
        "environment/world/worldkit.py": (KIT / "worldkit.py").read_text(),
        "tests/test.sh": TEST_SH,
        "tests/verify.py": verify,
        "solution/solve.sh": policies["oracle"],
    }
    for path, text in world.items():
        files[f"environment/world/{path}"] = text
    for seat, text in instructions.items():
        files[f"instructions/{seat}.md"] = text if text.endswith("\n") else text + "\n"
    for policy, text in policies.items():
        files[f"policies/{policy}.sh"] = text
    return files


def emit(build_dir: Path, tasks: dict[str, dict[str, str]], check: bool) -> int:
    """Write {task dir relative to build_dir: files}, or with check=True compare and report.

    Returns the process exit status: 0 when the tree is what the generator writes.
    """
    expected = {f"{task}/{path}": text for task, files in tasks.items() for path, text in files.items()}
    present = {
        path.relative_to(build_dir).as_posix(): path
        for path in sorted(build_dir.rglob("*"))
        if path.is_file() and path.name != ".DS_Store" and "__pycache__" not in path.parts
    }
    if check:
        wrong = sorted(
            set(expected) ^ set(present)
            | {path for path in set(expected) & set(present) if present[path].read_text() != expected[path]}
        )
        for path in wrong[:20]:
            print(f"differs: {path}", file=sys.stderr)
        return 1 if wrong else 0
    for path in sorted(set(present) - set(expected)):
        present[path].unlink()
    for path, text in expected.items():
        target = build_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_text() != text:
            target.write_text(text)
        if target.suffix == ".sh":
            target.chmod(0o755)
    return 0
