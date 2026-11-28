from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import _multiagent_io as _io
from ._bespoke_usage import (
    _bespoke_usage_for_member,
    _detect_board_policy,
    _has_unpriced,
    _parse_grade_marks,
    is_regular_dir,
    safe_iterdir,
)
from ._multiagent_io import (
    as_dict,
    as_nonneg_float,
    as_str,
    safe_regular_file,
    valid_member_name,
)

_CTRL_HARNESSES = frozenset({"oracle", "nop", "script", "scripted"})

_HARN = ("artifacts", "logs", "artifacts", "multi-agent-harness")
BESPOKE_BOARD_PATHS = (
    ("agent", "board-events.jsonl"),
    (*_HARN, "board-events.jsonl"),
    ("board-events.jsonl",),
)
BESPOKE_PROXY_PATHS = (
    ("agent", "proxy-usage.jsonl"),
    (*_HARN, "proxy-usage.jsonl"),
    ("proxy-usage.jsonl",),
)
BESPOKE_BUNDLE_PATHS = ((*_HARN, "team.bundle"), ("agent", "team.bundle"), ("team.bundle",))
BESPOKE_SANDBOX_LEDGER_PATHS = (
    ("sandbox-ledger.jsonl",),
    ("agent", "sandbox-ledger.jsonl"),
    (*_HARN, "sandbox-ledger.jsonl"),
)
BESPOKE_BUDGET_USAGE_PATHS = (
    ("agent", "subscription-usage.json"),
    ("agent", "proxy-usage.json"),
    ("subscription-usage.json",),
    ("proxy-usage.json",),
)
BESPOKE_BOOKKEEPING_DIRS = frozenset({"sessions", "logs", "__pycache__"})


@dataclass
class BespokeExtraContext:
    unauthenticated_proxy_requests: int = 0
    member_aliases: dict[str, str] = field(default_factory=dict)
    grade_marks: list[_io.GradeMarkRecord] = field(default_factory=list)
    board_policy: str | None = None
    comms_disabled: bool = False
    resumed_from: str | None = None
    restored_usd: float | None = None
    network_not_enforced: bool = False
    budget_unbounded_subscription: bool = False
    fallback_total_cost_usd: float | None = None


def read_jsonl_safe(path: Path | None, *, max_records: int = 50_000) -> list[dict[str, Any]]:
    assert max_records > 0
    rows, _ = _io.read_jsonl_records(path)
    return rows[:max_records]


def read_bespoke_integrity_signals(
    root: Path,
    *,
    proxy_summary: dict[str, Any] | None = None,
    sub_summary: dict[str, Any] | None = None,
) -> tuple[bool, bool]:
    if not is_regular_dir(root):
        return False, False
    net_not_enforced = any(
        row.get("event") == "network_not_enforced" or row.get("kind") == "network_not_enforced"
        for parts in BESPOKE_SANDBOX_LEDGER_PATHS
        if (lp := safe_regular_file(root, *parts)) is not None
        for row in read_jsonl_safe(lp)
    )
    budget_unbounded = any(
        d is not None and d.get("budget_enforced") is False
        for d in (
            sub_summary,
            proxy_summary,
            *(
                _io.read_json_object(safe_regular_file(root, *p))[0]
                for p in BESPOKE_BUDGET_USAGE_PATHS
            ),
        )
    )
    return net_not_enforced, budget_unbounded


