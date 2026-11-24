from __future__ import annotations

import os
import stat
import tomllib
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Mapping
from http.client import HTTPException
from pathlib import Path
from typing import Any

from worldwatch._io import (
    check_no_symlink_ancestors,
    read_regular_text,
    split_text_lines_strict,
    stat_regular_file,
)
from worldwatch._preflight_rules import (
    check_dockerignores,
    check_toml_security,
    inspect_compose_lines,
    make_finding,
)
from worldwatch.model import (
    SEAT_NAME_RE,
    InputError,
    as_map,
    finite_float,
    parse_strict_json,
)

MAX_PREFLIGHT_TASKS = 2_000
_MAX_TASK_ENTRIES = 5_000
_MAX_DISCOVER_DIRS = 20_000
_MAX_DISCOVER_DEPTH = 4
_MAX_HTTP_BYTES = 262_144
_OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
_OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
_COMPOSE_FILENAMES = ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml")


def _is_valid_api_token(token: str) -> bool:
    if not isinstance(token, str) or not (1 <= len(token) <= 4096):
        return False
    return all(0x21 <= ord(ch) <= 0x7E and ch not in ('"', "'") for ch in token)


def _walk_discover_root(
    scan_root: Path,
    seen: set[Path],
    found: list[Path],
    roots_map: dict[Path, Path],
    findings: list[dict[str, Any]],
) -> None:
    stack: list[tuple[Path, int]] = [(scan_root, 0)]
    visited = 0
    while stack:
        visited += 1
        assert visited <= _MAX_DISCOVER_DIRS
        cur, depth = stack.pop()
        if os.path.lexists(cur / "task.toml"):
            if cur not in seen:
                seen.add(cur)
                found.append(cur)
                roots_map[cur] = scan_root
                if len(found) > MAX_PREFLIGHT_TASKS:
                    raise InputError(f"discovered tasks exceed MAX_PREFLIGHT_TASKS ({MAX_PREFLIGHT_TASKS})")
            continue
        if depth >= _MAX_DISCOVER_DEPTH:
            continue
        if cur != scan_root and os.path.lexists(cur / "family.toml"):
            bsub = cur / "build"
            if os.path.islink(bsub):
                findings.append(make_finding("symlink-forbidden", f"symlink directory build under {cur}", cur.name))
            elif os.path.isdir(bsub):
                stack.append((bsub, depth + 1))
            continue
        try:
            entries = sorted(os.scandir(cur), key=lambda e: e.name, reverse=True)
        except OSError as exc:
            findings.append(make_finding("unreadable-directory", f"cannot scan {cur}: {exc.strerror or exc}"))
            continue
        for entry in entries:
            if entry.name.startswith(".") or entry.name in ("__pycache__", "template"):
                continue
            if entry.is_symlink():
                findings.append(make_finding("symlink-forbidden", f"symlink directory {entry.name} under {cur}", entry.name))
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append((Path(entry.path), depth + 1))


def _discover_one_target(
    raw: str,
    seen: set[Path],
    found: list[Path],
    roots_map: dict[Path, Path],
    findings: list[dict[str, Any]],
) -> None:
    base = Path(raw)
    anc_err = check_no_symlink_ancestors(base)
    if anc_err is not None:
        raise InputError(f"preflight target unsafe: {anc_err}")
    try:
        st = os.lstat(base)
    except FileNotFoundError:
        raise InputError(f"preflight target does not exist: {base}") from None
    except OSError as exc:
        raise InputError(f"cannot stat preflight target {base}: {exc.strerror or exc}") from None
    if stat.S_ISLNK(st.st_mode):
        raise InputError(f"preflight target is a symlink: {base}")
    if stat.S_ISREG(st.st_mode):
        if base.name != "task.toml":
            raise InputError(f"preflight file target must be task.toml: {base}")
        tdir = base.parent if str(base.parent) != "" else Path(".")
        if tdir not in seen:
            seen.add(tdir)
            found.append(tdir)
            roots_map[tdir] = tdir
            if len(found) > MAX_PREFLIGHT_TASKS:
                raise InputError(f"discovered tasks exceed MAX_PREFLIGHT_TASKS ({MAX_PREFLIGHT_TASKS})")
        return
    if not stat.S_ISDIR(st.st_mode):
        raise InputError(f"preflight target is not a directory: {base}")
    build_sub = base / "build"
    scan_root = (
        build_sub
        if os.path.lexists(base / "family.toml") and os.path.isdir(build_sub) and not os.path.islink(build_sub)
        else base
    )
    _walk_discover_root(scan_root, seen, found, roots_map, findings)


