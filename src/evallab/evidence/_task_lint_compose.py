from __future__ import annotations

import os
import posixpath
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import yaml

LintSeverity = Literal["error", "warn", "info"]
EmitFn = Callable[[str, LintSeverity, str, str, str | None], None]

_DANGEROUS_CAPS = frozenset({"SYS_ADMIN", "NET_ADMIN", "SYS_PTRACE", "ALL"})
_FALSE_STRINGS = frozenset({"", "0", "false", "no", "off"})
_OVERRIDE_NAMES = (
    "docker-compose.override.yaml",
    "docker-compose.override.yml",
    "compose.override.yaml",
    "compose.override.yml",
)
_MAX_READ_BYTES = 512_000
_MAX_WALK_DIRS = 2_000
_MAX_WALK_FILES = 500
_SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache"})


def safe_contained_path(root: Path, candidate: Path) -> Path | None:
    assert isinstance(root, Path) and isinstance(candidate, Path)
    try:
        root_res = root.resolve()
        cand_res = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if cand_res != root_res and root_res not in cand_res.parents:
        return None
    cur = candidate
    steps = 0
    while steps < 64:
        steps += 1
        try:
            is_link = cur.is_symlink()
        except (OSError, RuntimeError, ValueError):
            return None
        if is_link:
            try:
                target_res = cur.resolve()
            except (OSError, RuntimeError, ValueError):
                return None
            if target_res != root_res and root_res not in target_res.parents:
                return None
        if cur == root or cur.parent == cur:
            break
        cur = cur.parent
    if cur != root and cur.parent != cur:
        return None
    return cand_res


def read_bounded_text(
    root: Path, candidate: Path, *, max_bytes: int = _MAX_READ_BYTES
) -> tuple[str | None, bool]:
    assert max_bytes > 0
    resolved = safe_contained_path(root, candidate)
    if resolved is None:
        return None, True
    if not resolved.is_file():
        return None, False
    try:
        with resolved.open("rb") as fh:
            raw = fh.read(max_bytes)
        return raw.decode("utf-8", errors="replace"), False
    except OSError:
        return None, False


def iter_bounded_files(
    root: Path,
    start: Path,
    *,
    suffixes: frozenset[str] | None = None,
    filename: str | None = None,
    prune_on_match: bool = False,
    max_dirs: int = _MAX_WALK_DIRS,
    max_files: int = _MAX_WALK_FILES,
) -> tuple[list[Path], list[Path]]:
    assert max_dirs > 0 and max_files > 0
    found: list[Path] = []
    escaped: list[Path] = []
    start_res = safe_contained_path(root, start)
    if start_res is None:
        return found, [start]
    if not start_res.is_dir():
        return found, escaped
    dirs_seen = 0
    for cur_root, dirs, files in os.walk(start_res, followlinks=False):
        dirs_seen += 1
        if dirs_seen > max_dirs:
            break
        cur_p = Path(cur_root)
        if prune_on_match and filename is not None and filename in files:
            dirs[:] = []
        else:
            next_dirs: list[str] = []
            for d in sorted(dirs):
                if d in _SKIP_DIRS:
                    continue
                dpath = cur_p / d
                try:
                    is_link = dpath.is_symlink()
                except (OSError, RuntimeError, ValueError):
                    escaped.append(dpath)
                    continue
                if is_link:
                    if safe_contained_path(root, dpath) is None:
                        escaped.append(dpath)
                    continue
                next_dirs.append(d)
            dirs[:] = next_dirs
        for fname in sorted(files):
            if filename is not None and fname != filename:
                continue
            fpath = cur_p / fname
            if suffixes is not None and fpath.suffix not in suffixes:
                continue
            if safe_contained_path(root, fpath) is None:
                escaped.append(fpath)
                continue
            found.append(fpath)
            if len(found) >= max_files:
                return found, escaped
    return found, escaped