def _build_alias_maps(
    meta: dict[str, Any],
    anomalies: list[str],
    extra_slot_groups: tuple[set[str], ...] = (),
) -> tuple[dict[str, str], dict[str, str], set[str]]:
    slot_to_facing: dict[str, str] = {}
    facing_to_slot: dict[str, str] = {}
    colliding_facings: set[str] = set()
    raw = as_dict(meta.get("agent_names"))
    canonical_slots = {v for slot in sorted(raw) if (v := valid_member_name(slot)) is not None}
    raw_agents = meta.get("agents")
    groups: list[set[str]] = list(extra_slot_groups)
    if isinstance(raw_agents, dict):
        groups.append({v for s in sorted(raw_agents) if (v := valid_member_name(s)) is not None})
    elif isinstance(raw_agents, list):
        canonical_slots.update(
            v for e in raw_agents if (v := valid_member_name(as_dict(e).get("id"))) is not None
        )
    for grp in groups:
        facing_only = {
            vf
            for slot, facing in raw.items()
            if (vs := valid_member_name(slot)) is not None
            and (vf := valid_member_name(facing)) is not None
            and vf != vs
            and vs not in grp
        }
        canonical_slots.update(grp - facing_only)
    pairs = [
        (vs, vf)
        for slot, facing in sorted(raw.items())
        if (vs := valid_member_name(slot)) is not None
        and (vf := valid_member_name(facing)) is not None
    ]
    if isinstance(raw_agents, list):
        pairs.extend(
            (vs, vf)
            for entry in raw_agents
            for rec in (as_dict(entry),)
            if (vs := valid_member_name(rec.get("id"))) is not None
            and (vf := valid_member_name(rec.get("name"))) is not None
        )
    seen_slots: set[str] = set()
    for v_slot, v_face in pairs:
        if v_slot in seen_slots:
            continue
        seen_slots.add(v_slot)
        if (v_face != v_slot and v_face in canonical_slots) or v_face in colliding_facings:
            anomalies.append(f"alias_collision:{v_face}")
            colliding_facings.add(v_face)
            continue
        if v_face in facing_to_slot and facing_to_slot[v_face] != v_slot:
            anomalies.append(f"alias_collision:{v_face}")
            colliding_facings.add(v_face)
            if (prev_slot := facing_to_slot.pop(v_face, None)) is not None:
                slot_to_facing.pop(prev_slot, None)
            continue
        slot_to_facing[v_slot] = v_face
        if v_face != v_slot:
            facing_to_slot[v_face] = v_slot
    for v_face in colliding_facings:
        facing_to_slot.pop(v_face, None)
    return slot_to_facing, facing_to_slot, colliding_facings


def _canon_member(name: object, alias_to_slot: dict[str, str]) -> str | None:
    valid = valid_member_name(name)
    return alias_to_slot.get(valid, valid) if valid is not None else None


def _collect_bespoke_roster(
    meta: dict[str, Any],
    meta_agents: dict[str, Any],
    slot_to_facing: dict[str, str],
    alias_to_slot: dict[str, str],
    board_rows: list[dict[str, Any]],
    proxy_rows: list[dict[str, Any]],
    proxy_summary: dict[str, Any] | None,
    sub_summary: dict[str, Any] | None,
    details: dict[str, Any] | None,
    disk_traj_dirs: set[str],
) -> tuple[set[str], dict[str, str]]:
    roles_from_board: dict[str, str] = {}
    roster_set: set[str] = {c for n in meta_agents if (c := _canon_member(n, alias_to_slot))} | set(
        slot_to_facing
    )
    roster_set.update(
        v for k in as_dict(meta.get("agent_names")) if (v := valid_member_name(k)) is not None
    )
    for ev in board_rows:
        ev_name = ev.get("event") or ev.get("type")
        if ev_name == "register" and (canon := _canon_member(ev.get("agent"), alias_to_slot)):
            roster_set.add(canon)
            if role := as_str(ev.get("role")):
                roles_from_board[canon] = role
        elif (
            ev_name == "subagent"
            and ev.get("op") == "spawn"
            and (canon := _canon_member(ev.get("name"), alias_to_slot))
        ):
            roster_set.add(canon)
    roster_set.update(
        c for row in proxy_rows if (c := _canon_member(row.get("agent"), alias_to_slot))
    )
    for summary in (proxy_summary, sub_summary):
        if summary and isinstance(summary.get("agents"), dict):
            roster_set.update(
                c for n in summary["agents"] if (c := _canon_member(n, alias_to_slot))
            )
    if details and isinstance(details.get("agents"), list):
        roster_set.update(c for n in details["agents"] if (c := _canon_member(n, alias_to_slot)))
    roster_set.update(
        c for name in disk_traj_dirs if (c := _canon_member(name, alias_to_slot)) is not None
    )
    return roster_set, roles_from_board


