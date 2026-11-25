from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import inspect
import io
import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

from worldwatch._chain import verify_event_row_chain
from worldwatch._io import read_regular_text, split_text_lines_strict
from worldwatch.model import (
    GENESIS_HASH,
    as_map,
    canonical_json,
    finite_float,
    parse_strict_json,
    strict_int,
)

_MAX_TURN_STEPS = 100_000
_REMOTE_IP, _LOOPBACK_IP = "10.0.0.2", "127.0.0.1"


def _pos_param_names(target: Any) -> list[str]:
    return [
        p.name for p in inspect.signature(target).parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD) and p.name != "self"
    ]


def load_py_module(mod_name: str, path: Path) -> tuple[ModuleType | None, str | None]:
    buf_out, buf_err = io.StringIO(), io.StringIO()
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            return None, f"cannot create module spec for {path.name}"
        module = importlib.util.module_from_spec(spec)
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            spec.loader.exec_module(module)
        return module, None
    except BaseException as exc:
        return None, f"{path.name} import failed: {type(exc).__name__}: {exc}"


def build_sys_paths(family_dir: Path) -> list[str]:
    repo_tasks = Path(__file__).resolve().parents[1] / "muster" / "tasks"
    built_world = _find_built_file(family_dir, "environment/world/worldkit.py")
    built_dir = built_world.parent if built_world is not None else None
    cands = (
        family_dir,
        family_dir / "template",
        *([built_dir] if built_dir is not None else []),
        family_dir.parent / "_kit_v2",
        family_dir.parent / "_kit",
        repo_tasks / "_kit_v2",
        repo_tasks / "_kit",
    )
    out: list[str] = []
    for cand in cands:
        s_cand = str(cand)
        if s_cand not in out and os.path.isdir(cand) and not os.path.islink(cand):
            out.append(s_cand)
    return out


def verify_replayed_chain(events: list[dict[str, Any]], codec_mod: ModuleType | None = None) -> tuple[bool | None, str | None]:
    if not events:
        return None, None
    chain_mode: bool | None = None
    prev = GENESIS_HASH
    has_hmac = False
    for idx, row in enumerate(events, start=1):
        chain_mode, prev, has_hmac, cerr = verify_event_row_chain(
            row, idx, prev, chain_mode, has_hmac, require_seq=True
        )
        if cerr is not None:
            return False, cerr
    if not chain_mode:
        return None, None
    vc_fn = getattr(codec_mod, "verify_chain", None) if codec_mod is not None else None
    if callable(vc_fn):
        res = vc_fn(events)
        if isinstance(res, tuple) and len(res) >= 2 and not bool(res[0]):
            return False, f"codec.verify_chain failed: {res[1]}"
        if isinstance(res, bool) and not res:
            return False, "codec.verify_chain returned False"
    return True, None


