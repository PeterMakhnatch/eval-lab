from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from random import Random
from typing import Any

from evallab.sim._multiagent_gen_public import build_bespoke_public_md, build_task_dir
from evallab.sim.multiagent_gen import (
    WORD_NAMES,
    inject_symlink_canary,
    iso_utc,
    maybe_xss,
    write_json,
    write_jsonl,
)

__all__ = [
    "build_bespoke_public_md",
    "build_bespoke_trial",
    "build_decompose_arms",
    "build_task_dir",
]

_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
}


def _git_exec(
    repo: Path,
    *args: str,
    author_name: str = "seed",
    author_email: str | None = None,
    ts: int = 0,
) -> None:
    addr = author_email or f"{author_name}@agents.local"
    env = {
        **_GIT_ENV,
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": addr,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": addr,
        "GIT_AUTHOR_DATE": f"{ts} +0000",
        "GIT_COMMITTER_DATE": f"{ts} +0000",
    }
    subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True)


def _git_commit_file(
    repo: Path, fname: str, body: str, msg: str, author: str, ts: int, email: str | None = None
) -> None:
    (repo / fname).write_text(body, encoding="utf-8")
    _git_exec(repo, "add", fname, ts=ts)
    _git_exec(repo, "commit", "-m", msg, author_name=author, author_email=email, ts=ts)


def _create_git_bundle(bundle_path: Path, aliases: list[str], t0: float, rng: Random) -> None:
    repo = bundle_path.parent / "_tmp_repo"
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True, exist_ok=True)
    base_ts = int(t0)
    try:
        _git_exec(repo, "init", "-b", "main", ts=base_ts)
        _git_commit_file(
            repo,
            "app.py",
            "x = 1\n",
            "initial seed",
            "multi-agent-harness",
            base_ts,
            "harness@local",
        )
        _git_commit_file(repo, "app.py", "x = 2\n", "agent1 edit", aliases[0], int(t0 + 12))
        if len(aliases) > 1:
            mismatch = rng.random() < 0.35
            email = f"{aliases[0]}@agents.local" if mismatch else None
            _git_commit_file(
                repo, "app.py", "x = 3\n", "agent2 edit", aliases[1], int(t0 + 25), email
            )
            if rng.random() < 0.35:
                _git_exec(repo, "checkout", "-b", "feat-a", ts=base_ts)
                _git_commit_file(repo, "feat_a.py", "a = 1\n", "feat a", aliases[0], int(t0 + 30))
                _git_exec(repo, "checkout", "main", ts=base_ts)
                _git_exec(
                    repo,
                    "merge",
                    "--no-ff",
                    "feat-a",
                    "-m",
                    "merge feat-a",
                    author_name=aliases[0],
                    ts=int(t0 + 32),
                )
                _git_exec(repo, "checkout", "-b", "feat-b", ts=base_ts)
                _git_commit_file(repo, "feat_b.py", "b = 1\n", "feat b", aliases[1], int(t0 + 35))
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        _git_exec(repo, "bundle", "create", str(bundle_path), "--all", ts=base_ts)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def _write_bespoke_board_log(
    trial_dir: Path,
    words: list[str],
    k: int,
    t0: float,
    rng: Random,
    *,
    silent: bool,
    legacy: bool,
) -> None:
    board_rows: list[dict[str, Any]] = []
    if silent:
        board_rows.append(
            {
                "ts": t0 + 10.0,
                "event": "refused",
                "agent": words[0],
                "action": "send",
                "reason": "no_messages_policy",
            }
        )
    else:
        for i in range(1, 4):
            sender = words[(i - 1) % k]
            txt = maybe_xss(rng, f"Update {i} on app.py\nline 2")
            if i == 2 and rng.random() < 0.25:
                txt = f"{txt} \ud800surrogate"
            ev_ts: Any = 1e22 if (i == 3 and rng.random() < 0.2) else (t0 + 8.0 + i * 4.0)
            if legacy:
                board_rows.append(
                    {"ts": ev_ts, "type": "post", "seq": i, "from": sender, "text": txt}
                )
            else:
                board_rows.append(
                    {"ts": ev_ts, "event": "send", "seq": i, "agent": sender, "text": txt}
                )
        if k > 1:
            board_rows.append(
                {
                    "ts": t0 + 22.0,
                    "event": "inbox" if not legacy else "read",
                    "agent": words[1],
                    "seqs": [1, 2],
                }
            )
    write_jsonl(
        trial_dir / "agent" / "board-events.jsonl",
        board_rows,
        corrupt_tail=rng.random() < 0.25,
        bad_utf8=rng.random() < 0.1,
    )


