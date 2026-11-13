"""Keep executable instruction references aligned with the checked-out repository.

Only repository references are checked: host paths, generated runtime artifacts,
URLs, task-image paths, and Python API names are not repository source paths.
Historical research and task payloads are not agent instruction manuals.
"""

from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path

import pytest

pytestmark = pytest.mark.docs_consumer
ROOT = Path(__file__).resolve().parents[1]
# A retired generated snapshot, not a maintained instruction manual.
HISTORICAL = {"docs/STATUS.md"}
# Exact external/runtime names only. Keep each reason specific to this manual.
EXCEPTIONS = {
    "agents/CHECKS.md": {
        ("path", "tests/test_foo.py"): "Literal pytest selector example in the CI manual, not a repository test.",
    },
    "docs/line.md": {
        ("path", "evidence/station-inputs/"): "Generated Station snapshot directory inside a line batch.",
        ("path", "evidence/station-inputs/*.jsonl"): "Generated Station snapshots inside a line batch.",
    },
    "docs/mimo/node-build-leak.md": {
        ("path", "./babel-plugin-idx"): "Literal JS require target inside external MiMo task 000919.",
        ("path", "../src"): "Literal JS import target inside external MiMo task 000928.",
    },
    "docs/mimo/separate-verifier.md": {
        ("path", ".solve.sh"): "Filename suffix of the external HAR-207 probe scripts.",
        ("path", "conftest.py"): "Pytest auto-load filename removed from evaluated task-container diffs.",
    },
    "docs/task-quality.md": {
        ("path", "burnman/classes/solution.py"): "Production module inside the external burnman task repository.",
        ("path", "checks/validate.py"): "Illustrative unrecognized grader path inside an evaluated task.",
        ("path", "conftest.py"): "Pytest auto-load filename inside evaluated task workspaces.",
        ("path", "scripts/fix.py"): "Illustrative agent helper script inside an evaluated task.",
        ("path", "semantic-apply.sh"): "Generated candidate custody script retained with its patch and sidecar.",
        ("path", "setup.py"): "Task-workspace packaging configuration matched by L0.",
        ("path", "src/test_runner.py"): "Illustrative task production path in the recorded L0 false-drop limit.",
        ("path", "tests/test_core.py"): "Illustrative protected test path inside an evaluated task.",
    },
}

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})([^\n]*)$")
_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)
_COMMAND = re.compile(r"(?=(?<![\w./-])evallab[ \t]+([^\n`]+))")
_MAKE = re.compile(r"(?=(?<![\w./-])make[ \t]+([^\n`]+))")
_PATH = re.compile(r"(?<![\w./-])(?:~?/|\$\w+/|\.{1,2}/)?[.\w-]+(?:/[.\w*?<>@{}=-]+)*/?")
_SOURCE_SUFFIXES = {".py", ".sh", ".sql", ".md"}
_ASSET_SUFFIXES = {".json", ".jsonl", ".yaml", ".yml", ".toml", ".txt"}
_ROOT_SOURCE_FILES = {"AGENTS.md", "README.md", "LICENSE", "Makefile", "pyproject.toml", "uv.lock", "compose.yaml", ".env.example", ".gitignore", ".ignore", ".gitattributes", ".python-version"}
_REPO_ROOTS = {
    "agents", "authoring", "containers", "digests", "docs", "grids",
    "library", "policy", "research", "scripts", "sql", "src", "tests",
    "tools", ".github", ".omp", ".claude", ".githooks",
}
# Application-relative paths in the domain router and package instructions.
_PACKAGE_ROOTS = {"schemas", "storage", "evidence", "interpretation", "recovery", "cli", "execution", "gold", "score", "failure_library", "line", "production", "qa", "data"}
# These are generated/host state, not source files that must exist in a checkout.
_RUNTIME_ROOTS = {"runs", "queue", "derived", "backups", "exports", "logs", ".worktrees", ".venv"}
# Bare report filenames are runtime artifacts. A qualified docs/foo.md or
# scripts/foo.py reference still receives the repository existence check.
_REPORT_NAMES = {"SCOREBOARD.md", "BOARD.md", "gate.md", "facts.md", "analysis.md", "prompt.md", "trajectory_summary.md", "trace-comparison.md"}
# These names describe paths inside a Harbor task, not this repository's tests.
_TASK_LAYOUT = {"instruction.md", "solve.sh", "solution/solve.sh", "tests/test.sh", "tests/Dockerfile", "tests/test.patch", "environment/setup/setup.sh", "_verifier-setup/setup.sh", "tests/wheels/"}