def parse_jsonl_events(raw_bytes: bytes) -> tuple[list[dict[str, Any]] | None, str | None]:
    try:
        lines = split_text_lines_strict(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return None, f"invalid JSONL encoding/newlines: {exc}"
    rows: list[dict[str, Any]] = []
    for idx, line in enumerate(lines, start=1):
        if not line:
            return None, f"line {idx}: blank line in events.jsonl"
        try:
            obj = parse_strict_json(line)
        except ValueError as exc:
            return None, f"line {idx}: {exc}"
        if not isinstance(obj, dict):
            return None, f"line {idx}: JSON row is not an object"
        rows.append(obj)
    return rows, None


def encode_events_jsonl(events: list[dict[str, Any]]) -> bytes:
    return "".join(canonical_json(row) + "\n" for row in events).encode("utf-8")


def call_verify_score(verify_mod: ModuleType, events: list[dict[str, Any]], inst: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    score_fn = getattr(verify_mod, "score", None) or getattr(verify_mod, "grade", None)
    if not callable(score_fn):
        return None, "verify module has no callable score() or grade()"
    buf_out, buf_err = io.StringIO(), io.StringIO()
    try:
        sig = inspect.signature(score_fn)
        has_var = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            raw = score_fn(events, dict(inst)) if (len(_pos_param_names(score_fn)) >= 2 or has_var) else score_fn(events)
    except BaseException as exc:
        return None, f"verify.score failed: {type(exc).__name__}: {exc}"
    if not isinstance(raw, Mapping):
        return None, f"verify.score returned {type(raw).__name__}, expected Mapping"
    if finite_float(raw.get("reward")) is None:
        return None, f"verify.score missing finite 'reward': {raw.get('reward')!r}"
    return dict(raw), None


def rewards_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if set(left.keys()) != set(right.keys()):
        return False
    for key, lval in left.items():
        rval = right[key]
        if isinstance(lval, bool) or isinstance(rval, bool):
            if lval is not rval:
                return False
            continue
        lf, rf = finite_float(lval), finite_float(rval)
        if lf is not None and rf is not None:
            if abs(lf - rf) > 1e-12:
                return False
            continue
        try:
            if canonical_json(lval) != canonical_json(rval):
                return False
        except ValueError:
            return False
    return True


def _find_built_file(family_dir: Path, rel_tail: str) -> Path | None:
    build_dir = family_dir / "build"
    if not os.path.isdir(build_dir) or os.path.islink(build_dir):
        return None
    with contextlib.suppress(OSError):
        for c_ent in sorted(os.scandir(build_dir), key=lambda e: e.name)[:64]:
            if not c_ent.is_dir(follow_symlinks=False):
                continue
            for t_ent in sorted(os.scandir(c_ent.path), key=lambda e: e.name)[:64]:
                if not t_ent.is_dir(follow_symlinks=False):
                    continue
                cand = Path(t_ent.path) / rel_tail
                if os.path.isfile(cand) and not os.path.islink(cand):
                    return cand
    return None


def discover_family_modules(family_dir: Path) -> tuple[dict[str, ModuleType | None], str | None]:
    tag = abs(hash(str(family_dir.resolve())))
    mods: dict[str, ModuleType | None] = {"ref": None, "world": None, "verify": None, "policies": None, "codec": None, "worldkit": None}
    ref_p = family_dir / "reference.py"
    if os.path.isfile(ref_p) and not os.path.islink(ref_p):
        rmod, rerr = load_py_module(f"_ww_audit_ref_{tag}", ref_p)
        if rerr is not None:
            return mods, rerr
        mods["ref"] = rmod
        for attr in ("world", "verify", "codec", "worldkit"):
            if isinstance(getattr(rmod, attr, None), ModuleType):
                mods[attr] = getattr(rmod, attr)
    search_spec = (
        ("world", (family_dir / "template" / "world.py", family_dir / "world.py", _find_built_file(family_dir, "environment/world/world.py"))),
        ("verify", (family_dir / "template" / "verify.py", family_dir / "verify.py", _find_built_file(family_dir, "tests/verify.py"))),
        ("policies", (family_dir / "template" / "policies.py", family_dir / "policies.py")),
        ("codec", (_find_built_file(family_dir, "environment/world/codec.py"),)),
        ("worldkit", (_find_built_file(family_dir, "environment/world/worldkit.py"),)),
    )
    for key, cands in search_spec:
        if mods[key] is not None:
            continue
        for cp in cands:
            if cp is not None and os.path.isfile(cp) and not os.path.islink(cp):
                loaded, lerr = load_py_module(f"_ww_audit_{key}_{tag}", cp)
                if lerr is not None:
                    return mods, lerr
                mods[key] = loaded
                break
    for holder in (mods["world"], mods["worldkit"]):
        if holder is not None and mods["codec"] is None and isinstance(c_mod := getattr(holder, "codec", None), ModuleType):
            mods["codec"] = c_mod
    if mods["verify"] is None:
        return mods, "missing verify.py module for score() evaluation"
    if mods["ref"] is None and mods["world"] is None:
        return mods, "missing reference.py and world.py for in-memory replay"
    return mods, None


def resolve_instances(config: Mapping[str, Any], condition: str, family_dir: Path, ref_mod: ModuleType | None) -> tuple[list[dict[str, Any]], str | None]:
    inst_fn = getattr(ref_mod, "instances", None) if ref_mod is not None else None
    if callable(inst_fn):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                raw = inst_fn(dict(config), condition) if len(_pos_param_names(inst_fn)) >= 2 else inst_fn(condition)
            if isinstance(raw, list) and raw and all(isinstance(x, Mapping) for x in raw):
                return [dict(x) for x in raw[:1000]], None
        except BaseException as exc:
            return [], f"instances({condition!r}) failed: {type(exc).__name__}: {exc}"
    cond_build = family_dir / "build" / condition
    if os.path.isdir(cond_build) and not os.path.islink(cond_build):
        built: list[dict[str, Any]] = []
        for entry in sorted(os.scandir(cond_build), key=lambda e: e.name)[:1000]:
            if not entry.is_dir(follow_symlinks=False):
                continue
            txt, err = read_regular_text(Path(entry.path) / "environment" / "world" / "instance.json", root=family_dir)
            if err is None and txt is not None and isinstance((parsed := parse_strict_json(txt)), dict):
                built.append(parsed)
        if built:
            return built, None
    spec = as_map(as_map(config.get("conditions")).get(condition))
    n_inst, fam_name = strict_int(config.get("instances")) or 1, str(config.get("name") or family_dir.name)
    seats = [str(s) for s in (spec.get("seats") or ["p1"]) if isinstance(s, str)]
    return [
        {**dict(spec), "family": fam_name, "condition": condition, "instance": idx, "seats": seats}
        for idx in range(1, max(1, min(n_inst, 1000)) + 1)
    ], None


def resolve_seat_tokens(config: Mapping[str, Any], inst: Mapping[str, Any], ref_mod: ModuleType | None) -> dict[str, str]:
    tok_fn = getattr(ref_mod, "tokens", None) if ref_mod is not None else None
    if callable(tok_fn):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with contextlib.suppress(BaseException), contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            res = tok_fn(dict(config), dict(inst)) if len(_pos_param_names(tok_fn)) >= 2 else tok_fn(dict(inst))
            if isinstance(res, Mapping) and res:
                return {str(k): str(v) for k, v in res.items()}
    seats = [str(s) for s in (inst.get("seats") or ["p1"])]
    seed, cond, inum = str(config.get("seed") or config.get("name") or "audit"), str(inst.get("condition") or "default"), inst.get("instance", 1)
    return {s: hashlib.sha256(f"{seed}/{cond}/{inum}/{s}".encode()).hexdigest()[:32] for s in seats}


def _find_world_factory(world_mod: ModuleType) -> Any:
    for fn_name in ("make_world", "create_world", "build_world"):
        if callable(cand := getattr(world_mod, fn_name, None)):
            return cand
    base_cls = getattr(world_mod, "World", None)
    for _, obj in inspect.getmembers(world_mod, inspect.isclass):
        if (
            obj.__module__ == world_mod.__name__
            and obj is not base_cls
            and ((isinstance(base_cls, type) and issubclass(obj, base_cls)) or hasattr(obj, "dispatch") or hasattr(obj, "handle"))
        ):
            return obj
    return base_cls


def _instantiate_world(world_mod: ModuleType, inst: dict[str, Any], seat_tokens: dict[str, str], events_path: Path) -> Any:
    target = _find_world_factory(world_mod)
    if target is None:
        raise RuntimeError("no World class or make_world factory found in world.py")
    pnames = _pos_param_names(target)
    tok_to_seat = {tok: seat for seat, tok in seat_tokens.items()}
    for tok_arg in (seat_tokens if "seat_tokens" in pnames else tok_to_seat, seat_tokens, tok_to_seat):
        args_list: list[Any] = []
        for pname in pnames:
            if pname in ("instance", "inst", "sample", "spec", "config"):
                args_list.append(inst)
            elif pname in ("tokens", "seat_tokens", "token_map", "token_to_seat"):
                args_list.append(dict(tok_arg))
            elif pname in ("events", "events_path", "log_path", "path"):
                args_list.append(events_path)
        with contextlib.suppress(BaseException):
            w_obj = target(*args_list)
            w_seats = getattr(w_obj, "seats", None)
            if isinstance(w_seats, (list, tuple, set)) and set(w_seats) == set(seat_tokens.values()):
                continue
            return w_obj
    return target(inst, tok_to_seat, events_path)


def _close_world_obj(world_obj: Any) -> None:
    if getattr(world_obj, "closed", False) is True:
        return
    for mname in ("close_world", "admin_close", "close"):
        if callable(fn := getattr(world_obj, mname, None)):
            with contextlib.suppress(TypeError):
                fn()
                if getattr(world_obj, "closed", False) is True:
                    return
    if callable(handle_fn := getattr(world_obj, "handle", None)):
        handle_fn("POST", "/admin/close", None, {}, _LOOPBACK_IP)
        return
    if callable(disp_fn := getattr(world_obj, "dispatch", None)):
        with contextlib.suppress(BaseException):
            disp_fn(None, "POST", "/admin/close", {})


def _drive_world_policy(
    world_obj: Any, inst: dict[str, Any], seat_tokens: dict[str, str], policy: str, pol_map: Mapping[str, Any]
) -> dict[str, Any] | None:
    disp_fn, handle_fn = getattr(world_obj, "dispatch", None), getattr(world_obj, "handle", None)

    def call_world(seat: str | None, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        if callable(disp_fn):
            return disp_fn(seat, method, path, body if body is not None else {})
        if callable(handle_fn):
            return handle_fn(method, path, seat_tokens.get(seat) if seat else None, body, _REMOTE_IP if seat else _LOOPBACK_IP)
        raise RuntimeError("world object has neither dispatch() nor handle()")

    if policy == "nop" and "nop" not in pol_map:
        _close_world_obj(world_obj)
        return None
    if policy not in pol_map:
        raise KeyError(f"policy {policy!r} not found in policy map")
    entry, atif_out = pol_map[policy], None
    if isinstance(entry, (list, tuple)):
        for s_seat, s_meth, s_path, s_body in entry[:_MAX_TURN_STEPS]:
            call_world(s_seat, s_meth, s_path, s_body)
    elif callable(entry):
        n_pos = len(_pos_param_names(entry))
        res = entry(world_obj, inst, seat_tokens) if n_pos >= 3 else (entry(world_obj, inst) if n_pos == 2 else entry(world_obj))
        if isinstance(res, Mapping) and all(isinstance(v, Iterator) for v in res.values()):
            active = dict(res)
            for turn in range(_MAX_TURN_STEPS):
                if not active:
                    break
                s_keys = sorted(active)
                chosen = s_keys[turn % len(s_keys)]
                if next(active[chosen], False) is False:
                    del active[chosen]
        elif isinstance(res, Iterator):
            for _ in range(_MAX_TURN_STEPS):
                if next(res, False) is False:
                    break
        elif isinstance(res, Mapping) and isinstance(res.get("atif"), Mapping):
            atif_out = dict(res["atif"])
    _close_world_obj(world_obj)
    return atif_out


def _invoke_family_runner(
    mods: Mapping[str, ModuleType | None], inst: dict[str, Any], seat_tokens: dict[str, str],
    policy: str, pol_map: Mapping[str, Any], ep_dir: Path,
) -> tuple[list[dict[str, Any]], bytes, Any, dict[str, Any] | None]:
    ref_mod, buf_out, buf_err = mods.get("ref"), io.StringIO(), io.StringIO()
    for runner_name in ("replay", "trial", "play"):
        fn = getattr(ref_mod, runner_name, None) if ref_mod is not None else None
        if not callable(fn):
            continue
        try:
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                out = fn(dict(inst), dict(seat_tokens), policy)
        except KeyError:
            if policy == "nop":
                break
            raise
        if isinstance(out, Mapping):
            evs = list(out["events"])
            raw_b = out["raw_bytes"] if isinstance(out.get("raw_bytes"), bytes) else encode_events_jsonl(evs)
            return evs, raw_b, out.get("world"), (dict(out["atif"]) if isinstance(out.get("atif"), Mapping) else None)
        if isinstance(out, tuple) and len(out) >= 2:
            evs = list(out[0])
            return evs, (out[1] if isinstance(out[1], bytes) else encode_events_jsonl(evs)), (out[2] if len(out) >= 3 else None), None
        if isinstance(out, list):
            return list(out), encode_events_jsonl(out), None, None
    world_mod = mods.get("world")
    if world_mod is None:
        raise RuntimeError(f"cannot replay policy {policy!r}: no world module or trial/play runner")
    ev_path = ep_dir / "events.jsonl"
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        w_obj = _instantiate_world(world_mod, dict(inst), seat_tokens, ev_path)
        atif_map = _drive_world_policy(w_obj, dict(inst), seat_tokens, policy, pol_map)
    raw_mem: Any = getattr(w_obj, "events", None) or getattr(w_obj, "event_log", None) or []
    mem_evs: list[dict[str, Any]] = [dict(x) for x in raw_mem if isinstance(x, Mapping)]
    if os.path.isfile(ev_path):
        raw_bytes = ev_path.read_bytes()
        if mem_evs:
            return mem_evs, raw_bytes, w_obj, atif_map
        parsed_evs, perr = parse_jsonl_events(raw_bytes)
        if perr is not None or parsed_evs is None:
            raise ValueError(perr or "failed to parse world events.jsonl")
        return parsed_evs, raw_bytes, w_obj, atif_map
    return mem_evs, encode_events_jsonl(mem_evs), w_obj, atif_map


def _check_roundtrip_parity(
    verify_mod: ModuleType, inst: Mapping[str, Any], score_direct: Mapping[str, Any], rt_bytes: bytes, raw_reload: list[dict[str, Any]] | None
) -> tuple[bool, str | None]:
    rt_events, rt_parse_err = parse_jsonl_events(rt_bytes)
    if rt_parse_err is not None or rt_events is None:
        return False, rt_parse_err or "round-trip JSONL parse failed"
    score_rt, sc_rt_err = call_verify_score(verify_mod, rt_events, inst)
    if sc_rt_err is not None or score_rt is None:
        return False, sc_rt_err or "round-trip verify.score failed"
    if not rewards_equal(score_direct, score_rt):
        return False, "verify.score JSONL round-trip mismatch"
    if raw_reload is not None and raw_reload != rt_events:
        score_raw, sc_raw_err = call_verify_score(verify_mod, raw_reload, inst)
        if sc_raw_err is not None or score_raw is None or not rewards_equal(score_direct, score_raw):
            return False, sc_raw_err or "verify.score raw JSONL reload mismatch"
    return True, None


def replay_single_episode(
    mods: Mapping[str, ModuleType | None], inst: dict[str, Any], seat_tokens: dict[str, str], policy: str, pol_map: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    verify_mod = mods.get("verify")
    assert verify_mod is not None
    with tempfile.TemporaryDirectory(prefix="ww-audit-ep-") as tmp:
        try:
            events, raw_bytes, w_obj, atif_map = _invoke_family_runner(mods, inst, seat_tokens, policy, pol_map, Path(tmp))
        except BaseException as exc:
            return None, f"replay failed ({policy}): {type(exc).__name__}: {exc}"
    w_mod = mods.get("world")
    used_dispatch = (
        callable(getattr(w_obj, "dispatch", None))
        if w_obj is not None
        else (callable(getattr(_find_world_factory(w_mod), "dispatch", None)) if w_mod is not None else False)
    )
    log_intact = bool(getattr(w_obj, "log_intact", True)) if w_obj is not None else True
    wk_intact_fn = getattr(mods.get("worldkit"), "log_intact", None) if mods.get("worldkit") is not None else None
    if callable(wk_intact_fn) and raw_bytes:
        with contextlib.suppress(TypeError):
            log_intact = log_intact and bool(wk_intact_fn(raw_bytes))
    chain_ok, chain_err = verify_replayed_chain(events, mods.get("codec"))
    raw_reload, raw_err = parse_jsonl_events(raw_bytes) if raw_bytes else (events, None)
    if raw_err is not None or raw_reload is None:
        chain_ok, chain_err = False, raw_err or "corrupt raw JSONL"
    elif chain_ok is not False and (r_chk := verify_replayed_chain(raw_reload, mods.get("codec")))[0] is False:
        chain_ok, chain_err = False, r_chk[1]
    rt_bytes = encode_events_jsonl(events)
    score_direct, sc_err = call_verify_score(verify_mod, events, inst)
    if sc_err is not None or score_direct is None:
        return None, sc_err or "verify.score failed"
    if finite_float(score_direct.get("chain_ok")) == 0.0 or score_direct.get("chain_ok") is False:
        chain_ok, chain_err = False, chain_err or "verify.score reported chain_ok=0"
    roundtrip_ok, rt_err = _check_roundtrip_parity(verify_mod, inst, score_direct, rt_bytes, raw_reload)
    return {
        "events": events, "raw_bytes": raw_bytes if raw_bytes else rt_bytes, "atif": atif_map,
        "score": score_direct, "reward": float(score_direct["reward"]), "log_intact": log_intact,
        "chain_ok": chain_ok, "chain_error": chain_err, "roundtrip_ok": roundtrip_ok,
        "roundtrip_error": rt_err, "used_dispatch": bool(used_dispatch),
    }, None


def write_synthetic_trial(
    tdir: Path, family_name: str, inst: Mapping[str, Any], policy: str,
    raw_bytes: bytes, score_map: Mapping[str, Any], task_path: Path, atif_by_seat: Mapping[str, Any] | None = None,
) -> None:
    seats, inum = [str(s) for s in (inst.get("seats") or ["p1"])], strict_int(inst.get("instance")) or 1
    (tdir / "verifier").mkdir(parents=True, exist_ok=True)
    (tdir / "verifier" / "events.jsonl").write_bytes(raw_bytes)
    (tdir / "verifier" / "reward.json").write_text(json.dumps(dict(score_map)) + "\n", encoding="utf-8")
    ag_label = "oracle" if policy == "oracle" else ("nop" if policy == "nop" else "script")
    cfg_doc = {
        "trial_name": tdir.name, "task": {"name": f"muster/{family_name}-i{inum:02d}", "path": str(task_path)},
        "participants": [{"seat": s, "name": ag_label} for s in seats],
    }
    (tdir / "config.json").write_text(json.dumps(cfg_doc) + "\n", encoding="utf-8")
    sr_list = []
    for s in seats:
        s_dir = tdir / "seats" / s / "agent"
        s_dir.mkdir(parents=True, exist_ok=True)
        (s_dir / "exit-code.txt").write_text("0\n", encoding="utf-8")
        if atif_by_seat and isinstance(atif_by_seat.get(s), Mapping):
            (s_dir / "trajectory.json").write_text(json.dumps(dict(atif_by_seat[s])) + "\n", encoding="utf-8")
        sr_list.append({"seat": s, "under_test": True, "agent_info": {"name": ag_label}, "exit_code": 0})
    res_doc = {
        "trial_name": tdir.name, "task_name": f"muster/{family_name}-i{inum:02d}", "config": cfg_doc,
        "seat_results": sr_list, "verifier_result": {"rewards": dict(score_map)}, "reward_valid": True,
    }
    (tdir / "result.json").write_text(json.dumps(res_doc) + "\n", encoding="utf-8")