def read_world_helper_text(task_dir: Path, emit: EmitFn) -> str:
    texts: list[str] = []
    world_py = task_dir / "environment" / "world.py"
    if world_py.exists() or world_py.is_symlink():
        txt, esc = read_bounded_text(task_dir, world_py)
        if esc:
            msg = "environment/world.py symlink escapes task dir"
            emit("unsafe_path_escape", "error", "isolation_hazards", msg, "environment/world.py")
        elif txt is not None:
            texts.append(txt)
    world_dir = task_dir / "environment" / "world"
    if world_dir.exists() or world_dir.is_symlink():
        h_files, h_escaped = iter_bounded_files(
            task_dir, world_dir, suffixes=frozenset({".py", ".sh"})
        )
        for esc in h_escaped:
            msg = f"World helper path escapes task dir ({esc.name})"
            emit("unsafe_path_escape", "error", "isolation_hazards", msg, "environment/world")
        for p in h_files:
            txt, _ = read_bounded_text(task_dir, p)
            if txt is not None:
                texts.append(txt)
    return "\n".join(texts)


def find_compose(task_dir: Path) -> tuple[Path | None, bool]:
    for parts in (
        ("environment", "docker-compose.yaml"),
        ("environment", "docker-compose.yml"),
        ("environment", "compose.yaml"),
        ("environment", "compose.yml"),
        ("docker-compose.yaml",),
        ("docker-compose.yml",),
        ("compose.yaml",),
        ("compose.yml",),
    ):
        cand = task_dir.joinpath(*parts)
        if not cand.exists() and not cand.is_symlink():
            continue
        if safe_contained_path(task_dir, cand) is None:
            return cand, True
        if cand.is_file():
            return cand, False
    return None, False


def _has_active_healthcheck(svc: dict[str, Any]) -> bool:
    hc = svc.get("healthcheck")
    if not isinstance(hc, dict) or hc.get("disable") is True:
        return False
    test = hc.get("test")
    if isinstance(test, str):
        cleaned = test.strip()
        return bool(cleaned) and cleaned.upper() != "NONE"
    if isinstance(test, list):
        parts = [str(p).strip() for p in test if str(p).strip()]
        return bool(parts) and parts[0].upper() != "NONE"
    return False


def _is_host_bind_source(src: str) -> bool:
    if not src:
        return False
    low = src.lower()
    if "docker.sock" in low or "docker_engine" in low or "$" in src:
        return True
    if src.startswith(("/", "~", "\\")) or (len(src) >= 2 and src[0].isalpha() and src[1] == ":"):
        return True
    return posixpath.normpath(src.replace("\\", "/")).startswith(("/", "~", ".."))


def _is_windows_drive_short_mount(raw: str) -> bool:
    if len(raw) < 3 or not (raw[0].isalpha() and raw[1] == ":"):
        return False
    if raw[2] not in ("/", "\\") or ":" not in raw[2:]:
        return False
    after = raw[2:].split(":", 1)[1].strip()
    return after.startswith(("/", "\\", "$")) or (
        len(after) >= 3 and after[0].isalpha() and after[1] == ":" and after[2] in ("/", "\\")
    )


def _is_host_bind_mount(
    vol: Any,
    *,
    task_dir: Path | None = None,
    compose_dir: Path | None = None,
) -> bool:
    if isinstance(vol, dict):
        src = str(vol.get("source") or "").strip()
        vtype = str(vol.get("type") or "bind").strip().lower()
        if (
            vtype == "npipe"
            or "docker.sock" in src.lower()
            or "docker_engine" in src.lower()
            or "$" in src
            or "$" in vtype
        ):
            return True
        if vtype in ("volume", "tmpfs", "cluster"):
            return bool(src) and (src.startswith(".") or "/" in src or "\\" in src)
        return True
    raw = str(vol).strip()
    if "docker.sock" in raw.lower() or "docker_engine" in raw.lower():
        return True
    if raw.startswith("\\") or _is_windows_drive_short_mount(raw) or ":" not in raw:
        return raw.startswith("\\") or _is_windows_drive_short_mount(raw)
    src = raw.split(":", 1)[0].strip()
    return _is_host_bind_source(src) or src.startswith(".") or "/" in src or "\\" in src


def _is_unsafe_privileged(raw_priv: Any) -> bool:
    if raw_priv is None or raw_priv is False:
        return False
    if raw_priv is True:
        return True
    if isinstance(raw_priv, (int, float)):
        return raw_priv != 0
    s = str(raw_priv).strip()
    return "$" in s or s.lower() not in _FALSE_STRINGS


