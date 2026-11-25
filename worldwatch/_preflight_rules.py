from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from worldwatch._io import read_regular_text, split_text_lines_strict

_FORBIDDEN_MOUNT_KEYS = frozenset({"volumes", "mounts", "binds", "host_mounts"})
_FORBIDDEN_COMPOSE_KEYS = frozenset({"volumes", "mounts", "binds", "host_mounts", "source"})
_FORBIDDEN_SEGMENTS = frozenset({"tests", "solution", "world"})
_MAX_TOML_WALK = 10_000


@dataclass
class _AnchorProps:
    prefix: tuple[str, ...]
    nets: set[str] = field(default_factory=set)
    build_dirs: set[str] = field(default_factory=set)
    internal: bool | None = None
    external: bool = False
    eff_name: str | None = None
    merges: list[str] = field(default_factory=list)


@dataclass
class ComposeTopology:
    services: set[str] = field(default_factory=set)
    svc_nets: dict[str, set[str]] = field(default_factory=dict)
    explicit_svc_nets: set[str] = field(default_factory=set)
    svc_merges: dict[str, list[str]] = field(default_factory=dict)
    svc_builds: dict[str, set[str]] = field(default_factory=dict)
    explicit_svc_builds: set[str] = field(default_factory=set)
    net_internal: dict[str, bool] = field(default_factory=dict)
    explicit_net_internal: set[str] = field(default_factory=set)
    net_names: dict[str, str] = field(default_factory=dict)
    net_external: set[str] = field(default_factory=set)
    net_merges: dict[str, list[str]] = field(default_factory=dict)
    anchors: dict[str, _AnchorProps] = field(default_factory=dict)
    has_health: bool = False


def make_finding(code: str, message: str, task: str | None = None) -> dict[str, Any]:
    return {"code": code, "severity": "error", "task": task, "message": message}


def strip_yaml_comment(line: str) -> str:
    in_single = in_double = escaped = False
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
        elif ch == "#" and not in_single and not in_double and (idx == 0 or line[idx - 1].isspace()):
            return line[:idx].rstrip()
    return line.rstrip()


