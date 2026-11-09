"""Developer fast-loop command: map changed files to affected test modules.

Maps git diff / working tree changes to pytest modules using direct module
references and asset-path mentions, avoiding whole-suite runs during local
development loops.
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
    """Return relative paths of files modified according to `since`."""
    root_path = Path(root).resolve()
    if since == "working" or since is None or since == "":
        diff_res = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=root_path,
            capture_output=True,
            text=True,
            check=True,
        )
        untracked_res = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=root_path,
            capture_output=True,
            text=True,
            check=True,
        )
        lines = diff_res.stdout.splitlines() + untracked_res.stdout.splitlines()
    else:
        diff_res = subprocess.run(
            ["git", "diff", "--name-only", since],
            cwd=root_path,
            capture_output=True,
            text=True,
            check=True,
        )
        lines = diff_res.stdout.splitlines()

    return sorted({line.strip() for line in lines if line.strip()})


def is_full_suite_trigger(path_str: str) -> bool:
    """Return True if changed path requires running the full test suite."""
    norm = path_str.replace("\\", "/")
    if any(norm == p or norm.startswith(p) for p in FULL_SUITE_PATTERNS):
        return True
    p = PurePosixPath(norm)
    # Any conftest.py rewrites collection for its whole subtree.
    if p.name == "conftest.py":
        return True
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


class _ConsumerFeatures(TypedDict):
    tokens: list[str]
    import_names: list[str]
    from_imports: list[tuple[str, list[str]]]
    constant_text: str | None


@dataclass
class _DevloopCache:
    consumers: dict[str, _ConsumerFeatures]
    dirty: bool = False


def _devloop_cache_path(root: Path) -> Path | None:
    override = os.environ.get("EVALLAB_DEVLOOP_CACHE_DIR")
    if override is not None:
        return Path(override).expanduser() / "consumer-features.json"
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
        return (root / git_dir) / "devloop/consumer-features.json"
    except (OSError, subprocess.SubprocessError):
        return None


def _load_devloop_cache(path: Path | None) -> _DevloopCache:
    empty = _DevloopCache({})
    if path is None:
        return empty
    try:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or data.get("version") != 3:
            return empty
        consumers = data.get("consumers")
        if not isinstance(consumers, dict):
            return empty
        cache = _DevloopCache({})
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


def _write_devloop_cache(path: Path, cache: _DevloopCache) -> None:
    temporary: str | None = None
    try:
        merged = _load_devloop_cache(path)
        merged.consumers.update(cache.consumers)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = stream.name
            json.dump(
                {"version": 3, "consumers": merged.consumers},
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
    candidates = {token[:end] for token in features["tokens"] for end in range(1, len(token) + 1)}
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


def _is_asset_path(path_str: str, test_roots: Sequence[str] = ("tests",)) -> bool:
    """Return True for data/config/asset files that tests may mention by path."""
    return not (
        is_full_suite_trigger(path_str)
        or is_doc_path(path_str)
        or is_test_path(path_str, test_roots)
        or is_src_path(path_str)
    )


def _asset_needles(path_str: str) -> list[str]:
    """Path strings a test can mention to be affected by an asset change.

    A test selects the asset when it mentions the asset path itself or its
    immediate parent directory (tests often read a data directory wholesale).
    Grandparent mentions are too broad for a focused lane.
    """
    path = PurePosixPath(path_str.replace("\\", "/"))
    parent = path.parent.as_posix()
    return [path.as_posix(), parent] if len(path.parent.parts) > 1 else [path.as_posix()]


def _research_roots_for_asset(path_str: str, test_roots: Sequence[str]) -> list[str]:
    """Declared test roots that consume an asset under ``research/<subdir>/``.

    The asset's own ``research/<subdir>`` (or ``research/experiments/<exp>``)
    declared test roots cover it; the shared ``research/experiments/tests``
    root dynamically reads sibling research data (for example coverage
    recomputation over research/evidence cells), so it covers every
    research asset.
    """
    norm = path_str.replace("\\", "/")
    if not norm.startswith("research/"):
        return []
    parts = norm.split("/")
    if len(parts) < 3 or not parts[1]:
        return []
    asset_dir = "/".join(parts[:3]) if parts[1] == "experiments" else "/".join(parts[:2])
    return [
        root
        for root in test_roots
        if root == "research/experiments/tests"
        or root == asset_dir
        or root.startswith(asset_dir + "/")
    ]


def affected_test_modules(changed: Sequence[str], root: Path) -> list[str]:
    """Pure function mapping changed file paths to affected test modules."""
    return plan_devloop(changed, root).modules


def plan_devloop(changed: Sequence[str], root: Path) -> DevloopPlan:
    """Compute selection and reasons together from one local consumer inventory."""
    root_path = Path(root).resolve()
    if not changed:
        return DevloopPlan(changed=[], modules=[], command="", reasons={}, lane="none")

    test_roots = _test_roots(root_path)
    # Removed/renamed tests cannot be passed to pytest. Collect the remaining
    # suite instead of silently skipping coverage formerly owned by that file.
    full_triggers = [
        path
        for path in changed
        if is_full_suite_trigger(path)
        or (is_test_path(path, test_roots) and not (root_path / path.replace("\\", "/")).is_file())
    ]
    # Assets (research artifacts, ops scripts, package data) select tests that
    # mention their path; uncovered assets still fall back to the full suite.
    assets = [path for path in changed if _is_asset_path(path, test_roots)]
    markdown_assets = [path for path in changed if path.replace("\\", "/").endswith(".md")]
    reasons: dict[str, list[str]] = {}

    if full_triggers:
        modules = ["__all__"]
        lane = "full"
        command = "uv run --no-sync pytest -q -n0 -p no:cacheprovider"
        reasons["__all__"] = [
            f"Global, configuration, or removed test change: {', '.join(full_triggers)}"
        ]
    elif all(is_doc_path(p) for p in changed):
        modules = []
        lane = "docs_consumer"
        command = "uv run --no-sync pytest -q -n0 -p no:cacheprovider -m docs_consumer"
        reasons["docs_consumer"] = [f"Documentation-only change: {', '.join(changed)}"]
    else:
        targets = {src_path_to_module(path) for path in changed if is_src_path(path)}
        cache_path = _devloop_cache_path(root_path) if targets else None
        cache = _load_devloop_cache(cache_path)
        test_paths: dict[str, Path] = {}
        for test_root in test_roots:
            root_candidate = root_path / test_root
            candidates = (
                [root_candidate]
                if root_candidate.is_file()
                else (
                    test_file
                    for pattern in ("test_*.py", "*_test.py")
                    for test_file in root_candidate.rglob(pattern)
                )
            )
            for test_file in candidates:
                if not test_file.is_file():
                    continue
                rel = test_file.relative_to(root_path).as_posix()
                if is_test_path(rel, test_roots):
                    test_paths[rel] = test_file
        # Directly changed consumers need reasons even when their names do
        # not match pytest's discovery patterns.
        test_paths.update(
            {
                path.replace("\\", "/"): root_path / path.replace("\\", "/")
                for path in changed
                if is_test_path(path, test_roots)
            }
        )
        inventory = (
            {
                path: _referenced_modules(path, targets, cache)
                for path in sorted(set(test_paths.values()))
            }
            if targets
            else {}
        )
        if cache.dirty and cache_path is not None:
            _write_devloop_cache(cache_path, cache)
        consumers = sorted(test_paths)
        contents: dict[Path, str] = {}

        def _content(test_file: Path) -> str:
            if test_file not in contents:
                try:
                    contents[test_file] = test_file.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    contents[test_file] = ""
            return contents[test_file]

        covered_assets: set[str] = set()
        for path_str in changed:
            norm = path_str.replace("\\", "/")
            if is_test_path(norm, test_roots):
                reasons.setdefault(norm, []).append("Direct test file modification")
            elif is_src_path(norm):
                target_mod = src_path_to_module(norm)
                for rel in consumers:
                    if target_mod in inventory[test_paths[rel]]:
                        reasons.setdefault(rel, []).append(f"References {target_mod} ({norm})")
        for asset in assets:
            norm = asset.replace("\\", "/")
            needles = _asset_needles(norm)
            research_roots = _research_roots_for_asset(norm, test_roots)
            for rel in consumers:
                selected = False
                covering = next(
                    (root for root in research_roots if rel == root or rel.startswith(root + "/")),
                    None,
                )
                if covering is not None:
                    reasons.setdefault(rel, []).append(f"Declared test under {covering} ({norm})")
                    selected = True
                else:
                    for needle in needles:
                        if needle in _content(test_paths[rel]):
                            reasons.setdefault(rel, []).append(f"Mentions {needle} ({norm})")
                            selected = True
                            break
                if selected:
                    covered_assets.add(norm)
        if markdown_assets:
            for rel in consumers:
                if "docs_consumer" in _content(test_paths[rel]):
                    reasons.setdefault(rel, []).append(
                        f"Documentation consumer for {', '.join(markdown_assets)}"
                    )
        uncovered = [asset for asset in assets if asset.replace("\\", "/") not in covered_assets]
        if uncovered:
            modules = ["__all__"]
            lane = "full"
            command = "uv run --no-sync pytest -q -n0 -p no:cacheprovider"
            reasons["__all__"] = [f"Asset change with no mentioning test: {', '.join(uncovered)}"]
        else:
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
) -> int:
    """Execute devloop resolution and optionally stream pytest run."""
    root_path = Path(root).resolve()
    try:
        changed = get_changed_files(root_path, since=since)
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"error: Git change inventory failed for {since!r}: {detail}", file=sys.stderr)
        return 1
    plan = plan_devloop(changed, root_path)

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
    if not plan.changed:
        print("devloop: no working tree changes detected.")
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
