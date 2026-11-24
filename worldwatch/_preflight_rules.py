from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from worldwatch._io import read_regular_text, split_text_lines_strict

_FORBIDDEN_MOUNT_KEYS = frozenset({"volumes", "mounts", "binds", "host_mounts"})
_FORBIDDEN_COMPOSE_KEYS = frozenset({"volumes", "mounts", "binds", "host_mounts", "source"})
_FORBIDDEN_SEGMENTS = frozenset({"tests", "solution", "world"})
_MAX_TOML_WALK = 10_000


def make_finding(code: str, message: str, task: str | None = None) -> dict[str, Any]:
    return {"code": code, "severity": "error", "task": task, "message": message}


def strip_yaml_comment(line: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_double:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif (
            ch == "#"
            and not in_single
            and not in_double
            and (idx == 0 or line[idx - 1].isspace())
        ):
            return line[:idx].rstrip()
    return line.rstrip()


def _unquote_yaml(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        inner = s[1:-1]
        if s[0] == '"':
            return inner.replace('\\"', '"').replace("\\\\", "\\").strip()
        return inner.replace("''", "'").strip()
    return s


def _clean_yaml_scalar(raw: str) -> str:
    s = raw.strip()
    for _ in range(4):
        if s.startswith(("&", "!")) and any(ch.isspace() for ch in s):
            s = s.split(None, 1)[1].strip()
        else:
            break
    return _unquote_yaml(s)


def has_forbidden_path(
    text: str,
    *,
    allow_logs_world: bool = False,
    allow_world_dir: bool = False,
    allow_bare_world: bool = False,
) -> bool:
    clean = text.strip().strip("\"'")
    if not clean:
        return False
    if "docker.sock" in clean or "source:" in clean:
        return True
    norm = clean.replace("\\", "/")
    if ".." in [p for p in norm.split("/") if p]:
        return True
    expanded: list[str] = []
    for raw_tok in norm.split():
        tok = raw_tok.strip("\"',[]{}")
        if not tok or tok.startswith(("http://", "https://")):
            continue
        expanded.extend(p for p in tok.split(":") if p)
    for seg in expanded:
        s_clean = _unquote_yaml(seg)
        while s_clean.startswith("./"):
            s_clean = s_clean[2:]
        no_lead = s_clean.lstrip("/")
        if allow_logs_world and (no_lead == "logs/world" or no_lead.startswith("logs/world/")):
            continue
        if allow_bare_world and s_clean == "world":
            continue
        first = no_lead.split("/", 1)[0]
        if first == "world" and allow_world_dir:
            continue
        if first in _FORBIDDEN_SEGMENTS:
            return True
    return False


def check_toml_security(doc: Mapping[str, Any], label: str, findings: list[dict[str, Any]]) -> None:
    stack: list[tuple[tuple[str, ...], Any]] = [((), doc)]
    visited = 0
    while stack:
        visited += 1
        if visited > _MAX_TOML_WALK:
            findings.append(make_finding("invalid-task-toml", "task.toml exceeds max node limit", label))
            return
        path, node = stack.pop()
        if isinstance(node, Mapping):
            for raw_k, val in node.items():
                k = str(raw_k).strip()
                k_low = k.lower()
                if k_low in _FORBIDDEN_MOUNT_KEYS:
                    findings.append(make_finding("forbidden-host-mount", f"forbidden mount key {k!r} in task.toml", label))
                elif k_low == "privileged" and (val is True or str(val).strip().lower() in ("true", "yes", "1")):
                    findings.append(make_finding("compose-privileged-forbidden", "privileged=true in task.toml", label))
                elif k_low == "network_mode" and str(val or "").strip().lower() == "host":
                    findings.append(make_finding("compose-host-network-forbidden", "network_mode=host in task.toml", label))
                stack.append(((*path, k), val))
        elif isinstance(node, list):
            for idx, item in enumerate(node[:1024]):
                stack.append(((*path, str(idx)), item))
        elif isinstance(node, str) and path and path[0] not in ("task", "metadata"):
            allow_lw = path[0] == "artifacts"
            if has_forbidden_path(node, allow_logs_world=allow_lw):
                loc = ".".join(path[:2])
                findings.append(make_finding("forbidden-host-mount", f"forbidden path {node!r} in task.toml ({loc})", label))


def _split_yaml_kv(text: str) -> tuple[str, bool, str]:
    s = text.strip()
    if not s:
        return "", False, ""
    if s[0] in ('"', "'"):
        q = s[0]
        escaped = False
        for idx in range(1, len(s)):
            ch = s[idx]
            if escaped:
                escaped = False
                continue
            if ch == "\\" and q == '"':
                escaped = True
                continue
            if ch == q:
                after = s[idx + 1 :].lstrip()
                if after.startswith(":") and (len(after) == 1 or after[1] != "/"):
                    return _unquote_yaml(s[: idx + 1]), True, after[1:].strip()
                return "", False, s
        return "", False, s
    key, has_colon, rest = s.partition(":")
    key_s = key.strip()
    if not has_colon or not key_s or "/" in key_s or (rest and rest.startswith("//")):
        return "", False, s
    return _unquote_yaml(key_s), True, rest.strip()


def _parse_inline_list(raw: str) -> list[str]:
    val = raw.strip()
    if not (val.startswith("[") and val.endswith("]")):
        return []
    inner = val[1:-1].strip()
    if not inner:
        return []
    return [_unquote_yaml(tok) for tok in inner.split(",") if _unquote_yaml(tok)]


def _check_compose_kv_pair(
    key_u: str,
    val_raw: str,
    label: str,
    findings: list[dict[str, Any]],
    *,
    allow_world_dir: bool = False,
    allow_bare_world: bool = False,
) -> None:
    k_low = key_u.lower()
    val_u = _clean_yaml_scalar(val_raw).lower()
    if k_low in _FORBIDDEN_COMPOSE_KEYS:
        findings.append(make_finding("forbidden-host-mount", f"forbidden {key_u}: in compose", label))
    if k_low == "privileged" and val_u in ("true", "yes", "1", "on"):
        findings.append(make_finding("compose-privileged-forbidden", "privileged: true in compose", label))
    if k_low == "network_mode" and val_u == "host":
        findings.append(make_finding("compose-host-network-forbidden", "network_mode: host in compose", label))
    if val_raw and has_forbidden_path(
        val_raw,
        allow_logs_world=True,
        allow_world_dir=allow_world_dir,
        allow_bare_world=allow_bare_world,
    ):
        findings.append(make_finding("forbidden-host-mount", f"forbidden mount/path in compose: {val_raw[:80]}", label))


def _scan_flow_mappings(clean: str, label: str, findings: list[dict[str, Any]]) -> bool:
    in_s = in_d = escaped = False
    depth = 0
    buf: list[str] = []
    segments: list[str] = []
    saw_flow = False
    bracket_depth = 0
    for idx, ch in enumerate(clean):
        if escaped:
            escaped = False
            if depth > 0:
                buf.append(ch)
            continue
        if ch == "\\" and in_d:
            escaped = True
            if depth > 0:
                buf.append(ch)
            continue
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif not in_s and not in_d:
            if ch == "{" and (idx == 0 or clean[idx - 1] != "$"):
                saw_flow = True
                if depth > 0 and buf:
                    segments.append("".join(buf))
                    buf.clear()
                depth += 1
                continue
            if ch == "}" and depth > 0:
                if buf:
                    segments.append("".join(buf))
                    buf.clear()
                depth -= 1
                continue
            if depth > 0:
                if ch == "[":
                    bracket_depth += 1
                elif ch == "]" and bracket_depth > 0:
                    bracket_depth -= 1
                elif ch == "," and bracket_depth == 0:
                    segments.append("".join(buf))
                    buf.clear()
                    continue
        if depth > 0:
            buf.append(ch)
    if buf:
        segments.append("".join(buf))
    for seg in segments:
        fk, fhas, fv = _split_yaml_kv(seg)
        if fhas:
            _check_compose_kv_pair(fk, fv, label, findings)
        elif seg.strip() and has_forbidden_path(seg, allow_logs_world=True):
            findings.append(make_finding("forbidden-host-mount", f"forbidden path in compose flow: {seg.strip()[:80]}", label))
    if saw_flow:
        findings.append(make_finding("invalid-compose", "YAML flow mapping {...} is forbidden in compose", label))
    return saw_flow


def _update_compose_topology(
    path_keys: list[str],
    has_colon: bool,
    is_seq: bool,
    val_raw: str,
    seq_val: str,
    clean_line: str,
    services: set[str],
    svc_nets: dict[str, set[str]],
    net_internal: dict[str, bool],
) -> bool:
    top_sec = path_keys[0] if len(path_keys) >= 1 else ""
    sub_key = path_keys[1] if len(path_keys) >= 2 else ""
    prop_key = path_keys[2] if len(path_keys) >= 3 else ""
    leaf_key = path_keys[-1] if path_keys else ""
    if top_sec == "services" and sub_key:
        services.add(sub_key)
        svc_nets.setdefault(sub_key, set())
    elif top_sec == "networks" and sub_key:
        net_internal.setdefault(sub_key, False)
    if top_sec == "networks" and sub_key and prop_key == "internal":
        cand = val_raw if has_colon else ("" if is_seq else clean_line.strip())
        if cand and cand not in ("|", ">", "|-", ">-"):
            net_internal[sub_key] = _clean_yaml_scalar(cand).lower() == "true"
    if top_sec == "services" and sub_key and prop_key == "networks":
        if len(path_keys) == 3 and has_colon and val_raw:
            svc_nets[sub_key].update(_parse_inline_list(val_raw))
        elif len(path_keys) == 3 and is_seq and seq_val:
            svc_nets[sub_key].add(_unquote_yaml(seq_val))
        elif len(path_keys) == 4 and has_colon and leaf_key != "aliases":
            svc_nets[sub_key].add(leaf_key)
    return top_sec == "services" and sub_key in ("main", "world") and prop_key == "healthcheck" and "/health" in clean_line


def inspect_compose_lines(
    lines: list[str],
    label: str,
    findings: list[dict[str, Any]],
) -> tuple[set[str], dict[str, set[str]], dict[str, bool], bool]:
    services: set[str] = set()
    svc_nets: dict[str, set[str]] = {}
    net_internal: dict[str, bool] = {}
    has_health = False
    stack: list[tuple[int, str]] = []
    for raw_line in lines:
        clean = strip_yaml_comment(raw_line)
        if not clean.strip():
            continue
        lead = clean[: len(clean) - len(clean.lstrip())]
        if "\t" in lead:
            findings.append(make_finding("invalid-compose", "tab indentation forbidden in compose", label))
        indent = len(lead.expandtabs(2))
        stripped = clean.strip()
        _scan_flow_mappings(clean, label, findings)
        is_seq = stripped == "-" or stripped.startswith(("- ", "-\t"))
        seq_val = stripped[1:].strip() if is_seq else ""
        key_c, has_colon, val_c = ("", False, "") if is_seq else _split_yaml_kv(stripped)
        if has_colon:
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, key_c))
        elif is_seq:
            while stack and stack[-1][0] > indent:
                stack.pop()
        else:
            while stack and stack[-1][0] >= indent:
                stack.pop()
        path_keys = [k for _, k in stack]
        if _update_compose_topology(path_keys, has_colon, is_seq, val_c, seq_val, stripped, services, svc_nets, net_internal):
            has_health = True
        top_sec = path_keys[0] if len(path_keys) >= 1 else ""
        sub_key = path_keys[1] if len(path_keys) >= 2 else ""
        prop_key = path_keys[2] if len(path_keys) >= 3 else ""
        leaf_key = path_keys[-1] if path_keys else ""
        is_world_cmd = top_sec == "services" and sub_key in ("main", "world") and prop_key == "command"
        is_alias = top_sec == "services" and prop_key == "networks" and leaf_key == "aliases"
        if has_colon:
            _check_compose_kv_pair(
                key_c, val_c, label, findings, allow_world_dir=is_world_cmd, allow_bare_world=is_alias
            )
        elif is_seq:
            sk, shas, sv = _split_yaml_kv(seq_val)
            if shas:
                _check_compose_kv_pair(sk, sv, label, findings, allow_world_dir=is_world_cmd, allow_bare_world=is_alias)
            elif has_forbidden_path(seq_val, allow_logs_world=True, allow_world_dir=is_world_cmd, allow_bare_world=is_alias):
                findings.append(make_finding("forbidden-host-mount", f"forbidden mount/path in compose: {stripped[:80]}", label))
        else:
            _check_compose_kv_pair(
                leaf_key, stripped, label, findings, allow_world_dir=is_world_cmd, allow_bare_world=is_alias
            )
    return services, svc_nets, net_internal, has_health