def _unquote_yaml(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        inner = s[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\").strip() if s[0] == '"' else inner.replace("''", "'").strip()
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
    text: str, *, allow_logs_world: bool = False, allow_world_dir: bool = False, allow_bare_world: bool = False
) -> bool:
    clean = text.strip().strip("\"'")
    if not clean:
        return False
    if "docker.sock" in clean or "source:" in clean:
        return True
    expanded: list[str] = []
    for raw_chunk in clean.replace("\\", "/").replace(",", " ").split():
        tok = _unquote_yaml(raw_chunk).strip("\"',[]{}")
        if not tok or (tok.startswith(("http://", "https://")) and ".." not in tok):
            continue
        for eq_part in (tok.split("=") if "=" in tok else (tok,)):
            ep = _unquote_yaml(eq_part).strip("\"',[]{}")
            if ep and not (ep.startswith(("http://", "https://")) and ".." not in ep):
                expanded.extend(p for p in ep.split(":") if p)
    for seg in expanded:
        s_clean = _unquote_yaml(seg).strip("\"',[]{}")
        if not s_clean:
            continue
        parts = [p for p in s_clean.split("/") if p and p != "."]
        if ".." in parts:
            return True
        if not parts:
            continue
        if allow_logs_world and parts[:2] == ["logs", "world"]:
            continue
        if (allow_bare_world and s_clean == "world") or (parts[0] == "world" and allow_world_dir):
            continue
        if parts[0] in _FORBIDDEN_SEGMENTS:
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
        elif isinstance(node, str) and path and path[0] not in ("task", "metadata") and has_forbidden_path(node, allow_logs_world=(path[0] == "artifacts")):
            findings.append(make_finding("forbidden-host-mount", f"forbidden path {node!r} in task.toml ({'.'.join(path[:2])})", label))


def _split_yaml_kv(text: str) -> tuple[str, bool, str]:
    s = text.strip()
    if not s:
        return "", False, ""
    if s[0] in ('"', "'"):
        q, escaped = s[0], False
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
    val = _clean_yaml_scalar(raw)
    if not (val.startswith("[") and val.endswith("]")):
        return []
    inner = val[1:-1].strip()
    return [_clean_yaml_scalar(tok) for tok in inner.split(",") if _clean_yaml_scalar(tok)] if inner else []


def _parse_merge_refs(raw: str) -> list[str]:
    s = raw.strip()
    items = _parse_inline_list(s) if (s.startswith("[") and s.endswith("]")) else [s]
    return [tok[1:].strip() for item in items if (tok := _unquote_yaml(item).strip()).startswith("*") and len(tok) > 1]


def _norm_rel_build_dir(raw: str) -> str | None:
    s = _clean_yaml_scalar(raw).replace("\\", "/").strip()
    if not s or s.startswith(("/", "http://", "https://", "git@", "{")):
        return None
    while s.startswith("./"):
        s = s[2:]
    s = s.rstrip("/")
    if not s or s == ".":
        return None
    parts = [p for p in s.split("/") if p and p != "."]
    return "/".join(parts) if (parts and ".." not in parts) else None


def _check_compose_kv_pair(
    key_u: str, val_raw: str, label: str, findings: list[dict[str, Any]],
    *, allow_world_dir: bool = False, allow_bare_world: bool = False,
) -> None:
    k_low, val_u = key_u.lower(), _clean_yaml_scalar(val_raw).lower()
    if k_low in _FORBIDDEN_COMPOSE_KEYS:
        findings.append(make_finding("forbidden-host-mount", f"forbidden {key_u}: in compose", label))
    if k_low == "privileged" and val_u in ("true", "yes", "1", "on"):
        findings.append(make_finding("compose-privileged-forbidden", "privileged: true in compose", label))
    if k_low == "network_mode" and val_u == "host":
        findings.append(make_finding("compose-host-network-forbidden", "network_mode: host in compose", label))
    if val_raw and has_forbidden_path(val_raw, allow_logs_world=True, allow_world_dir=allow_world_dir, allow_bare_world=allow_bare_world):
        findings.append(make_finding("forbidden-host-mount", f"forbidden mount/path in compose: {val_raw[:80]}", label))


def _scan_flow_mappings(clean: str, label: str, findings: list[dict[str, Any]]) -> list[tuple[str, str]]:
    in_s = in_d = escaped = saw_flow = False
    depth = bracket_depth = 0
    buf, segments = list[str](), list[str]()
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
    pairs: list[tuple[str, str]] = []
    for seg in segments:
        fk, fhas, fv = _split_yaml_kv(seg)
        if fhas:
            pairs.append((fk, fv))
            _check_compose_kv_pair(fk, fv, label, findings)
        elif seg.strip() and has_forbidden_path(seg, allow_logs_world=True):
            findings.append(make_finding("forbidden-host-mount", f"forbidden path in compose flow: {seg.strip()[:80]}", label))
    if saw_flow:
        findings.append(make_finding("invalid-compose", "YAML flow mapping {...} is forbidden in compose", label))
    return pairs


def _record_anchor_line(
    anc: _AnchorProps, rel: list[str], has_colon: bool, is_seq: bool, val_raw: str, seq_val: str, cand: str
) -> None:
    r0, r1 = (rel[0] if len(rel) >= 1 else ""), (rel[1] if len(rel) >= 2 else "")
    if len(rel) == 1 and r0 == "<<":
        anc.merges.extend(_parse_merge_refs(val_raw if has_colon else seq_val))
    elif r0 == "networks":
        if len(rel) == 1 and has_colon and val_raw:
            anc.nets.update(_parse_inline_list(val_raw))
        elif len(rel) == 1 and is_seq and seq_val:
            anc.nets.add(_clean_yaml_scalar(seq_val))
        elif len(rel) == 2 and has_colon and r1 not in ("aliases", "<<"):
            anc.nets.add(r1)
    elif r0 == "build":
        raw_b = val_raw if (len(rel) == 1 and has_colon and not val_raw.startswith("&")) else (cand if len(rel) == 2 and r1 == "context" else "")
        if raw_b and (bd := _norm_rel_build_dir(raw_b)) is not None:
            anc.build_dirs.add(bd)
    elif len(rel) == 1 and r0 == "internal" and cand and cand not in ("|", ">", "|-", ">-"):
        anc.internal = _clean_yaml_scalar(cand).lower() == "true"
    elif r0 == "external" and (len(rel) >= 2 or (cand and _clean_yaml_scalar(cand).lower() in ("true", "yes", "1", "on"))):
        anc.external = True
    elif len(rel) == 1 and r0 == "name" and cand and cand not in ("|", ">", "|-", ">-") and (enm := _clean_yaml_scalar(cand)):
        anc.eff_name = enm


def _apply_flow_pairs_to_topo(path_keys: list[str], flow_pairs: list[tuple[str, str]], topo: ComposeTopology) -> None:
    if len(path_keys) != 2 or not flow_pairs:
        return
    top_sec, sub_key = path_keys[0], path_keys[1]
    for fk, fv in flow_pairs:
        if top_sec == "networks":
            if fk == "internal":
                topo.explicit_net_internal.add(sub_key)
                topo.net_internal[sub_key] = _clean_yaml_scalar(fv).lower() == "true"
            elif fk == "external" and _clean_yaml_scalar(fv).lower() in ("true", "yes", "1", "on"):
                topo.net_external.add(sub_key)
            elif fk == "name" and (enm := _clean_yaml_scalar(fv)):
                topo.net_names[sub_key] = enm
            elif fk == "<<":
                topo.net_merges.setdefault(sub_key, []).extend(_parse_merge_refs(fv))
        elif top_sec == "services":
            if fk == "networks":
                topo.explicit_svc_nets.add(sub_key)
                topo.svc_nets[sub_key].update(_parse_inline_list(fv))
            elif fk == "build" and (bd := _norm_rel_build_dir(fv)) is not None:
                topo.explicit_svc_builds.add(sub_key)
                topo.svc_builds[sub_key].add(bd)
            elif fk == "<<":
                topo.svc_merges.setdefault(sub_key, []).extend(_parse_merge_refs(fv))


def _update_compose_topology(
    path_keys: list[str], has_colon: bool, is_seq: bool, val_raw: str, seq_val: str,
    clean_line: str, flow_pairs: list[tuple[str, str]], topo: ComposeTopology,
) -> None:
    if has_colon and val_raw.strip().startswith("&") and (anc_nm := val_raw.strip()[1:].split(None, 1)[0].strip(",[]{}")):
        topo.anchors[anc_nm] = _AnchorProps(prefix=tuple(path_keys))
    cand = val_raw if has_colon else ("" if is_seq else clean_line.strip())
    for anc in topo.anchors.values():
        plen = len(anc.prefix)
        if len(path_keys) > plen and tuple(path_keys[:plen]) == anc.prefix:
            _record_anchor_line(anc, path_keys[plen:], has_colon, is_seq, val_raw, seq_val, cand)
    top_sec = path_keys[0] if len(path_keys) >= 1 else ""
    sub_key = path_keys[1] if len(path_keys) >= 2 else ""
    prop_key = path_keys[2] if len(path_keys) >= 3 else ""
    leaf_key = path_keys[-1] if path_keys else ""
    if top_sec == "services" and sub_key:
        topo.services.add(sub_key)
        topo.svc_nets.setdefault(sub_key, set())
        topo.svc_builds.setdefault(sub_key, set())
        if len(path_keys) == 3 and prop_key == "<<":
            topo.svc_merges.setdefault(sub_key, []).extend(_parse_merge_refs(val_raw if has_colon else seq_val))
        elif prop_key == "networks":
            topo.explicit_svc_nets.add(sub_key)
            if len(path_keys) == 3 and has_colon and val_raw:
                topo.svc_nets[sub_key].update(_parse_inline_list(val_raw))
            elif len(path_keys) == 3 and is_seq and seq_val:
                topo.svc_nets[sub_key].add(_clean_yaml_scalar(seq_val))
            elif len(path_keys) == 4 and has_colon and leaf_key not in ("aliases", "<<"):
                topo.svc_nets[sub_key].add(leaf_key)
        elif prop_key == "build":
            topo.explicit_svc_builds.add(sub_key)
            raw_b = val_raw if (len(path_keys) == 3 and has_colon and not val_raw.startswith("&")) else (cand if len(path_keys) == 4 and leaf_key == "context" else "")
            if raw_b and (bd := _norm_rel_build_dir(raw_b)) is not None:
                topo.svc_builds[sub_key].add(bd)
        if sub_key in ("main", "world") and prop_key == "healthcheck" and "/health" in clean_line:
            topo.has_health = True
    elif top_sec == "networks" and sub_key:
        topo.net_internal.setdefault(sub_key, False)
        if len(path_keys) == 3 and prop_key == "<<":
            topo.net_merges.setdefault(sub_key, []).extend(_parse_merge_refs(val_raw if has_colon else seq_val))
        elif len(path_keys) == 3 and prop_key == "internal" and cand and cand not in ("|", ">", "|-", ">-"):
            topo.explicit_net_internal.add(sub_key)
            topo.net_internal[sub_key] = _clean_yaml_scalar(cand).lower() == "true"
        elif prop_key == "external" and (len(path_keys) >= 4 or (cand and _clean_yaml_scalar(cand).lower() in ("true", "yes", "1", "on"))):
            topo.net_external.add(sub_key)
        elif len(path_keys) == 3 and prop_key == "name" and cand and cand not in ("|", ">", "|-", ">-") and (enm := _clean_yaml_scalar(cand)):
            topo.net_names[sub_key] = enm
    _apply_flow_pairs_to_topo(path_keys, flow_pairs, topo)


def _expand_anchor_chain(refs: list[str], anchors: Mapping[str, _AnchorProps]) -> list[_AnchorProps]:
    queue, seen, resolved, idx = list(refs), set[str](), list[_AnchorProps](), 0
    while idx < len(queue) and idx < 64:
        nm = queue[idx]
        idx += 1
        if nm not in seen and nm in anchors:
            seen.add(nm)
            resolved.append(anchors[nm])
            queue.extend(anchors[nm].merges[:16])
    return resolved


def _resolve_compose_merges(topo: ComposeTopology) -> None:
    for svc, refs in topo.svc_merges.items():
        chain = _expand_anchor_chain(refs, topo.anchors)
        if svc not in topo.explicit_svc_nets and (inh_n := next((a.nets for a in chain if a.nets), None)):
            topo.svc_nets.setdefault(svc, set()).update(inh_n)
        if svc not in topo.explicit_svc_builds and (inh_b := next((a.build_dirs for a in chain if a.build_dirs), None)):
            topo.svc_builds.setdefault(svc, set()).update(inh_b)
    for net, refs in topo.net_merges.items():
        for anc in _expand_anchor_chain(refs, topo.anchors):
            if net not in topo.explicit_net_internal and anc.internal is not None:
                topo.net_internal[net] = anc.internal
                topo.explicit_net_internal.add(net)
            if anc.external:
                topo.net_external.add(net)
            if net not in topo.net_names and anc.eff_name:
                topo.net_names[net] = anc.eff_name
    for ext_n in topo.net_external:
        topo.net_internal[ext_n] = False


def inspect_compose_topology(
    lines: list[str], label: str, findings: list[dict[str, Any]]
) -> ComposeTopology:
    topo = ComposeTopology()
    stack: list[tuple[int, str]] = []
    for raw_line in lines:
        clean = strip_yaml_comment(raw_line)
        if not clean.strip():
            continue
        lead = clean[: len(clean) - len(clean.lstrip())]
        if "\t" in lead:
            findings.append(make_finding("invalid-compose", "tab indentation forbidden in compose", label))
        indent, stripped = len(lead.expandtabs(2)), clean.strip()
        flow_pairs = _scan_flow_mappings(clean, label, findings)
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
        _update_compose_topology(path_keys, has_colon, is_seq, val_c, seq_val, stripped, flow_pairs, topo)
        top_sec = path_keys[0] if len(path_keys) >= 1 else ""
        sub_key = path_keys[1] if len(path_keys) >= 2 else ""
        prop_key = path_keys[2] if len(path_keys) >= 3 else ""
        leaf_key = path_keys[-1] if path_keys else ""
        is_world_cmd = top_sec == "services" and sub_key in ("main", "world") and prop_key == "command"
        is_alias = top_sec == "services" and prop_key == "networks" and leaf_key == "aliases"
        if has_colon:
            _check_compose_kv_pair(key_c, val_c, label, findings, allow_world_dir=is_world_cmd, allow_bare_world=is_alias)
        elif is_seq:
            sk, shas, sv = _split_yaml_kv(seq_val)
            if shas:
                _check_compose_kv_pair(sk, sv, label, findings, allow_world_dir=is_world_cmd, allow_bare_world=is_alias)
            elif has_forbidden_path(seq_val, allow_logs_world=True, allow_world_dir=is_world_cmd, allow_bare_world=is_alias):
                findings.append(make_finding("forbidden-host-mount", f"forbidden mount/path in compose: {stripped[:80]}", label))
        else:
            _check_compose_kv_pair(leaf_key, stripped, label, findings, allow_world_dir=is_world_cmd, allow_bare_world=is_alias)
    _resolve_compose_merges(topo)
    return topo


def inspect_compose_lines(
    lines: list[str], label: str, findings: list[dict[str, Any]]
) -> tuple[set[str], dict[str, set[str]], dict[str, bool], bool]:
    topo = inspect_compose_topology(lines, label, findings)
    return topo.services, topo.svc_nets, topo.net_internal, topo.has_health


def _collect_seat_dirs(env_dir: Path) -> list[Path]:
    if not os.path.isdir(env_dir) or os.path.islink(env_dir):
        return []
    try:
        entries = sorted(os.scandir(env_dir), key=lambda e: e.name)
    except OSError:
        return []
    return [Path(e.path) for e in entries[:512] if e.name.startswith("seat") and not e.is_symlink() and e.is_dir(follow_symlinks=False)]


def _eval_dockerignore_rules(raw_lines: list[str]) -> bool:
    has_pycache = has_pyc = False
    for raw_ln in raw_lines:
        rule = raw_ln.strip()
        if not rule or rule.startswith("#"):
            continue
        if rule.startswith("!"):
            neg = rule[1:].strip().replace("\\", "/")
            while neg.startswith(("./", "/")):
                neg = neg[2:] if neg.startswith("./") else neg[1:]
            segs = [s for s in neg.rstrip("/").split("/") if s]
            if not segs or "__pycache__" in neg or any(fnmatch.fnmatchcase("__pycache__", s) for s in segs):
                has_pycache = False
            if not segs or segs[-1] == "**" or fnmatch.fnmatchcase("x.pyc", segs[-1]) or fnmatch.fnmatchcase("x.pyo", segs[-1]):
                has_pyc = False
            continue
        pos = rule.replace("\\", "/")
        while pos.startswith(("./", "/")):
            pos = pos[2:] if pos.startswith("./") else pos[1:]
        pos = pos.rstrip("/")
        if pos in ("__pycache__", "**/__pycache__", "*/__pycache__"):
            has_pycache = True
        if pos in ("*.pyc", "**/*.pyc", "*.py[cod]", "**/*.py[cod]"):
            has_pyc = True
    return has_pycache and has_pyc


def check_dockerignores(
    task_dir: Path, label: str, findings: list[dict[str, Any]], *, extra_build_rels: Iterable[str] = ()
) -> None:
    env_dir = task_dir / "environment"
    if not any(os.path.lexists(p) for p in (env_dir, env_dir / "Dockerfile", env_dir / "docker-compose.yaml", env_dir / "docker-compose.yml")):
        return
    cands = [
        env_dir / ".dockerignore",
        *(sdir / ".dockerignore" for sdir in _collect_seat_dirs(env_dir)),
        *(env_dir / rel_b / ".dockerignore" for rel_b in sorted(set(extra_build_rels)) if rel_b),
    ]
    for di_path in dict.fromkeys(cands):
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
        if not _eval_dockerignore_rules(raw_lines):
            findings.append(make_finding("missing-dockerignore", f"{rel} missing required __pycache__ and *.pyc rules", label))