def _lint_service_hazards(
    svc_name: str,
    raw_svc: dict[str, Any],
    emit: EmitFn,
    rel: str,
    *,
    task_dir: Path | None = None,
    compose_dir: Path | None = None,
) -> None:
    def hz(code: str, sev: LintSeverity, msg: str) -> None:
        emit(code, sev, "isolation_hazards", msg, rel)

    if _is_unsafe_privileged(raw_svc.get("privileged")):
        hz("compose_privileged_container", "error", f"'{svc_name}' is privileged")
    for hk in ("network_mode", "pid", "ipc"):
        val = str(raw_svc.get(hk) or "").strip()
        low = val.lower()
        if low == "host":
            hz(f"compose_host_{hk}", "error", f"'{svc_name}' uses {hk}: host")
        elif "$" in val or low.startswith(("service:", "container:")):
            code = (
                "compose_shared_network_namespace"
                if hk == "network_mode"
                else f"compose_unsafe_{hk}"
            )
            hz(code, "error", f"'{svc_name}' uses unsafe {hk} ({val})")
        elif hk == "network_mode" and low == "bridge":
            hz("compose_unsafe_network_mode", "error", f"'{svc_name}' uses network_mode: bridge")
    caps = raw_svc.get("cap_add")
    if isinstance(caps, list):
        if len(caps) > 128:
            hz("compose_cap_add_cap_exceeded", "error", f"'{svc_name}' exceeds 128 cap_add entries")
        bad = [
            str(c)
            for c in caps[:128]
            if "$" in str(c) or str(c).strip().upper().removeprefix("CAP_") in _DANGEROUS_CAPS
        ]
        if bad:
            hz("compose_dangerous_cap_add", "error", f"'{svc_name}' adds {', '.join(bad)}")
    elif caps is not None:
        hz("compose_dangerous_cap_add", "error", f"'{svc_name}' has non-list cap_add")
    for dk, code in (
        ("volumes_from", "compose_volumes_from"),
        ("devices", "compose_host_devices"),
        ("extends", "compose_unsafe_extends"),
    ):
        if raw_svc.get(dk):
            hz(code, "error", f"'{svc_name}' uses disallowed directive '{dk}'")
    if raw_svc.get("ports"):
        hz("compose_published_host_ports", "warn", f"'{svc_name}' publishes host ports")
    vols = raw_svc.get("volumes")
    if isinstance(vols, list):
        if len(vols) > 128:
            hz("compose_volumes_cap_exceeded", "error", f"'{svc_name}' exceeds 128 volume entries")
        for vol in vols[:128]:
            if _is_host_bind_mount(vol, task_dir=task_dir, compose_dir=compose_dir):
                hz("compose_host_bind_mount", "error", f"'{svc_name}' mounts host ({vol})")
    elif vols is not None:
        hz("compose_host_bind_mount", "error", f"'{svc_name}' has non-list volumes")


def _resolve_seat_networks(
    svc_name: str,
    raw_svc: dict[str, Any],
    svc_map: dict[str, Any],
    declared_set: set[str],
    svc_by_low: dict[str, str],
    seat_by_low: dict[str, str],
    aliases: dict[str, str],
    seat_networks: dict[str, set[str]],
) -> None:
    eff: set[str] = set()
    cur_svc: dict[str, Any] | None = raw_svc
    visited: set[str] = {svc_name}
    for _ in range(16):
        if cur_svc is None:
            break
        raw_net_mode = str(cur_svc.get("network_mode") or "").strip()
        net_mode = raw_net_mode.lower()
        if net_mode == "none":
            break
        if net_mode.startswith("container:"):
            eff.add(f"__netns:container:{raw_net_mode.split(':', 1)[1].strip().lower()}")
            break
        if net_mode.startswith("service:"):
            t = raw_net_mode.split(":", 1)[1].strip()
            if t in svc_map or t in declared_set:
                canon = t
            elif t.lower() != "main" and t.lower() in svc_by_low:
                canon = svc_by_low[t.lower()]
            elif t.lower() != "main" and t.lower() in seat_by_low:
                canon = seat_by_low[t.lower()]
            else:
                canon = t.lower()
            ns_tag = f"__netns:service:{canon}"
            eff.add(ns_tag)
            if canon in declared_set and canon != "main":
                seat_networks.setdefault(canon, set()).add(ns_tag)
            if canon in visited:
                break
            visited.add(canon)
            nxt = svc_map.get(canon)
            cur_svc = nxt if isinstance(nxt, dict) else None
            continue
        if net_mode:
            eff.add(f"__netmode:{net_mode}")
            break
        raw_nets = cur_svc.get("networks")
        parsed_nets = (
            {s for n in raw_nets if n is not None and (s := str(n).strip())}
            if isinstance(raw_nets, (list, dict))
            else set()
        )
        eff.update(aliases.get(n, n) for n in (parsed_nets or {"default"}))
        break
    seat_networks.setdefault(svc_name, set()).update(eff)


