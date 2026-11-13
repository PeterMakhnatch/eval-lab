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
# Other lanes own these manuals. Each exception names one confirmed reference,
# not a whole manual. See the golden lane coordination receipt for the findings.
EXCEPTIONS = {
    # Unowned checkpoint example; replace its literal filename with a placeholder.
    "agents/CHECKS.md": {("path", "tests/test_foo.py")},
    # Unowned retained manual still names a retired frontend.
    "docs/data-architecture.md": {("route", "evallab trials")},
    # Unowned backend manual: retired queue routes and external task examples.
    "docs/execution-tiers.md": {
        ("make", "test"), ("path", "batch.py"), ("path", "conftest.py"),
        ("path", "terminus/AGENTS.md"), ("path", "trace_agents.py"),
        ("path", "trace_verify.py"), ("path", "trace_workspace.py"),
        ("route", "evallab approve"), ("route", "evallab submit"),
        ("route", "evallab tasks prepare"), ("route", "evallab tick"),
    },
    # Unowned manual uses a source-package prefix for generated station inputs.
    "docs/line.md": {
        ("path", "evidence/station-inputs/"),
        ("path", "evidence/station-inputs/*.jsonl"),
    },
    # Unowned manual: retired tools, external adapters, and literal task examples.
    "docs/live-watch.md": {
        ("path", "harbor_atif2otel/ids.py"), ("path", "library/tasks/my-task"),
        ("path", "library/tasks/my-task-hardened"),
        ("path", "library/tasks/my-task/reference.patch"),
        ("path", "readers/scout_clouddb.py"),
        ("path", "scorecards/ra3-FULL-dev-20261028.md"),
        ("path", "scout_metr.py"), ("path", "tt1-scorecard.md"),
        ("route", "evallab atlas"), ("route", "evallab qa-data"),
        ("route", "evallab tick"),
    },
    # Unowned MiMo study manuals: paths inside external task source trees.
    "docs/mimo/node-build-leak.md": {
        ("path", "../node-build-leak-class/"), ("path", "../src"),
        ("path", "./babel-plugin-idx"),
    },
    "docs/mimo/separate-verifier.md": {
        ("path", ".solve.sh"), ("path", "conftest.py"),
        ("path", "mimo_test_command.sh"), ("path", "test.sh"),
        ("path", "research-context/analysis/2026-10-14-own-runs-env-flaw-scan.md"),
        ("path", "research-context/bridge/2026-10-29-ds-squad/raw/TQ-2/report.md"),
    },
    # Unowned task manual: archived source and task-image/example paths.
    "docs/task-quality.md": {
        ("path", "burnman/classes/solution.py"), ("path", "checks/validate.py"),
        ("path", "conftest.py"),
        ("path", "research/experiments/har105-exploration/setup_payload.py"),
        ("path", "research/qa-scoreboard/suite-YYYYMMDD.md"),
        ("path", "scripts/fix.py"), ("path", "semantic-apply.sh"),
        ("path", "setup.py"), ("path", "src/test_runner.py"),
        ("path", "tests/grade.py"), ("path", "tests/test_core.py"),
        ("path", "tests/wheels/*.whl"), ("route", "evallab audit"),
    },
    # Unowned tool inventory still names removed tools.
    "docs/third-party.md": {
        ("path", "cost_inventory.py"), ("path", "sft_tinker.py"),
        ("path", "tools/tinker-sft/"),
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
    if token.startswith(("/", "~/", "$")) or first in _RUNTIME_ROOTS or token in _REPORT_NAMES or token in _TASK_LAYOUT:
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
    failures = []
    for page in instruction_pages(ROOT):
        relative = page.relative_to(ROOT).as_posix()
        for line, kind, name in drift_findings(ROOT, page, routes, targets):
            if (kind, name) not in EXCEPTIONS.get(relative, set()):
                failures.append(f"{relative}:{line}: missing {kind}: {name}")
    assert not failures, "Instruction drift; correct the reference, do not restore a retired command:\n" + "\n".join(failures)


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
