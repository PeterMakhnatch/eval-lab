from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import inspect
import io
import json
import os
import sys
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

from worldwatch._chain import verify_event_row_chain
from worldwatch._io import (
    check_no_symlink_ancestors,
    read_regular_text,
    split_text_lines_strict,
    stat_regular_file,
)
from worldwatch.model import (
    GENESIS_HASH,
    SEAT_NAME_RE,
    as_map,
    canonical_json,
    finite_float,
    parse_strict_json,
    strict_int,
)

_MAX_TURN_STEPS, _MAX_SCAN_ENTRIES, _MAX_REWARD_ABS = 100_000, 8_000, 1e9
_REMOTE_IP, _LOOPBACK_IP = "10.0.0.2", "127.0.0.1"
_KIT_V2_MARKERS = ("_kit_v2", "import codec", "from codec", '"codec"', "'codec'", "self.route(", ".dispatch(")


def is_safe_ident(value: Any) -> bool:
    return isinstance(value, str) and bool(SEAT_NAME_RE.fullmatch(value)) and "/" not in value and "\\" not in value and value not in (".", "..")


def _pos_param_names(target: Any) -> list[str]:
    return [p.name for p in inspect.signature(target).parameters.values() if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD) and p.name != "self"]


def scan_family_symlinks(family_dir: Path) -> str | None:
    fdir = Path(os.path.abspath(family_dir))
    for rel in ("family.toml", "reference.py", "generate.py", "policies.py", "world.py", "verify.py", "codec.py", "worldkit.py"):
        if os.path.islink(fdir / rel):
            return f"{rel} is a symlink"
    stack: list[tuple[Path, int]] = []
    for sub_nm in ("template", "build"):
        sub = fdir / sub_nm
        if os.path.islink(sub):
            return f"{sub_nm} is a symlink"
        if os.path.isdir(sub):
            stack.append((sub, 1))
    for _ in range(_MAX_SCAN_ENTRIES):
        if not stack:
            break
        cur, depth = stack.pop()
        try:
            entries = sorted(os.scandir(cur), key=lambda e: e.name)
        except OSError as exc:
            return f"cannot scan {cur.relative_to(fdir)}: {exc.strerror or exc}"
        for ent in entries[:_MAX_SCAN_ENTRIES]:
            if ent.is_symlink():
                return f"{Path(ent.path).relative_to(fdir)} is a symlink"
            if depth < 6 and ent.is_dir(follow_symlinks=False):
                stack.append((Path(ent.path), depth + 1))
    return None


def load_py_module(mod_name: str, path: Path, *, root: Path | None = None) -> tuple[ModuleType | None, str | None]:
    froot = Path(os.path.abspath(root)) if root is not None else None
    p_abs = Path(os.path.abspath(path))
    info, serr = stat_regular_file(p_abs, root=froot)
    if serr is not None or info is None:
        return None, serr or f"missing module {p_abs.name}"
    if froot is not None and not p_abs.resolve().is_relative_to(froot.resolve()):
        return None, f"{p_abs.name} escapes family root"
    buf_out, buf_err = io.StringIO(), io.StringIO()
    had_prev, prev_mod = (mod_name in sys.modules), sys.modules.get(mod_name)
    try:
        spec = importlib.util.spec_from_file_location(mod_name, p_abs)
        if spec is None or spec.loader is None:
            return None, f"cannot create module spec for {p_abs.name}"
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            spec.loader.exec_module(module)
        return module, None
    except BaseException as exc:
        if had_prev and prev_mod is not None:
            sys.modules[mod_name] = prev_mod
        else:
            sys.modules.pop(mod_name, None)
        return None, f"{p_abs.name} import failed: {type(exc).__name__}: {exc}"


def _find_built_file(family_dir: Path, rel_tail: str) -> Path | None:
    fdir, build_dir = Path(os.path.abspath(family_dir)), Path(os.path.abspath(family_dir)) / "build"
    if check_no_symlink_ancestors(build_dir, root=fdir) is not None or not os.path.isdir(build_dir) or os.path.islink(build_dir):
        return None
    real_fam = fdir.resolve()
    with contextlib.suppress(OSError):
        for c_ent in sorted(os.scandir(build_dir), key=lambda e: e.name)[:64]:
            if not c_ent.is_symlink() and c_ent.is_dir(follow_symlinks=False):
                for t_ent in sorted(os.scandir(c_ent.path), key=lambda e: e.name)[:64]:
                    if not t_ent.is_symlink() and t_ent.is_dir(follow_symlinks=False):
                        cand = Path(t_ent.path) / rel_tail
                        info, err = stat_regular_file(cand, root=fdir)
                        if err is None and info is not None and cand.resolve().is_relative_to(real_fam):
                            return cand
    return None