def _write_bespoke_proxy_log(
    trial_dir: Path, words: list[str], k: int, t0: float, rng: Random
) -> None:
    proxy_rows: list[dict[str, Any]] = []
    use_v2_proxy = rng.random() < 0.7
    for idx in range(k + 1):
        unpriced = idx == 0 and rng.random() < 0.3
        model = "unpriced-model" if unpriced else "opus-5-5"
        usd = None if unpriced else 0.12
        if use_v2_proxy:
            proxy_rows.append(
                {
                    "ts": t0 + 10.0 + idx,
                    "agent": words[idx % k],
                    "model": model,
                    "tokens": {"input": 1000, "output": 200, "cache_read": 50},
                    "usd": usd,
                    "status": 200,
                }
            )
        else:
            proxy_rows.append(
                {
                    "ts": t0 + 10.0 + idx,
                    "agent": words[idx % k],
                    "model": model,
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "cost_usd": usd,
                    "status": 200,
                }
            )
    if rng.random() < 0.25:
        proxy_rows.append({"ts": t0 + 50.0, "agent": None, "status": 401})
    write_jsonl(trial_dir / "agent" / "proxy-usage.jsonl", proxy_rows)


def _bespoke_member_steps(idx: int, t0: float, first_word: str) -> list[dict[str, Any]]:
    secret_obs = (
        '{"reward": "SECRET_REWARD_999", "_final_reward": "SECRET_REWARD_999_U",'
        ' "a__reward": "SECRET_REWARD_999_D",'
        ' "my_verifier_result": {"secret": "SECRET_REWARD_999_N"},'
        ' "file": "x = 1"}'
    )
    inbox_obs = json.dumps(
        {"result": [{"seq": 1, "from": first_word, "text": "Update 1 on app.py"}]}
    )
    return [
        {
            "step_id": 1,
            "source": "agent",
            "timestamp": iso_utc(t0 + 7.5 + idx),
            "tool_calls": [
                {
                    "tool_call_id": f"r-{idx}",
                    "function_name": "read_file",
                    "arguments": {"path": "/workspace/app.py"},
                }
            ],
            "observation": {"results": [{"content": secret_obs}]},
        },
        {
            "step_id": 2,
            "source": "agent",
            "timestamp": iso_utc(t0 + 21.5),
            "tool_calls": [
                {
                    "tool_call_id": f"i-{idx}",
                    "function_name": "mcp__board__inbox",
                    "arguments": {"wait_seconds": 0},
                }
            ],
            "observation": {"results": [{"content": inbox_obs}]},
        },
        {
            "step_id": 3,
            "source": "agent",
            "timestamp": iso_utc(t0 + 24.5),
            "tool_calls": [
                {
                    "tool_call_id": f"b-{idx}",
                    "function_name": "bash",
                    "arguments": {"command": "git commit -am 'edit'"},
                }
            ],
            "observation": {"results": [{"content": "committed"}]},
        },
    ]