def _compute_task_labels(task_dirs: list[Path], roots_map: dict[Path, Path]) -> dict[Path, str]:
    prelim: dict[Path, str] = {}
    for tdir in task_dirs:
        sroot = roots_map.get(tdir, tdir)
        if tdir != sroot:
            parts = [p for p in tdir.relative_to(sroot).parts if p != "build"]
            prelim[tdir] = "/".join(parts[-2:]) if len(parts) >= 2 else (parts[0] if parts else tdir.name)
        elif tdir.parent.name and tdir.parent.parent.name == "build":
            prelim[tdir] = f"{tdir.parent.name}/{tdir.name}"
        else:
            prelim[tdir] = tdir.name or str(tdir)
    counts = Counter(prelim.values())
    final: dict[Path, str] = {}
    for tdir, lbl in prelim.items():
        if counts[lbl] > 1 and tdir.parent.name and "/" not in lbl:
            final[tdir] = f"{tdir.parent.name}/{tdir.name}"
        else:
            final[tdir] = lbl
    recounts = Counter(final.values())
    for tdir, lbl in list(final.items()):
        if recounts[lbl] > 1:
            final[tdir] = tdir.as_posix()
    return final


def _scan_task_tree(task_dir: Path, label: str, findings: list[dict[str, Any]]) -> None:
    stack: list[tuple[Path, int]] = [(task_dir, 0)]
    visited = 0
    while stack:
        cur, depth = stack.pop()
        if depth > 12:
            findings.append(make_finding("task-tree-too-deep", "task directory exceeds max depth 12", label))
            continue
        try:
            entries = sorted(os.scandir(cur), key=lambda e: e.name)
        except OSError as exc:
            findings.append(make_finding("unreadable-task-dir", f"cannot scan {cur.name}: {exc.strerror or exc}", label))
            continue
        for entry in entries:
            visited += 1
            if visited > _MAX_TASK_ENTRIES:
                findings.append(make_finding("task-tree-too-large", f"task exceeds {_MAX_TASK_ENTRIES} entries", label))
                return
            rel = Path(entry.path).relative_to(task_dir).as_posix()
            if entry.is_symlink():
                findings.append(make_finding("symlink-forbidden", f"symlink in task tree: {rel}", label))
                continue
            low_name = entry.name.lower()
            if entry.name == "__pycache__" or low_name.endswith((".pyc", ".pyo")):
                findings.append(make_finding("bytecode-artifact", f"bytecode artifact in task tree: {rel}", label))
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append((Path(entry.path), depth + 1))


def _check_task_toml(
    task_dir: Path,
    label: str,
    findings: list[dict[str, Any]],
) -> tuple[int, str | None]:
    toml_path = task_dir / "task.toml"
    text, err = read_regular_text(toml_path, max_bytes=512 * 1024, root=task_dir)
    if err is not None or text is None:
        findings.append(make_finding("invalid-task-toml", err or "missing task.toml", label))
        return 0, None
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        findings.append(make_finding("invalid-task-toml", f"TOML syntax error: {exc}", label))
        return 0, None
    meta = doc.get("metadata")
    if not isinstance(meta, Mapping) or not meta:
        findings.append(make_finding("missing-metadata", "task.toml must define non-empty [metadata]", label))
    ag_tbl = doc.get("agent")
    ags_tbl = doc.get("agents")
    seat_names: list[str] = []
    if isinstance(ags_tbl, Mapping):
        for sname, sval in ags_tbl.items():
            if isinstance(sname, str) and SEAT_NAME_RE.match(sname) and isinstance(sval, Mapping):
                seat_names.append(sname)
            else:
                findings.append(make_finding("invalid-agent-seat", f"invalid [agents.{sname}] entry", label))
    if not isinstance(ag_tbl, Mapping) and not seat_names:
        findings.append(make_finding("missing-agent-config", "task.toml must define [agent] or [agents.*]", label))
    ag_timeout = finite_float(as_map(ag_tbl).get("timeout_sec"))
    if ag_timeout is not None:
        if ag_timeout <= 0.0:
            findings.append(make_finding("invalid-timeout", "[agent].timeout_sec must be > 0", label))
    elif not seat_names or not all(
        (st := finite_float(as_map(as_map(ags_tbl).get(s)).get("timeout_sec"))) is not None and st > 0.0
        for s in seat_names
    ):
        findings.append(make_finding("invalid-timeout", "missing positive timeout_sec in [agent]/[agents.*]", label))
    ver_tbl = doc.get("verifier")
    if isinstance(ver_tbl, Mapping) and "timeout_sec" in ver_tbl:
        vt = finite_float(ver_tbl.get("timeout_sec"))
        if vt is None or vt <= 0.0:
            findings.append(make_finding("invalid-timeout", "[verifier].timeout_sec must be > 0", label))
    check_toml_security(doc, label, findings)
    env_vars = as_map(as_map(doc.get("environment")).get("env"))
    bake_raw = env_vars.get("COMPOSE_BAKE", as_map(doc.get("environment")).get("COMPOSE_BAKE"))
    bake_str = str(bake_raw) if isinstance(bake_raw, str) else None
    return len(seat_names), bake_str


