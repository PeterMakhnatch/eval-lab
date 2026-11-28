from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from ._multiagent_io import as_dict
from ._task_lint_compose import (
    EmitFn,
    iter_bounded_files,
    read_bounded_text,
    safe_contained_path,
)

_SILENT_MSG_REF_RE = re.compile(r"(?:\bPOST\s+\S*/msg\b|/msgs\b)", re.I)
_HARDCODED_SIGN_SECRET_RE = re.compile(
    r"\b(?:[A-Z0-9_]*SIGN(?:ING)?_KEY|[A-Z0-9_]*SECRET_KEY|HMAC_(?:KEY|SECRET))"
    r"\s*(?::\s*[^=\n]+)?=\s*"
    r"""(?:bytes\.fromhex\s*\(\s*['"][0-9a-fA-F]{8,}['"]\s*\)|b?['"][0-9a-fA-F]{32,}['"])"""
)


def _reject_nonfinite_json(token: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {token}")


def parse_compose_env(raw_env: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(raw_env, dict):
        for k, v in list(raw_env.items())[:512]:
            if k is not None:
                out[str(k).strip()] = "" if v is None else str(v)
    elif isinstance(raw_env, list):
        for entry in raw_env[:512]:
            if isinstance(entry, str) and entry.strip():
                if "=" in entry:
                    k, v = entry.split("=", 1)
                    out[k.strip()] = v
                else:
                    out[entry.strip()] = ""
    return out


def has_world_server_py(task_dir: Path) -> bool:
    assert isinstance(task_dir, Path)
    for rel in (("environment", "world", "world.py"), ("environment", "world.py")):
        res = safe_contained_path(task_dir, task_dir.joinpath(*rel))
        if res is not None and res.is_file():
            return True
    return False


def _check_main_seat_tokens_map(
    raw_tokens: str,
    seats: list[str],
    seat_envs: dict[str, dict[str, str]],
    rel_compose: str,
    emit: EmitFn,
) -> None:
    def bad(msg: str) -> None:
        emit("seat_tokens_map_mismatch", "error", "seat_token_contract", msg, rel_compose)

    try:
        parsed = json.loads(raw_tokens, parse_constant=_reject_nonfinite_json)
    except (ValueError, RecursionError):
        bad("Service 'main' sets malformed JSON in SEAT_TOKENS")
        return
    if (
        not isinstance(parsed, dict)
        or not parsed
        or not all(
            isinstance(k, str) and bool(k.strip()) and isinstance(v, str) and bool(v.strip())
            for k, v in parsed.items()
        )
    ):
        bad("Service 'main' SEAT_TOKENS must be a non-empty {token: seat} JSON mapping")
        return
    if set(parsed.values()) != set(seats) or len(parsed) != len(seats):
        bad(f"Service 'main' SEAT_TOKENS values {sorted(parsed.values())} != seats {seats}")
        return
    for s, s_env in sorted(seat_envs.items()):
        stok = s_env.get("SEAT_TOKEN", "").strip()
        if not stok or parsed.get(stok) != s:
            bad(f"Seat '{s}' SEAT_TOKEN does not map to '{s}' in main SEAT_TOKENS")
            return


def lint_seat_token_contract(
    task_dir: Path,
    compose: dict[str, Any] | None,
    rel_compose: str | None,
    declared_seats: list[str],
    emit: EmitFn,
) -> None:
    services = compose.get("services") if isinstance(compose, dict) else None
    if not isinstance(services, dict) or rel_compose is None:
        return
    seats = [s for s in declared_seats if s != "main"]
    main_env = parse_compose_env(as_dict(services.get("main")).get("environment"))
    seat_envs: dict[str, dict[str, str]] = {}
    tok_holders: dict[str, list[str]] = {}
    for s in seats:
        svc = services.get(s)
        if not isinstance(svc, dict):
            continue
        s_env = parse_compose_env(svc.get("environment"))
        seat_envs[s] = s_env
        if "SEAT_TOKENS" in s_env:
            msg = f"Seat '{s}' must not define SEAT_TOKENS (leaks peer credentials)"
            emit("seat_leaks_seat_tokens", "error", "seat_token_contract", msg, rel_compose)
        if "WORLD_HMAC_KEY" in s_env:
            msg = f"Seat '{s}' must not define WORLD_HMAC_KEY"
            emit("seat_leaks_hmac_key", "error", "seat_token_contract", msg, rel_compose)
        if "SEAT" in s_env and s_env["SEAT"].strip() != s:
            msg = f"Seat '{s}' sets SEAT='{s_env['SEAT'].strip()}' (expected '{s}')"
            emit("mismatched_seat_env", "error", "seat_token_contract", msg, rel_compose)
        if "SEAT_TOKEN" in s_env:
            tok = s_env["SEAT_TOKEN"].strip()
            if not tok:
                msg = f"Seat '{s}' sets empty SEAT_TOKEN"
                emit("duplicate_seat_token", "error", "seat_token_contract", msg, rel_compose)
            else:
                tok_holders.setdefault(tok, []).append(s)

    for _tok, holders in sorted(tok_holders.items()):
        if len(holders) > 1:
            msg = f"Seats {', '.join(holders)} share identical SEAT_TOKEN"
            emit("duplicate_seat_token", "error", "seat_token_contract", msg, rel_compose)

    if "SEAT_TOKENS" in main_env:
        _check_main_seat_tokens_map(main_env["SEAT_TOKENS"], seats, seat_envs, rel_compose, emit)
    elif tok_holders:
        msg = "Seats define SEAT_TOKEN while service 'main' omits SEAT_TOKENS"
        emit("seat_tokens_map_mismatch", "error", "seat_token_contract", msg, rel_compose)
    elif seats and has_world_server_py(task_dir):
        msg = "World server task requires SEAT_TOKENS on 'main' and SEAT_TOKEN on every seat"
        emit("missing_seat_tokens", "error", "seat_token_contract", msg, rel_compose)


def _effective_compose_networks(svc: dict[str, Any]) -> set[str]:
    if str(svc.get("network_mode") or "").strip():
        return set()
    raw = svc.get("networks")
    if isinstance(raw, (list, dict)):
        parsed = {s for n in raw if n is not None and (s := str(n).strip())}
        if parsed:
            return parsed
    return {"default"}


def _depends_on_main(svc: dict[str, Any]) -> bool:
    dep = svc.get("depends_on")
    if isinstance(dep, list):
        return "main" in {str(x).strip() for x in dep if x is not None}
    if isinstance(dep, dict):
        return "main" in {str(k).strip() for k in dep}
    return False


def lint_compose_isolation_contracts(
    task_dir: Path,
    toml_data: dict[str, Any],
    compose: dict[str, Any] | None,
    rel_compose: str | None,
    declared_seats: list[str],
    net_aliases: dict[str, str],
    emit: EmitFn,
) -> None:
    services = compose.get("services") if isinstance(compose, dict) else None
    if not isinstance(services, dict) or rel_compose is None:
        return
    seats = [s for s in declared_seats if s != "main"]
    main_svc = services.get("main")
    main_raw = _effective_compose_networks(main_svc) if isinstance(main_svc, dict) else set()
    main_eff = {net_aliases.get(n, n) for n in main_raw}
    seat_net_keys: set[str] = set()
    for s in seats:
        svc = services.get(s)
        if not isinstance(svc, dict):
            continue
        s_raw = _effective_compose_networks(svc)
        seat_net_keys.update(s_raw)
        s_eff = {net_aliases.get(n, n) for n in s_raw}
        if _depends_on_main(svc) and main_eff and s_eff and main_eff.isdisjoint(s_eff):
            msg = f"Seat '{s}' depends_on 'main' with disjoint networks ({sorted(s_raw)})"
            emit("seat_isolated_from_main", "error", "isolation_hazards", msg, rel_compose)

    meta_cfg = as_dict(toml_data.get("metadata"))
    agent_cfg = as_dict(toml_data.get("agent"))
    allow_public = (
        meta_cfg.get("internet") is True
        or str(agent_cfg.get("network_mode") or "").strip().lower() == "public"
    )
    top_nets = compose.get("networks") if isinstance(compose, dict) else None
    if has_world_server_py(task_dir) and not allow_public:
        for nk in sorted(seat_net_keys):
            ncfg = top_nets.get(nk) if isinstance(top_nets, dict) else None
            if not isinstance(ncfg, dict) or ncfg.get("internal") is not True:
                msg = f"Seat network '{nk}' lacks internal: true"
                emit("seat_network_not_internal", "warn", "isolation_hazards", msg, rel_compose)


def _check_instance_silent_consistency(
    toml_data: dict[str, Any],
    instance: dict[str, Any],
    inst_rel: str,
    seat_inst_texts: dict[str, tuple[str, str]],
    emit: EmitFn,
) -> None:
    meta_cfg = as_dict(toml_data.get("metadata"))
    is_silent = any(
        isinstance(c, str) and c.strip().lower().startswith(("indep", "silent"))
        for c in (meta_cfg.get("condition"), instance.get("condition"))
    )
    silent_flag_on = is_silent and any(
        instance.get(k) is True for k in ("channel", "pledges", "statements")
    )
    m_chan, i_chan = meta_cfg.get("channel"), instance.get("channel")
    chan_contradicts = isinstance(m_chan, bool) and isinstance(i_chan, bool) and m_chan != i_chan
    if silent_flag_on or chan_contradicts:
        msg = f"Silent/independent condition conflicts with enabled channel flags in {inst_rel}"
        emit("silent_condition_channel_enabled", "error", "world_instance_contract", msg, inst_rel)
    if instance.get("channel") is False:
        for seat, (rel_md, md_txt) in sorted(seat_inst_texts.items()):
            if _SILENT_MSG_REF_RE.search(md_txt):
                msg = f"Seat '{seat}' instruction mentions /msg or /msgs while channel=false"
                emit(
                    "silent_instruction_mentions_channel",
                    "warn",
                    "world_instance_contract",
                    msg,
                    rel_md,
                )


def _load_world_instance_json(task_dir: Path, inst_rel: str, emit: EmitFn) -> dict[str, Any] | None:
    inst_path = task_dir / "environment" / "world" / "instance.json"
    if not inst_path.exists() and not inst_path.is_symlink():
        return None
    txt, esc = read_bounded_text(task_dir, inst_path)
    if esc:
        msg = f"{inst_rel} symlink escapes task dir"
        emit("unsafe_path_escape", "error", "isolation_hazards", msg, inst_rel)
        return None
    if txt is None:
        msg = f"{inst_rel} is not a regular file"
        emit("invalid_world_instance_json", "error", "world_instance_contract", msg, inst_rel)
        return None
    try:
        instance = json.loads(txt, parse_constant=_reject_nonfinite_json)
    except (ValueError, RecursionError):
        msg = f"Malformed JSON in {inst_rel}"
        emit("invalid_world_instance_json", "error", "world_instance_contract", msg, inst_rel)
        return None
    if not isinstance(instance, dict):
        msg = f"{inst_rel} must be a JSON object"
        emit("invalid_world_instance_json", "error", "world_instance_contract", msg, inst_rel)
        return None
    return instance


def lint_world_instance_contract(
    task_dir: Path,
    toml_data: dict[str, Any],
    declared_seats: list[str],
    seat_inst_texts: dict[str, tuple[str, str]],
    emit: EmitFn,
) -> dict[str, Any] | None:
    inst_rel = "environment/world/instance.json"
    instance = _load_world_instance_json(task_dir, inst_rel, emit)
    if instance is None:
        return None
    seats = [s for s in declared_seats if s != "main"]
    if seats:
        bad_keys: list[str] = []
        raw_iseats = instance.get("seats")
        if isinstance(raw_iseats, list) and (
            not all(isinstance(x, str) for x in raw_iseats)
            or len(raw_iseats) != len(seats)
            or set(raw_iseats) != set(seats)
        ):
            bad_keys.append("seats")
        for dk in ("holds", "needs"):
            dval = instance.get(dk)
            if isinstance(dval, dict) and set(dval.keys()) != set(seats):
                bad_keys.append(dk)
        if bad_keys:
            msg = f"{inst_rel} {', '.join(bad_keys)} does not match declared seats {seats}"
            emit("instance_seats_mismatch", "error", "world_instance_contract", msg, inst_rel)

    _check_instance_silent_consistency(toml_data, instance, inst_rel, seat_inst_texts, emit)
    return instance


def _lint_policy_dir_permissions(task_dir: Path, emit: EmitFn) -> None:
    pol_dir = task_dir / "policies"
    if not pol_dir.exists() and not pol_dir.is_symlink():
        return
    if safe_contained_path(task_dir, pol_dir) is None:
        msg = "policies/ symlink escapes task dir"
        emit("unsafe_path_escape", "error", "isolation_hazards", msg, "policies")
        return
    if not pol_dir.is_dir():
        return
    for sh in sorted(pol_dir.glob("*.sh"))[:128]:
        rel_sh = f"policies/{sh.name}"
        sh_res = safe_contained_path(task_dir, sh)
        if sh_res is None:
            msg = f"{rel_sh} symlink escapes task dir"
            emit("unsafe_path_escape", "error", "isolation_hazards", msg, rel_sh)
        elif sh_res.is_file() and (sh_res.stat().st_mode & 0o111) == 0:
            msg = f"{rel_sh} lacks executable bit (+x)"
            emit("policy_script_not_executable", "warn", "control_policy_contract", msg, rel_sh)


def lint_control_policy_contract(
    task_dir: Path,
    toml_data: dict[str, Any],
    emit: EmitFn,
) -> None:
    raw_meta = toml_data.get("metadata")
    if isinstance(raw_meta, dict):
        if "oracle_reward" in raw_meta:
            val = raw_meta["oracle_reward"]
            valid = (
                not isinstance(val, bool)
                and isinstance(val, (int, float))
                and math.isfinite(float(val))
                and 0.0 <= float(val) <= 1.0
            )
            if not valid:
                msg = f"[metadata].oracle_reward must be finite in [0.0, 1.0], got {val!r}"
                emit("invalid_oracle_reward", "error", "control_policy_contract", msg, "task.toml")
        for field in ("exploits", "legal_strategies"):
            raw_list = raw_meta.get(field)
            if not isinstance(raw_list, list):
                continue
            for item in raw_list[:128]:
                pname = item.strip() if isinstance(item, str) else ""
                if not pname or "/" in pname or "\\" in pname or pname in (".", ".."):
                    msg = f"[metadata].{field} contains invalid policy name {item!r}"
                    emit(
                        "missing_declared_policy_script",
                        "error",
                        "control_policy_contract",
                        msg,
                        "task.toml",
                    )
                    continue
                rel_p = f"policies/{pname}.sh"
                cand = task_dir / "policies" / f"{pname}.sh"
                res = safe_contained_path(task_dir, cand)
                if res is None and cand.is_symlink():
                    msg = f"{rel_p} symlink escapes task dir"
                    emit("unsafe_path_escape", "error", "isolation_hazards", msg, rel_p)
                if res is None or not res.is_file():
                    msg = f"Missing {rel_p} declared in [metadata].{field}"
                    emit(
                        "missing_declared_policy_script",
                        "error",
                        "control_policy_contract",
                        msg,
                        rel_p,
                    )
    _lint_policy_dir_permissions(task_dir, emit)


def _instance_differentiates_seats(instance: dict[str, Any] | None, seats: list[str]) -> bool:
    if not isinstance(instance, dict):
        return False
    seat_set = set(seats)
    for k, v in list(instance.items())[:128]:
        if (
            isinstance(v, dict)
            and set(v.keys()) == seat_set
            and len({json.dumps(val, sort_keys=True) for val in v.values()}) > 1
        ):
            return True
        if (
            str(k).lower() in ("seeds", "salts", "seat_seeds", "seat_salts")
            and isinstance(v, list)
            and len(v) == len(seats)
            and len({json.dumps(val, sort_keys=True) for val in v}) > 1
        ):
            return True
    return False


def _lint_symmetric_seat_instructions(
    toml_data: dict[str, Any],
    seats: list[str],
    seat_inst_texts: dict[str, tuple[str, str]],
    instance: dict[str, Any] | None,
    emit: EmitFn,
) -> None:
    meta_cond = as_dict(toml_data.get("metadata")).get("condition")
    if not (
        isinstance(meta_cond, str) and meta_cond.strip().lower().startswith(("indep", "silent"))
    ):
        return
    if len(seats) < 2 or not all(s in seat_inst_texts for s in seats):
        return
    if _instance_differentiates_seats(instance, seats):
        return
    ordered = sorted(seats, key=lambda x: (-len(x), x))
    pat = re.compile(
        r"(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(s) for s in ordered) + r")(?![A-Za-z0-9_])"
    )
    normed = {
        pat.sub(
            lambda m, self_s=s: "<SELF>" if m.group(0) == self_s else "<PEER>",
            seat_inst_texts[s][1],
        )
        for s in seats
    }
    if len(normed) == 1:
        msg = "Silent/independent seat instructions are identical after seat substitution"
        emit(
            "symmetric_seat_instructions", "info", "static_security_contract", msg, "instructions/"
        )


def lint_static_security_contract(
    task_dir: Path,
    toml_data: dict[str, Any],
    declared_seats: list[str],
    seat_inst_texts: dict[str, tuple[str, str]],
    instance: dict[str, Any] | None,
    emit: EmitFn,
) -> None:
    env_dir = task_dir / "environment"
    if env_dir.exists() or env_dir.is_symlink():
        py_files, py_escaped = iter_bounded_files(
            task_dir, env_dir, suffixes=frozenset({".py"}), max_files=201
        )
        if len(py_files) > 200:
            msg = "environment/ exceeds 200 Python files (static scan cap exceeded)"
            emit(
                "lint_file_scan_cap_exceeded",
                "error",
                "static_security_contract",
                msg,
                "environment",
            )
        for esc in py_escaped:
            msg = f"Environment path escapes task dir ({esc.name})"
            emit("unsafe_path_escape", "error", "isolation_hazards", msg, "environment")
        root_res = task_dir.resolve()
        for p in py_files[:200]:
            txt, _ = read_bounded_text(task_dir, p)
            if txt is not None and _HARDCODED_SIGN_SECRET_RE.search(txt):
                rel_p = p.resolve().relative_to(root_res).as_posix()
                msg = f"{rel_p} defines a hardcoded signing/secret key literal"
                emit("hardcoded_signing_secret", "warn", "static_security_contract", msg, rel_p)

    wk_path = task_dir / "environment" / "world" / "worldkit.py"
    if wk_path.exists() or wk_path.is_symlink():
        wk_txt, _ = read_bounded_text(task_dir, wk_path)
        if wk_txt is not None and "admin/close" in wk_txt:
            v_py, _ = read_bounded_text(task_dir, task_dir / "tests" / "verify.py")
            t_sh, _ = read_bounded_text(task_dir, task_dir / "tests" / "test.sh")
            combined_v = f"{v_py or ''}\n{t_sh or ''}"
            if "close_world" not in combined_v and "admin/close" not in combined_v:
                msg = "worldkit.py defines /admin/close but verifier omits close_world/admin/close"
                emit(
                    "verifier_missing_world_close",
                    "warn",
                    "static_security_contract",
                    msg,
                    "tests/",
                )

    seats = [s for s in declared_seats if s != "main"]
    _lint_symmetric_seat_instructions(toml_data, seats, seat_inst_texts, instance, emit)