def check_dockerignores(task_dir: Path, label: str, findings: list[dict[str, Any]]) -> None:
    env_dir = task_dir / "environment"
    if not (
        os.path.lexists(env_dir)
        or os.path.lexists(env_dir / "Dockerfile")
        or os.path.lexists(env_dir / "docker-compose.yaml")
        or os.path.lexists(env_dir / "docker-compose.yml")
    ):
        return
    required = [env_dir / ".dockerignore"]
    if os.path.lexists(env_dir / "seat"):
        required.append(env_dir / "seat" / ".dockerignore")
    for di_path in required:
        rel = di_path.relative_to(task_dir).as_posix()
        txt, err = read_regular_text(di_path, max_bytes=65_536, root=task_dir)
        if err is not None or txt is None or not txt.strip():
            findings.append(make_finding("missing-dockerignore", f"missing or empty {rel}", label))
            continue
        try:
            raw_lines = split_text_lines_strict(txt)
        except ValueError as exc:
            findings.append(make_finding("missing-dockerignore", f"invalid {rel}: {exc}", label))
            continue
        has_pycache = False
        has_pyc = False
        for raw_ln in raw_lines:
            rule = raw_ln.strip()
            if not rule or rule.startswith("#"):
                continue
            norm = rule.rstrip("/")
            if norm in ("!__pycache__", "!**/__pycache__", "!*/__pycache__"):
                has_pycache = False
            elif norm in ("__pycache__", "**/__pycache__", "*/__pycache__"):
                has_pycache = True
            if norm in ("!*.pyc", "!**/*.pyc", "!*.py[cod]", "!**/*.py[cod]"):
                has_pyc = False
            elif norm in ("*.pyc", "**/*.pyc", "*.py[cod]", "**/*.py[cod]"):
                has_pyc = True
        if not (has_pycache and has_pyc):
            findings.append(
                make_finding("missing-dockerignore", f"{rel} missing required __pycache__ and *.pyc rules", label)
            )