def _check_compose(
    task_dir: Path,
    label: str,
    toml_seats: int,
    compose_bake: str | None,
    findings: list[dict[str, Any]],
) -> None:
    env_dir = task_dir / "environment"
    compose_paths = [env_dir / nm for nm in _COMPOSE_FILENAMES if os.path.lexists(env_dir / nm)]
    if not compose_paths:
        if toml_seats > 24 and compose_bake != "false":
            findings.append(make_finding("missing-compose-bake-false", "COMPOSE_BAKE must be 'false' when > 24 seats", label))
        return
    max_compose_seats = 0
    for compose_path in compose_paths:
        text, err = read_regular_text(compose_path, max_bytes=2 * 1024 * 1024, root=task_dir)
        if err is not None or text is None:
            findings.append(make_finding("invalid-compose", err or f"unreadable {compose_path.name}", label))
            continue
        try:
            lines = split_text_lines_strict(text)
        except ValueError as exc:
            findings.append(make_finding("invalid-compose", str(exc), label))
            continue
        services, svc_nets, net_internal, has_health = inspect_compose_lines(lines, label, findings)
        seat_svcs = {s for s in services if s not in ("main", "world")}
        max_compose_seats = max(max_compose_seats, len(seat_svcs))
        if ("main" in services or "world" in services or seat_svcs) and not has_health:
            findings.append(make_finding("compose-missing-healthcheck", "world service missing /health healthcheck", label))
        non_main_nets = {n for n in net_internal if n != "main"}
        bad_nets = sorted(n for n in non_main_nets if not net_internal[n])
        if bad_nets:
            findings.append(make_finding("compose-network-not-internal", f"network(s) missing internal: true: {bad_nets}", label))
        elif seat_svcs:
            for sname in sorted(seat_svcs):
                snets = svc_nets.get(sname, set())
                if not snets or any(not net_internal.get(n, False) for n in snets):
                    findings.append(make_finding("compose-network-not-internal", f"seat {sname} lacks internal: true network", label))
                    break
    total_seats = max(toml_seats, max_compose_seats)
    if total_seats > 24 and compose_bake != "false":
        findings.append(make_finding("missing-compose-bake-false", f"COMPOSE_BAKE must be 'false' for {total_seats} seats (> 24)", label))


