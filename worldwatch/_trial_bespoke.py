from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from worldwatch._chain import find_bespoke_board_log
from worldwatch._io import read_regular_text, split_text_lines_strict
from worldwatch.model import (
    MAX_SEATS,
    SEAT_NAME_RE,
     is_no_model_name,
    nonneg_int,
    parse_iso_utc,
)

_EXIT_INT_RE = re.compile(r"^[+-]?\d{1,6}$")


def _as_dict(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _valid_seat_token(name: str) -> bool:
    return bool(SEAT_NAME_RE.match(name)) and name not in (".", "..")


def parse_bespoke_exit(raw_exit: Any, lost: bool) -> tuple[int | None, str | None]:
    exit_code: int | None = None
    exit_err: str | None = None
    if isinstance(raw_exit, int) and not isinstance(raw_exit, bool):
        exit_code = raw_exit
    elif isinstance(raw_exit, str) and _EXIT_INT_RE.match(raw_exit.strip()):
        exit_code = int(raw_exit.strip())
    elif raw_exit is not None:
        exit_err = f"invalid exit {raw_exit!r}"
    if lost:
        exit_err = exit_err or "lost_agent"
    return exit_code, exit_err


def read_bespoke_register(trial_dir: Path) -> tuple[dict[str, str], list[str]]:
    board_path = find_bespoke_board_log(trial_dir)
    if board_path is None or not os.path.lexists(board_path):
        return {}, []
    text, err = read_regular_text(board_path, root=trial_dir)
    if err is not None or not text:
        return {}, []
    try:
        lines = split_text_lines_strict(text)
    except ValueError:
        return {}, []
    reg_map: dict[str, str] = {}
    facings: list[str] = []
    for line in lines[:5_000]:
        if not line or "register" not in line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(row, Mapping) or row.get("event") != "register":
            continue
        ag = row.get("agent") or row.get("name")
        sl = row.get("slot") or row.get("seat")
        if isinstance(ag, str) and ag:
            if ag not in facings:
                facings.append(ag)
            if isinstance(sl, str) and sl:
                reg_map.setdefault(sl, ag)
    return reg_map, facings


def bespoke_lost_and_timeout(meta: Mapping[str, Any]) -> tuple[set[str], bool]:
    lost_set: set[str] = set()
    lost_raw = meta.get("lost_agents")
    if isinstance(lost_raw, list):
        lost_set.update(str(x) for x in lost_raw if isinstance(x, str) and x)
    lost_obj = meta.get("lost")
    if isinstance(lost_obj, Mapping):
        lost_set.update(str(k) for k, v in lost_obj.items() if isinstance(k, str) and k and v)
    elif isinstance(lost_obj, list):
        lost_set.update(str(x) for x in lost_obj if isinstance(x, str) and x)
    tl = _as_dict(meta.get("keepalive")).get("timeline")
    time_up = meta.get("timed_out") is True or (
        isinstance(tl, list)
        and any(_as_dict(ev).get("op") == "freeze" and _as_dict(ev).get("why") == "time_up" for ev in tl)
    )
    return lost_set, time_up


def discover_bespoke_slots(
    trial_dir: Path,
    meta: Mapping[str, Any],
) -> tuple[list[str], dict[str, str], set[str], bool]:
    agents_meta = _as_dict(meta.get("agents"))
    reg_map, reg_facings = read_bespoke_register(trial_dir)
    alias_map = dict(reg_map)
    for k, v in _as_dict(meta.get("agent_names")).items():
        if isinstance(k, str) and isinstance(v, str) and k and v:
            alias_map[k] = v
    lost_set, time_up = bespoke_lost_and_timeout(meta)
    agent_root = trial_dir / "agent"
    disk_slots: list[str] = []
    if os.path.isdir(agent_root) and not os.path.islink(agent_root):
        try:
            disk_slots = sorted(
                e.name
                for e in os.scandir(agent_root)
                if e.is_dir(follow_symlinks=False)
                and not e.name.startswith(".")
                and (
                    os.path.lexists(Path(e.path) / "trajectory.json")
                    or os.path.lexists(Path(e.path) / "exit-code.txt")
                    or e.name.startswith(("agent-", "slot-"))
                )
            )
        except OSError:
            disk_slots = []
    slots = list(dict.fromkeys([*agents_meta.keys(), *alias_map.keys(), *disk_slots]))[:MAX_SEATS]
    if slots and reg_facings and len(reg_facings) == len(slots) and not any(s in alias_map for s in slots):
        for sl, fc in zip(slots, reg_facings, strict=True):
            alias_map[sl] = fc
    if not slots:
        slots = reg_facings[:MAX_SEATS] or ["agent-0"]
    return slots, alias_map, lost_set, time_up


def classify_bespoke_seat_attrs(
    entry: Mapping[str, Any],
    ag_top: str,
    ec: int | None,
    eerr: str | None,
    is_lost: bool,
    time_up: bool,
    agent_steps: int,
) -> tuple[str, bool, str | None]:
    harness = str(entry.get("harness") or ag_top)
    is_mod = not is_no_model_name(harness) and not is_no_model_name(ag_top)
    seat_exc: str | None = None
    is_timeout = ec == 124 or (
        ec not in (None, 0)
        and time_up
        and nonneg_int(entry.get("sessions")) in (None, 1)
        and agent_steps > 0
    )
    if not is_lost and eerr is None and is_timeout:
        seat_exc = "AgentTimeoutError"
    return harness, is_mod, seat_exc


def bespoke_exec_bounds(result: Mapping[str, Any]) -> tuple[Any, Any]:
    ag_exec = _as_dict(result.get("agent_execution"))
    return parse_iso_utc(ag_exec.get("started_at")), parse_iso_utc(ag_exec.get("finished_at"))