def _group_bespoke_proxy_rows(
    proxy_rows: list[dict[str, Any]],
    alias_to_slot: dict[str, str],
    anomalies: list[str],
) -> tuple[dict[str, list[dict[str, Any]]], int, bool]:
    proxy_by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unauth_proxy, unattributed_cost = 0, False
    for row in proxy_rows:
        ag = row.get("agent")
        if ag is None and row.get("status") == 401:
            unauth_proxy += 1
        elif (canon := _canon_member(ag, alias_to_slot)) is not None:
            proxy_by_agent[canon].append(row)
        elif (
            row.get("status") == 200
            or as_nonneg_float(row.get("usd")) is not None
            or as_nonneg_float(row.get("cost_usd")) is not None
        ):
            unattributed_cost = True
    if unattributed_cost:
        anomalies.append("unattributed_proxy_usage")
    return proxy_by_agent, unauth_proxy, unattributed_cost


def _lookup_agent_entry(
    m: dict[str, Any], slot: str, face: str | None, colliding_facings: set[str]
) -> dict[str, Any]:
    if slot in m:
        return as_dict(m[slot])
    if face and face not in colliding_facings and face in m:
        return as_dict(m[face])
    return {}


def _build_bespoke_member(
    name: str, root: Path, meta: dict[str, Any], meta_agents: dict[str, Any],
    slot_to_facing: dict[str, str], colliding_facings: set[str],
    roles_from_board: dict[str, str], lost_set: set[str],
    proxy_by_agent: dict[str, list[dict[str, Any]]],
    proxy_agents_map: dict[str, Any], sub_agents_map: dict[str, Any],
    *, solo_ptot: dict[str, Any], unattributed_cost: bool, budget_unbounded: bool, top_unpriced: bool,
) -> tuple[_io.MemberRecord, list[_io.NormalizedAtifStep]]:
    facing = slot_to_facing.get(name)
    safe_f = facing if facing and facing not in colliding_facings else None
    mrec = as_dict(meta_agents.get(name) or (meta_agents.get(safe_f) if safe_f else None))
    traj_file = safe_regular_file(root, "agent", name, "trajectory.json")
    if traj_file is None and safe_f and safe_f != name:
        traj_file = safe_regular_file(root, "agent", safe_f, "trajectory.json")
    t_st, t_rel, t_iss, t_ver, steps, f_met, t_mod = _io.parse_atif_file(name, traj_file, root)
    p_rows = proxy_by_agent.get(name, [])
    p_sum = _lookup_agent_entry(proxy_agents_map, name, safe_f, colliding_facings) or solo_ptot
    s_sum = _lookup_agent_entry(sub_agents_map, name, safe_f, colliding_facings)
    p_mod = next((m for r in p_rows if (m := as_str(r.get("model")))), None)
    model_name = as_str(mrec.get("model")) or p_mod or t_mod
    harness = as_str(mrec.get("harness")) or as_str(meta.get("harness"))
    h_low = (harness or "").lower()
    is_ctrl = bool(h_low) and (h_low in _CTRL_HARNESSES or h_low.endswith(("scriptagent", "scriptedagent"))) and model_name is None
    msub = mrec.get("billed_as") == "claude-subscription" or (
        budget_unbounded and mrec.get("billed_as") != "proxy" and not p_rows and not p_sum
    )
    usage = _bespoke_usage_for_member(
        p_rows, p_sum, s_sum, mrec, f_met,
        is_unbounded_sub=msub, top_unpriced=top_unpriced,
        is_known_no_model=is_ctrl, meta_cost_source=as_str(meta.get("cost_source")),
    )
    if unattributed_cost and usage.cost_status in ("recorded", "missing"):
        usage.cost_usd, usage.cost_status = None, "partial"
    model_provider = next((p for r in p_rows if (p := as_str(r.get("provider")))), None)
    raw_exit = mrec.get("exit")
    exit_code = raw_exit if type(raw_exit) is int else None
    is_lost = name in lost_set
    exc_type = "LostSandbox" if is_lost else (f"NonZeroExitCode({exit_code})" if exit_code not in (None, 0) else None)
    n_tc = sum(len(s.tool_calls) for s in steps) if t_st == "present" else None
    stopped = mrec.get("stopped") if isinstance(mrec.get("stopped"), bool) else None
    record = _io.MemberRecord(
        member_id=name, display_name=facing if facing and facing != name else None,
        role=as_str(mrec.get("role")) or roles_from_board.get(name),
        agent_name=harness, harness=harness, model_name=model_name, model_provider=model_provider,
        ran_model=not is_ctrl, trajectory_path=t_rel, trajectory_status=t_st,
        trajectory_issues=t_iss, atif_schema_version=t_ver,
        n_steps=len(steps) if t_st == "present" else None, n_tool_calls=n_tc,
        exception_type=exc_type, exit_code=exit_code, stopped=stopped, lost=is_lost, usage=usage,
    )
    return record, steps


