from __future__ import annotations

import contextlib
import json
from pathlib import Path
from random import Random
from typing import Any

from evallab.sim.multiagent_gen import WORD_NAMES, iso_utc, maybe_xss


def _populate_olam_task_dir(
    tdir: Path, rng: Random, canary: Path | None, expected_errors: set[str]
) -> None:
    (tdir / "instructions").mkdir(exist_ok=True)
    (tdir / "instructions" / "p1.md").write_text("Seat p1.\n", encoding="utf-8")
    (tdir / "instructions" / "p2.md").write_text("Seat p2.\n", encoding="utf-8")
    bad_main = rng.random() < 0.25
    bad_cap = rng.random() < 0.35
    bad_escape = canary is not None and rng.random() < 0.25
    toml_lines = [
        'artifacts = ["/logs/world/events.jsonl"]',
        '[task]\nname = "sim-olam-task"',
        "[agents.p1]",
        "[agents.p2]",
    ]
    if bad_main:
        toml_lines.append("[agents.main]")
        expected_errors.add("main_declared_as_seat")
    if bad_escape and canary is not None:
        toml_lines.append('[agents.p3]\ninstruction = "../../CANARY_SECRET.json"')
        expected_errors.add("unsafe_path_escape")
        expected_errors.add("seat_missing_compose_service")
    (tdir / "task.toml").write_text("\n\n".join(toml_lines) + "\n", encoding="utf-8")
    p1_extra = ""
    if bad_cap:
        p1_extra += "    cap_add:\n      - CAP_SYS_PTRACE\n"
        expected_errors.add("compose_dangerous_cap_add")
    if rng.random() < 0.25:
        p1_extra += "    volumes:\n      - ./../host_secret:/mnt/s\n"
        expected_errors.add("compose_host_bind_mount")
    if rng.random() < 0.2:
        (tdir / "tests" / "Dockerfile").write_text(
            "FROM python:3.12-slim\nCOPY . /tests\nRUN mkdir -p /logs/verifier\n",
            encoding="utf-8",
        )
        toml_lines.append('[verifier]\nenvironment_mode = "separate"')
        (tdir / "task.toml").write_text("\n\n".join(toml_lines) + "\n", encoding="utf-8")
    (tdir / "environment" / "docker-compose.yaml").write_text(
        "services:\n"
        "  main:\n"
        "    image: python:3.12-slim\n"
        "    healthcheck:\n"
        "      test: ['CMD', 'true']\n"
        "  p1:\n"
        "    image: python:3.12-slim\n"
        f"{p1_extra}"
        "  p2:\n"
        "    image: python:3.12-slim\n",
        encoding="utf-8",
    )


