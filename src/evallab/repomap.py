"""Generated repository map for `src/evallab/` (WS-F navigation).

Parses `src/evallab/` via the standard library AST module and regenerates
`docs/repo-map.md` with deterministic module purposes, subcommands owned,
and store references.

Audience and status front-matter are enforced fail-closed so that the living
operator doc does not rot silently.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from evallab.contextpack import VALID_AUDIENCES, parse_front_matter
from evallab.lineage import compute_file_digest

REPMAP_VERSION = "repomap v1"
GENERATED_BY_MARKER = "<!-- generated-by: repomap v1 -->"
DEFAULT_MAP_RELATIVE = "docs/repo-map.md"
MAP_TITLE = "Repository map"
FRONT_MATTER_BLOCK = """---
status: living
audience:
  - builder
  - analyst
  - runner
  - operator
inputs:
"""

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z0-9_]+)", re.IGNORECASE
)
_CREATE_VIEW_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z0-9_]+)",
    re.IGNORECASE,
)
_GENERIC_MODULES = frozenset({"cli", "schemas", "paths", "credentials"})


def repo_root() -> Path:
    """Return the repository root for this checkout."""
    return Path(__file__).resolve().parents[2]


def default_src_dir(root: Path | None = None) -> Path:
    """Return the repository `src/evallab/` directory."""
    return (root if root is not None else repo_root()) / "src" / "evallab"


def default_map_path(root: Path | None = None) -> Path:
    """Return the committed map path (`docs/repo-map.md`)."""
    return (root if root is not None else repo_root()) / DEFAULT_MAP_RELATIVE


def root_for_src_dir(src_dir: Path, root: Path | None = None) -> Path:
    """Use an explicit root, else the repo root above `src/evallab`."""
    if root is not None:
        return root
    resolved = src_dir.resolve()
    if resolved.name == "evallab" and resolved.parent.name == "src":
        return resolved.parent.parent
    return repo_root()


def module_name_for_path(path: Path, src_dir: Path) -> str:
    """Derive the canonical module name for a Python file within `src/evallab/`."""
    rel = path.resolve().relative_to(src_dir.resolve())
    if len(rel.parts) == 1:
        return path.stem
    if rel.name == "__init__.py":
        return rel.parent.as_posix().replace("/", ".")
    return rel.with_suffix("").as_posix().replace("/", ".")


def discover_module_paths(src_dir: Path) -> list[Path]:
    """Discover all Python modules and subpackage files under `src/evallab/`."""
    if not src_dir.is_dir():
        return []
    paths: list[Path] = []
    for path in src_dir.rglob("*.py"):
        if not path.is_file():
            continue
        rel = path.resolve().relative_to(src_dir.resolve())
        if any(part.startswith(".") for part in rel.parts):
            continue
        paths.append(path)
    return sorted(paths, key=lambda p: module_name_for_path(p, src_dir))


def first_sentence(text: str) -> str:
    """Return the first sentence of a docstring or description."""
    collapsed = " ".join(text.strip().split())
    if not collapsed:
        return ""
    for index, char in enumerate(collapsed):
        if char == "." and (index + 1 == len(collapsed) or collapsed[index + 1] == " "):
            return collapsed[: index + 1]
    return collapsed


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _kwarg(call: ast.Call, name: str) -> ast.AST | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


@dataclass(frozen=True)
class ModuleRecord:
    """One discovered `src/evallab` module and the commands it owns."""

    name: str
    path: str
    line_count: int
    purpose: str | None
    commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandRecord:
    """One argparse command and the module that implements it."""

    name: str
    help: str
    module: str
    source: str


@dataclass(frozen=True)
class StoreRecord:
    """One discovered durable store and the modules that write it."""

    kind: str
    name: str
    location: str
    writers: tuple[str, ...]


@dataclass(frozen=True)
class InputRecord:
    """One declared source input file and its content digest."""

    path: str
    digest: str


@dataclass(frozen=True)
class RepoMap:
    """Fully derived snapshot used to render and check the committed map."""

    modules: tuple[ModuleRecord, ...]
    commands: tuple[CommandRecord, ...]
    stores: tuple[StoreRecord, ...]
    inputs: tuple[InputRecord, ...] = ()


@dataclass(frozen=True)
class CheckIssue:
    """One fail-closed validation finding."""

    path: str
    message: str


def _public_definitions(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ) and not node.name.startswith("_"):
            names.append(node.name)
    return names


def _parser_description(tree: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != "ArgumentParser":
            continue
        description = _const_str(_kwarg(node, "description"))
        if description:
            return first_sentence(description)
    return None


def _first_definition_doc(tree: ast.AST, *, public_only: bool) -> str | None:
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if public_only and node.name.startswith("_"):
            continue
        docstring = ast.get_docstring(node)
        if docstring:
            return first_sentence(docstring)
    return None


def module_purpose(source: str, tree: ast.AST, *, module_name: str | None = None, is_package: bool = False) -> str | None:
    """Derive a one-line purpose from AST-available descriptions."""
    module_doc = (
        ast.get_docstring(tree)
        if isinstance(
            tree, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        else None
    )
    if module_doc:
        sentence = first_sentence(module_doc)
        if sentence:
            return sentence
    parser_doc = _parser_description(tree)
    if parser_doc:
        return parser_doc

    public_doc = _first_definition_doc(tree, public_only=True)
    if public_doc:
        return public_doc

    any_doc = _first_definition_doc(tree, public_only=False)
    if any_doc:
        return any_doc

    public = _public_definitions(tree)
    if public:
        shown = ", ".join(f"`{name}`" for name in public[:4])
        extra = ", …" if len(public) > 4 else ""
        return f"Defines {shown}{extra}."

    if is_package and module_name:
        qualified = module_name if module_name.startswith("evallab") else f"evallab.{module_name}"
        return f"Package `{qualified}`."

    return None


def load_module(path: Path, src_dir: Path) -> ModuleRecord:
    """Parse one module file into a map record."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    relative = path.resolve().relative_to(src_dir.resolve()).as_posix()
    mod_name = module_name_for_path(path, src_dir)
    is_pkg = path.name == "__init__.py"
    return ModuleRecord(
        name=mod_name,
        path=f"src/evallab/{relative}",
        line_count=len(source.splitlines()),
        purpose=module_purpose(source, tree, module_name=mod_name, is_package=is_pkg),
    )