def instruction_pages(root: Path) -> list[Path]:
    pages = [root / "AGENTS.md", root / "README.md"]
    pages.extend((root / "agents").glob("*.md"))
    pages.extend((root / "docs").glob("*.md"))
    pages.extend((root / "docs/mimo").glob("*.md"))
    pages.extend((root / "src/evallab").rglob("AGENTS.md"))
    return sorted(p for p in pages if p.relative_to(root).as_posix() not in HISTORICAL)


def code_units(text: str) -> list[tuple[int, str, bool]]:
    """Return original line, code, and whether shell commands are meaningful."""
    units: list[tuple[int, str, bool]] = []
    prose: list[str] = []
    fence: str | None = None
    shell = False
    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        marker = _FENCE.match(line.rstrip("\n"))
        if marker and fence is None:
            fence = marker[1]
            shell = marker[2].strip() in {"", "sh", "bash", "zsh", "shell", "console", "text"}
            prose.append("\n")
        elif marker and fence and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
            fence = None
            prose.append("\n")
        elif fence:
            if units and units[-1][1].rstrip().endswith("\\"):
                start, previous, _ = units[-1]
                units[-1] = (start, previous.rstrip("\n").removesuffix("\\") + "\n" + line, shell)
            else:
                units.append((line_number, line, shell))
            prose.append("\n")
        else:
            prose.append(line)
    outside = "".join(prose)
    for match in _SPAN.finditer(outside):
        # A linked filename belongs to its external source, not this checkout.
        if (
            _PATH.fullmatch(match[2])
            and outside[match.start() - 1:match.start()] == "["
            and re.match(r"\]\(https?://[^\s)]+\)", outside[match.end():])
        ):
            continue
        units.append((outside.count("\n", 0, match.start()) + 1, match[2], True))
    return sorted(units)


def route_tree(parser: argparse.ArgumentParser) -> dict:
    children = {
        name: route_tree(child)
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
        for name, child in action.choices.items()
    }
    # A leaf with no positional arguments cannot have a hidden subcommand.
    return children or {"": any(not action.option_strings for action in parser._actions)}


