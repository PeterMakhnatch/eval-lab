from __future__ import annotations

import contextlib
import math
import re
import statistics
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SLOT_RE = re.compile(r"^agent-(\d+)$")
_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
_BOOK_DIRS = frozenset({"sessions", "logs", "__pycache__"})
_CTRL_AGENTS = frozenset({"oracle", "nop", "script", "scripted"})
_SYS_ACTORS = frozenset({"@harness", "system", "world", "harbor"})
_NON_ACT_A = ("inbox", "history", "read", "config", "plugins", "register", "notify")
_NON_ACT = frozenset(
    (*_NON_ACT_A, "refused", "rate_limited", "guard_unread_block", "guard_unread_unblock")
)
_H_REL = ("artifacts", "logs", "artifacts", "multi-agent-harness")
_BOARD_PATHS = (("agent", "board-events.jsonl"), (*_H_REL, "board-events.jsonl"))
_PROXY_JSONL = (("agent", "proxy-usage.jsonl"), (*_H_REL, "proxy-usage.jsonl"))
_PROXY_JSON = (("agent", "proxy-usage.json"), ("proxy-usage.json",))
_SUB_JSON = (("agent", "subscription-usage.json"), ("subscription-usage.json",))
_ALL_TELEM = (*_BOARD_PATHS, *_PROXY_JSONL, *_PROXY_JSON, *_SUB_JSON)
_CKEYS = ("cache_read", "cache_write", "cache_write_1h")
_MAX_DIRS, _MAX_ITEMS = 256, 50_000


@dataclass(frozen=True, slots=True)
class BespokeIO:
    rfile: Callable[..., Path | None]
    subdirs: Callable[[Path], Sequence[Path]]
    has_dir: Callable[[Path], bool]
    read_json: Callable[[Path], Any]
    read_jsonl: Callable[[Path], list[Mapping[str, Any]]]
    read_text: Callable[[Path], str | None]


@dataclass(frozen=True, slots=True)
class BespokeTelemetry:
    fields: dict[str, Any]
    extra_rewards: dict[str, float]
    marks: dict[str, float]
    member_costs: dict[str, float | None]
    cost_status: str
    board_messages: int | None
    read_act_latency_sec: float | None


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    with contextlib.suppress(OverflowError):
        out = float(v)
        return out if math.isfinite(out) else None
    return None


def _nonneg(v: Any) -> float | None:
    return out if (out := _num(v)) is not None and out >= 0.0 else None


def _uint(v: Any) -> int | None:
    return v if not isinstance(v, bool) and isinstance(v, int) and v >= 0 else None


def _map(v: Any) -> Mapping[str, Any]:
    return v if isinstance(v, Mapping) else {}


def _cname(v: Any) -> str | None:
    return s if isinstance(v, str) and (s := v.strip()) and _SAFE_RE.match(s) else None


def _label(v: Any) -> str | None:
    return s if isinstance(v, str) and (s := v.strip()) and _LABEL_RE.match(s) else None


def _epoch(v: Any) -> float | None:
    if (n := _num(v)) is not None:
        return n if 0.0 <= n <= 1e12 else None
    if not isinstance(v, str) or not v.strip():
        return None
    with contextlib.suppress(ValueError):
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        ts = (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()
        return ts if math.isfinite(ts) else None
    return None


def _sort_roster(names: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            set(names), key=lambda n: (0, int(m[1]), n) if (m := _SLOT_RE.match(n)) else (1, 0, n)
        )
    )


