from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tidegauge.load import bespoke_telemetry, load_arm
from tidegauge.sim_fixture import START, Outcome, Scenario, TrialSpec, write_job, write_json

BESPOKE_NAMES = ("amber-cedar-hawk", "brisk-pine-otter", "calm-river-lynx", "dawn-stone-finch")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _bespoke_board_and_trajectories(
    path: Path, slots: list[str], facings: list[str], silent: bool
) -> None:
    board_rows: list[dict[str, object]] = [
        {"ts": 1000.0, "event": "config", "policy": "NoMessagesPolicy" if silent else "default"}
    ]
    for i, f in enumerate(facings):
        board_rows.append({"ts": 1001.0 + i, "event": "register", "agent": f, "role": "worker"})
    if not silent:
        board_rows.append(
            {"ts": 1010.0, "event": "send", "seq": 1, "agent": facings[0], "text": "plan ready"}
        )
        if len(facings) > 1:
            board_rows.append(
                {"ts": 1012.0, "event": "send", "seq": 2, "agent": facings[1], "text": "ack plan"}
            )
            board_rows.append({"ts": 1015.0, "event": "inbox", "agent": facings[0], "seqs": [2]})
            board_rows.append({"ts": 1016.0, "event": "inbox", "agent": facings[1], "seqs": [1]})
    _write_jsonl(path / "agent" / "board-events.jsonl", board_rows)
    for idx, slot in enumerate(slots):
        sdir = path / "agent" / slot
        sdir.mkdir(parents=True, exist_ok=True)
        steps = [
            {
                "step_id": 1,
                "source": "agent",
                "timestamp": "1970-01-01T00:16:54Z",
                "tool_calls": [{"function_name": "mcp__board__inbox", "arguments": {}}],
                "metrics": {"prompt_tokens": 120, "cached_tokens": 40, "completion_tokens": 15},
            },
            {
                "step_id": 2,
                "source": "agent",
                "timestamp": f"1970-01-01T00:16:{57 + idx:02d}Z",
                "tool_calls": [
                    {"function_name": "bash", "arguments": {"command": "git commit -am ok"}}
                ],
                "metrics": {"prompt_tokens": 200, "cached_tokens": 100, "completion_tokens": 25},
            },
        ]
        write_json(
            sdir / "trajectory.json",
            {
                "schema_version": "ATIF-v1.6",
                "agent": {"name": "claude-code", "model_name": "sim/bespoke-model"},
                "steps": steps,
                "final_metrics": {
                    "total_prompt_tokens": 320,
                    "total_cached_tokens": 140,
                    "total_completion_tokens": 40,
                },
            },
        )


def _bespoke_usage_files(path: Path, slots: list[str], facings: list[str], cost_mode: str) -> None:
    k = len(slots)
    tok = {"input": 320, "output": 40, "cache_read": 140}
    if cost_mode in ("proxy_jsonl", "partial_unpriced"):
        partial = cost_mode == "partial_unpriced"
        _write_jsonl(
            path / "agent" / "proxy-usage.jsonl",
            [
                {
                    "ts": 1011.0 + i,
                    "agent": facings[i],
                    "model": "sim/bespoke-model",
                    "tokens": tok,
                    "usd": 0.04 if (not partial or i == 0) else None,
                    "status": 200,
                }
                for i in range(k)
            ],
        )
    elif cost_mode in ("proxy_json_zero", "standalone_no_result"):
        usd = 0.0 if cost_mode == "proxy_json_zero" else 0.04
        write_json(
            path / "agent" / "proxy-usage.json",
            {
                "budget_enforced": True,
                "total": {"usd": round(usd * k, 6), "requests": k, "unpriced": {}},
                "agents": {
                    s: {"usd": usd, "requests": 1, "tokens": tok, "unpriced": {}} for s in slots
                },
            },
        )
    elif cost_mode == "subscription_unbounded":
        write_json(
            path / "agent" / "subscription-usage.json",
            {
                "billed_as": "claude-subscription",
                "budget_enforced": False,
                "total": {"usd": 0.5 * k, "requests": k, "unpriced": {}},
                "agents": {
                    s: {"usd": 0.5, "requests": 1, "tokens": tok, "unpriced": {}} for s in slots
                },
            },
        )