def _uses_kit_v2(fdir: Path) -> bool:
    if _find_built_file(fdir, "environment/world/codec.py") is not None:
        return True
    if any((p := stat_regular_file(fdir / r, root=fdir))[1] is None and p[0] is not None for r in ("codec.py", "template/codec.py")):
        return True
    for rel in ("reference.py", "generate.py", "policies.py", "template/policies.py", "world.py", "template/world.py", "verify.py", "template/verify.py"):
        txt, err = read_regular_text(fdir / rel, root=fdir)
        if err is None and txt is not None and any(m in txt for m in _KIT_V2_MARKERS):
            return True
    return False


def build_sys_paths(family_dir: Path) -> list[str]:
    fdir = Path(os.path.abspath(family_dir))
    real_fam, repo_tasks = fdir.resolve(), Path(__file__).resolve().parents[1] / "muster" / "tasks"
    built_world = _find_built_file(fdir, "environment/world/worldkit.py")
    local_cands = [fdir, fdir / "template", *([built_world.parent] if built_world is not None else [])]
    v2_pair, v1_pair = (fdir.parent / "_kit_v2", repo_tasks / "_kit_v2"), (fdir.parent / "_kit", repo_tasks / "_kit")
    kit_cands = (*v2_pair, *v1_pair) if _uses_kit_v2(fdir) else (*v1_pair, *v2_pair)
    out: list[str] = []
    for cand in local_cands:
        if check_no_symlink_ancestors(cand, root=fdir) is None and os.path.isdir(cand) and not os.path.islink(cand) and cand.resolve().is_relative_to(real_fam) and str(cand) not in out:
            out.append(str(cand))
    for cand in kit_cands:
        k_abs = Path(os.path.abspath(cand))
        if check_no_symlink_ancestors(k_abs) is None and os.path.isdir(k_abs) and not os.path.islink(k_abs) and str(k_abs) not in out:
            out.append(str(k_abs))
    return out


def verify_replayed_chain(events: list[dict[str, Any]], codec_mod: ModuleType | None = None) -> tuple[bool | None, str | None]:
    if not events:
        return None, "event log is empty"
    chain_mode, prev, has_hmac = None, GENESIS_HASH, False
    for idx, row in enumerate(events, start=1):
        chain_mode, prev, has_hmac, cerr = verify_event_row_chain(row, idx, prev, chain_mode, has_hmac, require_seq=True)
        if cerr is not None:
            return False, cerr
    if not chain_mode:
        return None, "event log is unhashed"
    if callable(vc_fn := getattr(codec_mod, "verify_chain", None) if codec_mod is not None else None):
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


def _reverse_dict_keys_deep(root: Any) -> Any:
    stack, built = [(root, False)], {}
    for _ in range(500_000):
        if not stack:
            break
        node, ready = stack.pop()
        if isinstance(node, Mapping):
            if ready:
                built[id(node)] = {k: built.get(id(v), v) for k, v in reversed(list(node.items()))}
            else:
                stack.extend([(node, True), *((v, False) for v in node.values() if isinstance(v, (Mapping, list, tuple)))])
        elif isinstance(node, (list, tuple)):
            if ready:
                seq = [built.get(id(v), v) for v in node]
                built[id(node)] = tuple(seq) if isinstance(node, tuple) else seq
            else:
                stack.extend([(node, True), *((v, False) for v in node if isinstance(v, (Mapping, list, tuple)))])
    assert not stack
    return built.get(id(root), root)


def call_verify_score(verify_mod: ModuleType, events: list[dict[str, Any]], inst: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    score_fn = getattr(verify_mod, "score", None) or getattr(verify_mod, "grade", None)
    if not callable(score_fn):
        return None, "verify module has no callable score() or grade()"
    buf_out, buf_err, evs_copy, inst_copy = io.StringIO(), io.StringIO(), copy.deepcopy(events), copy.deepcopy(dict(inst))
    try:
        has_var = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in inspect.signature(score_fn).parameters.values())
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            raw = score_fn(evs_copy, inst_copy) if (len(_pos_param_names(score_fn)) >= 2 or has_var) else score_fn(evs_copy)
    except BaseException as exc:
        return None, f"verify.score failed: {type(exc).__name__}: {exc}"
    if not isinstance(raw, Mapping):
        return None, f"verify.score returned {type(raw).__name__}, expected Mapping"
    if (rew_val := finite_float(raw.get("reward"))) is None or abs(rew_val) > _MAX_REWARD_ABS:
        return None, f"verify.score missing bounded finite 'reward': {raw.get('reward')!r}"
    return dict(raw), None