def _lint_seat_service_presence(
    services: dict[str, Any], seats: list[str], rel: str, emit: EmitFn
) -> None:
    if "main" not in services or not isinstance(services["main"], dict):
        msg = f"{rel} lacks valid 'main' service mapping"
        emit("compose_missing_main_service", "error", "compose_contract", msg, rel)
    elif not _has_active_healthcheck(services["main"]):
        msg = "Service 'main' lacks active healthcheck"
        emit("main_missing_healthcheck", "warn", "compose_contract", msg, rel)
    for seat in seats:
        if seat != "main" and (seat not in services or not isinstance(services[seat], dict)):
            msg = f"Seat '{seat}' missing or non-mapping in {rel}"
            emit("seat_missing_compose_service", "error", "compose_contract", msg, rel)


def lint_compose_services(
    services: dict[str, Any],
    seats: list[str],
    rel: str,
    emit: EmitFn,
    *,
    require_seat_contract: bool = True,
    task_dir: Path | None = None,
    compose_dir: Path | None = None,
    net_aliases: dict[str, str] | None = None,
) -> None:
    assert isinstance(services, dict)
    if require_seat_contract:
        _lint_seat_service_presence(services, seats, rel, emit)
    declared_set = set(seats)
    svc_map = {str(k): v for k, v in sorted(services.items(), key=lambda kv: str(kv[0]))}
    svc_by_low = {k.lower(): k for k in sorted(svc_map)}
    seat_by_low = {s.lower(): s for s in sorted(declared_set) if s != "main"}
    aliases = net_aliases or {}
    seat_networks: dict[str, set[str]] = {}
    for raw_key, raw_svc in sorted(services.items(), key=lambda kv: str(kv[0])):
        svc_name = str(raw_key)
        if not isinstance(raw_key, str):
            msg = f"{rel} has non-string service key {raw_key!r}"
            emit("compose_non_string_service_key", "error", "compose_contract", msg, rel)
        if not isinstance(raw_svc, dict):
            msg = f"{rel} service '{svc_name}' is not a mapping"
            emit("compose_invalid_service_mapping", "error", "compose_contract", msg, rel)
            continue
        _lint_service_hazards(
            svc_name, raw_svc, emit, rel, task_dir=task_dir, compose_dir=compose_dir
        )
        if require_seat_contract and svc_name in declared_set and svc_name != "main":
            _resolve_seat_networks(
                svc_name,
                raw_svc,
                svc_map,
                declared_set,
                svc_by_low,
                seat_by_low,
                aliases,
                seat_networks,
            )
    net_to_seats: dict[str, list[str]] = {}
    for s_name, nets in sorted(seat_networks.items()):
        for net in nets:
            net_to_seats.setdefault(net, []).append(s_name)
    for net, members in sorted(net_to_seats.items()):
        if len(members) > 1:
            msg = f"Seats {', '.join(members)} share network '{net}' (F2 side-channel hazard)"
            emit("shared_inter_seat_network", "warn", "isolation_hazards", msg, rel)


def _lint_top_directives_and_overrides(
    task_dir: Path,
    compose_dir: Path,
    compose: dict[str, Any],
    rel_compose: str,
    emit: EmitFn,
) -> None:
    if compose.get("include"):
        msg = f"{rel_compose} uses disallowed top-level 'include'"
        emit("compose_unsafe_include", "error", "isolation_hazards", msg, rel_compose)
    for section, code in (
        ("secrets", "compose_host_secret_file"),
        ("configs", "compose_host_config_file"),
    ):
        raw_sec = compose.get(section)
        if isinstance(raw_sec, dict):
            for k, cfg in sorted(raw_sec.items(), key=lambda kv: str(kv[0])):
                if isinstance(cfg, dict) and cfg.get("file") is not None:
                    msg = f"Top-level {section}.{k} mounts host file ({cfg.get('file')})"
                    emit(code, "error", "isolation_hazards", msg, rel_compose)
    for base_dir in (compose_dir, task_dir):
        for name in _OVERRIDE_NAMES:
            cand = base_dir / name
            if cand.exists() or cand.is_symlink():
                rel_ov = name if base_dir == task_dir else f"{compose_dir.name}/{name}"
                msg = f"Compose override file '{rel_ov}' is disallowed"
                emit("compose_override_file_present", "error", "isolation_hazards", msg, rel_ov)