def write_bespoke_trial(
    path: Path, spec: TrialSpec, *, k: int = 2, cost_mode: str = "proxy_jsonl", silent: bool = False
) -> None:
    assert 1 <= k <= 4
    path.mkdir(parents=True, exist_ok=True)
    slots, facings = [f"agent-{i}" for i in range(k)], list(BESPOKE_NAMES[:k])
    _bespoke_board_and_trajectories(path, slots, facings, silent)
    reward = 0.0 if spec.reward is None else spec.reward
    for label, scale in (("m30", 0.5), ("h01", 1.0)):
        mdir = path / "marks" / label
        mdir.mkdir(parents=True, exist_ok=True)
        write_json(mdir / "reward.json", {"reward": round(reward * scale, 6)})
    (path / "verifier").mkdir(exist_ok=True)
    write_json(path / "verifier" / "reward.json", {"reward": reward})
    _bespoke_usage_files(path, slots, facings, cost_mode)
    if cost_mode == "standalone_no_result":
        return
    is_sub = cost_mode == "subscription_unbounded"
    sub_f = {"billed_as": "claude-subscription", "cost_usd": 0.5} if is_sub else {}
    base_a = {"role": "worker", "harness": "claude-code", "model": "sim/bespoke-model", "exit": 0}
    meta: dict[str, object] = {
        "harness": "claude-code",
        "orchestration": "async",
        "agent_names": dict(zip(slots, facings, strict=True)),
        "board_policy": "NoMessagesPolicy" if silent else "default",
        "cost_source": "transcripts" if is_sub else "proxy",
        "agents": {s: {**base_a, **sub_f} for s in slots},
        "marks": [
            {"mark": "m30", "mark_s": 1800, "reward": round(reward * 0.5, 6), "status": "graded"},
            {"mark": "h01", "mark_s": 3600, "reward": reward, "status": "graded", "final": True},
        ],
        **({"billed_as": "claude-subscription", "budget_enforced": False} if is_sub else {}),
    }
    end = f"2026-02-05T16:{10 + spec.index // 60:02d}:{spec.index % 60:02d}Z"
    env_imp = "multi_agent_harness.harbor.environment:TeamEnvironment"
    cfg = {
        "task": {"path": spec.task},
        "agent": {"import_path": "multi_agent_harness.harbor.agent:TeamAgent"},
        "environment": {"import_path": env_imp},
    }
    write_json(
        path / "result.json",
        {
            "trial_name": spec.name,
            "task_name": spec.task,
            "task_id": {"path": spec.task},
            "source": "sim-bespoke",
            "task_checksum": f"sum-{spec.task}",
            "config": cfg,
            "agent_info": {"name": "TeamAgent", "version": "0.1.0"},
            "agent_result": {"cost_usd": 0.0, "metadata": meta},
            "verifier_result": {"rewards": {"reward": reward}},
            "exception_info": None,
            "started_at": START,
            "finished_at": end,
            "agent_execution": {"started_at": START, "finished_at": end},
        },
    )


def write_bespoke_arm(
    path: Path, outcomes: Outcome, k: int, *, cost_mode: str = "proxy_jsonl", silent: bool = False
) -> None:
    total = sum(len(rewards) for rewards in outcomes.values())
    write_job(path, total)
    for task, rewards in outcomes.items():
        for i, reward in enumerate(rewards):
            spec = TrialSpec(task, i, reward, 0)
            write_bespoke_trial(path / spec.name, spec, k=k, cost_mode=cost_mode, silent=silent)