def rewards_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if set(left.keys()) != set(right.keys()):
        return False
    for key, lval in left.items():
        rval = right[key]
        if isinstance(lval, bool) or isinstance(rval, bool):
            if lval is not rval:
                return False
        elif (lf := finite_float(lval)) is not None and (rf := finite_float(rval)) is not None:
            if abs(lf - rf) > 1e-12:
                return False
        else:
            try:
                if canonical_json(lval) != canonical_json(rval):
                    return False
            except ValueError:
                return False
    return True


def discover_family_modules(family_dir: Path) -> tuple[dict[str, ModuleType | None], str | None]:
    fdir = Path(os.path.abspath(family_dir))
    mods: dict[str, ModuleType | None] = {"ref": None, "world": None, "verify": None, "policies": None, "codec": None, "worldkit": None}
    if (sym_err := scan_family_symlinks(fdir)) is not None:
        return mods, f"unsafe symlink in family directory: {sym_err}"
    tag, ref_p = abs(hash(str(fdir.resolve()))), fdir / "reference.py"
    ref_info, ref_serr = stat_regular_file(ref_p, root=fdir)
    if ref_serr is not None:
        return mods, ref_serr
    if ref_info is not None:
        rmod, rerr = load_py_module(f"_ww_audit_ref_{tag}", ref_p, root=fdir)
        if rerr is not None:
            return mods, rerr
        mods["ref"] = rmod
        for attr in ("world", "verify", "codec", "worldkit"):
            if isinstance(getattr(rmod, attr, None), ModuleType):
                mods[attr] = getattr(rmod, attr)
    search_spec = (
        ("world", (fdir / "template" / "world.py", fdir / "world.py", _find_built_file(fdir, "environment/world/world.py"))),
        ("verify", (fdir / "template" / "verify.py", fdir / "verify.py", _find_built_file(fdir, "tests/verify.py"))),
        ("policies", (fdir / "template" / "policies.py", fdir / "policies.py")),
        ("codec", (_find_built_file(fdir, "environment/world/codec.py"),)),
        ("worldkit", (_find_built_file(fdir, "environment/world/worldkit.py"),)),
    )
    for key, cands in search_spec:
        for cp in (() if mods[key] is not None else (c for c in cands if c is not None)):
            cp_info, cp_serr = stat_regular_file(cp, root=fdir)
            if cp_serr is not None:
                return mods, cp_serr
            if cp_info is not None:
                loaded, lerr = load_py_module(f"_ww_audit_{key}_{tag}", cp, root=fdir)
                if lerr is not None:
                    return mods, lerr
                mods[key] = loaded
                break
    for holder in (mods["world"], mods["worldkit"]):
        if holder is not None and mods["codec"] is None and isinstance(c_mod := getattr(holder, "codec", None), ModuleType):
            mods["codec"] = c_mod
    if mods["verify"] is None:
        return mods, "missing verify.py module for score() evaluation"
    return (mods, "missing reference.py and world.py for in-memory replay") if (mods["ref"] is None and mods["world"] is None) else (mods, None)


def _validate_instance_list(raw_list: list[dict[str, Any]], condition: str) -> tuple[list[dict[str, Any]], str | None]:
    clean: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_list[:1000], start=1):
        raw_seats = item.get("seats")
        if not isinstance(raw_seats, list) or not raw_seats:
            return [], f"instance {idx} in {condition!r} has missing or empty seats list"
        seats = [s for s in raw_seats if is_safe_ident(s)]
        if len(seats) != len(raw_seats) or len(set(seats)) != len(seats):
            return [], f"invalid or duplicate seat identifiers in {condition!r}: {raw_seats!r}"
        if (inum := strict_int(item.get("instance"))) is not None and inum < 0:
            return [], f"negative instance index {inum} in {condition!r}"
        clean.append({**item, "seats": seats})
    return clean, None


