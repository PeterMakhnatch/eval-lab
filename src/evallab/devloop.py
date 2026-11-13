"""Developer fast-loop command: map changed files to affected test modules.

Maps git diff / working tree changes to pytest modules using direct references
and transitive imports through evallab modules, avoiding whole-suite runs during
local development loops.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypedDict

FULL_SUITE_PATTERNS = (
    ".github/",
    "Makefile",
    "pyproject.toml",
    "uv.lock",
    "sql/",
    "tests/conftest.py",
)

DOCS_PATTERNS = (
    "docs/",
    "agents/",
    ".omp/skills/",
    ".claude/skills/",
    "skills/",
)

CONFIG_SUFFIXES = {
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".ini",
    ".cfg",
    ".sh",
    ".env",
}


@dataclass(frozen=True)
class DevloopPlan:
    changed: list[str]
    modules: list[str]
    command: str
    reasons: dict[str, list[str]]
    lane: str


def get_changed_files(root: Path, since: str = "working") -> list[str]:
    """Include both rename sides, deletions, untracked files, and local edits."""
    root_path = Path(root).resolve()
    baseline = "HEAD"
    if since not in ("working", None, ""):
        baseline = subprocess.run(
            ["git", "merge-base", since, "HEAD"],
            cwd=root_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    diff_res = subprocess.run(
        ["git", "diff", "--no-renames", "--name-only", "-z", baseline],
        cwd=root_path,
        capture_output=True,
        text=True,
        check=True,
    )
    untracked_res = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=root_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(set(filter(None, (diff_res.stdout + untracked_res.stdout).split("\0"))))


def is_full_suite_trigger(path_str: str) -> bool:
    """Return True if changed path requires running the full test suite."""
    norm = path_str.replace("\\", "/")
    if any(norm == p or norm.startswith(p) for p in FULL_SUITE_PATTERNS):
        return True
    p = PurePosixPath(norm)
    return len(p.parts) == 1 and p.suffix in CONFIG_SUFFIXES


def is_doc_path(path_str: str) -> bool:
    """Return True if path is documentation-only."""
    norm = path_str.replace("\\", "/")
    if norm in {"AGENTS.md", "README.md"}:
        return True
    if norm.endswith(".md"):
        if any(norm.startswith(d) for d in DOCS_PATTERNS):
            return True
        if "/" not in norm:
            return True
    return False


def is_test_path(path_str: str, test_roots: Sequence[str] = ("tests",)) -> bool:
    """Return True if path is a Python test consumer under a declared test root."""
    path = PurePosixPath(path_str.replace("\\", "/"))
    return (
        path.suffix == ".py"
        and path.name != "conftest.py"
        and any(path.is_relative_to(test_root) for test_root in test_roots)
    )


def _test_roots(root: Path) -> list[str]:
    config_path = root / "pyproject.toml"
    if not config_path.is_file():
        return ["tests"]
    with config_path.open("rb") as config_file:
        config = tomllib.load(config_file)
    return (
        config.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("testpaths", ["tests"])
    )


def is_src_path(path_str: str) -> bool:
    """Return True if path is an evallab source file."""
    norm = path_str.replace("\\", "/")
    return norm.startswith("src/evallab/") and norm.endswith(".py")


def src_path_to_module(path_str: str) -> str:
    """Convert a src/evallab relative path to its dotted Python module name."""
    norm = path_str.replace("\\", "/")
    if not (norm.startswith("src/") and norm.endswith(".py")):
        raise ValueError(f"Path is not a src python file: {path_str}")
    inner = norm[len("src/") : -len(".py")]
    if inner.endswith("/__init__"):
        inner = inner[: -len("/__init__")]
    return inner.replace("/", ".")


type _ImportStatement = tuple[str, int, str, list[str]]


class _ConsumerFeatures(TypedDict):
    tokens: list[str]
    import_names: list[str]
    from_imports: list[tuple[str, list[str]]]
    constant_text: str | None


@dataclass
class _DevloopCache:
    sources: dict[str, list[_ImportStatement]]
    consumers: dict[str, _ConsumerFeatures]
    dirty: bool = False


def _import_graph_cache_path(root: Path) -> Path | None:
    override = os.environ.get("EVALLAB_DEVLOOP_CACHE_DIR")
    if override is not None:
        return Path(override).expanduser() / "import-graph.json"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        git_dir = Path(result.stdout.strip())
        return (root / git_dir) / "devloop/import-graph.json"
    except (OSError, subprocess.SubprocessError):
        return None


def _load_import_graph_cache(path: Path | None) -> _DevloopCache:
    empty = _DevloopCache({}, {})
    if path is None:
        return empty
    try:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or data.get("version") != 2:
            return empty
        sources, consumers = data.get("sources"), data.get("consumers")
        if not isinstance(sources, dict) or not isinstance(consumers, dict):
            return empty
        cache = _DevloopCache({}, {})
        for digest, records in sources.items():
            if not isinstance(digest, str) or not isinstance(records, list):
                return empty
            statements: list[_ImportStatement] = []
            for record in records:
                if not isinstance(record, list) or len(record) != 4:
                    return empty
                kind, level, module, names = record
                if (
                    kind not in ("import", "from")
                    or not isinstance(level, int)
                    or level < 0
                    or not isinstance(module, str)
                    or not isinstance(names, list)
                    or not all(isinstance(name, str) for name in names)
                ):
                    return empty
                statements.append((kind, level, module, names))
            cache.sources[digest] = statements
        for digest, features in consumers.items():
            if (
                not isinstance(digest, str)
                or not isinstance(features, dict)
                or set(features) != {"tokens", "import_names", "from_imports", "constant_text"}
            ):
                return empty
            tokens, import_names = features["tokens"], features["import_names"]
            from_imports, constant_text = features["from_imports"], features["constant_text"]
            if (
                not isinstance(tokens, list)
                or not all(isinstance(token, str) for token in tokens)
                or not isinstance(import_names, list)
                or not all(isinstance(name, str) for name in import_names)
                or not isinstance(from_imports, list)
                or not (constant_text is None or isinstance(constant_text, str))
            ):
                return empty
            validated_from: list[tuple[str, list[str]]] = []
            for record in from_imports:
                if (
                    not isinstance(record, list)
                    or len(record) != 2
                    or not isinstance(record[0], str)
                    or not isinstance(record[1], list)
                    or not all(isinstance(name, str) for name in record[1])
                ):
                    return empty
                validated_from.append((record[0], record[1]))
            cache.consumers[digest] = {
                "tokens": tokens,
                "import_names": import_names,
                "from_imports": validated_from,
                "constant_text": constant_text,
            }
        return cache
    except (OSError, UnicodeError, ValueError):
        return empty


def _write_import_graph_cache(path: Path, cache: _DevloopCache) -> None:
    temporary: str | None = None
    try:
        merged = _load_import_graph_cache(path)
        merged.sources.update(cache.sources)
        merged.consumers.update(cache.consumers)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = stream.name
            json.dump(
                {"version": 2, "sources": merged.sources, "consumers": merged.consumers},
                stream,
                separators=(",", ":"),
            )
        os.replace(temporary, path)
    except (OSError, UnicodeError, ValueError):
        pass
    finally:
        if temporary is not None:
            with suppress(OSError):
                os.unlink(temporary)


def _reverse_import_graph(root: Path, cache: _DevloopCache) -> dict[str, set[str]]:
    """Map evallab modules to the source modules that import them."""
    reverse: dict[str, set[str]] = {}
    for path in (root / "src/evallab").rglob("*.py"):
        module = src_path_to_module(path.relative_to(root).as_posix())
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        statements = cache.sources.get(digest)
        if statements is None:
            statements = []
            try:
                tree = ast.parse(content, filename=str(path))
            except SyntaxError:
                pass
            else:
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        statements.append(("import", 0, "", [alias.name for alias in node.names]))
                    elif isinstance(node, ast.ImportFrom):
                        statements.append(
                            (
                                "from",
                                node.level,
                                node.module or "",
                                [alias.name for alias in node.names],
                            )
                        )
            cache.sources[digest] = statements
            cache.dirty = True
        for kind, level, base, names in statements:
            if kind == "import":
                imports = names
            else:
                if level:
                    parts = package.split(".")
                    if level > len(parts):
                        continue
                    prefix = ".".join(parts[: len(parts) - level + 1])
                    base = f"{prefix}.{base}" if base else prefix
                imports = [base, *(f"{base}.{name}" for name in names)]
            for imported in imports:
                if imported != "evallab" and not imported.startswith("evallab."):
                    continue
                # Importing a child also executes its parent packages.
                parts = imported.split(".")
                for end in range(1, len(parts) + 1):
                    dependency = ".".join(parts[:end])
                    reverse.setdefault(dependency, set()).add(module)
    return reverse


def _importer_closure(module: str, reverse: dict[str, set[str]]) -> set[str]:
    """Include the changed module and all of its transitive importers."""
    reachable = {module}
    pending = [module]
    while pending:
        for importer in reverse.get(pending.pop(), ()):
            if importer not in reachable:
                reachable.add(importer)
                pending.append(importer)
    return reachable


def _consumer_features(content: str, test_path: Path) -> tuple[_ConsumerFeatures, list[str]]:
    features: _ConsumerFeatures = {
        "tokens": sorted(set(re.findall(r"(?=(evallab(?:\.\w+)*))", content))),
        "import_names": [],
        "from_imports": [],
        "constant_text": "",
    }
    strings: list[str] = []
    try:
        tree = ast.parse(content, filename=str(test_path))
    except Exception:
        return features, strings
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            features["import_names"].extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            features["from_imports"].append(
                (node.module, [f"{node.module}.{alias.name}" for alias in node.names])
            )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.append(node.value)
    features["constant_text"] = "\0".join(strings)
    return features, strings


def _referenced_modules(
    test_path: Path, target_modules: set[str], cache: _DevloopCache | None = None
) -> set[str]:
    """Read a consumer once and reuse target-independent features by content."""
    try:
        content = test_path.read_text(encoding="utf-8")
    except Exception:
        return set()
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    cached = cache.consumers.get(digest) if cache is not None else None
    strings: list[str] = []
    if cached is None or cached["constant_text"] is None or any("\0" in t for t in target_modules):
        features, strings = _consumer_features(content, test_path)
        if cache is not None and cached is None:
            stored = features.copy()
            if len(stored["constant_text"] or "") > 2 * 1024 * 1024:
                stored["constant_text"] = None
            cache.consumers[digest] = stored
            cache.dirty = True
    else:
        features = cached
    # Every module occurrence is a prefix of an evallab token. Overlap retains
    # occurrences inside longer tokens, such as evallab.evallab.consumer.
    candidates = {
        token[:end] for token in features["tokens"] for end in range(1, len(token) + 1)
    }
    matched = candidates & target_modules
    # Preserve textual matching for unusual source filenames, too.
    matched.update(
        target
        for target in target_modules - matched
        if re.fullmatch(r"evallab(?:\.\w+)*", target) is None and target in content
    )
    remaining = target_modules - matched
    import_names, from_imports = features["import_names"], features["from_imports"]
    # NUL-containing targets use the original per-constant predicate.
    constant_text = features["constant_text"] or ""

    for target in remaining:
        if (
            any(
                name == target
                or name.startswith(target + ".")
                or (target == "evallab" and name.startswith("evallab"))
                for name in import_names
            )
            or any(
                module == target
                or module.startswith(target + ".")
                or target.startswith(module + ".")
                and any(name == target or name.startswith(target + ".") for name in names)
                for module, names in from_imports
            )
            or (
                any(target in value for value in strings)
                if "\0" in target
                else target in constant_text
            )
        ):
            matched.add(target)

    return matched


def _is_unclassified(path_str: str, test_roots: Sequence[str] = ("tests",)) -> bool:
    """Check if file is unknown / unclassified configuration or root asset."""
    return is_full_suite_trigger(path_str) or not (
        is_doc_path(path_str) or is_test_path(path_str, test_roots) or is_src_path(path_str)
    )


CHECKPOINT_CONTRACT_TESTS = (
    "tests/test_quality_gates.py",
    "tests/test_ci_coverage.py",
    "tests/test_governance.py",
    "tests/test_route_page_links.py",
    "tests/test_fixture_conformance.py",
    "tests/test_attach.py::TestFrontMatterParsing",
)


def _path_references(path: str) -> list[str]:
    asset = PurePosixPath(path)
    # Basenames cover Path('docs') / 'page.md'; parents cover directory scans.
    needles = [path, asset.name]
    if len(asset.parent.parts) >= 2 and not is_src_path(path):
        needles.append(asset.parent.as_posix())
    return needles


def _checkpoint_plan(changed: Sequence[str], root: Path) -> DevloopPlan:
    """Select direct consumers and repository contracts without a full fallback."""
    test_roots = _test_roots(root)
    consumers: dict[str, Path] = {}
    for test_root in test_roots:
        candidate = root / test_root
        paths = (
            [candidate]
            if candidate.is_file()
            else [
                path
                for pattern in ("test_*.py", "*_test.py")
                for path in candidate.rglob(pattern)
            ]
        )
        for path in paths:
            if path.is_file():
                consumers[path.relative_to(root).as_posix()] = path
    normalized = [path.replace("\\", "/") for path in changed]
    targets = {src_path_to_module(path) for path in normalized if is_src_path(path)}
    references = {path: _path_references(path) for path in normalized}
    asset_needles = {
        needle
        for path, needles in references.items()
        if not path.endswith(".py")
        for needle in needles
        if needle != "README.md"
    }
    # Match literal loader paths, not comments or prose pointing at a manual.
    if asset_needles:
        for source in (root / "src/evallab").rglob("*.py"):
            try:
                tree = ast.parse(source.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, SyntaxError):
                continue
            if any(
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in asset_needles
                for node in ast.walk(tree)
            ):
                targets.add(src_path_to_module(source.relative_to(root).as_posix()))
    cache_path = _import_graph_cache_path(root) if targets else None
    cache = _load_import_graph_cache(cache_path)
    reasons: dict[str, list[str]] = {
        selector: ["Always-run repository contract"]
        for selector in CHECKPOINT_CONTRACT_TESTS
        if (root / selector.partition("::")[0]).is_file()
    }
    for path in normalized:
        if is_test_path(path, test_roots) and (root / path).is_file():
            reasons.setdefault(path, []).append("Direct test file modification")
    for rel, consumer in sorted(consumers.items()):
        matched = _referenced_modules(consumer, targets, cache) if targets else set()
        if matched:
            reasons.setdefault(rel, []).append(f"References {', '.join(sorted(matched))}")
        content = consumer.read_text(encoding="utf-8")
        for path, needles in references.items():
            if any(needle in content for needle in needles):
                reasons.setdefault(rel, []).append(f"References changed path {path}")
                break
    if cache.dirty and cache_path is not None:
        _write_import_graph_cache(cache_path, cache)
    # A whole file already contains any selected nodes from that file.
    modules = sorted(
        selector
        for selector in reasons
        if "::" not in selector or selector.partition("::")[0] not in reasons
    )
    command = (
        "uv run --no-sync pytest -q -n2 -p no:cacheprovider " + shlex.join(modules)
        if modules
        else ""
    )
    return DevloopPlan(list(changed), modules, command, reasons, "focused" if modules else "none")


def plan_devloop(
    changed: Sequence[str], root: Path, *, checkpoint: bool = False
) -> DevloopPlan:
    """Compute selection and reasons together from one local consumer inventory."""
    root_path = Path(root).resolve()
    if checkpoint:
        return _checkpoint_plan(changed, root_path)
    if not changed:
        return DevloopPlan(changed=[], modules=[], command="", reasons={}, lane="none")

    test_roots = _test_roots(root_path)
    # Removed/renamed tests cannot be passed to pytest. Collect the remaining
    # suite instead of silently skipping coverage formerly owned by that file.
    requires_full_suite = any(
        is_full_suite_trigger(path)
        or _is_unclassified(path, test_roots)
        or (is_test_path(path, test_roots) and not (root_path / path.replace("\\", "/")).is_file())
        for path in changed
    )
    reasons: dict[str, list[str]] = {}

    if requires_full_suite:
        modules = ["__all__"]
        lane = "full"
        command = "uv run --no-sync pytest -q -n0 -p no:cacheprovider"
        triggering = [
            p
            for p in changed
            if is_full_suite_trigger(p)
            or _is_unclassified(p, test_roots)
            or (is_test_path(p, test_roots) and not (root_path / p).is_file())
        ]
        reasons["__all__"] = [
            f"Global, configuration, or removed test change: {', '.join(triggering)}"
        ]
    elif all(is_doc_path(p) for p in changed):
        modules = []
        lane = "docs_consumer"
        command = "uv run --no-sync pytest -q -n0 -p no:cacheprovider -m docs_consumer"
        reasons["docs_consumer"] = [f"Documentation-only change: {', '.join(changed)}"]
    else:
        targets = {src_path_to_module(path) for path in changed if is_src_path(path)}
        cache_path = _import_graph_cache_path(root_path) if targets else None
        cache = _load_import_graph_cache(cache_path)
        reverse = _reverse_import_graph(root_path, cache) if targets else {}
        expansions = {target: _importer_closure(target, reverse) for target in targets}
        targets = {module for reachable in expansions.values() for module in reachable}
        test_paths: dict[str, Path] = {}
        if targets:
            test_paths = {
                test_file.relative_to(root_path).as_posix(): test_file
                for test_root in test_roots
                for pattern in ("test_*.py", "*_test.py")
                for test_file in (root_path / test_root).rglob(pattern)
                if test_file.is_file()
            }
            # Directly changed consumers need reasons even when their names do
            # not match pytest's discovery patterns.
            test_paths.update(
                {
                    path.replace("\\", "/"): root_path / path.replace("\\", "/")
                    for path in changed
                    if is_test_path(path, test_roots)
                }
            )
        inventory = {
            path: _referenced_modules(path, targets, cache)
            for path in sorted(set(test_paths.values()))
        }
        if cache.dirty and cache_path is not None:
            _write_import_graph_cache(cache_path, cache)
        consumers = sorted(test_paths)
        for path_str in changed:
            norm = path_str.replace("\\", "/")
            if is_test_path(norm, test_roots):
                reasons.setdefault(norm, []).append("Direct test file modification")
            elif is_src_path(norm):
                target_mod = src_path_to_module(norm)
                for rel in consumers:
                    matched = inventory[test_paths[rel]] & expansions[target_mod]
                    if target_mod in matched:
                        reason = f"Imports {target_mod} ({norm})"
                    elif matched:
                        reason = f"Imports {target_mod} via {min(matched)}"
                    else:
                        continue
                    reasons.setdefault(rel, []).append(reason)
        modules = sorted(reasons)
        lane = "focused" if modules else "none"
        command = (
            f"uv run --no-sync pytest -q -n0 -p no:cacheprovider {shlex.join(modules)}"
            if modules
            else ""
        )

    return DevloopPlan(
        changed=list(changed),
        modules=modules,
        command=command,
        reasons=reasons,
        lane=lane,
    )


def run_devloop(
    root: Path,
    *,
    since: str = "working",
    run: bool = False,
    as_json: bool = False,
    checkpoint: bool = False,
) -> int:
    """Execute devloop resolution and optionally stream pytest run."""
    root_path = Path(root).resolve()
    try:
        changed = get_changed_files(root_path, since=since)
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"error: Git change inventory failed for {since!r}: {detail}", file=sys.stderr)
        return 1
    plan = plan_devloop(changed, root_path, checkpoint=checkpoint)

    if as_json:
        payload = {
            "changed": plan.changed,
            "modules": plan.modules,
            "command": plan.command,
            "lane": plan.lane,
            "reasons": plan.reasons,
        }
        print(json.dumps(payload, indent=2), flush=True)
        if run and plan.command:
            proc = subprocess.run(shlex.split(plan.command), cwd=root_path, stdout=sys.stderr)
            return proc.returncode
        return 0

    # Human-readable output
    if not plan.changed and not plan.command:
        print("devloop: no changes detected.")
        return 0

    print(f"devloop: {len(plan.changed)} changed file(s) detected.")
    if plan.lane == "full":
        print("devloop: full test suite required.")
        for r in plan.reasons.get("__all__", []):
            print(f"  - {r}")
    elif plan.lane == "docs_consumer":
        print("devloop: documentation changes detected; using docs_consumer lane.")
        for r in plan.reasons.get("docs_consumer", []):
            print(f"  - {r}")
    elif plan.lane == "focused":
        print(f"devloop: {len(plan.modules)} affected test module(s):")
        for mod in plan.modules:
            mod_reasons = plan.reasons.get(mod, [])
            reasons_str = "; ".join(mod_reasons) if mod_reasons else "Affected"
            print(f"  - {mod} ({reasons_str})")

    if plan.command:
        print(f"\nCommand:\n  {plan.command}")

    if run:
        if not plan.command:
            print("devloop: no test command to execute.")
            return 0
        print("\nExecuting command...\n")
        proc = subprocess.run(shlex.split(plan.command), cwd=root_path)
        return proc.returncode

    return 0