def load_modules(src_dir: Path) -> list[ModuleRecord]:
    """Parse every discovered module."""
    return [load_module(path, src_dir) for path in discover_module_paths(src_dir)]


def _import_map(tree: ast.AST) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("evallab"):
            remainder = node.module.removeprefix("evallab").lstrip(".")
            if remainder:
                module = remainder.split(".", 1)[0]
                for alias in node.names:
                    mapping[alias.asname or alias.name] = module
            else:
                for alias in node.names:
                    mapping[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "evallab" or alias.name.startswith("evallab."):
                    target = alias.name.removeprefix("evallab").lstrip(".")
                    mapping[alias.asname or alias.name.split(".")[-1]] = (
                        target.split(".", 1)[0] if target else "cli"
                    )
    return mapping


def _add_parser_name(call: ast.Call) -> str | None:
    if not call.args:
        return None
    return _const_str(call.args[0])


def _parser_help(call: ast.Call) -> str:
    return _const_str(_kwarg(call, "help")) or ""


def _function_map(tree: ast.AST) -> dict[str, ast.AST]:
    if not isinstance(tree, ast.Module):
        return {}
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _called_names(node: ast.AST) -> list[str]:
    names: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _call_name(child)
            if name:
                names.append(name)
        elif isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name):
            names.append(child.attr)
        elif isinstance(child, ast.Name):
            names.append(child.id)
    return names