def resolve_instances(config: Mapping[str, Any], condition: str, family_dir: Path, ref_mod: ModuleType | None) -> tuple[list[dict[str, Any]], str | None]:
    if not is_safe_ident(condition):
        return [], f"invalid condition name: {condition!r}"
    fdir = Path(os.path.abspath(family_dir))
    if callable(inst_fn := getattr(ref_mod, "instances", None) if ref_mod is not None else None):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                raw = inst_fn(dict(config), condition) if len(_pos_param_names(inst_fn)) >= 2 else inst_fn(condition)
            if isinstance(raw, list) and raw and all(isinstance(x, Mapping) for x in raw):
                return _validate_instance_list([dict(x) for x in raw[:1000]], condition)
        except BaseException as exc:
            return [], f"instances({condition!r}) failed: {type(exc).__name__}: {exc}"
    cond_build = fdir / "build" / condition
    if check_no_symlink_ancestors(cond_build, root=fdir) is None and os.path.isdir(cond_build) and not os.path.islink(cond_build):
        built: list[dict[str, Any]] = []
        for entry in sorted(os.scandir(cond_build), key=lambda e: e.name)[:1000]:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            txt, err = read_regular_text(Path(entry.path) / "environment" / "world" / "instance.json", root=fdir)
            if err is None and txt is not None:
                try:
                    parsed = parse_strict_json(txt)
                except ValueError as exc:
                    return [], f"invalid instance.json in {entry.name}: {exc}"
                if isinstance(parsed, dict):
                    built.append(parsed)
        if built:
            return _validate_instance_list(built, condition)
    spec = as_map(as_map(config.get("conditions")).get(condition))
    n_inst, fam_name = strict_int(config.get("instances")) or 1, str(config.get("name") or fdir.name)
    raw_seats = spec.get("seats") if "seats" in spec else ["p1"]
    if not isinstance(raw_seats, list) or not raw_seats:
        return [], f"condition {condition!r} has empty or non-list seats"
    synthesized = [{**dict(spec), "family": fam_name, "condition": condition, "instance": idx, "seats": list(raw_seats)} for idx in range(1, max(1, min(n_inst, 1000)) + 1)]
    return _validate_instance_list(synthesized, condition)


def resolve_seat_tokens(config: Mapping[str, Any], inst: Mapping[str, Any], ref_mod: ModuleType | None) -> dict[str, str]:
    if callable(tok_fn := getattr(ref_mod, "tokens", None) if ref_mod is not None else None):
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
        if obj.__module__ == world_mod.__name__ and obj is not base_cls and ((isinstance(base_cls, type) and issubclass(obj, base_cls)) or hasattr(obj, "dispatch") or hasattr(obj, "handle")):
            return obj
    return base_cls


def _instantiate_world(world_mod: ModuleType, inst: dict[str, Any], seat_tokens: dict[str, str], events_path: Path) -> Any:
    target = _find_world_factory(world_mod)
    if target is None:
        raise RuntimeError("no World class or make_world factory found in world.py")
    pnames, tok_to_seat = _pos_param_names(target), {tok: seat for seat, tok in seat_tokens.items()}
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
    elif callable(disp_fn := getattr(world_obj, "dispatch", None)):
        with contextlib.suppress(BaseException):
            disp_fn(None, "POST", "/admin/close", {})


def _drive_world_policy(world_obj: Any, inst: dict[str, Any], seat_tokens: dict[str, str], policy: str, pol_map: Mapping[str, Any]) -> dict[str, Any] | None:
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
                chosen = sorted(active)[turn % len(active)]
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


def _extract_runner_result(out: Any) -> tuple[list[dict[str, Any]], bytes, Any, dict[str, Any] | None]:
    if isinstance(out, Mapping):
        evs, w_obj, atif = [dict(x) for x in out["events"]], out.get("world"), (dict(out["atif"]) if isinstance(out.get("atif"), Mapping) else None)
        if "raw_bytes" in out:
            if not isinstance(out["raw_bytes"], bytes):
                raise TypeError("runner 'raw_bytes' must be bytes")
            return evs, out["raw_bytes"], w_obj, atif
        if (w_ev_path := getattr(w_obj, "events_path", None)) is not None:
            return evs, (Path(w_ev_path).read_bytes() if os.path.isfile(w_ev_path) else b""), w_obj, atif
        return evs, encode_events_jsonl(evs), w_obj, atif
    if isinstance(out, tuple) and len(out) >= 2:
        if not isinstance(out[1], bytes):
            raise TypeError("runner tuple[1] must be raw bytes")
        return [dict(x) for x in out[0]], out[1], (out[2] if len(out) >= 3 else None), None
    if isinstance(out, list):
        return (evs := [dict(x) for x in out]), encode_events_jsonl(evs), None, None
    raise TypeError(f"unsupported runner return type {type(out).__name__}")


