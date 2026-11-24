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
FAMILIES = ("standing_v1", "trustcal_v1", "crowdscale_v1")
STDLIB_NAMES = sys.stdlib_module_names | {"__future__", "worldkit", "build"}


@dataclass
class _Fn:
    idx: int
    node: ast.FunctionDef | ast.AsyncFunctionDef
    name: str
    parent: int | None
    cls: str | None
    self_name: str | None
    params: frozenset[str]
    locals_map: dict[str, list[int]]


def _collect_targets(root: Path) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for path in sorted((root / "worldwatch").glob("*.py")):
        files.append((path, "stdlib"))
    for fam in FAMILIES:
        base = root / "muster" / "tasks" / fam
        for py in (base / "generate.py", base / "reference.py"):
            if py.is_file():
                files.append((py, "task"))
        for py in sorted((base / "template").glob("*.py")):
            files.append((py, "stdlib"))
    checker = root / "tools" / "check_tigerstyle_worldwatch.py"
    if checker.is_file():
        files.append((checker, "stdlib"))
    assert 0 < len(files) <= MAX_FILES
    return files


def _check_comments_and_imports(rel: str, src: str, tree: ast.Module, mode: str) -> list[str]:
    errs: list[str] = []
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
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            errs.append(f"{rel}:{node.body[0].lineno} disallowed docstring")
        if mode == "stdlib":
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top not in STDLIB_NAMES and top != "worldwatch":
                        errs.append(f"{rel}:{node.lineno} non-stdlib import {top!r}")
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top = node.module.split(".")[0]
                if top not in STDLIB_NAMES and top != "worldwatch":
                    errs.append(f"{rel}:{node.lineno} non-stdlib import {top!r}")
    return errs


def _collect_fns(tree: ast.Module) -> tuple[list[_Fn], dict[str, list[int]], dict[tuple[str, str], list[int]]]:
    fns: list[_Fn] = []
    mod_map: dict[str, list[int]] = {}
    cls_map: dict[tuple[str, str], list[int]] = {}
    stack: list[tuple[list[ast.stmt], int | None, str | None]] = [(tree.body, None, None)]
    while stack:
        stmts, parent, cls = stack.pop()
        for stmt in stmts:
            if isinstance(stmt, ast.ClassDef):
                stack.append((stmt.body, None, stmt.name))
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                pos = [a.arg for a in (*stmt.args.posonlyargs, *stmt.args.args)]
                kw = [a.arg for a in stmt.args.kwonlyargs]
                extra = ([stmt.args.vararg.arg] if stmt.args.vararg else []) + ([stmt.args.kwarg.arg] if stmt.args.kwarg else [])
                fid = len(fns)
                self_nm = pos[0] if cls is not None and pos else None
                info = _Fn(fid, stmt, stmt.name, parent, cls, self_nm, frozenset((*pos, *kw, *extra)), {})
                fns.append(info)
                if parent is not None:
                    fns[parent].locals_map.setdefault(stmt.name, []).append(fid)
                elif cls is not None:
                    cls_map.setdefault((cls, stmt.name), []).append(fid)
                else:
                    mod_map.setdefault(stmt.name, []).append(fid)
                stack.append((stmt.body, fid, None))
            elif isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)):
                for key in ("body", "orelse", "finalbody"):
                    if sub := getattr(stmt, key, None):
                        stack.append((sub, parent, cls))
                for h in getattr(stmt, "handlers", ()):
                    stack.append((h.body, parent, cls))
    return fns, mod_map, cls_map


def _resolve_calls(call: ast.Call, info: _Fn, fns: list[_Fn], mod_map: dict[str, list[int]], cls_map: dict[tuple[str, str], list[int]]) -> list[int]:
    if isinstance(call.func, ast.Name):
        nm, cur = call.func.id, info.idx
        steps = 0
        while cur is not None:
            steps += 1
            assert steps <= len(fns) + 1
            if nm in fns[cur].locals_map:
                return fns[cur].locals_map[nm]
            if nm in fns[cur].params:
                return []
            cur = fns[cur].parent
        return mod_map.get(nm, [])
    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
        recv, attr = call.func.value.id, call.func.attr
        if info.cls and info.self_name and recv == info.self_name:
            return cls_map.get((info.cls, attr), [])
        if recv not in info.params and (recv, attr) in cls_map:
            return cls_map[(recv, attr)]
    return []


def _check_cycles(rel: str, fns: list[_Fn], mod_map: dict[str, list[int]], cls_map: dict[tuple[str, str], list[int]]) -> list[str]:
    n = len(fns)
    adj: list[set[int]] = [set() for _ in range(n)]
    self_loops: set[int] = set()
    for info in fns:
        if info.parent is not None:
            adj[info.parent].add(info.idx)
        bstack: list[ast.AST] = list(reversed(info.node.body))
        while bstack:
            node = bstack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.Call):
                for tgt in _resolve_calls(node, info, fns, mod_map, cls_map):
                    adj[info.idx].add(tgt)
                    if tgt == info.idx:
                        self_loops.add(info.idx)
            bstack.extend(ast.iter_child_nodes(node))
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
                        errs.append(f"{rel}:{fns[comp[0]].node.lineno} recursion cycle: {names}")
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
        if lines > MAX_FILE_LINES:
            errs.append(f"{rel}: {lines} lines > {MAX_FILE_LINES}")
        tree = ast.parse(src, filename=rel)
        errs.extend(_check_comments_and_imports(rel, src, tree, mode))
        fns, mod_map, cls_map = _collect_fns(tree)
        for info in fns:
            fl = (info.node.end_lineno or info.node.lineno) - info.node.lineno + 1
            max_fn = max(max_fn, fl)
            if fl > MAX_FN_LINES:
                errs.append(f"{rel}:{info.node.lineno} {info.name} is {fl} lines > {MAX_FN_LINES}")
        errs.extend(_check_cycles(rel, fns, mod_map, cls_map))
    if errs:
        for e in errs[:40]:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(f"OK: {len(targets)} file(s) passed WorldWatch/Probes TigerStyle (max_file={max_file}/{MAX_FILE_LINES}, max_fn={max_fn}/{MAX_FN_LINES})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
