#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeGuard

MAX_FILE_LINES = 600
MAX_FN_LINES = 70
MAX_FILES = 20_000
MAX_AST_NODES = 500_000
MAX_TOKENS = 500_000
TARGET_ROOTS = frozenset({"src", "muster", "tidegauge", "tools"})
ALLOWED_COMMENT_RE = re.compile(
    r"^#\s*(?:"
    r"noqa(?::\s*[A-Z]{1,8}[0-9]{1,5}(?:\s*,\s*[A-Z]{1,8}[0-9]{1,5})*)?"
    r"|(?:type|ty):\s*ignore(?:\[[A-Za-z0-9_,\s-]+\])?"
    r"|pragma:\s*(?:no\s+(?:cover|branch)|allowlist\s+secret)"
    r")\s*$"
)


@dataclass(frozen=True)
class Violation:
    rel_path: str
    lineno: int
    rule: str
    detail: str


@dataclass
class _FnInfo:
    fn_id: int
    node: ast.FunctionDef | ast.AsyncFunctionDef
    qualname: str
    parent_fn: int | None
    class_name: str | None
    self_param: str | None
    local_fns: dict[str, list[int]]
    params: frozenset[str]


def _is_target_rel_path(rel_path: str) -> bool:
    assert isinstance(rel_path, str)
    if not rel_path.endswith(".py"):
        return False
    parts = PurePosixPath(rel_path).parts
    if len(parts) < 2 or parts[0] not in TARGET_ROOTS:
        return False
    return not (
        len(parts) >= 4 and parts[0] == "muster" and parts[1] == "tasks" and parts[3] == "build"
    )


def _run_git_lines(repo_root: Path, args: list[str]) -> list[str]:
    assert repo_root.is_dir()
    assert len(args) > 0
    proc = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [ln for ln in proc.stdout.split("\n") if ln]


