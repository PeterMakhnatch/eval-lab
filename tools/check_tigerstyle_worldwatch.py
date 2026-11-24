#!/usr/bin/env python3
from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_LINES = 600
MAX_FN_LINES = 70
MAX_FILES = 1_000
MAX_NODES = 200_000
ALLOWED_COMMENTS = re.compile(
    r"^#\s*(?:noqa(?::\s*[A-Z0-9_,\s-]+)?|(?:type|ty):\s*ignore(?:\[[A-Za-z0-9_,\s-]+\])?|pragma:\s*(?:no\s+(?:cover|branch)|allowlist\s+secret))\s*$"
)
FAMILIES = ("standing_v1", "trustcal_v1", "crowdscale_v1", "stalehand_v1")
PURE_STDLIB = sys.stdlib_module_names | {"__future__"}
WORLDWATCH_ALLOWED = PURE_STDLIB | {"worldwatch"}
TASK_TEMPLATE_ALLOWED = PURE_STDLIB | {"worldkit"}
TASK_HOST_ALLOWED = PURE_STDLIB | {"worldkit", "build", "world", "verify", "muster", "worldwatch"}
BANNED_DYNAMIC_IMPORTS = frozenset({"__import__", "import_module"})


@dataclass
class _Fn:
    idx: int
    node: ast.AST
    lineno: int
    end_lineno: int
    name: str
    parent: int | None
    cls: str | None
    self_name: str | None
    params: frozenset[str]
    locals_map: dict[str, list[int]]


def _collect_targets(root: Path) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for path in sorted((root / "worldwatch").glob("*.py")):
        files.append((path, "worldwatch"))
    for fam in FAMILIES:
        base = root / "muster" / "tasks" / fam
        for py in (base / "generate.py", base / "reference.py"):
            if py.is_file():
                files.append((py, "task_host"))
        for py in sorted((base / "template").glob("*.py")):
            files.append((py, "task_template"))
    checker = root / "tools" / "check_tigerstyle_worldwatch.py"
    if checker.is_file():
        files.append((checker, "checker"))
    exp_analyze = root / "research" / "experiments" / "worldwatch-probes" / "analyze.py"
    if exp_analyze.is_file():
        files.append((exp_analyze, "worldwatch"))
    assert 0 < len(files) <= MAX_FILES
    return files


def _allowed_for_mode(mode: str) -> frozenset[str]:
    if mode == "worldwatch":
        return WORLDWATCH_ALLOWED
    if mode == "task_template":
        return TASK_TEMPLATE_ALLOWED
    if mode == "task_host":
        return TASK_HOST_ALLOWED
    return PURE_STDLIB


def _check_import_node(rel: str, node: ast.AST, mode: str, allowed: frozenset[str]) -> list[str]:
    errs: list[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top not in allowed:
                errs.append(f"{rel}:{node.lineno} disallowed import {top!r} for mode {mode}")
    elif isinstance(node, ast.ImportFrom):
        max_rel = 1 if mode == "worldwatch" else 0
        if node.level > max_rel:
            errs.append(f"{rel}:{node.lineno} disallowed relative import level {node.level}")
        elif node.level == 0 and node.module:
            top = node.module.split(".")[0]
            if top not in allowed:
                errs.append(f"{rel}:{node.lineno} disallowed import {top!r} for mode {mode}")
        if mode in ("worldwatch", "task_template", "checker"):
            for alias in node.names:
                if alias.name in BANNED_DYNAMIC_IMPORTS:
                    errs.append(f"{rel}:{node.lineno} disallowed dynamic import {alias.name!r}")
    elif mode in ("worldwatch", "task_template", "checker") and isinstance(node, ast.Call):
        fn = node.func
        if (isinstance(fn, ast.Name) and fn.id in BANNED_DYNAMIC_IMPORTS) or (
            isinstance(fn, ast.Attribute) and fn.attr in BANNED_DYNAMIC_IMPORTS
        ):
            errs.append(f"{rel}:{node.lineno} disallowed dynamic import call")
    return errs


def _check_comments_and_imports(rel: str, src: str, tree: ast.Module, mode: str) -> list[str]:
    errs: list[str] = []
    allowed = _allowed_for_mode(mode)
    for i, tok in enumerate(tokenize.generate_tokens(io.StringIO(src).readline), 1):
        assert i <= MAX_NODES
        if (
            tok.type == tokenize.COMMENT
            and not (tok.start[0] == 1 and tok.start[1] == 0 and tok.string.startswith("#!"))
            and ALLOWED_COMMENTS.match(tok.string) is None
        ):
            errs.append(f"{rel}:{tok.start[0]} disallowed comment {tok.string.strip()[:50]!r}")
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
        ):
            doc_val = node.body[0].value
            if (isinstance(doc_val, ast.Constant) and isinstance(doc_val.value, str)) or isinstance(
                doc_val, ast.JoinedStr
            ):
                errs.append(f"{rel}:{node.body[0].lineno} disallowed docstring")
        errs.extend(_check_import_node(rel, node, mode, allowed))
    return errs