def _populate_bespoke_task_dir(
    tdir: Path, rng: Random, canary: Path | None, expected_errors: set[str]
) -> None:
    (tdir / "instruction.md").write_text("Solve cleanly.\n", encoding="utf-8")
    (tdir / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    (tdir / "task.toml").write_text(
        '[task]\nname = "sim-bespoke-task"\n\n[verifier]\ntimeout_sec = 60\n\n[agent]\ntimeout_sec = 120\n',
        encoding="utf-8",
    )
    if rng.random() < 0.35:
        extra_svc = "  1:\n    image: alpine\n" if rng.random() < 0.35 else ""
        if extra_svc:
            expected_errors.add("compose_non_string_service_key")
        (tdir / "environment" / "compose.yaml").write_text(
            f"services:\n  sidecar:\n    privileged: true\n{extra_svc}", encoding="utf-8"
        )
        expected_errors.add("compose_privileged_container")
    if canary is not None and rng.random() < 0.25:
        with contextlib.suppress(OSError):
            (tdir / "tests" / "leak.py").symlink_to(canary)
        expected_errors.add("unsafe_path_escape")
    if rng.random() < 0.2:
        loop_a = tdir / "tests" / "loop_a.py"
        loop_b = tdir / "tests" / "loop_b.py"
        with contextlib.suppress(OSError):
            loop_a.symlink_to(loop_b)
            loop_b.symlink_to(loop_a)
        expected_errors.add("unsafe_path_escape")


def build_task_dir(root: Path, rng: Random, canary: Path | None = None) -> tuple[Path, set[str]]:
    tdir = root / f"sim-task-{rng.randrange(10_000):04d}"
    tdir.mkdir(parents=True, exist_ok=True)
    expected_errors: set[str] = set()
    olam_mode = rng.random() < 0.5
    (tdir / "tests").mkdir(exist_ok=True)
    (tdir / "environment").mkdir(exist_ok=True)
    missing_reward = rng.random() < 0.2
    test_body = (
        "#!/bin/sh\necho done\n"
        if missing_reward
        else "#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n"
    )
    if missing_reward:
        expected_errors.add("verifier_missing_reward_output")
    test_sh = tdir / "tests" / "test.sh"
    test_sh.write_text(test_body, encoding="utf-8")
    test_sh.chmod(0o755)
    if olam_mode:
        _populate_olam_task_dir(tdir, rng, canary, expected_errors)
    else:
        _populate_bespoke_task_dir(tdir, rng, canary, expected_errors)
    return tdir, expected_errors


def _pub_agent_entry(
    aid: str,
    name: str,
    steps: int,
    in_tok: int,
    out_tok: int,
    cost_usd: float | None,
    traj_path: str | None = None,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "id": aid,
        "kind": "agent",
        "name": name,
        "steps": steps,
        "inputTokens": in_tok,
        "outputTokens": out_tok,
        "costUsd": cost_usd,
    }
    if traj_path is not None:
        rec["trajectoryPath"] = traj_path
    return rec


def _build_public_index_obj(s1: float, sc: float, sn: float) -> dict[str, Any]:
    runs = [
        {
            "runId": "sim-k1-r1",
            "teamSize": 1,
            "setting": "team",
            "rollout": 1,
            "metrics": {"score": s1, "marks": [0.1, s1], "costUsd": 10.0},
            "agents": [_pub_agent_entry("agent-1", WORD_NAMES[0], 10, 1000, 200, 10.0)],
        },
        {
            "runId": "sim-k8-control-r1",
            "teamSize": 8,
            "setting": "control",
            "rollout": 1,
            "metrics": {"score": sc, "marks": [0.15, sc], "costUsd": 60.0},
            "agents": [_pub_agent_entry("agent-1", WORD_NAMES[0], 10, 1000, 200, None)],
        },
        {
            "runId": "sim-k8-r1",
            "teamSize": 8,
            "setting": "team",
            "rollout": 1,
            "metrics": {"score": sn, "marks": [0.2, sn], "costUsd": 80.0},
            "agents": [
                _pub_agent_entry(
                    "agent-1",
                    WORD_NAMES[0],
                    2,
                    2000,
                    400,
                    10.0,
                    "public-rollout-data/trajectories/seed-to-profit/sim-k8-r1/agent-1.json.gz",
                ),
                _pub_agent_entry("agent-2", WORD_NAMES[1], 2, 2000, 400, 10.0),
                {
                    "id": "board",
                    "kind": "board",
                    "steps": 1,
                    "senders": 1,
                    "trajectoryPath": "public-rollout-data/trajectories/seed-to-profit/sim-k8-r1/board.json.gz",
                },
            ],
        },
    ]
    return {
        "markHours": [1, 4],
        "tasks": {
            "seed-to-profit": {
                "slug": "seed-to-profit",
                "title": "Seed to Profit",
                "models": {
                    "sim-model": {
                        "modelName": "Sim Model",
                        "modelSlug": "sim-model",
                        "harness": "Claude Code",
                        "runs": runs,
                    }
                },
            }
        },
    }


def _build_public_agent_and_board_objs(
    rng: Random, t0: float
) -> tuple[dict[str, Any], dict[str, Any]]:
    agent_obj = {
        "schema": "ATIF steps, shortened for the site",
        "agent": {"harness": "claude-code", "model": "sim-model"},
        "steps": [
            {
                "step_id": 1,
                "source": "agent",
                "minute": 0.1,
                "timestamp": iso_utc(t0 + 10.0),
                "message": "",
                "tool_calls": [
                    {"function_name": "mcp__board__inbox", "arguments": {"wait_seconds": 0}}
                ],
                "observation": json.dumps(
                    {"result": [{"seq": 1, "from": WORD_NAMES[1], "text": "hello team"}]}
                ),
            },
            {
                "step_id": 2,
                "source": "agent",
                "minute": 0.2,
                "timestamp": iso_utc(t0 + 20.0),
                "message": "",
                "tool_calls": [
                    {"function_name": "Bash", "arguments": {"command": "git commit -am 'work'"}}
                ],
                "observation": "done",
            },
        ],
    }
    board_obj = {
        "schema": "message board of the team",
        "steps": [
            {
                "step_id": 1,
                "source": WORD_NAMES[1],
                "kind": "board",
                "to": "everyone",
                "minute": 0.05,
                "timestamp": iso_utc(t0 + 5.0),
                "message": maybe_xss(rng, "hello team"),
            }
        ],
    }
    return agent_obj, board_obj


def build_bespoke_public_md(root: Path, rng: Random, t0: float) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    md_path = root / "bespoke_public.md"
    s1 = round(rng.uniform(0.3, 0.5), 4)
    sc = round(rng.uniform(0.35, 0.55), 4)
    sn = round(rng.uniform(0.4, 0.7), 4)
    index_obj = _build_public_index_obj(s1, sc, sn)
    agent_obj, board_obj = _build_public_agent_and_board_objs(rng, t0)
    content = (
        "### https://example.test/public-rollout-data/index.json\n```json\n"
        + json.dumps(index_obj)
        + "\n```\n### https://example.test/public-rollout-data/trajectories/seed-to-profit/sim-k8-r1/agent-1.json.gz\n```json\n"
        + json.dumps(agent_obj)
        + "\n```\n### https://example.test/public-rollout-data/trajectories/seed-to-profit/sim-k8-r1/board.json.gz\n```json\n"
        + json.dumps(board_obj)
        + "\n```\n"
    )
    md_path.write_text(content, encoding="utf-8")
    return md_path