def _compare_equals(node: ast.AST, attribute: str) -> str | None:
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return None
    if not isinstance(node.ops[0], ast.Eq):
        return None
    left = node.left
    if not (isinstance(left, ast.Attribute) and left.attr == attribute):
        return None
    return _const_str(node.comparators[0])


def _compare_in_values(node: ast.AST, attribute: str) -> list[str] | None:
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return None
    if not isinstance(node.ops[0], ast.In):
        return None
    left = node.left
    if not (isinstance(left, ast.Attribute) and left.attr == attribute):
        return None
    target = node.comparators[0]
    if isinstance(target, (ast.Tuple, ast.List, ast.Set)):
        values: list[str] = []
        for elt in target.elts:
            val = _const_str(elt)
            if val is not None:
                values.append(val)
        return values
    return None


def _command_keys(test: ast.AST) -> list[str]:
    direct = _compare_equals(test, "command")
    if direct is not None:
        return [direct]
    in_values = _compare_in_values(test, "command")
    if in_values is not None:
        return in_values
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        values: list[str] = []
        for value in test.values:
            values.extend(_command_keys(value))
        return values
    return []


def _score_module(
    command: str,
    names: Sequence[str],
    imports: dict[str, str],
    functions: dict[str, ast.AST],
    *,
    depth: int = 0,
) -> str:
    referenced: list[str] = []
    for name in names:
        if name in imports:
            referenced.append(imports[name])
        elif name in functions and depth < 3:
            nested = _called_names(functions[name])
            sub = _score_module(command, nested, imports, functions, depth=depth + 1)
            if sub != "cli":
                referenced.append(sub)

    counts: dict[str, int] = defaultdict(int)
    for mod in referenced:
        if mod not in _GENERIC_MODULES:
            counts[mod] += 1

    if counts:
        return max(counts.items(), key=lambda item: item[1])[0]

    for mod in referenced:
        counts[mod] += 1
    if counts:
        return max(counts.items(), key=lambda item: item[1])[0]

    for candidate in sorted(imports.values()):
        if candidate not in _GENERIC_MODULES and candidate in command.replace("-", "_"):
            return candidate

    return "cli"