def _register_fn(
    fns: list[_Fn],
    mod_map: dict[str, list[int]],
    cls_map: dict[tuple[str, str], list[int]],
    node: ast.AST,
    name: str,
    args: ast.arguments,
    parent: int | None,
    cls: str | None,
    lineno: int,
    end_lineno: int,
) -> int:
    pos = [a.arg for a in (*args.posonlyargs, *args.args)]
    kw = [a.arg for a in args.kwonlyargs]
    extra = ([args.vararg.arg] if args.vararg else []) + ([args.kwarg.arg] if args.kwarg else [])
    fid = len(fns)
    self_nm = pos[0] if cls is not None and pos else None
    info = _Fn(fid, node, lineno, end_lineno, name, parent, cls, self_nm, frozenset((*pos, *kw, *extra)), {})
    fns.append(info)
    if cls is not None:
        cls_map.setdefault((cls, name), []).append(fid)
    elif parent is not None:
        fns[parent].locals_map.setdefault(name, []).append(fid)
    else:
        mod_map.setdefault(name, []).append(fid)
    return fid


def _collect_fns(
    tree: ast.Module,
) -> tuple[list[_Fn], dict[str, list[int]], dict[tuple[str, str], list[int]]]:
    fns: list[_Fn] = []
    mod_map: dict[str, list[int]] = {}
    cls_map: dict[tuple[str, str], list[int]] = {}
    stack: list[tuple[list[ast.stmt], int | None, str | None]] = [(tree.body, None, None)]
    while stack:
        stmts, parent, cls = stack.pop()
        for stmt in stmts:
            if isinstance(stmt, ast.ClassDef):
                stack.append((stmt.body, parent, stmt.name))
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fid = _register_fn(
                    fns, mod_map, cls_map, stmt, stmt.name, stmt.args,
                    parent, cls, stmt.lineno, stmt.end_lineno or stmt.lineno,
                )
                stack.append((stmt.body, fid, None))
            elif isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Lambda):
                names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                nm = names[0] if names else "<lambda>"
                _register_fn(
                    fns, mod_map, cls_map, stmt.value, nm, stmt.value.args,
                    parent, cls, stmt.lineno, stmt.end_lineno or stmt.lineno,
                )
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.value, ast.Lambda) and isinstance(stmt.target, ast.Name):
                _register_fn(
                    fns, mod_map, cls_map, stmt.value, stmt.target.id, stmt.value.args,
                    parent, cls, stmt.lineno, stmt.end_lineno or stmt.lineno,
                )
            elif isinstance(stmt, ast.Match):
                for case in stmt.cases:
                    stack.append((case.body, parent, cls))
            elif isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.TryStar)):
                for key in ("body", "orelse", "finalbody"):
                    if sub := getattr(stmt, key, None):
                        stack.append((sub, parent, cls))
                for h in getattr(stmt, "handlers", ()):
                    stack.append((h.body, parent, cls))
    return fns, mod_map, cls_map


def _build_mod_aliases(tree: ast.Module, mod_map: dict[str, list[int]]) -> dict[str, set[int]]:
    aliases: dict[str, set[int]] = {}
    for stmt in tree.body:
        targets: list[str] = []
        val: ast.AST | None = None
        if isinstance(stmt, ast.Assign) and not isinstance(stmt.value, ast.Lambda):
            targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            val = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None and not isinstance(stmt.value, ast.Lambda) and isinstance(stmt.target, ast.Name):
            targets = [stmt.target.id]
            val = stmt.value
        if not targets or val is None:
            continue
        refs: set[int] = set()
        for sub in ast.walk(val):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                refs.update(mod_map.get(sub.id, ()))
                refs.update(aliases.get(sub.id, ()))
        if refs:
            for t in targets:
                aliases.setdefault(t, set()).update(refs)
    return aliases


def _resolve_symbol(
    nm: str,
    info: _Fn,
    fns: list[_Fn],
    mod_map: dict[str, list[int]],
    mod_aliases: dict[str, set[int]],
) -> list[int]:
    cur: int | None = info.idx
    steps = 0
    while cur is not None:
        steps += 1
        assert steps <= len(fns) + 1
        if nm in fns[cur].locals_map:
            return fns[cur].locals_map[nm]
        if nm in fns[cur].params:
            return []
        cur = fns[cur].parent
    if nm in mod_map:
        return mod_map[nm]
    if nm in mod_aliases:
        return sorted(mod_aliases[nm])
    return []