def _extract_bespoke_meta(
    root: Path, result: dict[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], set[str], set[str]]:
    raw_meta = as_dict(as_dict(result.get("agent_result")).get("metadata")) if result else {}
    nested_ma = as_dict(raw_meta.get("multi_agent"))
    meta = {**nested_ma, **raw_meta} if nested_ma else raw_meta
    raw_agents_obj = meta.get("agents")
    meta_agents: dict[str, Any] = as_dict(raw_agents_obj) or (
        {
            str(entry["id"]): entry
            for entry in raw_agents_obj
            if isinstance(entry, dict) and valid_member_name(entry.get("id"))
        }
        if isinstance(raw_agents_obj, list)
        else {}
    )
    board_path = next(
        (p for ps in BESPOKE_BOARD_PATHS if (p := safe_regular_file(root, *ps))), None
    )
    board_rows, _ = _io.read_jsonl_records(board_path)
    disk_traj_dirs = {
        v
        for child in (safe_iterdir(root / "agent") if is_regular_dir(root / "agent") else ())
        if is_regular_dir(child)
        and child.name not in BESPOKE_BOOKKEEPING_DIRS
        and safe_regular_file(child, "trajectory.json") is not None
        and (v := valid_member_name(child.name)) is not None
    }
    board_reg_names = {
        v
        for ev in board_rows
        if (ev.get("event") or ev.get("type")) == "register"
        and (v := valid_member_name(ev.get("agent"))) is not None
    }
    return meta, meta_agents, board_rows, disk_traj_dirs, board_reg_names


def _extract_lost_set(meta: dict[str, Any], alias_to_slot: dict[str, str]) -> set[str]:
    raw_lost = meta.get("lost_agents")
    lost_set = {
        c
        for x in (raw_lost if isinstance(raw_lost, list) else ())
        if isinstance(x, str) and (c := _canon_member(x, alias_to_slot))
    }
    if isinstance(meta.get("lost"), dict):
        lost_set.update(c for k in meta["lost"] if (c := _canon_member(k, alias_to_slot)))
    return lost_set


def _load_bespoke_proxy_and_integrity(
    root: Path, anomalies: list[str]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None, bool, bool]:
    proxy_path = next(
        (p for ps in BESPOKE_PROXY_PATHS if (p := safe_regular_file(root, *ps))), None
    )
    proxy_rows, proxy_errs = _io.read_jsonl_records(proxy_path)
    if proxy_errs:
        anomalies.append(f"proxy_usage_jsonl_parse_errors:{proxy_errs}")
    p_json = next(
        (
            p
            for ps in (("agent", "proxy-usage.json"), ("proxy-usage.json",))
            if (p := safe_regular_file(root, *ps))
        ),
        None,
    )
    s_json = next(
        (
            p
            for ps in (("agent", "subscription-usage.json"), ("subscription-usage.json",))
            if (p := safe_regular_file(root, *ps))
        ),
        None,
    )
    proxy_summary, _ = _io.read_json_object(p_json)
    sub_summary, _ = _io.read_json_object(s_json)
    net_not_enforced, budget_unbounded = read_bespoke_integrity_signals(
        root, proxy_summary=proxy_summary, sub_summary=sub_summary
    )
    if net_not_enforced:
        anomalies.append("network_not_enforced")
    if budget_unbounded:
        anomalies.append("budget_unbounded_subscription")
    return proxy_rows, proxy_summary, sub_summary, net_not_enforced, budget_unbounded