def _invoke_family_runner(
    mods: Mapping[str, ModuleType | None], inst: dict[str, Any], seat_tokens: dict[str, str], policy: str, pol_map: Mapping[str, Any], ep_dir: Path,
) -> tuple[list[dict[str, Any]], bytes, Any, dict[str, Any] | None]:
    ref_mod, buf_out, buf_err = mods.get("ref"), io.StringIO(), io.StringIO()
    for runner_name in ("replay", "trial", "play"):
        if not callable(fn := getattr(ref_mod, runner_name, None) if ref_mod is not None else None):
            continue
        try:
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                out = fn(dict(inst), dict(seat_tokens), policy)
        except KeyError:
            if policy == "nop":
                break
            raise
        return _extract_runner_result(out)
    if (world_mod := mods.get("world")) is None:
        raise RuntimeError(f"cannot replay policy {policy!r}: no world module or trial/play runner")
    ev_path = ep_dir / "events.jsonl"
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        w_obj = _instantiate_world(world_mod, dict(inst), seat_tokens, ev_path)
        atif_map = _drive_world_policy(w_obj, dict(inst), seat_tokens, policy, pol_map)
    raw_mem: Any = getattr(w_obj, "events", None) or getattr(w_obj, "event_log", None) or []
    mem_evs: list[dict[str, Any]] = [dict(x) for x in raw_mem if isinstance(x, Mapping)]
    disk_active = os.path.lexists(ev_path) or getattr(w_obj, "events_path", None) is not None or mods.get("worldkit") is not None
    if os.path.isfile(ev_path) and not os.path.islink(ev_path):
        raw_bytes = ev_path.read_bytes()
        if mem_evs:
            return mem_evs, raw_bytes, w_obj, atif_map
        parsed_evs, perr = parse_jsonl_events(raw_bytes)
        if perr is not None or parsed_evs is None:
            raise ValueError(perr or "failed to parse world events.jsonl")
        return parsed_evs, raw_bytes, w_obj, atif_map
    return mem_evs, (b"" if disk_active else encode_events_jsonl(mem_evs)), w_obj, atif_map


def _check_roundtrip_parity(
    verify_mod: ModuleType, inst: Mapping[str, Any], score_direct: Mapping[str, Any], rt_bytes: bytes, raw_reload: list[dict[str, Any]] | None
) -> tuple[bool, str | None]:
    if raw_reload is None or not raw_reload:
        return False, "empty or unreadable disk events.jsonl"
    rt_events, rt_parse_err = parse_jsonl_events(rt_bytes)
    if rt_parse_err is not None or rt_events is None:
        return False, rt_parse_err or "round-trip JSONL parse failed"
    if raw_reload != rt_events:
        return False, "disk events.jsonl diverges from replayed in-memory events"
    score_rt, sc_rt_err = call_verify_score(verify_mod, rt_events, inst)
    if sc_rt_err is not None or score_rt is None or not rewards_equal(score_direct, score_rt):
        return False, sc_rt_err or "verify.score JSONL round-trip mismatch"
    score_rev, sc_rev_err = call_verify_score(verify_mod, _reverse_dict_keys_deep(rt_events), _reverse_dict_keys_deep(dict(inst)))
    if sc_rev_err is not None or score_rev is None or not rewards_equal(score_direct, score_rev):
        return False, sc_rev_err or "verify.score depends on dict key insertion order"
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
    used_dispatch = callable(getattr(w_obj, "dispatch", None)) if w_obj is not None else (callable(getattr(_find_world_factory(w_mod), "dispatch", None)) if w_mod is not None else False)
    log_intact = bool(getattr(w_obj, "log_intact", True)) if w_obj is not None else True
    wk_intact_fn = getattr(mods.get("worldkit"), "log_intact", None) if mods.get("worldkit") is not None else None
    if not raw_bytes:
        log_intact = False
    elif callable(wk_intact_fn):
        with contextlib.suppress(TypeError):
            log_intact = log_intact and bool(wk_intact_fn(raw_bytes))
    chain_ok, chain_err = verify_replayed_chain(events, mods.get("codec"))
    raw_reload, raw_err = parse_jsonl_events(raw_bytes) if raw_bytes else (None, "empty disk events.jsonl")
    if raw_err is not None or raw_reload is None or not raw_reload:
        chain_ok, chain_err = False, raw_err or "empty or corrupt raw events.jsonl"
    elif (r_chk := verify_replayed_chain(raw_reload, mods.get("codec")))[0] is not True:
        chain_ok, chain_err = r_chk[0], chain_err or r_chk[1]
    rt_bytes = encode_events_jsonl(events)
    score_direct, sc_err = call_verify_score(verify_mod, events, inst)
    if sc_err is not None or score_direct is None:
        return None, sc_err or "verify.score failed"
    if finite_float(score_direct.get("chain_ok")) == 0.0 or score_direct.get("chain_ok") is False:
        chain_ok, chain_err = False, chain_err or "verify.score reported chain_ok=0"
    roundtrip_ok, rt_err = _check_roundtrip_parity(verify_mod, inst, score_direct, rt_bytes, raw_reload)
    return {
        "events": events, "raw_bytes": raw_bytes, "atif": atif_map, "score": score_direct,
        "reward": float(score_direct["reward"]), "log_intact": log_intact, "chain_ok": chain_ok,
        "chain_error": chain_err, "roundtrip_ok": roundtrip_ok, "roundtrip_error": rt_err, "used_dispatch": bool(used_dispatch),
    }, None