def is_bespoke_trial_dir(
    path: Path,
    io: BespokeIO,
    result: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> bool:
    res, cfg = result or {}, config or _map((result or {}).get("config"))
    has_olam = isinstance(res.get("seat_results"), list) and bool(res["seat_results"])
    if has_olam or io.has_dir(path / "seats"):
        return False
    if isinstance(cfg.get("participants"), list) and cfg["participants"]:
        return False
    if any(io.rfile(path, *ps) for ps in _ALL_TELEM):
        return True
    kids = io.subdirs(path / "agent")
    if any(_SLOT_RE.match(c.name) for c in kids):
        return True
    has_traj = any(c.name not in _BOOK_DIRS and io.rfile(c, "trajectory.json") for c in kids)
    if kids and not io.rfile(path, "agent", "trajectory.json") and has_traj:
        return True
    m_ok = (
        io.rfile(m, "reward.json") or io.rfile(m, "reward.txt") for m in io.subdirs(path / "marks")
    )
    if any(m_ok):
        return True
    ac, ec = _map(cfg.get("agent")), _map(cfg.get("environment"))
    t1 = (_map(res.get("agent_info")).get("name"), ac.get("name"), ac.get("import_path"))
    tags = (*t1, ec.get("type"), ec.get("import_path"))
    markers = ("teamagent", "teamenvironment", "multi_agent_harness")
    if any(t in str(m or "").lower() for m in tags for t in markers):
        return True
    raw_m = _map(_map(res.get("agent_result")).get("metadata"))
    meta = {**_map(raw_m.get("multi_agent")), **raw_m}
    agents = meta.get("agents")
    mkeys = ("orchestration", "cost_source", "agent_names", "harness", "board_policy", "billed_as")
    return bool(isinstance(agents, (Mapping, list)) and agents) and any(k in meta for k in mkeys)


def _alias_maps(
    meta: Mapping[str, Any], root: Path, brows: Sequence[Mapping[str, Any]], io: BespokeIO
) -> tuple[dict[str, str], dict[str, str], set[str]]:
    rnames, ragents = _map(meta.get("agent_names")), meta.get("agents")
    items = sorted(rnames.items(), key=lambda kv: str(kv[0]))
    pairs = [(vs, vf) for s, f in items if (vs := _cname(s)) and (vf := _cname(f))]
    slots = {vs for vs, _ in pairs}
    if isinstance(ragents, Mapping):
        akeys = {v for k in ragents if (v := _cname(k))}
        slots.update(akeys - {vf for vs, vf in pairs if vf != vs and vs not in akeys})
    elif isinstance(ragents, list):
        for entry in ragents[:_MAX_DIRS]:
            rec = _map(entry)
            if vs := _cname(rec.get("id")):
                slots.add(vs)
                if vf := _cname(rec.get("name")):
                    pairs.append((vs, vf))
    if not pairs:
        sdirs = _sort_roster(c.name for c in io.subdirs(root / "agent") if _SLOT_RE.match(c.name))
        regs: list[str] = []
        for r in brows[:_MAX_ITEMS]:
            c = _cname(r.get("agent")) if (r.get("event") or r.get("type")) == "register" else None
            if c and not _SLOT_RE.match(c) and c not in regs:
                regs.append(c)
        if sdirs and len(regs) == len(sdirs):
            pairs, slots = list(zip(sdirs, regs, strict=True)), set(sdirs)
    s2f, f2s = {}, {}
    bad, seen = set(), set()
    for vs, vf in pairs:
        if vs in seen:
            continue
        seen.add(vs)
        if (vf != vs and vf in slots) or vf in bad:
            bad.add(vf)
        elif vf in f2s and f2s[vf] != vs:
            bad.add(vf)
            if prev := f2s.pop(vf, None):
                s2f.pop(prev, None)
        else:
            s2f[vs] = vf
            if vf != vs:
                f2s[vf] = vs
    for b in bad:
        f2s.pop(b, None)
    return s2f, f2s, bad


def _has_unpriced(v: Any) -> bool:
    stack: list[Any] = [v]
    for _ in range(_MAX_DIRS):
        if not stack:
            return False
        cur = stack.pop()
        if cur is True or (isinstance(cur, list) and bool(cur)):
            return True
        if not isinstance(cur, bool) and isinstance(cur, (int, float)) and cur > 0:
            return True
        if isinstance(cur, Mapping):
            vals = list(cur.values())[:_MAX_DIRS]
            stack.extend(vals[: max(0, _MAX_DIRS - len(stack))])
    return False


def _fvec(iv: int | None, cv: int | None, ov: int | None, cost: float | None) -> list[float | None]:
    return [float(x) if x is not None else None for x in (iv, cv, ov)] + [cost]


def _traj_info(
    root: Path, slot: str, sf: str | None, io: BespokeIO
) -> tuple[dict[str, Any], str | None, list[float]]:
    alt = io.rfile(root, "agent", sf, "trajectory.json") if sf and sf != slot else None
    tp = io.rfile(root, "agent", slot, "trajectory.json") or alt
    if tp is None:
        return {}, None, []
    try:
        doc = _map(io.read_json(tp))
    except (OSError, ValueError):
        return {}, None, []
    acts: list[float] = []
    poll = ("__inbox", "__history", ":inbox", ":history")
    raw_steps = doc.get("steps")
    for step in raw_steps[:_MAX_ITEMS] if isinstance(raw_steps, list) else ():
        sm = _map(step)
        tcs = sm.get("tool_calls")
        if sm.get("source") != "agent" or not isinstance(tcs, list) or not tcs:
            continue
        fns = [str(_map(tc).get("function_name") or "").lower() for tc in tcs[:64]]
        is_poll = all(n.endswith(poll) or n in ("inbox", "history") for n in fns)
        if not is_poll and (ts := _epoch(sm.get("timestamp"))) is not None:
            acts.append(ts)
    tmod = _label(_map(doc.get("agent")).get("model_name"))
    return dict(_map(doc.get("final_metrics"))), tmod, acts


def _cstatus(sub: bool, unp: bool, has_u: bool) -> str:
    if sub:
        return "unbounded_subscription"
    return "partial" if unp else ("recorded" if has_u else "missing")


def _proxy_row_usage(rows: Sequence[Mapping[str, Any]], sub: bool, bad_p: bool = False) -> tuple[list[float | None], str]:
    iv = cv = ov = 0
    hi = hc = ho = False
    unp = bad_p
    usds: list[float] = []
    for r in rows:
        t = _map(r.get("tokens"))
        vi = _uint(t.get("input"))
        if (vi := vi if vi is not None else _uint(r.get("input_tokens"))) is not None:
            hi, iv = True, iv + vi
        vo = _uint(t.get("output"))
        if (vo := vo if vo is not None else _uint(r.get("output_tokens"))) is not None:
            ho, ov = True, ov + vo
        cvals = [v for k in _CKEYS if (v := _uint(t.get(k))) is not None]
        if (vc := sum(cvals) if cvals else _uint(r.get("cache_tokens"))) is not None:
            hc, cv = True, cv + vc
        u = _nonneg(r.get("usd"))
        if (u := u if u is not None else _nonneg(r.get("cost_usd"))) is not None:
            usds.append(u)
        elif r.get("status") == 200:
            unp = True
    st = _cstatus(sub, unp, bool(usds))
    cost = round(math.fsum(usds), 9) if st == "recorded" else None
    return _fvec(iv if hi else None, cv if hc else None, ov if ho else None, cost), st


def _member_usage(
    prows: Sequence[Mapping[str, Any]],
    sums: tuple[Mapping[str, Any], Mapping[str, Any]],
    mrec: Mapping[str, Any],
    fmet: Mapping[str, Any],
    flags: tuple[bool, bool, bool, str | None, bool],
) -> tuple[list[float | None], str]:
    psum, ssum = sums
    sub, tunp, bad_p, csrc, ctrl = flags
    if prows:
        vec, st = _proxy_row_usage(prows, sub, False)
    elif psum or ssum:
        src = psum or ssum
        t = _map(src.get("tokens"))
        cvals = [v for k in _CKEYS if (v := _uint(t.get(k))) is not None]
        vi, vc, vo = _uint(t.get("input")), (sum(cvals) if cvals else None), _uint(t.get("output"))
        uraw = src.get("usd") if src.get("usd") is not None else src.get("cost_usd")
        unp, u = _has_unpriced(src.get("unpriced")) or tunp, _nonneg(uraw)
        st = _cstatus(sub, unp, u is not None)
        vec = _fvec(vi, vc, vo, u if st == "recorded" else None)
    else:
        vi = _uint(mrec.get("n_input_tokens"))
        vc, vo = _uint(mrec.get("n_cache_tokens")), _uint(mrec.get("n_output_tokens"))
        u = _nonneg(mrec.get("cost_usd"))
        fake0 = u == 0.0 and csrc == "trajectories" and (vi or 0) > 0 and not ctrl
        st = _cstatus(sub, False, u is not None and not fake0)
        vec = _fvec(vi, vc, vo, u if st == "recorded" else None)
    for i, fk in enumerate(("total_prompt_tokens", "total_cached_tokens", "total_completion_tokens")):
        if vec[i] is None and (fv := _uint(fmet.get(fk))) is not None:
            vec[i] = float(fv)
    if vec[3] is None and st == "missing":
        if sub:
            st = "unbounded_subscription"
        elif (fc := _nonneg(fmet.get("total_cost_usd"))) is not None:
            vec[3], st = fc, "recorded"
        elif ctrl:
            vec[3], st = 0.0, "zero_no_model"
    if bad_p and st in ("recorded", "missing"):
        vec[3], st = None, "partial"
    return vec, st


def _parse_seq_list(rseqs: list[Any]) -> set[int]:
    out: set[int] = set()
    for s in rseqs[:512]:
        if isinstance(s, int) and not isinstance(s, bool):
            out.add(s)
        elif isinstance(s, str) and s.strip().isdigit():
            out.add(int(s.strip()))
    return out


def _board_metrics(
    rows: Sequence[Mapping[str, Any]],
    canon: Callable[[Any], str | None],
    tacts: Mapping[str, Sequence[float]],
) -> tuple[int, float | None]:
    msgs = lseq = 0
    posted: list[tuple[int, str | None, set[str]]] = []
    pseqs: list[int] = []
    mono = True
    reads: list[tuple[str, float]] = []
    bacts: dict[str, list[float]] = defaultdict(list)
    for r in rows[:_MAX_ITEMS]:
        ev = str(r.get("event") or r.get("type") or "")
        actor = canon(r.get("agent") if r.get("agent") is not None else r.get("from"))
        ts = _epoch(r.get("ts") if r.get("ts") is not None else r.get("timestamp"))
        if ev in ("send", "post"):
            msgs += 1
            rs = r.get("seq")
            ms = rs if isinstance(rs, int) and not isinstance(rs, bool) and rs >= 1 else lseq + 1
            lseq, mono = max(lseq, ms), mono and (not pseqs or ms >= pseqs[-1])
            rto = r.get("to")
            rec = {rc} if isinstance(rto, str) and (rc := canon(rto)) else set()
            if isinstance(rto, list):
                rec.update(rc for x in rto[:64] if (rc := canon(x)))
            posted.append((ms, actor, rec))
            pseqs.append(ms)
            if actor and ts is not None:
                bacts[actor].append(ts)
        elif ev in ("inbox", "history", "read") or (ev == "subagent" and r.get("op") == "wake"):
            if actor is None or ts is None:
                continue
            rseqs = r.get("seqs")
            if isinstance(rseqs, list):
                want = _parse_seq_list(rseqs)
                hit = any(ms in want and snd != actor for ms, snd, _ in posted)
            elif (_uint(r.get("returned")) or 0) > 0:
                hit = True
            elif ev in ("history", "read"):
                aft = _uint(r.get("after")) or 0
                i0 = bisect_right(pseqs, aft) if mono else 0
                hit = any(
                    ms > aft and snd != actor and (not rc or actor in rc)
                    for ms, snd, rc in posted[i0:]
                )
            else:
                hit = False
            if hit:
                reads.append((actor, ts))
        elif actor and ts is not None and ev not in _NON_ACT:
            bacts[actor].append(ts)
    lats: list[float] = []
    for actor, rts in reads:
        bl, tl = sorted(bacts.get(actor, ())), sorted(tacts.get(actor, ()))
        bi, ti = bisect_left(bl, rts), bisect_right(tl, rts)
        cands = [*(bl[bi : bi + 1]), *(tl[ti : ti + 1])]
        if cands:
            lats.append(round(max(0.0, min(cands) - rts), 6))
    return msgs, (round(float(statistics.median(lats)), 6) if lats else None)


def _verifier_msgs(root: Path, io: BespokeIO) -> int | None:
    vmp = io.rfile(root, "verifier", "messages.jsonl")
    if vmp is None:
        return None
    keys = ("text", "from", "channel")
    return sum(1 for r in io.read_jsonl(vmp)[:_MAX_ITEMS] if any(k in r for k in keys))


def _read_marks(root: Path, meta: Mapping[str, Any], io: BespokeIO) -> dict[str, float]:
    marks: dict[str, float] = {}
    raw_m = meta.get("marks")
    for item in raw_m[:_MAX_DIRS] if isinstance(raw_m, list) else ():
        rec = _map(item)
        if (lbl := _cname(rec.get("mark"))) and (val := _num(rec.get("reward"))) is not None:
            marks[lbl] = val
    for child in io.subdirs(root / "marks"):
        if not (lbl := _cname(child.name)):
            continue
        val = None
        if rj := io.rfile(child, "reward.json"):
            doc = None
            with contextlib.suppress(OSError, ValueError):
                doc = io.read_json(rj)
            fb = (
                next(iter(doc.values()), None)
                if isinstance(doc, Mapping) and len(doc) == 1
                else None
            )
            val = _num(doc.get("reward", fb) if isinstance(doc, Mapping) else doc)
        elif (rt := io.rfile(child, "reward.txt")) and (txt := io.read_text(rt)) is not None:
            with contextlib.suppress(ValueError):
                val = _num(float(txt.strip()))
        if val is not None:
            marks[lbl] = val
    return marks


def _collect_roster(
    root: Path, meta: Mapping[str, Any], s2f: Mapping[str, str], prows: Sequence[Mapping[str, Any]],
    sums: tuple[Mapping[str, Any], Mapping[str, Any]], brows: Sequence[Mapping[str, Any]],
    canon: Callable[[Any], str | None], io: BespokeIO,
) -> tuple[tuple[str, ...], dict[str, Mapping[str, Any]], set[str]]:
    ra = meta.get("agents")
    mag = {ck: v for k, v in ra.items() if (ck := _cname(k)) and isinstance(v, Mapping)} if isinstance(ra, Mapping) else ({cid: rec for e in ra[:_MAX_DIRS] if (cid := _cname((rec := _map(e)).get("id")))} if isinstance(ra, list) else {})
    found = {c for k in mag if (c := canon(k))} | set(s2f)
    found.update(v for k in _map(meta.get("agent_names")) if (v := _cname(k)))
    for sm in sums:
        found.update(c for k in _map(sm.get("agents")) if (c := canon(k)))
    if isinstance(da := _safe_jmap(io.rfile(root, "verifier", "details.json"), io).get("agents"), list):
        found.update(c for x in da[:_MAX_DIRS] if (c := canon(x)))
    found.update(c for r in prows if (c := canon(r.get("agent"))) and c not in _SYS_ACTORS)
    for c in io.subdirs(root / "agent"):
        if c.name not in _BOOK_DIRS and io.rfile(c, "trajectory.json") is not None and (cn := canon(c.name)):
            found.add(cn)
    for r in brows[:_MAX_ITEMS]:
        ev = r.get("event") or r.get("type")
        who = r.get("agent") if ev == "register" else (r.get("name") if ev == "subagent" and r.get("op") == "spawn" else None)
        if (c := canon(who)) and c not in _SYS_ACTORS:
            found.add(c)
    rl = meta.get("lost_agents")
    lost = {c for x in (rl[:_MAX_DIRS] if isinstance(rl, list) else ()) if (c := canon(x))}
    lost.update(c for k in _map(meta.get("lost")) if (c := canon(k)))
    return tuple(sorted(found or {"agent-0"})), mag, lost


def _rollup_totals(
    urows: Sequence[Sequence[float | None]],
    uflags: Sequence[bool],
    sts: Sequence[str],
    ptot: Mapping[str, Any],
    tsub: bool,
    tunp: bool,
) -> tuple[list[float | None], tuple[str, ...]]:
    totals: list[float | None] = []
    partial: list[str] = []
    for ki, kn in enumerate(("tokens_in", "tokens_cache", "tokens_out", "cost_usd")):
        vals = [v for r in urows if (v := r[ki]) is not None]
        rtot = round(math.fsum(vals), 9) if vals else None
        if any(uflags):
            comp = all(r[ki] is not None for r, u in zip(urows, uflags, strict=True) if u)
            tusd = _nonneg(ptot.get("usd")) if ptot else None
            ok_fb = kn == "cost_usd" and not comp and not tsub and not tunp and "partial" not in sts
            if ok_fb and tusd is not None:
                rtot, comp = tusd, True
            totals.append(rtot if comp else None)
            if (rtot is not None and not comp) or (kn == "cost_usd" and "partial" in sts):
                partial.append(kn)
        else:
            totals.append(rtot if rtot is not None else (0.0 if kn == "cost_usd" else None))
    return totals, tuple(dict.fromkeys(partial))


def _eval_member(
    ctx: tuple[Path, set[str], str, str | None, bool, bool, bool, str | None, BespokeIO],
    name: str, sf: str | None, mrec: Mapping[str, Any],
    mr: Sequence[Mapping[str, Any]], sums: tuple[Mapping[str, Any], Mapping[str, Any]],
) -> tuple[tuple[str | None, str | None], list[float | None], bool, str, str | None, list[float]]:
    root, lost, dagent, dmod, tsub, tunp, bad_p, csrc, io = ctx
    fmet, tmod, tacts = _traj_info(root, name, sf, io)
    h = _label(mrec.get("harness")) or dagent
    rmod = next((m for r in mr if (m := _label(r.get("model")))), None)
    mod = _label(mrec.get("model")) or rmod or tmod or dmod
    h_low = h.lower() if isinstance(h, str) else ""
    ctrl = bool(h_low) and (h_low in _CTRL_AGENTS or h_low.endswith(("scriptagent", "scriptedagent"))) and mod is None
    msub = mrec.get("billed_as") == "claude-subscription" or (tsub and mrec.get("billed_as") != "proxy" and not mr and not sums[0])
    uv, st = _member_usage(mr, sums, mrec, fmet, (msub, tunp, bad_p, csrc, ctrl))
    rex = mrec.get("exit")
    err: str | None = "LostSandbox" if name in lost else None
    if err is None and type(rex) is int and rex != 0:
        err = f"NonZeroExitCode({rex})"
    elif err is None and isinstance(rex, str) and rex.strip():
        with contextlib.suppress(ValueError):
            rex = "" if int(rex.strip()) == 0 else f"NonZeroExitCode({int(rex.strip())})"
        err = rex.strip() or None
    return (h, mod), uv, not ctrl, st, err, tacts


def _build_fields(
    roster: tuple[str, ...],
    who: Sequence[tuple[str | None, str | None]],
    totals: Sequence[float | None],
    errs: dict[str, str],
    uflags: Sequence[bool],
    partial: tuple[str, ...],
) -> dict[str, Any]:
    t0, t1, t2, c = totals
    return {
        "agent": ",".join(f"{n}={w[0] or '-'}" for n, w in zip(roster, who, strict=True)),
        "agent_version": "unknown",
        "model": ",".join(f"{n}={w[1] or '-'}" for n, w in zip(roster, who, strict=True)),
        "tokens_in": int(t0) if t0 is not None else None,
        "tokens_cache": int(t1) if t1 is not None else None,
        "tokens_out": int(t2) if t2 is not None else None,
        "cost_usd": c,
        "seats": roster,
        "model_seats": tuple(n for n, u in zip(roster, uflags, strict=True) if u),
        "seat_exceptions": errs,
        "partial_usage": partial,
    }


def _safe_jmap(path: Path | None, io: BespokeIO) -> Mapping[str, Any]:
    with contextlib.suppress(OSError, ValueError):
        return _map(io.read_json(path)) if path else {}
    return {}


def read_bespoke_telemetry(
    root: Path, result: Mapping[str, Any], config: Mapping[str, Any], io: BespokeIO
) -> BespokeTelemetry:
    raw_m = _map(_map(result.get("agent_result")).get("metadata"))
    meta = {**_map(raw_m.get("multi_agent")), **raw_m}
    csets = (_PROXY_JSONL, _PROXY_JSON, _SUB_JSON, _BOARD_PATHS)
    pjl, pjs, sjs, bp = (next((p for ps in cs if (p := io.rfile(root, *ps))), None) for cs in csets)
    prows, brows = (io.read_jsonl(pjl) if pjl else []), (io.read_jsonl(bp) if bp else [])
    psum, ssum = _safe_jmap(pjs, io), _safe_jmap(sjs, io)
    s2f, f2s, bad = _alias_maps(meta, root, brows, io)

    def canon(x: Any) -> str | None:
        return f2s.get(c, c) if (c := _cname(x)) else None

    roster, magents, lost = _collect_roster(root, meta, s2f, prows, (psum, ssum), brows, canon, io)
    assert len(roster) >= 1
    p_by_m: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    bad_p = False
    for r in prows:
        if r.get("agent") is None and r.get("status") == 401:
            continue
        if cm := canon(r.get("agent")):
            p_by_m[cm].append(r)
        elif r.get("__torn__") or r.get("status") == 200 or _nonneg(r.get("usd")) is not None or _nonneg(r.get("cost_usd")) is not None:
            bad_p = True
    pag, sag = _map(psum.get("agents")), _map(ssum.get("agents"))
    ptot, stot = dict(_map(psum.get("total"))), _map(ssum.get("total"))
    if "usd" not in ptot and "total_cost_usd" in psum:
        ptot["usd"] = psum["total_cost_usd"]
    tsub = any(m.get("budget_enforced") is False for m in (ssum, psum, meta)) or (meta.get("billed_as") == "claude-subscription")
    tunp = _has_unpriced(ptot.get("unpriced")) or _has_unpriced(psum.get("unpriced"))
    csrc = str(meta["cost_source"]) if isinstance(meta.get("cost_source"), str) else None
    ainfo, acfg = _map(result.get("agent_info")), _map(config.get("agent"))
    acands = (meta.get("harness"), ainfo.get("name"), acfg.get("name"), acfg.get("import_path"))
    dagent = next((v for x in acands if (v := _label(x))), "team-agent")
    dmod = _label(acfg.get("model_name")) or _label(_map(ainfo.get("model_info")).get("name"))
    who, urows, uflags, sts = [], [], [], []
    errs, mcosts, tacts = {}, {}, {}
    solo, ctx = len(roster) == 1, (root, lost, dagent, dmod, tsub, tunp, bad_p, csrc, io)
    for name in roster:
        sf = f if (f := s2f.get(name)) and f not in bad else None
        mrec = magents.get(name) or (magents.get(sf) if sf else None) or {}
        pm = _map(pag.get(name) or (pag.get(sf) if sf else None)) or (ptot if solo else {})
        sm = _map(sag.get(name) or (sag.get(sf) if sf else None)) or (stot if solo else {})
        wm, uv, uf, st, err, tacts[name] = _eval_member(ctx, name, sf, mrec, p_by_m.get(name, []), (pm, sm))
        for lst, item in ((who, wm), (urows, uv), (uflags, uf), (sts, st)):
            lst.append(item)
        mcosts[name] = uv[3]
        errs.update({name: err} if err else {})
    totals, partial = _rollup_totals(urows, uflags, sts, ptot, tsub, tunp or bad_p)
    msgs, mlat = _board_metrics(brows, canon, tacts) if bp else (None, None)
    if (msgs is None or msgs == 0) and (vmsgs := _verifier_msgs(root, io)) is not None:
        msgs = vmsgs if (msgs is None or vmsgs > 0) else msgs
    marks = _read_marks(root, meta, io)
    extra = {f"mark.{k}": v for k, v in sorted(marks.items())}
    extra.update({"board.messages": float(msgs)} if msgs is not None else {})
    extra.update({"board.read_act_latency_sec": mlat} if mlat is not None else {})
    cst = _cstatus("unbounded_subscription" in sts, "partial" in sts, totals[3] is not None)
    fields = _build_fields(roster, who, totals, errs, uflags, partial)
    return BespokeTelemetry(fields, extra, marks, mcosts, cst, msgs, mlat)