def build_bespoke_trial(
    root: Path, rng: Random, t0: float, canary: Path, *, with_git_bundle: bool = False
) -> Path:
    trial_dir = root / f"bespoke-team__{rng.randrange(10_000):04d}"
    k = rng.choice([1, 2, 3])
    silent = k > 1 and rng.random() < 0.3
    members = [f"agent-{i}" for i in range(1, k + 1)]
    words = list(WORD_NAMES[:k])
    alias_map = dict(zip(members, words, strict=True))
    legacy = rng.random() < 0.3
    resumed = rng.random() < 0.25

    ma_meta: dict[str, Any] = {
        "agents": [
            {"id": m, "name": alias_map[m], "harness": "claude-code", "model": "opus-5-5"}
            for m in members
        ],
        "board_policy": "NoMessagesPolicy" if silent else "default",
        "marks": [{"mark": "1h", "mark_s": 3600, "reward": 0.42, "status": "ok", "final": True}],
    }
    if resumed:
        ma_meta["resumed_from"] = "prev-run-001"
        ma_meta["restored_usd"] = 1.25
    write_json(
        trial_dir / "result.json",
        {
            "id": f"sim-bespoke-{rng.randrange(1_000_000)}",
            "trial_name": trial_dir.name,
            "task_name": "seed-to-profit",
            "started_at": iso_utc(t0),
            "finished_at": iso_utc(t0 + 120.0),
            "agent_execution": {
                "started_at": iso_utc(t0 + 1.0),
                "finished_at": iso_utc(t0 + 100.0),
            },
            "agent_result": {"metadata": {"multi_agent": ma_meta}},
            "verifier_result": {"rewards": {"reward": round(rng.uniform(0.1, 0.9), 4)}},
        },
    )
    if rng.random() < 0.3:
        write_json(trial_dir / "marks" / "1h" / "reward.json", {"reward": 0.42})
    _write_bespoke_board_log(trial_dir, words, k, t0, rng, silent=silent, legacy=legacy)
    _write_bespoke_proxy_log(trial_dir, words, k, t0, rng)
    for idx, m in enumerate(members):
        write_json(
            trial_dir / "agent" / m / "trajectory.json",
            {"schema_version": "ATIF-v1.6", "steps": _bespoke_member_steps(idx, t0, words[0])},
        )
    if with_git_bundle:
        bundle_path = (
            trial_dir / "artifacts" / "logs" / "artifacts" / "multi-agent-harness" / "team.bundle"
        )
        _create_git_bundle(bundle_path, words, t0, rng)
    inject_symlink_canary(trial_dir, canary, rng)
    return trial_dir


def build_decompose_arms(root: Path, rng: Random, t0: float) -> tuple[Path, Path, Path]:
    solo_dir, silent_dir, team_dir = root / "solo", root / "silent", root / "team"
    n_tasks = rng.randint(4, 7)
    for tidx in range(1, n_tasks + 1):
        base = rng.uniform(0.1, 0.5)
        s_val = round(min(1.0, base + rng.uniform(-0.05, 0.25)), 4)
        t_val = round(min(1.0, s_val + rng.uniform(-0.05, 0.35)), 4)
        for arm_dir, k, val, cost in (
            (solo_dir, 1, round(base, 4), 0.1),
            (silent_dir, 3, s_val, 0.3),
            (team_dir, 3, t_val, 0.35),
        ):
            tdir = arm_dir / f"boxes-i{tidx:02d}__sim"
            write_json(
                tdir / "result.json",
                {
                    "id": f"{arm_dir.name}-{tidx}",
                    "trial_name": tdir.name,
                    "task_name": f"muster/boxes-i{tidx:02d}",
                    "started_at": iso_utc(t0),
                    "finished_at": iso_utc(t0 + 30.0),
                    "agent_execution": {
                        "started_at": iso_utc(t0),
                        "finished_at": iso_utc(t0 + 20.0),
                    },
                    "seat_results": [
                        {
                            "seat": f"p{i}",
                            "under_test": True,
                            "agent_info": {"name": "terminus-2"},
                            "agent_result": {
                                "n_input_tokens": 1000,
                                "n_output_tokens": 200,
                                "cost_usd": cost / k,
                            },
                        }
                        for i in range(1, k + 1)
                    ],
                    "verifier_result": {"rewards": {"reward": val}},
                },
            )
    return solo_dir, silent_dir, team_dir