def _fetch_openrouter_json(url: str, api_key: str) -> tuple[Mapping[str, Any] | None, str | None]:
    assert url.startswith("https://openrouter.ai/")
    ep = url.rsplit("/", 1)[-1]
    if not _is_valid_api_token(api_key):
        return None, f"OPENROUTER_API_KEY has invalid token format before querying {ep}"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            raw = resp.read(_MAX_HTTP_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code} from {ep}"
    except (urllib.error.URLError, HTTPException, TimeoutError, OSError, ValueError, UnicodeError) as exc:
        return None, f"{type(exc).__name__} querying {ep}"
    if len(raw) > _MAX_HTTP_BYTES:
        return None, f"{ep} response exceeds {_MAX_HTTP_BYTES} bytes"
    try:
        parsed = parse_strict_json(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return None, f"invalid JSON from {ep}: {type(exc).__name__}"
    return (parsed, None) if isinstance(parsed, Mapping) else (None, "root is not a JSON object")


def _eval_openrouter_key(
    key_snap: Mapping[str, Any], min_key_usd: float, findings: list[dict[str, Any]]
) -> dict[str, Any]:
    data = as_map(key_snap.get("data")) if isinstance(key_snap.get("data"), Mapping) else as_map(key_snap)
    raw_rem, raw_lim, raw_use = data.get("limit_remaining"), data.get("limit"), data.get("usage")
    raw_eff = as_map(data.get("effective_budget")).get("remaining_usd")
    lim_val = finite_float(raw_lim) if raw_lim is not None else None
    use_val = finite_float(raw_use) if raw_use is not None else None
    if raw_lim is not None and (lim_val is None or lim_val < 0.0):
        findings.append(make_finding("openrouter-key-invalid", "non-finite or negative limit in key snapshot"))
        lim_val = None
    if raw_use is not None and (use_val is None or use_val < 0.0):
        findings.append(make_finding("openrouter-key-invalid", "non-finite or negative usage in key snapshot"))
        use_val = None
    rem_val: float | None = None
    capped = False
    if raw_rem is not None:
        rem_val, capped = finite_float(raw_rem), True
        if rem_val is None:
            findings.append(make_finding("openrouter-key-invalid", "non-finite limit_remaining in key snapshot"))
    elif raw_lim is not None:
        capped = True
        if lim_val is not None and (raw_use is None or use_val is not None):
            rem_val = round(lim_val - (use_val or 0.0), 6)
    elif raw_eff is None and not any(k in data for k in ("usage", "label", "is_free_tier", "limit", "limit_remaining", "effective_budget")):
        findings.append(make_finding("openrouter-key-invalid", "empty or unrecognized OpenRouter key payload"))
    if raw_eff is not None:
        eff_val = finite_float(raw_eff)
        if eff_val is None:
            findings.append(make_finding("openrouter-key-invalid", "non-finite effective_budget.remaining_usd in key snapshot"))
        else:
            capped = True
            rem_val = eff_val if rem_val is None else min(rem_val, eff_val)
    if capped and rem_val is not None and (rem_val <= 0.0 or rem_val < min_key_usd):
        msg = f"OpenRouter per-key limit_remaining ${rem_val:.6f} is exhausted or below min_key_usd ${min_key_usd:.2f}"
        findings.append(make_finding("openrouter-key-limit-low", msg))
    return {
        "capped": capped,
        "limit_usd": round(lim_val, 6) if lim_val is not None else None,
        "usage_usd": round(use_val, 6) if use_val is not None else None,
        "limit_remaining_usd": round(rem_val, 6) if rem_val is not None else None,
    }


def _eval_openrouter_credits(
    cred_snap: Mapping[str, Any], min_account_usd: float, findings: list[dict[str, Any]]
) -> dict[str, Any]:
    data = as_map(cred_snap.get("data")) if isinstance(cred_snap.get("data"), Mapping) else as_map(cred_snap)
    tot_cred, tot_use = finite_float(data.get("total_credits")), finite_float(data.get("total_usage"))
    if tot_cred is None or tot_use is None or tot_cred < 0.0 or tot_use < 0.0:
        findings.append(make_finding("openrouter-account-invalid", "missing or negative total_credits/total_usage"))
        return {"total_credits_usd": None, "total_usage_usd": None, "account_remaining_usd": None}
    rem = round(tot_cred - tot_use, 6)
    if rem <= 0.0 or rem < min_account_usd:
        msg = f"OpenRouter account remaining ${rem:.6f} is exhausted or below min_account_usd ${min_account_usd:.2f}"
        findings.append(make_finding("openrouter-account-low", msg))
    return {"total_credits_usd": round(tot_cred, 6), "total_usage_usd": round(tot_use, 6), "account_remaining_usd": rem}


def _check_openrouter_budget(
    *,
    check_openrouter: bool,
    min_key_usd: float,
    min_account_usd: float,
    key_snapshot: Mapping[str, Any] | None,
    credits_snapshot: Mapping[str, Any] | None,
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    need_live = (key_snapshot is None or credits_snapshot is None) and check_openrouter
    k_snap, c_snap = key_snapshot, credits_snapshot
    if need_live:
        raw_env_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not raw_env_key or not raw_env_key.strip():
            findings.append(make_finding("openrouter-key-missing", "OPENROUTER_API_KEY is not set in environment"))
            return {"checked": True, "passed": False, "min_key_usd": min_key_usd, "min_account_usd": min_account_usd}
        api_key = raw_env_key.strip(" \t")
        if raw_env_key != api_key or not _is_valid_api_token(api_key):
            findings.append(make_finding("openrouter-key-invalid", "OPENROUTER_API_KEY contains invalid or non-ASCII characters"))
            return {"checked": True, "passed": False, "min_key_usd": min_key_usd, "min_account_usd": min_account_usd}
        if k_snap is None:
            k_snap, k_err = _fetch_openrouter_json(_OPENROUTER_KEY_URL, api_key)
            if k_err is not None:
                findings.append(make_finding("openrouter-fetch-failed", k_err))
        if c_snap is None:
            c_snap, c_err = _fetch_openrouter_json(_OPENROUTER_CREDITS_URL, api_key)
            if c_err is not None:
                findings.append(make_finding("openrouter-fetch-failed", c_err))
    before = len(findings)
    key_info = _eval_openrouter_key(k_snap, min_key_usd, findings) if k_snap is not None else None
    cred_info = _eval_openrouter_credits(c_snap, min_account_usd, findings) if c_snap is not None else None
    if k_snap is None and not need_live:
        findings.append(make_finding("openrouter-key-missing", "key_snapshot missing for OpenRouter check"))
    if c_snap is None and not need_live:
        findings.append(make_finding("openrouter-account-invalid", "credits_snapshot missing for OpenRouter check"))
    return {
        "checked": True,
        "passed": len(findings) == before,
        "min_key_usd": min_key_usd,
        "min_account_usd": min_account_usd,
        "key": key_info,
        "credits": cred_info,
    }


def _dedup_findings(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str | None, str]] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        sig = (str(item["code"]), item.get("task"), str(item["message"]))
        if sig not in seen:
            seen.add(sig)
            out.append(item)
    return out


def _evaluate_one_task(tdir: Path, label: str) -> dict[str, Any]:
    t_findings: list[dict[str, Any]] = []
    _, st_err = stat_regular_file(tdir / "task.toml", root=tdir)
    if st_err is not None:
        t_findings.append(make_finding("invalid-task-toml", st_err, label))
    _scan_task_tree(tdir, label, t_findings)
    toml_seats, compose_bake = _check_task_toml(tdir, label, t_findings)
    _check_compose(tdir, label, toml_seats, compose_bake, t_findings)
    check_dockerignores(tdir, label, t_findings)
    deduped = _dedup_findings(t_findings)
    return {"task": label, "path": str(tdir), "passed": len(deduped) == 0, "findings": deduped}


def run_preflight(
    targets: list[str],
    *,
    check_openrouter: bool = False,
    min_key_usd: float = 1.0,
    min_account_usd: float = 4.50,
    key_snapshot: Mapping[str, Any] | None = None,
    credits_snapshot: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    if not isinstance(targets, list):
        raise InputError("targets must be a list of paths")
    min_k, min_a = finite_float(min_key_usd), finite_float(min_account_usd)
    if min_k is None or min_k < 0.0:
        raise InputError("--min-key-usd must be a non-negative finite float")
    if min_a is None or min_a < 0.0:
        raise InputError("--min-account-usd must be a non-negative finite float")
    do_or = bool(check_openrouter) or (key_snapshot is not None) or (credits_snapshot is not None)
    if not targets and not do_or:
        raise InputError("at least one preflight target path is required")
    if len(targets) > MAX_PREFLIGHT_TASKS:
        raise InputError(f"targets exceed MAX_PREFLIGHT_TASKS ({MAX_PREFLIGHT_TASKS})")
    all_findings: list[dict[str, Any]] = []
    seen_dirs: set[Path] = set()
    task_dirs: list[Path] = []
    roots_map: dict[Path, Path] = {}
    for raw_target in targets:
        _discover_one_target(raw_target, seen_dirs, task_dirs, roots_map, all_findings)
    labels = _compute_task_labels(task_dirs, roots_map)
    task_dirs.sort(key=lambda p: (labels[p], str(p)))
    if targets and not task_dirs and not all_findings:
        raise InputError("no task.toml directories found under preflight targets")
    task_reports = [_evaluate_one_task(tdir, labels[tdir]) for tdir in task_dirs]
    for tr in task_reports:
        all_findings.extend(tr["findings"])
    or_report: dict[str, Any] = {"checked": False, "passed": True}
    if do_or:
        or_findings: list[dict[str, Any]] = []
        or_report = _check_openrouter_budget(
            check_openrouter=bool(check_openrouter),
            min_key_usd=min_k,
            min_account_usd=min_a,
            key_snapshot=key_snapshot,
            credits_snapshot=credits_snapshot,
            findings=or_findings,
        )
        all_findings.extend(_dedup_findings(or_findings))
    passed = len(all_findings) == 0
    report: dict[str, Any] = {
        "command": "preflight",
        "targets": [str(t) for t in targets],
        "passed": passed,
        "tasks_checked": len(task_reports),
        "tasks_passed": sum(1 for r in task_reports if r["passed"]),
        "tasks_failed": sum(1 for r in task_reports if not r["passed"]),
        "findings_count": len(all_findings),
        "findings": all_findings,
        "openrouter": or_report,
        "tasks": task_reports,
    }
    return report, (0 if passed else 1)