def _discover_target_files(repo_root: Path, base_ref: str) -> list[str]:
    assert repo_root.is_dir()
    assert bool(base_ref)
    candidates: set[str] = set()
    diff_lines = _run_git_lines(
        repo_root,
        [
            "diff",
            "--no-renames",
            "--name-status",
            "--diff-filter=ACR",
            f"{base_ref}...HEAD",
            "--",
            "*.py",
        ],
    )
    assert len(diff_lines) <= MAX_FILES
    for line in diff_lines:
        parts = line.split("\t")
        if len(parts) >= 2 and _is_target_rel_path(parts[-1]):
            candidates.add(parts[-1])

    status_proc = subprocess.run(
        ["git", "status", "--porcelain", "-z", "-uall"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    entries = status_proc.stdout.split("\x00")
    assert len(entries) <= MAX_FILES * 2
    idx = 0
    while idx < len(entries):
        entry = entries[idx]
        idx += 1
        if len(entry) < 4:
            continue
        xy = entry[:2]
        rel = entry[3:]
        if "R" in xy or "C" in xy:
            idx += 1
        if (xy == "??" or any(c in xy for c in ("A", "R", "C"))) and _is_target_rel_path(rel):
            candidates.add(rel)

    return sorted(rel for rel in candidates if (repo_root / rel).is_file())


def _is_allowed_comment(tok: tokenize.TokenInfo) -> bool:
    if tok.start[0] == 1 and tok.start[1] == 0 and tok.string.startswith("#!"):
        return True
    return ALLOWED_COMMENT_RE.match(tok.string) is not None


def _check_comments(rel_path: str, source: str) -> list[Violation]:
    violations: list[Violation] = []
    reader = io.StringIO(source).readline
    for count, tok in enumerate(tokenize.generate_tokens(reader), 1):
        assert count <= MAX_TOKENS, f"token bound exceeded in {rel_path}"
        if tok.type == tokenize.COMMENT and not _is_allowed_comment(tok):
            snippet = tok.string.strip()[:80]
            violations.append(
                Violation(rel_path, tok.start[0], "comment", f"disallowed comment {snippet!r}")
            )
    return violations


def _extract_fn_params(
    fn_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[list[str], frozenset[str]]:
    pos_names = [a.arg for a in (*fn_node.args.posonlyargs, *fn_node.args.args)]
    extra = [a.arg for a in fn_node.args.kwonlyargs]
    if fn_node.args.vararg is not None:
        extra.append(fn_node.args.vararg.arg)
    if fn_node.args.kwarg is not None:
        extra.append(fn_node.args.kwarg.arg)
    return pos_names, frozenset((*pos_names, *extra))


def _resolve_call_targets(
    call_node: ast.Call,
    info: _FnInfo,
    fns: list[_FnInfo],
    mod_fns: dict[str, list[int]],
    cls_methods: dict[tuple[str, str], list[int]],
) -> list[int]:
    if isinstance(call_node.func, ast.Name):
        nm = call_node.func.id
        scope_id: int | None = info.fn_id
        steps = 0
        while scope_id is not None:
            steps += 1
            assert steps <= len(fns)
            cur_info = fns[scope_id]
            if nm in cur_info.local_fns:
                return cur_info.local_fns[nm]
            if nm in cur_info.params:
                return []
            scope_id = cur_info.parent_fn
        return mod_fns.get(nm, [])
    if isinstance(call_node.func, ast.Attribute) and isinstance(call_node.func.value, ast.Name):
        recv = call_node.func.value.id
        attr = call_node.func.attr
        if info.class_name is not None and info.self_param is not None and recv == info.self_param:
            return cls_methods.get((info.class_name, attr), [])
        if recv not in info.params and (recv, attr) in cls_methods:
            return cls_methods[(recv, attr)]
    return []


def _find_recursion_violations(
    rel_path: str,
    fns: list[_FnInfo],
    mod_fns: dict[str, list[int]],
    cls_methods: dict[tuple[str, str], list[int]],
) -> list[Violation]:
    adj: list[set[int]] = [set() for _ in fns]
    for info in fns:
        if info.parent_fn is not None:
            adj[info.parent_fn].add(info.fn_id)
    call_sites: list[tuple[int, int, int]] = []
    visited = 0
    for info in fns:
        bstack: list[ast.AST] = list(reversed(info.node.body))
        while bstack:
            visited += 1
            assert visited <= MAX_AST_NODES, f"AST call scan bound exceeded in {rel_path}"
            cur = bstack.pop()
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(cur, ast.Call):
                for tid in _resolve_call_targets(cur, info, fns, mod_fns, cls_methods):
                    adj[info.fn_id].add(tid)
                    call_sites.append((info.fn_id, tid, cur.lineno))
            bstack.extend(reversed(list(ast.iter_child_nodes(cur))))

    violations: list[Violation] = []
    bfs_steps = 0
    for u, v, lineno in call_sites:
        if u == v:
            violations.append(
                Violation(
                    rel_path,
                    lineno,
                    "recursion",
                    f"function {fns[u].qualname!r} makes a direct self-recursive call",
                )
            )
            continue
        seen = {v}
        queue = [v]
        reaches_u = False
        while queue:
            bfs_steps += 1
            assert bfs_steps <= MAX_AST_NODES * 4, f"cycle check bound exceeded in {rel_path}"
            cur_v = queue.pop()
            for nxt in adj[cur_v]:
                if nxt == u:
                    reaches_u = True
                    break
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
            if reaches_u:
                break
        if reaches_u:
            violations.append(
                Violation(
                    rel_path,
                    lineno,
                    "recursion",
                    f"function {fns[u].qualname!r} participates in mutual/closure recursion via {fns[v].qualname!r}",
                )
            )
    return violations


def _register_fn_node(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    in_class_body: bool,
    parent_fn: int | None,
    class_name: str | None,
    parent_self: str | None,
    fns: list[_FnInfo],
    mod_fns: dict[str, list[int]],
    cls_methods: dict[tuple[str, str], list[int]],
) -> tuple[int, str | None, int]:
    fid = len(fns)
    pos_names, params = _extract_fn_params(node)
    if in_class_body and class_name is not None:
        self_p = pos_names[0] if pos_names and pos_names[0] in ("self", "cls") else None
        qname = (
            f"{class_name}.{node.name}"
            if parent_fn is None
            else f"{fns[parent_fn].qualname}.{class_name}.{node.name}"
        )
    else:
        self_p = parent_self if parent_self not in params else None
        qname = f"{fns[parent_fn].qualname}.{node.name}" if parent_fn is not None else node.name
    fns.append(_FnInfo(fid, node, qname, parent_fn, class_name, self_p, {}, params))
    if in_class_body and class_name is not None:
        cls_methods.setdefault((class_name, node.name), []).append(fid)
    elif parent_fn is not None:
        fns[parent_fn].local_fns.setdefault(node.name, []).append(fid)
    else:
        mod_fns.setdefault(node.name, []).append(fid)
    end_line = node.end_lineno if node.end_lineno is not None else node.lineno
    return fid, self_p, end_line - node.lineno + 1


def _is_str_expr(node: ast.AST) -> TypeGuard[ast.Expr]:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _check_ast(rel_path: str, source: str) -> tuple[list[Violation], int]:
    violations: list[Violation] = []
    tree = ast.parse(source, filename=rel_path)
    reported_doc_exprs: set[int] = set()
    if tree.body and _is_str_expr(tree.body[0]):
        reported_doc_exprs.add(id(tree.body[0]))
        violations.append(
            Violation(rel_path, tree.body[0].lineno, "docstring", "module has a docstring")
        )

    max_fn_span = 0
    fns: list[_FnInfo] = []
    mod_fns: dict[str, list[int]] = {}
    cls_methods: dict[tuple[str, str], list[int]] = {}
    stack: list[tuple[ast.AST, bool, int | None, str | None, str | None]] = [
        (child, False, None, None, None) for child in reversed(tree.body)
    ]
    visited = 0
    while stack:
        visited += 1
        assert visited <= MAX_AST_NODES, f"AST node bound exceeded in {rel_path}"
        node, in_class_body, p_fn, c_name, p_self = stack.pop()
        if _is_str_expr(node):
            if id(node) not in reported_doc_exprs:
                msg = "standalone string expression / docstring"
                violations.append(Violation(rel_path, node.lineno, "docstring", msg))
            continue
        if isinstance(node, ast.ClassDef):
            if node.body and _is_str_expr(node.body[0]):
                reported_doc_exprs.add(id(node.body[0]))
                msg = f"class {node.name!r} has a docstring"
                violations.append(Violation(rel_path, node.lineno, "docstring", msg))
            for child in reversed(node.body):
                stack.append((child, True, p_fn, node.name, None))
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fid, self_p, span = _register_fn_node(
                node, in_class_body, p_fn, c_name, p_self, fns, mod_fns, cls_methods
            )
            max_fn_span = max(max_fn_span, span)
            if span > MAX_FN_LINES:
                msg = f"function {node.name!r} spans {span} lines (max {MAX_FN_LINES})"
                violations.append(Violation(rel_path, node.lineno, "fn_length", msg))
            if node.body and _is_str_expr(node.body[0]):
                reported_doc_exprs.add(id(node.body[0]))
                msg = f"function {node.name!r} has a docstring"
                violations.append(Violation(rel_path, node.lineno, "docstring", msg))
            for child in reversed(node.body):
                stack.append((child, False, fid, c_name, self_p))
            continue
        for child in reversed(list(ast.iter_child_nodes(node))):
            stack.append((child, False, p_fn, c_name, p_self))

    violations.extend(_find_recursion_violations(rel_path, fns, mod_fns, cls_methods))
    return violations, max_fn_span


def _count_source_lines(source: str) -> int:
    if not source:
        return 0
    return source.count("\n") + (0 if source.endswith("\n") else 1)


def check_file(repo_root: Path, rel_path: str) -> tuple[list[Violation], int, int]:
    file_path = repo_root / rel_path
    assert file_path.is_file(), f"missing file: {rel_path}"
    source = file_path.read_text(encoding="utf-8")
    n_lines = _count_source_lines(source)
    violations: list[Violation] = []
    if n_lines > MAX_FILE_LINES:
        violations.append(
            Violation(
                rel_path,
                n_lines,
                "file_length",
                f"file has {n_lines} lines (max {MAX_FILE_LINES})",
            )
        )
    try:
        ast_violations, max_fn_span = _check_ast(rel_path, source)
    except SyntaxError as exc:
        return (
            [Violation(rel_path, exc.lineno or 1, "syntax", f"SyntaxError: {exc.msg}")],
            n_lines,
            0,
        )
    violations.extend(ast_violations)
    try:
        violations.extend(_check_comments(rel_path, source))
    except tokenize.TokenError as exc:
        violations.append(Violation(rel_path, 1, "tokenize", f"TokenError: {exc}"))
    violations.sort(key=lambda v: (v.lineno, v.rule, v.detail))
    return violations, n_lines, max_fn_span


def _resolve_explicit_paths(repo_root: Path, raw_paths: list[str]) -> tuple[list[str], list[str]]:
    assert len(raw_paths) <= MAX_FILES
    resolved: list[str] = []
    errors: list[str] = []
    for raw in raw_paths:
        candidate = Path(raw)
        abs_path = (
            candidate.resolve() if candidate.is_absolute() else (repo_root / candidate).resolve()
        )
        if not abs_path.is_relative_to(repo_root):
            errors.append(f"path outside repository root: {raw}")
            continue
        if not abs_path.exists():
            errors.append(f"path does not exist: {raw}")
            continue
        rel = abs_path.relative_to(repo_root).as_posix()
        if abs_path.is_file() and rel.endswith(".py"):
            resolved.append(rel)
    return sorted(set(resolved)), errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_tigerstyle_multiagent")
    parser.add_argument("--base-ref", default="origin/main")
    parser.add_argument("paths", nargs="*")
    ns = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    assert (repo_root / "pyproject.toml").is_file(), f"unexpected repo root: {repo_root}"
    try:
        if ns.paths:
            targets, path_errors = _resolve_explicit_paths(repo_root, ns.paths)
            if path_errors:
                for err in path_errors:
                    print(f"ERROR: {err}", file=sys.stderr)
                return 2
        else:
            targets = _discover_target_files(repo_root, ns.base_ref)
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or str(exc)).strip()
        print(f"ERROR: git discovery failed: {msg}", file=sys.stderr)
        return 2

    all_violations: list[Violation] = []
    max_lines_seen = 0
    max_fn_seen = 0
    for rel_path in targets:
        file_violations, n_lines, max_fn_span = check_file(repo_root, rel_path)
        all_violations.extend(file_violations)
        if n_lines > max_lines_seen:
            max_lines_seen = n_lines
        if max_fn_span > max_fn_seen:
            max_fn_seen = max_fn_span

    if all_violations:
        for v in all_violations:
            print(f"{v.rel_path}:{v.lineno}: [{v.rule}] {v.detail}", file=sys.stderr)
        print(
            f"FAIL: {len(all_violations)} violation(s) across {len(targets)} checked file(s)",
            file=sys.stderr,
        )
        return 1
    print(
        f"OK: {len(targets)} file(s) passed TigerStyle checks "
        f"(max_file_lines={max_lines_seen}/{MAX_FILE_LINES}, max_fn_lines={max_fn_seen}/{MAX_FN_LINES})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