def _check_bespoke_modes(b_root: Path, require_fn: Callable[[str, str, bool], None]) -> None:
    modes = (
        "proxy_json_zero",
        "subscription_unbounded",
        "partial_unpriced",
        "standalone_no_result",
    )
    m_root = b_root / "modes"
    for idx, mode in enumerate(modes):
        write_bespoke_trial(m_root / mode, TrialSpec("t000", idx, 1.0, 0), k=2, cost_mode=mode)
    by_name = {t.path.name: t for t in load_arm(m_root).trials}
    require_fn("bespoke/modes", "all-modes-loaded", set(by_name) == set(modes))
    require_fn("bespoke/modes", "zero-preserved", by_name["proxy_json_zero"].cost_usd == 0.0)
    sub_t = by_name["subscription_unbounded"]
    sub_tel = bespoke_telemetry(sub_t.path)
    sub_ok = (
        sub_t.cost_usd is None
        and sub_tel is not None
        and sub_tel.cost_status == "unbounded_subscription"
    )
    require_fn("bespoke/modes", "subscription-not-zero", sub_ok)
    part_t = by_name["partial_unpriced"]
    require_fn(
        "bespoke/modes",
        "partial-flagged",
        part_t.cost_usd is None and "cost_usd" in part_t.partial_usage,
    )
    st_t = by_name["standalone_no_result"]
    st_ok = (
        st_t.valid
        and st_t.seats == ("agent-0", "agent-1")
        and st_t.rewards.get("mark.m30") == 0.5
        and st_t.rewards.get("board.messages") == 2.0
        and st_t.rewards.get("board.read_act_latency_sec") == 2.0
    )
    require_fn("bespoke/modes", "standalone-telemetry", st_ok)


def _check_bespoke_redteam(
    b_root: Path,
    require_fn: Callable[[str, str, bool], None],
    close_fn: Callable[[float, float], bool],
) -> None:
    rt = b_root / "redteam"
    p_vm, p_sl, p_unp, p_tot, p_ex = (rt / n for n in ("vm", "sl", "unp", "tot", "ex"))
    write_bespoke_trial(p_vm, TrialSpec("t010", 0, 1.0, 0), k=2, silent=True)
    vrows: list[dict[str, object]] = [
        {"t": i, "from": "alpha", "channel": "say", "text": f"m{i}"} for i in range(4)
    ]
    _write_jsonl(p_vm / "verifier" / "messages.jsonl", vrows)
    require_fn(
        "bespoke/rt", "vmsgs", load_arm(p_vm, reward_key="board.messages").trials[0].reward == 4.0
    )
    write_bespoke_trial(p_sl, TrialSpec("t011", 0, 1.0, 0), k=2, cost_mode="none", silent=True)
    res_sl = json.loads((p_sl / "result.json").read_text(encoding="utf-8"))
    srec = {"harness": "script", "model": "openrouter/anthropic/claude-haiku-5.5", "exit": 0}
    res_sl["agent_result"]["metadata"]["agents"] = {"agent-0": srec, "agent-1": dict(srec)}
    write_json(p_sl / "result.json", res_sl)
    sl_t = load_arm(p_sl).trials[0]
    sl_ok = sl_t.agent == "agent-0=script,agent-1=script" and sl_t.cost_usd is None
    require_fn("bespoke/rt", "slash-model", sl_ok and "openrouter/anthropic/" in (sl_t.model or ""))
    write_bespoke_trial(p_unp, TrialSpec("t012", 0, 1.0, 0), k=2, cost_mode="none", silent=True)
    deep: dict[str, object] = {}
    for _ in range(300):
        deep = {"n": deep}
    unp_m = {"by_model": {"haiku": 0}, "reason": "none", "deep": deep}
    ag_u = {s: {"usd": 0.05, "unpriced": unp_m} for s in ("agent-0", "agent-1")}
    write_json(
        p_unp / "agent" / "proxy-usage.json",
        {"total": {"usd": 0.1, "unpriced": unp_m}, "agents": ag_u},
    )
    unp_t = load_arm(p_unp).trials[0]
    require_fn(
        "bespoke/rt",
        "unp-zero",
        bool(unp_t.cost_usd and close_fn(unp_t.cost_usd, 0.1) and not unp_t.partial_usage),
    )
    write_bespoke_trial(p_tot, TrialSpec("t013", 0, 1.0, 0), k=2, cost_mode="none", silent=True)
    write_json(
        p_tot / "agent" / "proxy-usage.json", {"budget_enforced": True, "total_cost_usd": 0.125}
    )
    tot_t = load_arm(p_tot).trials[0]
    require_fn(
        "bespoke/rt",
        "tot-usd",
        bool(tot_t.cost_usd and close_fn(tot_t.cost_usd, 0.125) and not tot_t.partial_usage),
    )
    write_bespoke_trial(p_ex, TrialSpec("t014", 0, 1.0, 0), k=2)
    res_ex = json.loads((p_ex / "result.json").read_text(encoding="utf-8"))
    res_ex["exception_info"] = {"exception_type": "VerifierTimeoutError"}
    write_json(p_ex / "result.json", res_ex)
    ex_m, ex_b = (
        load_arm(p_ex, reward_key="mark.m30").trials[0],
        load_arm(p_ex, reward_key="board.messages").trials[0],
    )
    require_fn("bespoke/rt", "extra-exc", not ex_m.valid and not ex_b.valid)