def _cli_parser_commands(tree: ast.AST) -> list[tuple[str, str]]:
    dest_by_id: dict[int, str] = {}
    commands: list[tuple[str, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != "add_subparsers":
            continue
        dest = _const_str(_kwarg(node, "dest")) or "command"
        dest_by_id[id(node)] = dest

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != "add_parser":
            continue
        name = _add_parser_name(node)
        if name is None:
            continue
        help_text = _parser_help(node)
        commands.append((name, help_text))

    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, help_text in commands:
        if name in seen:
            continue
        seen.add(name)
        unique.append((name, help_text))
    return unique


def _qualified_command_name(call: ast.Call, name: str) -> str:
    """Resolve `add_parser("x")` to its full command path, e.g. `schedule install`."""
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        parent_var = func.value.id
        prefix = parent_var.replace("_subparsers", "").replace("_commands", "").strip("_")
        if prefix and prefix not in {"subparsers", "commands", "root"}:
            return f"{prefix} {name}"
    return name


def _statements_in_source_order(node: ast.AST) -> Iterator[ast.AST]:
    """Depth-first pre-order walk, which matches source order for straight-line code."""
    for child in ast.iter_child_nodes(node):
        yield child
        yield from _statements_in_source_order(child)


def _body_names(func: ast.AST) -> list[str]:
    """Names referenced in a function's body, excluding its signature annotations.

    Annotations must not count as references: a handler typed
    `harbor: HarborBackend | None` would otherwise be attributed to whichever
    module exports that type rather than to the module it actually drives.
    """
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _called_names(func)
    names: list[str] = []
    for stmt in func.body:
        names.extend(_called_names(stmt))
    return names


def _handler_module(
    command: str,
    handler: str,
    imports: dict[str, str],
    functions: dict[str, ast.AST],
) -> str:
    """Score the module implementing `command` from its registered handler.

    Names referenced *directly* in the handler body win over names reached by
    recursing through local helpers: a handler that calls one domain module and a
    shared helper should be attributed to the domain module, not to whatever the
    helper happens to import most often.
    """
    body = functions.get(handler)
    if body is None:
        direct = imports.get(handler)
        if direct is not None:
            return direct
        return _score_module(command, [handler], imports, functions)
    shallow = _score_module(command, _body_names(body), imports, {})
    if shallow != "cli":
        return shallow
    return _score_module(command, _body_names(body), imports, functions)


def _registry_owners(
    tree: ast.AST,
    imports: dict[str, str],
    functions: dict[str, ast.AST],
) -> dict[str, str]:
    """Attribute commands declared with `parser.set_defaults(func=handler)`.

    A declarative registry has no `args.command == "x"` dispatch chain to read, so
    the implementing module is scored from the registered handler's body instead.
    Without this, converting `cli.py` to a registry would silently drop every
    command-to-module edge — and a repo map that under-reports reachability is the
    exact signal this lab uses to find built-but-unreachable code.
    """
    command_by_var: dict[str, str] = {}
    owners: dict[str, str] = {}

    for node in _statements_in_source_order(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
            if _call_name(call) != "add_parser":
                continue
            name = _add_parser_name(call)
            if name is None:
                continue
            full = _qualified_command_name(call, name)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    command_by_var[target.id] = full
            continue
        if not isinstance(node, ast.Call) or _call_name(node) != "set_defaults":
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            continue
        command = command_by_var.get(func.value.id)
        handler_node = _kwarg(node, "func")
        if command is None or handler_node is None:
            continue
        handler = _call_name(handler_node)
        if handler is None:
            continue
        owners[command] = _handler_module(command, handler, imports, functions)
    return owners


def parse_cli_commands(cli_path: Path) -> list[CommandRecord]:
    """Map every `cli.py` subcommand to the module that implements it."""
    if not cli_path.is_file():
        return []
    source = cli_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = _import_map(tree)
    functions = _function_map(tree)
    dispatch = functions.get("run_cli")
    owners: dict[str, str] = {}
    if dispatch is not None:
        for node in ast.walk(dispatch):
            if not isinstance(node, ast.If):
                continue
            for key in _command_keys(node.test):
                owners[key] = _score_module(key, _called_names(node), imports, functions)
    owners.update(_registry_owners(tree, imports, functions))

    records: list[CommandRecord] = []
    for name, help_text in _cli_parser_commands(tree):
        module = owners.get(name)
        if module is None:
            top = name.split()[0]
            module = owners.get(top, "cli")
        records.append(CommandRecord(name=name, help=help_text, module=module, source="evallab"))
    return records


def parse_module_cli(path: Path, module: str) -> list[CommandRecord]:
    """Collect `python -m evallab.<module>` subcommands from argparse."""
    if module == "cli":
        return []
    if not path.is_file():
        return []
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    has_main = any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and _const_str(node.test.comparators[0] if node.test.comparators else None) is None
        and any(
            isinstance(child, ast.Name) and child.id == "__name__" for child in ast.walk(node.test)
        )
        for node in (tree.body if isinstance(tree, ast.Module) else [])
    )
    # Presence of ArgumentParser is enough: these modules are invoked via -m.
    parser_prog: str | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) == "ArgumentParser":
            parser_prog = _const_str(_kwarg(node, "prog")) or f"evallab.{module}"
            break
    if parser_prog is None:
        return []

    records: list[CommandRecord] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != "add_parser":
            continue
        name = _add_parser_name(node)
        if name is None:
            continue
        records.append(
            CommandRecord(
                name=f"python -m evallab.{module} {name}",
                help=_parser_help(node),
                module=module,
                source="module",
            )
        )
    if not records and has_main:
        records.append(
            CommandRecord(
                name=f"python -m evallab.{module}",
                help=_parser_description(tree) or "",
                module=module,
                source="module",
            )
        )
    return records


def _path_like_constant(text: str) -> bool:
    if not text or "\n" in text or " " in text or len(text) > 80:
        return False
    return "/" in text or text.endswith((".parquet", ".jsonl", ".sql")) or text in {
        "events.jsonl",
        "STOP",
    }


def _string_constants(tree: ast.AST) -> list[str]:
    values: list[str] = []
    for node in ast.walk(tree):
        text = _const_str(node)
        if text and _path_like_constant(text):
            values.append(text)
    return values


def _assigned_string_tuple(tree: ast.AST, name: str) -> list[str]:
    for node in tree.body if isinstance(tree, ast.Module) else []:
        target_names: list[str] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            value = node.value
            for target in node.targets:
                if isinstance(target, ast.Name):
                    target_names.append(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names.append(node.target.id)
            value = node.value
        if name not in target_names or value is None:
            continue
        if isinstance(value, (ast.Tuple, ast.List)):
            items = [_const_str(item) for item in value.elts]
            return [item for item in items if item is not None]
    return []


def _schema_keys(tree: ast.AST, name: str) -> list[str]:
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        if isinstance(node.value, ast.Dict):
            keys = [_const_str(key) for key in node.value.keys]
            return [key for key in keys if key is not None]
    return []


def _writes_parquet(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) in {"write_table", "write_parquet"}:
            return True
    return False


def _sql_names(path: Path, pattern: re.Pattern[str]) -> list[str]:
    if not path.is_file():
        return []
    return pattern.findall(path.read_text(encoding="utf-8"))


def discover_stores(src_dir: Path, root: Path) -> list[StoreRecord]:
    """Derive queue, Parquet, DuckDB, and Postgres stores from source."""
    stores: list[StoreRecord] = []
    writers_by_hint: dict[str, set[str]] = defaultdict(set)
    queue_states: list[str] = []
    parquet_tables: dict[str, set[str]] = defaultdict(set)
    queue_files: dict[str, set[str]] = defaultdict(set)

    for path in discover_module_paths(src_dir):
        if path.stem == "repomap":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = module_name_for_path(path, src_dir)
        states = _assigned_string_tuple(tree, "QUEUE_STATES")
        if states:
            queue_states = states
            writers_by_hint["queue"].add(module)
        for table in _schema_keys(tree, "PARQUET_SCHEMAS"):
            parquet_tables[f"{table}.parquet"].add(module)
        for table in _schema_keys(tree, "FACT_SCHEMAS"):
            parquet_tables[f"{table}.parquet"].add(module)
        if _writes_parquet(tree):
            writers_by_hint["parquet"].add(module)
        for text in _string_constants(tree):
            if text.startswith("queue/") or text in {"events.jsonl", "STOP"}:
                queue_files[text].add(module)
                writers_by_hint["queue"].add(module)
            if text.endswith(".parquet") or text.startswith("derived/parquet"):
                parquet_tables[text].add(module)
                writers_by_hint["parquet"].add(module)
            if text.endswith(".sql") or text.startswith("sql/"):
                writers_by_hint[text].add(module)
            if "schema.sql" in text:
                writers_by_hint["postgres"].add(module)

    queue_writers = tuple(sorted(writers_by_hint["queue"] or {"queue"}))
    if queue_states:
        for state in queue_states:
            stores.append(StoreRecord("queue", state, f"queue/{state}/", queue_writers))
    else:
        stores.append(StoreRecord("queue", "queue", "queue/", queue_writers))
    seen_queue: set[str] = set()
    for name, writers in sorted(queue_files.items()):
        location = name if name.startswith("queue/") else f"queue/{name}"
        if location in seen_queue:
            continue
        seen_queue.add(location)
        stores.append(StoreRecord("queue", Path(location).name, location, tuple(sorted(writers))))

    parquet_writers_default = tuple(sorted(writers_by_hint["parquet"]))
    stores.append(
        StoreRecord("parquet", "derived root", "derived/parquet/", parquet_writers_default)
    )
    stores.append(
        StoreRecord(
            "parquet",
            "job partitions",
            "derived/parquet/job_id=*/trial_id=*/",
            parquet_writers_default,
        )
    )
    parquet_by_name: dict[str, tuple[str, set[str]]] = {}
    for name, writers in parquet_tables.items():
        basename = Path(name).name
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.parquet", basename):
            continue
        location = name if "/" in name else f"derived/parquet/**/{basename}"
        existing = parquet_by_name.get(basename)
        if existing is None:
            parquet_by_name[basename] = (location, set(writers))
        else:
            current_location, current_writers = existing
            preferred = current_location if "/" in current_location else location
            current_writers.update(writers)
            parquet_by_name[basename] = (preferred, current_writers)
    for basename, (location, writers) in sorted(parquet_by_name.items()):
        stores.append(StoreRecord("parquet", basename, location, tuple(sorted(writers))))

    sql_dir = root / "sql"
    if sql_dir.is_dir():
        for path in sorted(sql_dir.glob("*.sql")):
            relative = f"sql/{path.name}"
            if path.name == "schema.sql":
                tables = _sql_names(path, _CREATE_TABLE_RE)
                views = _sql_names(path, _CREATE_VIEW_RE)
                writers = tuple(sorted(writers_by_hint.get("postgres", set()) | {"database"}))
                for table in tables:
                    stores.append(StoreRecord("postgres", table, relative, writers))
                for view in views:
                    stores.append(StoreRecord("postgres-view", view, relative, writers))
            else:
                views = _sql_names(path, _CREATE_VIEW_RE)
                hinted = set(writers_by_hint.get(relative, set()))
                hinted.update(writers_by_hint.get(path.name, set()))
                if path.name.startswith("craft"):
                    hinted.add("craft")
                if path.name.startswith("lessons"):
                    hinted.add("lessons")
                writers = tuple(sorted(hinted))
                for view in views:
                    stores.append(StoreRecord("duckdb", view, relative, writers))

    return stores


def build_map(src_dir: Path, root: Path) -> RepoMap:
    """Parse the tree into modules, commands, and stores."""
    modules = load_modules(src_dir)
    commands = parse_cli_commands(src_dir / "cli.py")
    module_commands: dict[str, list[str]] = defaultdict(list)
    for command in commands:
        module_commands[command.module].append(command.name)
    for module in modules:
        rel_path = module.path.removeprefix("src/evallab/").removeprefix("src/evallab")
        mod_file = src_dir / rel_path.lstrip("/")
        for command in parse_module_cli(mod_file, module.name):
            if command.name not in module_commands[module.name]:
                commands.append(command)
                module_commands[module.name].append(command.name)
    owned = [
        ModuleRecord(
            name=module.name,
            path=module.path,
            line_count=module.line_count,
            purpose=module.purpose,
            commands=tuple(module_commands.get(module.name, ())),
        )
        for module in modules
    ]
    inputs: list[InputRecord] = []
    for mod_path in discover_module_paths(src_dir):
        rel = _relative_path(mod_path, root)
        digest = compute_file_digest(mod_path)
        inputs.append(InputRecord(path=rel, digest=digest))
    inputs.sort(key=lambda x: x.path)

    return RepoMap(
        modules=tuple(owned),
        commands=tuple(commands),
        stores=tuple(discover_stores(src_dir, root)),
        inputs=tuple(inputs),
    )


def _command_cell(commands: Sequence[str]) -> str:
    return ", ".join(f"`{name}`" for name in commands) if commands else "—"


def _writer_cell(writers: Sequence[str]) -> str:
    return ", ".join(f"`{name}`" for name in writers) if writers else "—"


def render_map(snapshot: RepoMap) -> str:
    """Render the committed map markdown. No timestamp; byte-stable."""
    lines = [
        "---",
        "status: living",
        "audience:",
        "  - builder",
        "  - analyst",
        "  - runner",
        "  - operator",
    ]
    if snapshot.inputs:
        lines.append("inputs:")
        for inp in snapshot.inputs:
            lines.append(f"  - path: {inp.path}")
            lines.append(f"    digest: {inp.digest}")
    else:
        lines.append("inputs: []")
    lines.extend(
        [
            "---",
            "",
            GENERATED_BY_MARKER,
            "",
            f"# {MAP_TITLE}",
            "",
            "AST-derived map of `src/evallab/`. Regenerated by",
            "`python -m evallab.repomap generate`. A stale committed copy fails",
            "`python -m evallab.repomap check`.",
            "",
            "## Modules",
            "",
            "| Module | Lines | Purpose | CLI |",
            "|---|---:|---|---|",
        ]
    )
    for module in snapshot.modules:
        purpose = module.purpose or "_(missing docstring)_"
        lines.append(
            f"| `{module.name}` | {module.line_count} | {purpose} | "
            f"{_command_cell(module.commands)} |"
        )

    lines.extend(
        [
            "",
            "## CLI surface",
            "",
            "Subcommands registered in `src/evallab/cli.py`, plus module-local",
            "`python -m evallab.<module>` entry points.",
            "",
            "| Command | Module | Help |",
            "|---|---|---|",
        ]
    )
    for command in snapshot.commands:
        help_text = command.help.replace("|", "\\|") if command.help else "—"
        lines.append(f"| `{command.name}` | `{command.module}` | {help_text} |")

    lines.extend(["", "## Data stores", ""])
    sections = (
        ("queue", "Queue directories"),
        ("parquet", "Derived Parquet"),
        ("duckdb", "DuckDB views (`sql/`)"),
        ("postgres", "Postgres catalog"),
        ("postgres-view", "Postgres catalog views"),
    )
    for kind, title in sections:
        rows = [store for store in snapshot.stores if store.kind == kind]
        lines.append(f"### {title}")
        lines.append("")
        lines.append("| Name | Location | Written by |")
        lines.append("|---|---|---|")
        if rows:
            for store in rows:
                lines.append(
                    f"| `{store.name}` | `{store.location}` | {_writer_cell(store.writers)} |"
                )
        else:
            lines.append("| — | _None._ | — |")
        lines.append("")

    return "\n".join(lines)


def generate_map(
    src_dir: Path | None = None,
    root: Path | None = None,
) -> str:
    """Generate the map text for the given source tree."""
    resolved_root = root if root is not None else repo_root()
    resolved_src = src_dir if src_dir is not None else default_src_dir(resolved_root)
    return render_map(build_map(resolved_src, resolved_root))


def write_map(
    output: Path,
    src_dir: Path | None = None,
    root: Path | None = None,
) -> str:
    """Generate and write the map. Returns the written text."""
    text = generate_map(src_dir=src_dir, root=root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return text


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def collect_check_issues(
    src_dir: Path,
    map_path: Path,
    root: Path | None = None,
) -> list[CheckIssue]:
    """Return every fail-closed problem in the source tree and committed map."""
    resolved_root = root if root is not None else repo_root()
    issues: list[CheckIssue] = []
    snapshot = build_map(src_dir, resolved_root)

    for module in snapshot.modules:
        if not module.purpose:
            issues.append(
                CheckIssue(module.path, "module has no docstring to describe it")
            )

    expected = render_map(snapshot)
    map_rel = _relative_path(map_path, resolved_root)
    if not map_path.is_file():
        issues.append(CheckIssue(map_rel, "committed map is missing"))
        return issues

    actual = map_path.read_text(encoding="utf-8")
    fm, _body = parse_front_matter(actual)
    if fm is None:
        issues.append(
            CheckIssue(map_rel, "committed map must begin with valid front-matter")
        )
    else:
        status_val = str(fm.get("status", "")).strip().lower()
        if status_val != "living":
            issues.append(CheckIssue(map_rel, f"status must be 'living', got {status_val!r}"))
        raw_audience = fm.get("audience", [])
        if isinstance(raw_audience, str):
            audiences = {raw_audience.strip().lower()}
        elif isinstance(raw_audience, (list, tuple)):
            audiences = {str(item).strip().lower() for item in raw_audience}
        else:
            audiences = set()
        missing_roles = [role for role in VALID_AUDIENCES if role not in audiences]
        if missing_roles:
            issues.append(
                CheckIssue(
                    map_rel,
                    f"audience must cover all four roles; missing {missing_roles}",
                )
            )
        if "inputs" not in fm or not isinstance(fm["inputs"], list):
            issues.append(
                CheckIssue(map_rel, "inputs field in front-matter must be a list")
            )
    if GENERATED_BY_MARKER not in actual:
        issues.append(CheckIssue(map_rel, "committed map is missing the generated-by marker"))
    if actual != expected:
        issues.append(
            CheckIssue(map_rel, "committed map is stale relative to a fresh generation")
        )
    return issues


def check_map(
    src_dir: Path | None = None,
    map_path: Path | None = None,
    root: Path | None = None,
) -> list[CheckIssue]:
    """Validate module descriptions and map freshness. Empty list means pass."""
    resolved_root = root if root is not None else repo_root()
    resolved_src = src_dir if src_dir is not None else default_src_dir(resolved_root)
    resolved_map = map_path if map_path is not None else default_map_path(resolved_root)
    return collect_check_issues(resolved_src, resolved_map, root=resolved_root)


def build_parser() -> argparse.ArgumentParser:
    """Construct the `python -m evallab.repomap` argument parser."""
    parser = argparse.ArgumentParser(
        prog="repomap",
        description="Generate and validate the deterministic docs/repo-map.md.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Subcommand to execute")

    generate_cmd = subparsers.add_parser(
        "generate", help="Write a deterministic repository map"
    )
    generate_cmd.add_argument(
        "-o",
        "--out",
        type=Path,
        metavar="FILE",
        default=None,
        help="Path to write the map (defaults to docs/repo-map.md)",
    )
    generate_cmd.add_argument(
        "--src-dir",
        type=Path,
        metavar="DIR",
        default=None,
        help="Directory containing evallab modules (defaults to src/evallab/)",
    )

    check_cmd = subparsers.add_parser(
        "check",
        help="Fail-closed validation of module descriptions and map freshness",
    )
    check_cmd.add_argument(
        "--src-dir",
        type=Path,
        metavar="DIR",
        default=None,
        help="Directory containing evallab modules (defaults to src/evallab/)",
    )
    check_cmd.add_argument(
        "--map",
        type=Path,
        metavar="FILE",
        default=None,
        help="Committed map path (defaults to docs/repo-map.md)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the repository map generator."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    root = repo_root()

    if args.command == "generate":
        src_dir = args.src_dir if args.src_dir is not None else default_src_dir(root)
        if not src_dir.is_dir():
            print(f"error: source directory not found: {src_dir}", file=sys.stderr)
            return 1
        resolved_root = root_for_src_dir(src_dir, None if args.src_dir else root)
        output = args.out if args.out is not None else default_map_path(resolved_root)
        write_map(output, src_dir=src_dir, root=resolved_root)
        print(f"Wrote repository map -> {output}")
        return 0

    if args.command == "check":
        src_dir = args.src_dir if args.src_dir is not None else default_src_dir(root)
        if not src_dir.is_dir():
            print(f"error: source directory not found: {src_dir}", file=sys.stderr)
            return 1
        resolved_root = root_for_src_dir(src_dir, None if args.src_dir else root)
        map_path = args.map if args.map is not None else default_map_path(resolved_root)
        issues = check_map(src_dir=src_dir, map_path=map_path, root=resolved_root)
        if issues:
            print("repomap check failed:", file=sys.stderr)
            for issue in issues:
                print(f"  {issue.path}: {issue.message}", file=sys.stderr)
            return 1
        print("repomap check passed")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
