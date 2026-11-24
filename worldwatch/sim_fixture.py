from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from random import Random
from typing import Any

from worldwatch.model import GENESIS_HASH, canonical_json, compute_entry_hash, stamp_utc


def seal_chain(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prev = GENESIS_HASH
    out: list[dict[str, Any]] = []
    for idx, raw in enumerate(events, start=1):
        row = {k: v for k, v in raw.items() if k not in ("entry_hash", "hmac")}
        row["seq"] = idx
        row["prev_hash"] = prev
        digest = compute_entry_hash(row)
        row["entry_hash"] = digest
        prev = digest
        out.append(row)
    return out


def encode_jsonl(rows: list[dict[str, Any]], *, crlf: bool = False) -> bytes:
    sep = "\r\n" if crlf else "\n"
    return "".join(canonical_json(r) + sep for r in rows).encode("utf-8")


def iso_at(epoch_s: float) -> str:
    dt = datetime.fromtimestamp(epoch_s, UTC)
    return stamp_utc(dt) or ""


def write_atif(
    path: Path,
    *,
    seat: str,
    t0: float,
    steps: int = 2,
    tool_errors: int = 0,
    complete: bool = True,
    cost_usd: float = 0.02,
    model_name: str = "sim/gpt-test",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    step_rows: list[dict[str, Any]] = []
    for idx in range(1, steps + 1):
        is_last = idx == steps
        fn_name = "mark_task_complete" if (is_last and complete) else "bash"
        exit_c = 1 if idx <= tool_errors else 0
        step_rows.append({
            "step_id": idx,
            "source": "agent",
            "timestamp": iso_at(t0 + idx * 5.0),
            "llm_call_count": 1,
            "metrics": {"prompt_tokens": 200, "completion_tokens": 30, "cost_usd": round(cost_usd / max(1, steps), 6)},
            "tool_calls": [{"tool_call_id": f"c{idx}", "function_name": fn_name, "arguments": {"cmd": "echo ok"}}],
            "observation": {"results": [{"source_call_id": f"c{idx}", "content": "ok", "extra": {"exit_code": exit_c}}]},
        })
    doc = {
        "schema_version": "ATIF-v1.6",
        "session_id": f"sess-{seat}",
        "agent": {"name": "terminus-2", "version": "1.0.0", "model_name": model_name},
        "steps": step_rows,
        "final_metrics": {
            "total_prompt_tokens": 200 * steps,
            "total_completion_tokens": 30 * steps,
            "total_cost_usd": round(cost_usd, 6),
        },
    }
    path.write_text(json.dumps(doc), encoding="utf-8")


def _write_olam_result_doc(
    trial_dir: Path,
    trial_name: str,
    seats: tuple[str, ...],
    cfg_doc: dict[str, Any],
    ag_name: str,
    mod_name: str | None,
    no_model: bool,
    cost_per_seat: float,
    t0: float,
    duration_s: float,
    reward: float | None,
    extra_rewards: dict[str, float] | None,
    reward_valid: bool,
    stalled_seat: str | None,
    active_timeout_seat: str | None,
    missing_sr_seat: str | None,
) -> None:
    sr_list = []
    for s in seats:
        if s == missing_sr_seat:
            continue
        exc = None
        if s in (stalled_seat, active_timeout_seat):
            exc = {"exception_type": "AgentTimeoutError", "exception_message": "timed out"}
        sr_list.append({
            "seat": s,
            "under_test": True,
            "agent_info": {"name": ag_name, "version": "1.0", "model_info": {"name": mod_name} if mod_name else None},
            "agent_result": {
                "n_input_tokens": None if no_model else 400,
                "n_output_tokens": None if no_model else 60,
                "cost_usd": None if no_model else cost_per_seat,
            },
            "agent_execution": {"started_at": iso_at(t0), "finished_at": iso_at(t0 + duration_s)},
            "exception_info": exc,
        })
    rew_map = dict(extra_rewards or {})
    if reward is not None:
        rew_map["reward"] = reward
    res_doc = {
        "trial_name": trial_name,
        "task_name": "sim-task",
        "config": cfg_doc,
        "agent_info": {"name": ag_name, "version": "1.0"},
        "verifier_result": {"rewards": rew_map} if rew_map else None,
        "reward_valid": reward_valid,
        "exception_info": None,
        "started_at": iso_at(t0),
        "finished_at": iso_at(t0 + duration_s + 2.0),
        "agent_execution": {"started_at": iso_at(t0), "finished_at": iso_at(t0 + duration_s)},
        "seat_results": sr_list,
    }
    (trial_dir / "result.json").write_text(json.dumps(res_doc), encoding="utf-8")


def write_olam_trial(
    trial_dir: Path,
    *,
    trial_name: str,
    seats: tuple[str, ...] = ("p1", "p2"),
    events: list[dict[str, Any]] | None = None,
    hashed: bool = True,
    crlf: bool = False,
    reward: float | None = 1.0,
    extra_rewards: dict[str, float] | None = None,
    reward_valid: bool = True,
    finished: bool = True,
    t0: float = 1_770_000_000.0,
    duration_s: float = 60.0,
    cost_per_seat: float = 0.05,
    no_model: bool = False,
    stalled_seat: str | None = None,
    active_timeout_seat: str | None = None,
    lost_seat: str | None = None,
    missing_sr_seat: str | None = None,
    atif_errors: int = 0,
) -> list[dict[str, Any]]:
    trial_dir.mkdir(parents=True, exist_ok=True)
    ag_name = "oracle" if no_model else "terminus-2"
    mod_name = None if no_model else "sim/gpt-test"
    participants = [{"seat": s, "name": ag_name, "model_name": mod_name} for s in seats]
    cfg_doc = {"trial_name": trial_name, "task": {"name": "sim-task"}, "participants": participants}
    (trial_dir / "config.json").write_text(json.dumps(cfg_doc), encoding="utf-8")
    for s in seats:
        s_dir = trial_dir / "seats" / s / "agent"
        s_dir.mkdir(parents=True, exist_ok=True)
        if not no_model:
            last_t = t0 + 5.0 if s == stalled_seat else t0 + duration_s - 5.0
            write_atif(
                s_dir / "trajectory.json",
                seat=s,
                t0=last_t - 10.0,
                steps=2,
                tool_errors=atif_errors if s == seats[0] else 0,
                complete=(finished and s != stalled_seat),
                cost_usd=cost_per_seat,
            )
        if finished or s == lost_seat:
            (s_dir / "exit-code.txt").write_text(f"{137 if s == lost_seat else 0}\n", encoding="utf-8")
    ev_rows = list(events) if events is not None else _default_olam_events(seats, t0, finished)
    sealed = seal_chain(ev_rows) if hashed else [{**r, "seq": i + 1} for i, r in enumerate(ev_rows)]
    ev_path = trial_dir / ("verifier/events.jsonl" if finished else "logs/world/events.jsonl")
    ev_path.parent.mkdir(parents=True, exist_ok=True)
    ev_path.write_bytes(encode_jsonl(sealed, crlf=crlf))
    if finished:
        _write_olam_result_doc(
            trial_dir, trial_name, seats, cfg_doc, ag_name, mod_name, no_model, cost_per_seat,
            t0, duration_s, reward, extra_rewards, reward_valid, stalled_seat, active_timeout_seat, missing_sr_seat,
        )
    return sealed


def _default_olam_events(seats: tuple[str, ...], t0: float, closed: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, s in enumerate(seats, start=1):
        rows.append({
            "t": round(t0 + idx * 2.0, 3),
            "seat": s,
            "kind": "open",
            "method": "POST",
            "status": 200,
            "ok": True,
            "request": {"box": idx},
            "response": {"box": idx, "flag": True, "budget_left": 0},
        })
    if closed:
        rows.append({
            "t": round(t0 + 30.0, 3),
            "seat": None,
            "kind": "admin.close",
            "method": "POST",
            "status": 200,
            "ok": True,
            "request": {},
            "response": {"closed": True},
        })
    return rows


def write_bespoke_trial(
    trial_dir: Path,
    *,
    trial_name: str,
    slots: tuple[str, ...] = ("agent-0", "agent-1"),
    facings: tuple[str, ...] = ("amber-hawk", "brisk-otter"),
    board_rows: list[dict[str, Any]] | None = None,
    proxy_rows: list[dict[str, Any]] | None = None,
    hashed_proxy: bool = True,
    finished: bool = True,
    reward: float = 0.85,
    t0: float = 1_770_010_000.0,
    duration_s: float = 90.0,
) -> None:
    assert len(slots) == len(facings)
    trial_dir.mkdir(parents=True, exist_ok=True)
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    for idx, slot in enumerate(slots):
        write_atif(
            agent_dir / slot / "trajectory.json",
            seat=slot,
            t0=t0 + idx * 2.0,
            steps=2,
            complete=finished,
            cost_usd=0.04,
        )
    b_rows = board_rows if board_rows is not None else [
        {"ts": t0 + 1.0, "event": "config", "policy": "default"},
        *[{"ts": t0 + 2.0 + i, "event": "register", "agent": f, "role": "worker"} for i, f in enumerate(facings)],
        {"ts": t0 + 6.0, "event": "send", "seq": 1, "agent": facings[0], "text": "claiming a.py\u2028ok"},
        {"ts": t0 + 8.0, "event": "inbox", "agent": facings[-1], "seqs": [1]},
    ]
    (agent_dir / "board-events.jsonl").write_bytes(encode_jsonl(b_rows))
    p_raw = proxy_rows if proxy_rows is not None else [
        {"ts": t0 + 7.0 + i, "agent": f, "model": "sim/claude", "usd": 0.04, "status": 200}
        for i, f in enumerate(facings)
    ]
    p_out = seal_chain(p_raw) if hashed_proxy else [{**r, "seq": i + 1} for i, r in enumerate(p_raw)]
    (agent_dir / "proxy-usage.jsonl").write_bytes(encode_jsonl(p_out))
    if finished:
        ag_meta = {s: {"role": "worker", "harness": "claude-code", "model": "sim/claude", "exit": 0} for s in slots}
        res_doc = {
            "trial_name": trial_name,
            "task_name": "bespoke-sim",
            "config": {
                "task": {"path": "bespoke-sim"},
                "agent": {"import_path": "multi_agent_harness.harbor.agent:TeamAgent"},
                "environment": {"import_path": "multi_agent_harness.harbor.environment:TeamEnvironment"},
            },
            "agent_info": {"name": "TeamAgent", "version": "0.1.0"},
            "agent_result": {
                "metadata": {
                    "harness": "claude-code",
                    "agents": ag_meta,
                    "agent_names": dict(zip(slots, facings, strict=True)),
                    "cost_source": "proxy",
                }
            },
            "verifier_result": {"rewards": {"reward": reward}},
            "reward_valid": True,
            "exception_info": None,
            "started_at": iso_at(t0),
            "finished_at": iso_at(t0 + duration_s + 3.0),
            "agent_execution": {"started_at": iso_at(t0 + 1.0), "finished_at": iso_at(t0 + duration_s)},
        }
        (trial_dir / "result.json").write_text(json.dumps(res_doc), encoding="utf-8")


def write_synthetic_family(
    family_dir: Path,
    *,
    mode: str,
    rng: Random,
) -> None:
    family_dir.mkdir(parents=True, exist_ok=True)
    if mode == "missing_toml":
        (family_dir / "reference.py").write_text("def reference(cfg): return {}\n", encoding="utf-8")
        return
    if mode == "bad_toml":
        (family_dir / "family.toml").write_text("name = [broken\n", encoding="utf-8")
        (family_dir / "reference.py").write_text("def reference(cfg): return {}\n", encoding="utf-8")
        return
    if mode == "missing_ref":
        (family_dir / "family.toml").write_text('name = "f"\n[conditions.solo]\nseats = ["p1"]\n', encoding="utf-8")
        return
    if mode == "syntax_err_ref":
        (family_dir / "family.toml").write_text('name = "f"\n[conditions.solo]\nseats = ["p1"]\n', encoding="utf-8")
        (family_dir / "reference.py").write_text("def reference(cfg):\n    return (\n", encoding="utf-8")
        return
    has_ctrl = mode != "missing_control"
    oracle_sc = 0.5 if mode == "oracle_ties" else 1.0
    always_sc = 0.5 if mode == "oracle_ties" else round(rng.uniform(0.2, 0.6), 4)
    spoof_sc = 0.25 if mode == "spoof_wins" else 0.0
    stored_oracle = 0.9 if mode == "table_drift" else oracle_sc
    conds = (["none-ctrl", "peer-release"] if has_ctrl else ["peer-release", "bystander-k4"])
    toml_lines = [
        'name = "synth_probe"',
        'exploits = ["spoof"]',
        '[policies]',
        'oracle = "optimal"',
        'always_execute = "degenerate"',
        'nop = "null"',
        'spoof = "exploit"',
    ]
    for c in conds:
        toml_lines.extend([f"[conditions.{c}]", 'seats = ["p1"]'])
    for c in conds:
        for pol, val in (("oracle", stored_oracle), ("always_execute", always_sc), ("nop", 0.0), ("spoof", spoof_sc)):
            toml_lines.extend([f"[reference.{c}.{pol}]", f"mean = {val}", f"min = {val}", f"max = {val}"])
    (family_dir / "family.toml").write_text("\n".join(toml_lines) + "\n", encoding="utf-8")
    ref_table = {
        c: {
            "oracle": {"mean": oracle_sc, "min": oracle_sc, "max": oracle_sc},
            "always_execute": {"mean": always_sc, "min": always_sc, "max": always_sc},
            "nop": {"mean": 0.0, "min": 0.0, "max": 0.0},
            "spoof": {"mean": spoof_sc, "min": spoof_sc, "max": spoof_sc},
        }
        for c in conds
    }
    ref_code = f"TABLE = {ref_table!r}\ndef reference(config):\n    return TABLE\n"
    (family_dir / "reference.py").write_text(ref_code, encoding="utf-8")