def write_synthetic_trial(
    tdir: Path, family_name: str, inst: Mapping[str, Any], policy: str, raw_bytes: bytes,
    score_map: Mapping[str, Any], task_path: Path, atif_by_seat: Mapping[str, Any] | None = None, *, root: Path | None = None,
) -> None:
    if not is_safe_ident(family_name) or not is_safe_ident(policy):
        raise ValueError(f"unsafe synthetic trial identifier: family={family_name!r} policy={policy!r}")
    raw_seats = inst.get("seats") or ["p1"]
    if not isinstance(raw_seats, list) or not raw_seats or not all(is_safe_ident(s) for s in raw_seats):
        raise ValueError(f"unsafe seat identifiers: {raw_seats!r}")
    seats, inum = [str(s) for s in raw_seats], strict_int(inst.get("instance")) or 1
    if inum < 0:
        raise ValueError(f"negative instance number: {inum}")
    tdir_abs = Path(os.path.abspath(tdir))
    base_root = Path(os.path.abspath(root)) if root is not None else tdir_abs.parent
    real_root = base_root.resolve()
    if check_no_symlink_ancestors(tdir_abs, root=base_root) is not None or not tdir_abs.resolve().is_relative_to(real_root):
        raise ValueError(f"synthetic trial path escapes root: {tdir_abs}")
    ver_dir = tdir_abs / "verifier"
    ver_dir.mkdir(parents=True, exist_ok=True)
    if check_no_symlink_ancestors(ver_dir / "events.jsonl", root=base_root) is not None:
        raise ValueError("unsafe verifier directory")
    (ver_dir / "events.jsonl").write_bytes(raw_bytes)
    (ver_dir / "reward.json").write_text(json.dumps(dict(score_map)) + "\n", encoding="utf-8")
    ag_label = "oracle" if policy == "oracle" else ("nop" if policy == "nop" else "script")
    cfg_doc = {"trial_name": tdir_abs.name, "task": {"name": f"muster/{family_name}-i{inum:02d}", "path": str(task_path)}, "participants": [{"seat": s, "name": ag_label} for s in seats]}
    (tdir_abs / "config.json").write_text(json.dumps(cfg_doc) + "\n", encoding="utf-8")
    sr_list = []
    for s in seats:
        s_dir = tdir_abs / "seats" / s / "agent"
        if check_no_symlink_ancestors(s_dir, root=base_root) is not None or not s_dir.resolve().is_relative_to(real_root):
            raise ValueError(f"seat path escapes root: {s!r}")
        s_dir.mkdir(parents=True, exist_ok=True)
        (s_dir / "exit-code.txt").write_text("0\n", encoding="utf-8")
        if atif_by_seat and isinstance(atif_by_seat.get(s), Mapping):
            (s_dir / "trajectory.json").write_text(json.dumps(dict(atif_by_seat[s])) + "\n", encoding="utf-8")
        sr_list.append({"seat": s, "under_test": True, "agent_info": {"name": ag_label}, "exit_code": 0})
    res_doc = {"trial_name": tdir_abs.name, "task_name": f"muster/{family_name}-i{inum:02d}", "config": cfg_doc, "seat_results": sr_list, "verifier_result": {"rewards": dict(score_map)}, "reward_valid": True}
    (tdir_abs / "result.json").write_text(json.dumps(res_doc) + "\n", encoding="utf-8")