def _fn_roots_for_edges(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        roots: list[ast.AST] = [
            *node.decorator_list,
            *node.args.defaults,
            *(d for d in node.args.kw_defaults if d is not None),
            *node.body,
        ]
        return roots
    if isinstance(node, ast.Lambda):
        return [
            *node.args.defaults,
            *(d for d in node.args.kw_defaults if d is not None),
            node.body,
        ]
    return []


def _populate_edges(
    info: _Fn,
    fns: list[_Fn],
    mod_map: dict[str, list[int]],
    mod_aliases: dict[str, set[int]],
    cls_map: dict[tuple[str, str], list[int]],
    adj: list[set[int]],
    self_loops: set[int],
) -> None:
    if info.parent is not None:
        adj[info.parent].add(info.idx)
    bstack: list[ast.AST] = list(reversed(_fn_roots_for_edges(info.node)))
    while bstack:
        node = bstack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for hdr in (*node.decorator_list, *node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)):
                bstack.append(hdr)
            continue
        if isinstance(node, ast.ClassDef):
            for dec in node.decorator_list:
                bstack.append(dec)
            for base in node.bases:
                bstack.append(base)
            continue
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Lambda):
            continue
        if isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Lambda) and isinstance(node.target, ast.Name):
            continue
        targets: list[int] = []
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            targets = _resolve_symbol(node.id, info, fns, mod_map, mod_aliases)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            recv, attr = node.value.id, node.attr
            if info.cls and info.self_name and recv == info.self_name:
                targets = cls_map.get((info.cls, attr), [])
            elif recv not in info.params and (recv, attr) in cls_map:
                targets = cls_map[(recv, attr)]
        for tgt in targets:
            adj[info.idx].add(tgt)
            if tgt == info.idx:
                self_loops.add(info.idx)
        bstack.extend(ast.iter_child_nodes(node))


def _check_cycles(
    rel: str,
    tree: ast.Module,
    fns: list[_Fn],
    mod_map: dict[str, list[int]],
    cls_map: dict[tuple[str, str], list[int]],
) -> list[str]:
    n = len(fns)
    adj: list[set[int]] = [set() for _ in range(n)]
    self_loops: set[int] = set()
    mod_aliases = _build_mod_aliases(tree, mod_map)
    for info in fns:
        _populate_edges(info, fns, mod_map, mod_aliases, cls_map, adj, self_loops)
    errs: list[str] = []
    idx = 0
    indices, low, on_st = [-1] * n, [0] * n, [False] * n
    tarjan_st: list[int] = []
    for root in range(n):
        if indices[root] != -1:
            continue
        work: list[tuple[int, list[int], int]] = [(root, sorted(adj[root]), 0)]
        indices[root] = low[root] = idx
        idx += 1
        on_st[root] = True
        tarjan_st.append(root)
        while work:
            u, nbrs, pos = work[-1]
            if pos < len(nbrs):
                work[-1] = (u, nbrs, pos + 1)
                v = nbrs[pos]
                if indices[v] == -1:
                    indices[v] = low[v] = idx
                    idx += 1
                    on_st[v] = True
                    tarjan_st.append(v)
                    work.append((v, sorted(adj[v]), 0))
                elif on_st[v]:
                    low[u] = min(low[u], indices[v])
            else:
                work.pop()
                if work:
                    p = work[-1][0]
                    low[p] = min(low[p], low[u])
                if low[u] == indices[u]:
                    comp: list[int] = []
                    while True:
                        w = tarjan_st.pop()
                        on_st[w] = False
                        comp.append(w)
                        if w == u:
                            break
                    if len(comp) > 1 or (len(comp) == 1 and comp[0] in self_loops):
                        names = ", ".join(fns[c].name for c in comp)
                        errs.append(f"{rel}:{fns[comp[0]].lineno} recursion cycle: {names}")
    return errs


def check_source_text(rel: str, src: str, mode: str) -> list[str]:
    errs: list[str] = []
    lines = len(src.splitlines())
    if lines > MAX_FILE_LINES:
        errs.append(f"{rel}: {lines} lines > {MAX_FILE_LINES}")
    tree = ast.parse(src, filename=rel)
    errs.extend(_check_comments_and_imports(rel, src, tree, mode))
    fns, mod_map, cls_map = _collect_fns(tree)
    for info in fns:
        fl = info.end_lineno - info.lineno + 1
        if fl > MAX_FN_LINES:
            errs.append(f"{rel}:{info.lineno} {info.name} is {fl} lines > {MAX_FN_LINES}")
    errs.extend(_check_cycles(rel, tree, fns, mod_map, cls_map))
    return errs


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    targets = _collect_targets(root)
    errs: list[str] = []
    max_file = max_fn = 0
    for path, mode in targets:
        rel = path.relative_to(root).as_posix()
        src = path.read_text(encoding="utf-8")
        lines = len(src.splitlines())
        max_file = max(max_file, lines)
        tree = ast.parse(src, filename=rel)
        fns, _, _ = _collect_fns(tree)
        for info in fns:
            max_fn = max(max_fn, info.end_lineno - info.lineno + 1)
        errs.extend(check_source_text(rel, src, mode))
    if errs:
        for e in errs[:40]:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(
        f"OK: {len(targets)} file(s) passed WorldWatch/Probes TigerStyle "
        f"(max_file={max_file}/{MAX_FILE_LINES}, max_fn={max_fn}/{MAX_FN_LINES})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