def read_bespoke_members(
    root: Path, result: dict[str, Any] | None, anomalies: list[str]
) -> tuple[list[_io.MemberRecord], dict[str, list[_io.NormalizedAtifStep]], BespokeExtraContext]:
    meta, meta_agents, board_rows, disk_traj_dirs, board_reg_names = _extract_bespoke_meta(root, result)
    slot_to_facing, alias_to_slot, colliding_facings = _build_alias_maps(
        meta, anomalies, (disk_traj_dirs, board_reg_names)
    )
    lost_set = _extract_lost_set(meta, alias_to_slot)
    proxy_rows, proxy_summary, sub_summary, net_not_enforced, budget_unbounded = (
        _load_bespoke_proxy_and_integrity(root, anomalies)
    )
    details, _ = _io.read_json_object(safe_regular_file(root, "verifier", "details.json"))
    board_policy, comms_disabled = _detect_board_policy(board_rows)
    if board_policy is None and (meta_pol := as_str(meta.get("board_policy"))):
        board_policy = meta_pol
        pol_low = meta_pol.lower().replace("-", "").replace("_", "")
        comms_disabled = comms_disabled or any(t in pol_low for t in ("nomessages", "silent"))
    roster_set, roles_from_board = _collect_bespoke_roster(
        meta, meta_agents, slot_to_facing, alias_to_slot, board_rows, proxy_rows,
        proxy_summary, sub_summary, details, disk_traj_dirs,
    )
    proxy_by_agent, unauth_proxy, unattributed_cost = _group_bespoke_proxy_rows(proxy_rows, alias_to_slot, anomalies)
    unattributed_cost = unattributed_cost or any(a.startswith("proxy_usage_jsonl_parse_errors:") for a in anomalies)
    proxy_agents_map = as_dict(proxy_summary.get("agents")) if proxy_summary else {}
    sub_agents_map = as_dict(sub_summary.get("agents")) if sub_summary else {}
    ptot = dict(as_dict(proxy_summary.get("total"))) if proxy_summary else {}
    if proxy_summary and "usd" not in ptot and "total_cost_usd" in proxy_summary:
        ptot["usd"] = proxy_summary["total_cost_usd"]
    tsub = budget_unbounded or meta.get("budget_enforced") is False or meta.get("billed_as") == "claude-subscription"
    top_unp = _has_unpriced(ptot.get("unpriced")) or (bool(proxy_summary) and _has_unpriced(proxy_summary.get("unpriced")))
    solo_ptot = ptot if len(roster_set) == 1 else {}
    members: list[_io.MemberRecord] = []
    member_steps: dict[str, list[_io.NormalizedAtifStep]] = {}
    for name in sorted(roster_set):
        rec, steps = _build_bespoke_member(
            name, root, meta, meta_agents, slot_to_facing, colliding_facings,
            roles_from_board, lost_set, proxy_by_agent, proxy_agents_map, sub_agents_map,
            solo_ptot=solo_ptot, unattributed_cost=unattributed_cost, budget_unbounded=tsub, top_unpriced=top_unp,
        )
        members.append(rec)
        member_steps[name] = steps
    fb_tot = as_nonneg_float(ptot.get("usd")) if (not tsub and not top_unp and not unattributed_cost) else None
    ctx = BespokeExtraContext(
        unauthenticated_proxy_requests=unauth_proxy, member_aliases=alias_to_slot,
        grade_marks=_parse_grade_marks(root, meta), board_policy=board_policy,
        comms_disabled=comms_disabled,
        resumed_from=as_str(as_dict(meta.get("resumed_from")).get("path")) or as_str(meta.get("resumed_from")),
        restored_usd=as_nonneg_float(meta.get("restored_usd")),
        network_not_enforced=net_not_enforced, budget_unbounded_subscription=budget_unbounded,
        fallback_total_cost_usd=fb_tot,
    )
    return members, member_steps, ctx