def _check_bespoke_trunc(root: Path, require_fn: Callable[[str, str, bool], None]) -> None:
    t_res, t_st = root / "b-trunc" / "res", root / "b-trunc" / "no-res"
    write_bespoke_trial(t_res, TrialSpec("t090", 0, 1.0, 0), k=2, cost_mode="proxy_json")
    for fn in ("proxy-usage.json", "subscription-usage.json"):
        (t_res / "agent" / fn).write_text("{truncated", encoding="utf-8")
    write_bespoke_trial(
        t_st, TrialSpec("t091", 0, 1.0, 0), k=2, cost_mode="standalone_no_result"
    )
    (t_st / "agent" / "proxy-usage.json").write_text("{truncated", encoding="utf-8")
    arm_t = load_arm(root / "b-trunc")
    require_fn(
        "bespoke/trunc",
        "graceful",
        len(arm_t.trials) == 2 and not arm_t.problems and all(t.valid for t in arm_t.trials),
    )


def check_bespoke_suite(
    case: Scenario,
    root: Path,
    *,
    report_fn: Callable[..., dict[str, Any] | None],
    require_fn: Callable[[str, str, bool], None],
    effect_fn: Callable[[str, dict[str, Any]], None],
    close_fn: Callable[[float, float], bool],
) -> None:
    b_root = root / "bespoke"
    b_root.mkdir()
    arms_out = [case.arms[0], case.arms[1 % len(case.arms)], case.arms[2 % len(case.arms)]]
    b_paths = [b_root / "b-solo", b_root / "b-indep", b_root / "b-team"]
    write_bespoke_arm(b_paths[0], arms_out[0], k=1, silent=True)
    write_bespoke_arm(b_paths[1], arms_out[1], k=3, silent=True)
    write_bespoke_arm(b_paths[2], arms_out[2], k=3, silent=False)
    if aud := report_fn("bespoke/audit", "audit", b_paths):
        require_fn("bespoke/audit", "clean-bespoke-no-errors", aud["counts"]["error"] == 0)
    if sc := report_fn("bespoke/score", "score", b_paths):
        require_fn("bespoke/score", "three-arms-scored", len(sc["arms"]) == 3)
    if cmp := report_fn("bespoke/compare", "compare", b_paths[1:]):
        effect_fn("bespoke/compare", cmp)
    if dec := report_fn("bespoke/decompose", "decompose", b_paths):
        seats = [a["seats"] for a in dec["arms"]]
        require_fn("bespoke/decompose", "seat-counts", seats == [[1, 1], [3, 3], [3, 3]])
        c_ok = (
            close_fn(dec["arms"][0]["cost_per_trial"], 0.04)
            and close_fn(dec["arms"][2]["cost_per_trial"], 0.12)
            and close_fn(dec["arms"][2]["cost_ratio"], 3.0)
        )
        require_fn("bespoke/decompose", "cost-and-ratio", c_ok)
        eff = dec["effects"]
        d_sum = eff["headcount"]["difference"] + eff["coordination"]["difference"]
        require_fn("bespoke/decompose", "identity", close_fn(d_sum, eff["total"]["difference"]))
    if mdec := report_fn(
        "bespoke/mark-decompose", "decompose", b_paths, options=("--metric", "mark.m30")
    ):
        meff = mdec["effects"]
        m_sum = meff["headcount"]["difference"] + meff["coordination"]["difference"]
        require_fn(
            "bespoke/mark-decompose", "mark-identity", close_fn(m_sum, meff["total"]["difference"])
        )
    _check_bespoke_modes(b_root, require_fn)
    _check_bespoke_redteam(b_root, require_fn, close_fn)
    _check_bespoke_trunc(root, require_fn)