def _lint_top_volumes_and_aliases(
    task_dir: Path,
    compose_dir: Path,
    compose: dict[str, Any],
    rel_compose: str,
    emit: EmitFn,
) -> dict[str, str]:
    _lint_top_directives_and_overrides(task_dir, compose_dir, compose, rel_compose, emit)
    top_volumes = compose.get("volumes")
    if isinstance(top_volumes, dict):
        for raw_vk, raw_vcfg in sorted(top_volumes.items(), key=lambda kv: str(kv[0])):
            dopts = raw_vcfg.get("driver_opts") if isinstance(raw_vcfg, dict) else None
            if not isinstance(dopts, dict):
                continue
            device = str(dopts.get("device") or "").strip()
            raw_o = str(dopts.get("o") or "")
            opts = {o.strip() for o in raw_o.lower().split(",") if o.strip()}
            if bool(opts & {"bind", "rbind"}) or bool(device) or "$" in raw_o:
                msg = f"Top-level volume '{raw_vk}' mounts host ({device or 'bind'})"
                emit("compose_host_bind_mount", "error", "isolation_hazards", msg, rel_compose)
    net_aliases: dict[str, str] = {}
    top_nets = compose.get("networks")
    if isinstance(top_nets, dict):
        for raw_nk, raw_ncfg in sorted(top_nets.items(), key=lambda kv: str(kv[0])):
            if not isinstance(raw_ncfg, dict):
                continue
            nk = str(raw_nk).strip()
            ext = raw_ncfg.get("external")
            ext_name = str(ext.get("name") or "").strip() if isinstance(ext, dict) else ""
            explicit = str(raw_ncfg.get("name") or "").strip()
            if explicit:
                net_aliases[nk] = f"__extnet:{explicit}"
            elif ext_name:
                net_aliases[nk] = f"__extnet:{ext_name}"
            elif ext is True:
                net_aliases[nk] = f"__extnet:{nk}"
    return net_aliases


def lint_compose_file(
    task_dir: Path,
    compose_path: Path | None,
    compose_escaped: bool,
    seats: list[str],
    emit: EmitFn,
    *,
    require_seat_contract: bool,
) -> tuple[dict[str, Any] | None, str | None, dict[str, str]]:
    if compose_escaped:
        cname = compose_path.name if compose_path is not None else "docker-compose.yaml"
        emit("unsafe_path_escape", "error", "isolation_hazards", "Compose escapes task dir", cname)
        return None, None, {}
    if compose_path is None:
        if require_seat_contract:
            rel_def = "environment/docker-compose.yaml"
            msg = f"Olam seat task requires {rel_def}"
            emit("missing_docker_compose", "error", "compose_contract", msg, rel_def)
        return None, None, {}
    raw_txt, escaped = read_bounded_text(task_dir, compose_path)
    if escaped:
        msg = f"{compose_path.name} escapes task dir"
        emit("unsafe_path_escape", "error", "isolation_hazards", msg, compose_path.name)
        return None, None, {}
    rel_compose = compose_path.resolve().relative_to(task_dir.resolve()).as_posix()
    try:
        compose = yaml.safe_load(raw_txt or "")
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        msg = f"Malformed YAML in {rel_compose}: {exc}"
        emit("invalid_docker_compose_yaml", "error", "compose_contract", msg, rel_compose)
        return None, rel_compose, {}
    services = compose.get("services") if isinstance(compose, dict) else None
    if not isinstance(compose, dict) or not isinstance(services, dict):
        msg = f"{rel_compose} has no 'services' mapping"
        emit("compose_missing_services", "error", "compose_contract", msg, rel_compose)
        return None, rel_compose, {}
    net_aliases = _lint_top_volumes_and_aliases(
        task_dir, compose_path.parent, compose, rel_compose, emit
    )
    lint_compose_services(
        services,
        seats,
        rel_compose,
        emit,
        require_seat_contract=require_seat_contract,
        task_dir=task_dir,
        compose_dir=compose_path.parent,
        net_aliases=net_aliases,
    )
    return compose, rel_compose, net_aliases