def _words(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        # A prose span or a continued shell line can contain an open quote.
        return [word for word in re.split(r"\s+|(?=[;&|])|(?<=[;&|])", command) if word]


def _missing_route(words: list[str], routes: dict) -> str | None:
    current = routes
    seen = ["evallab"]
    for word in words:
        word = word.rstrip("'\",)")
        if word.startswith(("-", "<", "$", "{", "[")) or word in {"\\", "…", "...", "&&", "||", ";", "|"}:
            break
        if "" in current:
            return None if current[""] else " ".join([*seen, word])
        if not current:
            break  # Remaining tokens are leaf arguments, not command routes.
        seen.append(word)
        if word not in current:
            return " ".join(seen)
        current = current[word]
    return None


def _repository_reference(token: str, *, package_instruction: bool) -> bool:
    first = token.split("/", 1)[0]
    task_layout = any(token == entry or (entry.endswith("/") and token.startswith(entry)) for entry in _TASK_LAYOUT)
    if token.startswith(("/", "~/", "$")) or first in _RUNTIME_ROOTS or token in _REPORT_NAMES or task_layout:
        return False
    if first in _REPO_ROOTS or first in _PACKAGE_ROOTS or token.startswith(("./", "../")):
        return True
    # Bare source/module references use the manual or application package root.
    return (
        token in _ROOT_SOURCE_FILES
        or Path(token).suffix in _SOURCE_SUFFIXES
        or (package_instruction and Path(token).suffix in _ASSET_SUFFIXES)
    )


def _path_exists(root: Path, page: Path, token: str) -> bool:
    bases = [root, page.parent, root / "src/evallab"]
    if page.is_relative_to(root / "src/evallab"):
        bases.append(page.parent.parent)
    candidates = [base / token for base in bases]
    if "*" in token or "?" in token:
        return any(next(base.glob(token), None) is not None for base in bases if base.is_dir())
    if any(p.exists() for p in candidates):
        return True
    if "/" not in token and token.endswith(".sh"):
        return any((root / "scripts").rglob(token))
    return False


def drift_findings(
    root: Path, page: Path, routes: dict, targets: set[str], *, text: str | None = None,
) -> list[tuple[int, str, str]]:
    findings: set[tuple[int, str, str]] = set()
    package_instruction = page.is_relative_to(root / "src/evallab")
    for line, code, shell in code_units(page.read_text(encoding="utf-8") if text is None else text):
        if shell:
            for match in _COMMAND.finditer(code.replace("\n", " ")):
                missing = _missing_route(_words(match[1]), routes)
                if missing:
                    findings.add((line + code.count("\n", 0, match.start()), "route", missing))
            for match in _MAKE.finditer(code.replace("\n", " ")):
                for word in _words(match[1]):
                    word = word.rstrip("'\",)")
                    if word.startswith(("-", "<", "$", "[", "{")) or "=" in word or word in {"&&", "||", ";", "|", "\\", "…", "..."}:
                        break
                    if word not in targets:
                        findings.add((line + code.count("\n", 0, match.start()), "make", word))
        for match in _PATH.finditer(code):
            token = match[0]
            if code.strip().startswith(("/", "~/", "$")):
                continue
            # Do not reinterpret components of host paths, URLs, or Python APIs.
            before = code[:match.start()]
            if before.endswith(("/", "~/", "$")) or re.search(r"(?:https?://|[\w.]\.)$", before):
                continue
            if "<" in token or "{" in token or not _repository_reference(token, package_instruction=package_instruction):
                continue
            if not _path_exists(root, page, token):
                findings.add((line + code.count("\n", 0, match.start()), "path", token))
    return sorted(findings)


def test_instruction_references_resolve() -> None:
    # Parser construction registers routes but never executes their handlers.
    from evallab.cli import parser

    routes = route_tree(parser())
    targets = set(re.findall(r"^([A-Za-z][\w-]*)\s*:", (ROOT / "Makefile").read_text(), re.MULTILINE))
    exemptions = {(page, kind, name) for page, entries in EXCEPTIONS.items() for kind, name in entries}
    assert len(exemptions) <= 15, "Instruction drift exemptions exceed 15; correct references instead of expanding the list."
    assert all(reason.strip() for entries in EXCEPTIONS.values() for reason in entries.values()), (
        "Every instruction drift exemption needs a specific external/runtime reason."
    )
    used_exemptions = set()
    failures = []
    for page in instruction_pages(ROOT):
        relative = page.relative_to(ROOT).as_posix()
        for line, kind, name in drift_findings(ROOT, page, routes, targets):
            if (kind, name) in EXCEPTIONS.get(relative, {}):
                used_exemptions.add((relative, kind, name))
            else:
                failures.append(f"{relative}:{line}: missing {kind}: {name}")
    assert not failures, "Instruction drift; correct the reference, do not restore a retired command:\n" + "\n".join(failures)
    assert not (exemptions - used_exemptions), f"Remove stale instruction drift exemptions: {sorted(exemptions - used_exemptions)}"


def test_missing_references_report_original_lines(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "scripts").mkdir()
    page = tmp_path / "docs/manual.md"
    page.write_text(
        "---\nstatus: living\n---\n\n# Manual\n\n"
        "`evallab tasks prepare` and `make absent`.\n\n"
        "```sh\nuv run evallab gone\npython scripts/missing.py\n```\n"
        "\n`docs/absent.md`\n",
        encoding="utf-8",
    )
    assert drift_findings(tmp_path, page, {"tasks": {"lint": {}}}, {"prepush"}) == [
        (7, "make", "absent"), (7, "route", "evallab tasks prepare"),
        (10, "route", "evallab gone"), (11, "path", "scripts/missing.py"),
        (14, "path", "docs/absent.md"),
    ]


def test_nested_routes_arguments_and_package_paths(tmp_path: Path) -> None:
    package = tmp_path / "src/evallab/storage"
    package.mkdir(parents=True)
    (package / "paths.py").touch()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/example.py").touch()
    page = package / "AGENTS.md"
    page.write_text(
        "Use `paths.py` and `evallab failures bank gold-counts --json`.\n"
        "Use `make prepush TESTS='tests/example.py'`.\n"
        "Artifacts: `runs/job/result.json`, `~/state/output.md`, `/tmp/receipt.py`.\n",
        encoding="utf-8",
    )
    routes = {"failures": {"bank": {"gold-counts": {}}}}
    assert drift_findings(tmp_path, page, routes, {"prepush"}) == []


def test_multiline_spans_and_compound_commands(tmp_path: Path) -> None:
    page = tmp_path / "AGENTS.md"
    page.write_text(
        "``evallab tasks\nprepare``\n\n"
        "```sh\nevallab preflight && evallab gone\n```\n"
        "\n```sh\nevallab tasks \\\n  prepare\n```\n",
        encoding="utf-8",
    )
    routes = {"tasks": {"lint": {}}, "preflight": {}}
    assert drift_findings(tmp_path, page, routes, set()) == [
        (1, "route", "evallab tasks prepare"), (5, "route", "evallab gone"),
        (9, "route", "evallab tasks prepare"),
    ]


def test_leaf_without_positionals_rejects_a_stale_subcommand() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers()
    commands.add_parser("watch")
    ship = commands.add_parser("ship")
    ship.add_argument("pr")
    routes = route_tree(parser)
    assert _missing_route(["watch", "ack"], routes) == "evallab watch ack"
    assert _missing_route(["ship", "123"], routes) is None


def test_root_entry_stays_bounded() -> None:
    assert len((ROOT / "AGENTS.md").read_text().splitlines()) <= 120, (
        "AGENTS.md exceeds 120 lines; move detail to the single owning manual."
    )


def test_package_assets_and_root_dotfiles_are_source_references(tmp_path: Path) -> None:
    package = tmp_path / "src/evallab/data"
    package.mkdir(parents=True)
    (package / "present.json").touch()
    page = package / "AGENTS.md"
    page.write_text("Use `present.json`, `missing.json`, and `.ignore`.\n", encoding="utf-8")
    assert drift_findings(tmp_path, page, {}, set()) == [
        (1, "path", ".ignore"), (1, "path", "missing.json"),
    ]


def test_external_source_links_do_not_hide_local_references(tmp_path: Path) -> None:
    page = tmp_path / "AGENTS.md"
    page.write_text(
        "[`batch.py`](https://example.org/upstream/batch.py)\n"
        "`batch.py`\n"
        "[`scripts/missing.py`](scripts/missing.py)\n",
        encoding="utf-8",
    )
    assert drift_findings(tmp_path, page, {}, set()) == [
        (2, "path", "batch.py"), (3, "path", "scripts/missing.py"),
    ]


def test_task_wheel_paths_do_not_hide_repository_tests(tmp_path: Path) -> None:
    page = tmp_path / "AGENTS.md"
    page.write_text(
        "Task wheels: `tests/wheels/*.whl` and `tests/wheels/package.whl`.\n"
        "Repository test: `tests/missing.py`.\n",
        encoding="utf-8",
    )
    assert drift_findings(tmp_path, page, {}, set()) == [(2, "path", "tests/missing.py")]
