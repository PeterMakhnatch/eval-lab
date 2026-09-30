"""Per-task health census for the MiMo Python code pool (HAR-108).

One row per pool task, labeled ``sound``, ``broken_environment``,
``grader_suspect`` or ``unknown`` from the task files plus the latest nop
trial. No model runs: this only reads task directories and finished nop
jobs. The catalog table is ``task_health.parquet``, written next to
``task_qualification.parquet``.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.evidence.facts import exception_phase_for
from evallab.task_qualification import (
    _latest_key,
    _trial_reward_file,
    detect_grader_collection_failure,
)

#: Catalog table written by :func:`write_task_health_parquet`.
TABLE_FILENAME = "task_health.parquet"
TABLE = Path(TABLE_FILENAME).stem

LABELS = ("sound", "broken_environment", "grader_suspect", "unknown")

#: Files the MiMo harness adds to every code task; not hidden tests.
HARNESS_FILES = frozenset({"mimo_test_command.sh", "test_commands.json"})

#: A nop whose verifier output matches this broke before grading: a raised
#: missing module or package, a pytest collection or fixture-setup error, a
#: conftest that cannot import, a package whose C extensions were never built
#: (pandas, CuPy in part 3), or a missing command. Two near misses are
#: deliberately not matched: "cannot import name X" from the task's own code
#: is often the function the agent must write (1702), and a logged "No module
#: named" (stevedore skipping Bandit's optional sarif formatter in 1789) is not
#: a raised error. A pytest collection error can also be the missing feature
#: itself; :func:`setup_error_excused` records those, and
#: ``select_python.SETUP_ERROR_OK`` records the ones checked by hand.
SETUP_ERROR = re.compile(
    r"ModuleNotFoundError|PackageNotFoundError"
    r"|ERROR collecting|ERROR at setup|command not found"
    r"|ImportError while loading conftest|is not correctly installed|build_ext"
)

#: Grader text meaning the hidden test patch never landed, so no reward exists.
_NOT_APPLIED = re.compile(r"the hidden tests could not be applied", re.IGNORECASE)

_PYTEST_RAN = re.compile(r"collected \d+ items?|\b\d+ passed\b|\b\d+ failed\b", re.IGNORECASE)
_UNITTEST_RAN = re.compile(r"^Ran (?P<n>\d+) tests?\b", re.MULTILINE)
_COLLECTED = re.compile(r"collected (?P<n>\d+) items?", re.IGNORECASE)
_COLLECTION_ERROR = re.compile(r"ERROR collecting|error during collection", re.IGNORECASE)

_PYTEST_CMD = re.compile(r"\bpytest\b")
_UNITTEST_CMD = re.compile(r"\bunittest\b")
_BUNDLED_ARCHIVE = "mimo_build_env.tar.gz.b64"
_BUNDLED_RUNNER = ".build_env/test_command.sh"
_DJANGO_CMD = re.compile(r"manage\.py\s+test\b")
_PYTEST_TARGET = re.compile(r"(?:^|\s)((?:[\w./-]+/)?[\w.-]+\.py)\b")
_UNITTEST_TARGET = re.compile(r"\bunittest\s+((?:[\w.-]+\.)+[\w.-]+)")
_PYTEST_NODE = re.compile(r"([\w./-]+\.py)::")

_FROM_IMPORT = re.compile(r"^from\s+(\S+)\s+import\s+(.+)$")
_IMPORT_NAME = re.compile(r"[A-Za-z_]\w*")
_PY_STRING = re.compile(r"""(?P<q>['\"])(?P<path>[^'\"]+\.py)(?P=q)""")
_ASSIGN = re.compile(r"^(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<expr>.+)$")
_GETSOURCE = re.compile(r"\binspect\.getsource\s*\(")
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
_GIT_HEADER = re.compile(
    r"^(?:old mode |new mode |deleted file mode |new file mode |index |"
    r"similarity index |dissimilarity index |rename from |rename to |"
    r"copy from |copy to |--- |\+\+\+ )"
)

#: Top-level modules a hidden test imports that are not the project.
_EXCLUDED_IMPORTS = frozenset(
    {
        "pytest",
        "mock",
        "hypothesis",
        "numpy",
        "pandas",
        "requests",
        "yaml",
        "six",
        "attr",
        "attrs",
        "pydantic",
        "sqlalchemy",
        "django",
        "flask",
        "typing_extensions",
        "tests",
        "test",
        "conftest",
    }
)

_CANNOT_IMPORT_NAME = re.compile(r"cannot import name '(?P<name>[^']+)'")
_NO_MODULE = re.compile(r"No module named '(?P<module>[^']+)'")

_EVIDENCE_LIMIT = 200

#: Hosts the harness blocklist must name before git cannot be fetched.
_GIT_LEAK_HOSTS = (
    "github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "gitlab.com",
    "bitbucket.org",
)
#: Either host is enough: both serve the released package.
_PYPI_LEAK_HOSTS = ("pypi.org", "files.pythonhosted.org")
LEAK_CHANNELS = ("pypi_fix_released", "pypi_package", "git_only", "none_found", "unknown")
_HOST_LINE = re.compile(r"^\S+\s+(?P<host>\S+)\s*$")


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def setup_error_excused(stdout_texts: Sequence[str], instruction: str | None) -> bool:
    """True when every ``SETUP_ERROR`` match is the agent's missing work.

    :func:`evallab.task_qualification.detect_grader_collection_failure`
    excuses only a missing *module* whose top-level name the instruction
    names, and only inside a pytest collection failure. That misses the
    census case: ``cannot import name 'rename' from 'siuba'`` is a missing
    symbol, not a missing module, and the instruction names ``rename``.
    A match is excused when that helper returns ``None`` and every name the
    error says is missing (the ``cannot import name`` symbol, or the leaf of
    a ``No module named`` module) appears as a whole word in the instruction.
    An environment defect the instruction happens to mention (CuPy's
    ``is not correctly installed``, pandas' ``build_ext``) names no missing
    symbol, so it is never excused.
    """
    if detect_grader_collection_failure(stdout_texts, instruction_text=instruction) is not None:
        return False
    if not instruction:
        return False
    combined = "\n".join(stdout_texts)
    matched = [line for line in combined.splitlines() if SETUP_ERROR.search(line)]
    if not matched:
        return False
    names = [match.group("name") for match in _CANNOT_IMPORT_NAME.finditer(combined)] + [
        match.group("module").rsplit(".", 1)[-1] for match in _NO_MODULE.finditer(combined)
    ]
    return bool(names) and all(_word_in(instruction, name) for name in names)


def _word_in(text: str, word: str) -> bool:
    return re.search(r"\b" + re.escape(word) + r"\b", text) is not None


def _trim(text: str, limit: int = _EVIDENCE_LIMIT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- static checks ---------------------------------------------------------


def _patch_sections(patch_text: str) -> list[tuple[str, str]]:
    """``(path, section text)`` for every ``diff --git`` file section."""
    sections: list[tuple[str, str]] = []
    for match in re.finditer(
        r"(?m)^diff --git a/(\S+) b/\S+.*?(?=^diff --git |\Z)", patch_text, re.S
    ):
        sections.append((match.group(1), match.group(0)))
    return sections


def _added_lines(section: str) -> list[str]:
    """Added source lines of one patch section, hunk headers excluded."""
    lines: list[str] = []
    in_hunk = False
    for raw in section.splitlines():
        if raw.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            lines.append(raw[1:])
    return lines


def _test_command_text(sections: Sequence[tuple[str, str]]) -> str:
    for path, section in sections:
        if Path(path).name == "mimo_test_command.sh":
            return "\n".join(_added_lines(section))
    return ""


def _runner_of(command: str) -> str:
    if not command.strip():
        return "none"
    if _BUNDLED_ARCHIVE in command and _BUNDLED_RUNNER in command:
        return "bundled"
    if _DJANGO_CMD.search(command):
        return "django"
    if _PYTEST_CMD.search(command):
        return "pytest"
    if _UNITTEST_CMD.search(command):
        return "unittest"
    return "other"


def _command_targets(command: str, runner: str) -> list[str]:
    """Test paths or dotted modules the harness command names."""
    if runner == "pytest":
        targets = _PYTEST_TARGET.findall(command) + _PYTEST_NODE.findall(command)
    elif runner == "unittest":
        targets = [module.replace(".", "/") + ".py" for module in _UNITTEST_TARGET.findall(command)]
    else:
        targets = _PYTEST_TARGET.findall(command)
    seen: list[str] = []
    for target in targets:
        if target not in seen:
            seen.append(target)
    return seen


def _classify_target(target: str, patched: set[str]) -> str:
    """``patched`` when the command's target is a patched file, else
    ``pre_existing`` for a plausible repo path, else ``unresolved``."""
    normalized = target.lstrip("./")
    if normalized in patched or any(path.endswith("/" + normalized) for path in patched):
        return "patched"
    if re.fullmatch(r"[\w./-]+\.py", normalized):
        return "pre_existing"
    return "unresolved"


def _section_parses(section: str) -> bool:
    """True unless this git diff section is not a diff git would emit.

    An empty new file, a binary file, a mode-only change and a content-free
    rename all parse. A section fails only on a malformed hunk header or on
    body text that sits outside a hunk.
    """
    lines = section.splitlines()
    if not lines or not lines[0].startswith("diff --git ") or " b/" not in lines[0]:
        return False
    in_hunk = False
    in_binary = False
    for line in lines[1:]:
        if in_binary:
            continue
        if line.startswith("GIT binary patch") or line.startswith("Binary files "):
            in_binary = True
            in_hunk = False
            continue
        if line.startswith("@@"):
            if _HUNK_HEADER.match(line) is None:
                return False
            in_hunk = True
            continue
        if in_hunk:
            if line[:1] in {"+", "-", " ", "\\"} or line == "":
                continue
            return False
        if line == "" or _GIT_HEADER.match(line):
            continue
        return False
    return True


def _py_literals(text: str) -> list[str]:
    return [match.group("path") for match in _PY_STRING.finditer(text)]


def _name_token(text: str) -> str | None:
    token = text.strip()
    return token if re.fullmatch(r"[A-Za-z_]\w*", token) else None


def _bound_project_path(expr: str, path_names: Mapping[str, str]) -> str | None:
    """A ``.py`` path literal, or ``Path`` of one. Joins are generated files."""
    stripped = expr.strip()
    if " / " in stripped:
        return None
    quoted = re.fullmatch(r"""(['\"])([^'\"]+\.py)\1""", stripped)
    if quoted:
        return quoted.group(2)
    constructed = re.fullmatch(r"Path\s*\(\s*([^)]+)\s*\)", stripped)
    if constructed is None:
        return None
    arg = constructed.group(1).strip()
    quoted = re.fullmatch(r"""(['\"])([^'\"]+\.py)\1""", arg)
    if quoted:
        return quoted.group(2)
    name = _name_token(arg)
    if name and name in path_names:
        return path_names[name]
    return None


def _is_patched_path(path: str, patched: set[str]) -> bool:
    normalized = path.lstrip("./")
    return normalized in patched or any(
        item == normalized or item.endswith("/" + normalized) or normalized.endswith("/" + item)
        for item in patched
    )


def _project_py(
    token: str,
    path_names: Mapping[str, str],
    written_literals: set[str],
    written_names: set[str],
    patched: set[str],
) -> bool:
    """True when ``token`` is a ``.py`` path the test does not itself write."""
    name = _name_token(token)
    if name is not None:
        if name in written_names:
            return False
        literal = path_names.get(name)
        if not literal:
            return False
        return literal not in written_literals and not _is_patched_path(literal, patched)
    literals = _py_literals(token)
    if not literals and token.strip().strip("'\"").endswith(".py"):
        literals = [token.strip().strip("'\"")]
    return bool(literals) and all(
        literal not in written_literals and not _is_patched_path(literal, patched)
        for literal in literals
    )


def _write_open_arg(line: str) -> str | None:
    match = re.search(r"""\bopen\s*\(\s*([^,)\n]+)\s*,\s*(['\"])([^'\"]*)\2""", line)
    if match is None or not any(flag in match.group(3) for flag in ("w", "a", "x", "+")):
        return None
    return match.group(1).strip()


def _acquires_project_source(
    expr: str,
    path_names: Mapping[str, str],
    written_literals: set[str],
    written_names: set[str],
    patched: set[str],
) -> bool:
    if _GETSOURCE.search(expr):
        return True
    opened = re.search(r"\bopen\s*\(\s*([^,)\n]+)", expr)
    if opened and re.search(r"\bopen\s*\([^)\n]*\)\s*\.read\s*\(", expr):
        return _project_py(
            opened.group(1).strip(), path_names, written_literals, written_names, patched
        )
    if ".read_text" in expr:
        constructed = re.search(r"\bPath\s*\(\s*([^)\n]+)\s*\)", expr)
        if constructed and _project_py(
            constructed.group(1).strip(), path_names, written_literals, written_names, patched
        ):
            return True
        bound = re.search(r"\b([A-Za-z_]\w*)\s*\.read_text\s*\(", expr)
        if bound and _project_py(
            bound.group(1), path_names, written_literals, written_names, patched
        ):
            return True
    return False


def _asserts_on_source(
    line: str,
    source_names: Mapping[str, str],
    path_names: Mapping[str, str],
    written_literals: set[str],
    written_names: set[str],
    patched: set[str],
) -> str | None:
    """The source variable an assertion checks a substring of, or ``inline``."""
    if not re.search(r"\bassert(?:In|NotIn)?\b", line):
        return None
    if _GETSOURCE.search(line) and re.search(r"\bin\b|assertIn\b|assertNotIn\b", line):
        return "inline"
    if re.search(r"\bin\b", line) and _acquires_project_source(
        line, path_names, written_literals, written_names, patched
    ):
        return "inline"
    for name in source_names:
        if re.search(rf"\bin\s+{re.escape(name)}\b", line):
            return name
        if re.search(rf"assert(?:In|NotIn)\s*\([^,\n]*,\s*{re.escape(name)}\b", line):
            return name
        if re.search(rf"\b{re.escape(name)}\s*\.(?:find|index|count)\s*\(", line):
            return name
    return None


def _literal_source_asserts(sections: Sequence[tuple[str, str]]) -> list[str]:
    """Asserts on a substring of project source (the 1634 shape).

    A hit obtains that text with ``inspect.getsource``, ``open(<path>.py).read()``
    or ``Path(<...>.py).read_text()`` and asserts a substring of it. The path
    must be a literal project path, not a file the test writes, not a path join
    (a generated ``Path(tmp) / 'out.py'``), and not a file the hidden patch adds.
    """
    evidence: list[str] = []
    patched = {path for path, _section in sections}
    for path, section in sections:
        if Path(path).name in HARNESS_FILES or not str(path).endswith(".py"):
            continue
        added = _added_lines(section)
        path_names: dict[str, str] = {}
        written_literals: set[str] = set()
        written_names: set[str] = set()
        for line in added:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if re.search(r"\.(?:write_text|write_bytes)\s*\(|\bwritestr\s*\(", stripped):
                written_literals.update(_py_literals(stripped))
            for match in re.finditer(r"\b([A-Za-z_]\w*)\s*\.write_(?:text|bytes)\s*\(", stripped):
                written_names.add(match.group(1))
            write_open = _write_open_arg(stripped)
            if write_open is not None:
                name = _name_token(write_open)
                if name:
                    written_names.add(name)
                else:
                    written_literals.update(_py_literals(write_open) or [write_open.strip("'\"")])
            assigned = _ASSIGN.match(stripped)
            if assigned:
                bound = _bound_project_path(assigned.group("expr"), path_names)
                if bound:
                    path_names[assigned.group("name")] = bound
        for name, literal in path_names.items():
            if literal in written_literals or _is_patched_path(literal, patched):
                written_names.add(name)
        source_names: dict[str, str] = {}
        handle_names: set[str] = set()
        for line in added:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            assigned = _ASSIGN.match(stripped)
            expr = assigned.group("expr") if assigned else ""
            if expr and _acquires_project_source(
                expr, path_names, written_literals, written_names, patched
            ):
                source_names[assigned.group("name")] = stripped
            opened = re.search(r"\bwith\s+open\s*\(\s*([^,)\n]+)", stripped)
            if opened and _project_py(
                opened.group(1).strip(), path_names, written_literals, written_names, patched
            ):
                as_name = re.search(r"\bas\s+([A-Za-z_]\w*)", stripped)
                if as_name:
                    handle_names.add(as_name.group(1))
            if assigned and handle_names:
                handle_read = re.search(r"\b([A-Za-z_]\w*)\s*\.read\s*\(", expr)
                if handle_read and handle_read.group(1) in handle_names:
                    source_names[assigned.group("name")] = stripped
        for lineno, line in enumerate(added, start=1):
            stripped = line.strip()
            used = _asserts_on_source(
                stripped, source_names, path_names, written_literals, written_names, patched
            )
            if used is None:
                continue
            note = ""
            if used != "inline" and source_names.get(used) not in (None, stripped):
                note = f" (source: {_trim(source_names[used])})"
            evidence.append(f"{path}:{lineno}: {_trim(stripped)}{note}")
    return evidence


def _imported_names(line: str) -> list[str]:
    body = line.split("#", 1)[0]
    if "(" in body:  # a parenthesised import continues on later lines
        body = body.split("(", 1)[0]
    return [name for name in _IMPORT_NAME.findall(body) if name != "as"]


def _top_level_imports(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("from "):
        module = stripped[len("from ") :].split(" import ", 1)[0].strip()
        top = module.split(".")[0]
        return [top] if top and not top.startswith(".") else []
    if not stripped.startswith("import "):
        return []
    found: list[str] = []
    for part in stripped[len("import ") :].split("#", 1)[0].split(","):
        name = part.strip().split()[0] if part.strip() else ""
        top = name.split(".")[0]
        if top and top != "as":
            found.append(top)
    return found


def _imported_modules(sections: Sequence[tuple[str, str]]) -> list[str]:
    modules: list[str] = []
    for path, section in sections:
        if Path(path).name in HARNESS_FILES or not str(path).endswith(".py"):
            continue
        for line in _added_lines(section):
            modules.extend(_top_level_imports(line))
    return modules


def _excluded_module(name: str) -> bool:
    return name in _EXCLUDED_IMPORTS or name in sys.stdlib_module_names


#: Patch path directories that name a test layout, not a project.
_GENERIC_DIRS = frozenset(
    {
        "tests",
        "test",
        "testing",
        "unittests",
        "unit_tests",
        "usercase-test-coderl",
        "src",
        "lib",
        "python",
        "t",
        "spec",
        "specs",
    }
)


def project_key_for(
    split_group: str | None,
    task_id: str | None,
    imported_modules: Sequence[str],
    patch_files: Sequence[str],
) -> tuple[str, str]:
    """``(project_key, source)`` for one pool task.

    ``split_group`` names a repo when, after a ``code:`` prefix is stripped,
    it is not a ``format-code-task-`` singleton. Otherwise the most common
    top-level module the hidden tests import (stdlib and the usual test
    libraries excluded; ties keep the earliest), else the top directory of a
    patched ``.py`` path unless it is a generic layout directory (``tests/``,
    ``src/``; a subdirectory under those is too often ``core`` or ``unit`` to
    name a project), else the task id: an unresolved singleton.
    """
    group = str(split_group or "")
    rest = group.split(":", 1)[1] if group.startswith("code:") else group
    if rest and not rest.startswith("format-code-task-"):
        return rest, "split_group"
    candidates = [name for name in imported_modules if name and not _excluded_module(name)]
    if candidates:
        counts = Counter(candidates)
        best = min(counts, key=lambda name: (-counts[name], candidates.index(name)))
        return best, "test_import"
    for path in patch_files:
        parts = Path(str(path)).parts
        if str(path).endswith(".py") and len(parts) > 1 and parts[0] not in _GENERIC_DIRS:
            return parts[0], "test_path"
    return str(task_id or "unknown"), "task_id"


def _undisclosed_names(sections: Sequence[tuple[str, str]], instruction: str) -> list[str]:
    """Names hidden tests import from the project, absent from the instruction."""
    names: list[str] = []
    for path, section in sections:
        if Path(path).name in HARNESS_FILES or not path.endswith(".py"):
            continue
        project = Path(path).parts[0]
        for line in _added_lines(section):
            match = _FROM_IMPORT.match(line.strip())
            if match is None or match.group(1).split(".")[0] != project:
                continue
            for name in _imported_names(match.group(2)):
                if name not in names and not _word_in(instruction, name):
                    names.append(name)
    return names


def blocklist_hosts(text: str) -> set[str]:
    """Hostnames from ``0.0.0.0 host`` lines. Comments and blanks are ignored."""
    hosts: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _HOST_LINE.match(stripped)
        if match:
            hosts.add(match.group("host").lower().rstrip("."))
    return hosts


def blocklist_facts(task_dir: Path) -> dict[str, bool | None]:
    """Whether the task's hosts file blocks git and PyPI.

    Null when ``environment/setup/files/blocklist`` is absent. Git is blocked
    only when every leak host is listed. PyPI is blocked when either package
    host is listed.
    """
    path = task_dir / "environment" / "setup" / "files" / "blocklist"
    if not path.is_file():
        return {"leak_git_blocked": None, "leak_pypi_blocked": None}
    try:
        hosts = blocklist_hosts(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {"leak_git_blocked": None, "leak_pypi_blocked": None}
    return {
        "leak_git_blocked": all(host in hosts for host in _GIT_LEAK_HOSTS),
        "leak_pypi_blocked": any(host in hosts for host in _PYPI_LEAK_HOSTS),
    }


def hosts_bypassable(task_dir: Path) -> bool:
    """True when the agent can rewrite ``/etc/hosts``.

    ``task.toml`` with no ``[agent]`` user, or ``user = "root"``, runs the
    agent as root (the same default ``task_lint`` uses). A root agent can
    rewrite ``/etc/hosts`` and bypass the answer-leak blocklist.
    """
    try:
        payload = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return True
    agent = payload.get("agent")
    user = agent.get("user") if isinstance(agent, Mapping) else None
    return not isinstance(user, str) or not user.strip() or user.strip() == "root"


def _pypi_text(entry: Mapping[str, Any] | None, key: str) -> str | None:
    if not isinstance(entry, Mapping):
        return None
    value = entry.get(key)
    return value if isinstance(value, str) and value else None


def leak_assessment(
    git_blocked: bool | None,
    pypi_blocked: bool | None,
    pypi_entry: Mapping[str, Any] | None,
    hosts_bypassable: bool | None = None,
) -> dict[str, Any]:
    """Download channel for the fix. Does not affect the health label.

    Precedence: ``pypi_fix_released`` (a matched issue closed, a release was
    uploaded after that, and PyPI is not blocked), then ``pypi_package`` (a
    project matched with a release and PyPI is open, but no dated upstream
    fix was found), then ``git_only`` (a repo is known and no release followed
    the fix; the note says whether git hosts are blocked and, if the agent is
    root, that it can rewrite ``/etc/hosts``), then ``none_found``. ``unknown``
    is a missing blocklist or a missing pypi.json entry.
    """
    issue = pypi_entry.get("issue") if isinstance(pypi_entry, Mapping) else None
    issue = issue if isinstance(issue, Mapping) else None
    released_after = (
        pypi_entry.get("released_after_close") if isinstance(pypi_entry, Mapping) else None
    )
    project = _pypi_text(pypi_entry, "pypi_project")
    match = _pypi_text(pypi_entry, "match")
    releases = pypi_entry.get("releases") if isinstance(pypi_entry, Mapping) else None
    n_releases = releases if isinstance(releases, int) and not isinstance(releases, bool) else 0
    matched = bool(project) and match in ("repo_url", "name")
    dated_fix = issue is not None and bool(_pypi_text(issue, "closed_at"))
    repo_known = bool(_pypi_text(pypi_entry, "repo_url")) or _pypi_text(
        pypi_entry, "repo_source"
    ) in ("split_group", "pypi_urls")
    columns = {
        "leak_git_blocked": git_blocked,
        "leak_pypi_blocked": pypi_blocked,
        "leak_pypi_project": project,
        "leak_pypi_match": match,
        "leak_pypi_latest": _pypi_text(pypi_entry, "latest_version"),
        "leak_pypi_last_upload": _pypi_text(pypi_entry, "last_upload"),
        "leak_issue_url": _pypi_text(issue, "url"),
        "leak_issue_closed_at": _pypi_text(issue, "closed_at"),
        "leak_first_release_after_close": _pypi_text(pypi_entry, "first_release_after_close"),
        "leak_hosts_bypassable": hosts_bypassable,
    }
    if git_blocked is None or pypi_entry is None:
        note = "no blocklist file" if git_blocked is None else "no pypi.json entry"
        return {**columns, "leak_channel": "unknown", "leak_note": note}
    pypi_open = pypi_blocked is not True
    if released_after is True and pypi_open and issue is not None:
        channel = "pypi_fix_released"
    elif matched and n_releases >= 1 and pypi_open and not dated_fix:
        channel = "pypi_package"
    elif repo_known:
        channel = "git_only"
    else:
        channel = "none_found"
    if matched:
        latest = columns["leak_pypi_latest"] or "?"
        uploaded = columns["leak_pypi_last_upload"] or "?"
        detail = f"pypi {project} ({match} match), latest {latest}, {uploaded}"
    else:
        detail = "no pypi project matched"
    repo = _pypi_text(pypi_entry, "repo_url")
    if repo:
        detail += f"; repo {repo}"
    if channel == "pypi_fix_released":
        detail += (
            f"; release {columns['leak_first_release_after_close'] or '?'} "
            f"after issue close {columns['leak_issue_closed_at'] or '?'}"
        )
    elif dated_fix:
        detail += "; no release after issue close"
    if git_blocked:
        detail += "; git hosts blocked"
        if channel == "git_only":
            if hosts_bypassable:
                detail += (
                    "; a root agent can rewrite /etc/hosts and bypass the answer-leak blocklist"
                )
            else:
                detail += "; agent user is not root, so the hosts blocklist holds"
    else:
        detail += "; git hosts open"
    return {**columns, "leak_channel": channel, "leak_note": detail}


def load_pypi_index(path: Path) -> dict[str, dict[str, Any]]:
    """Task-id keyed PyPI metadata. The module does not fetch it."""
    payload = json.loads(path.read_text())
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, dict):
        raise ValueError(f"{path} has no tasks map")
    return {str(key): value for key, value in tasks.items() if isinstance(value, dict)}


def static_checks(task_dir: Path) -> dict[str, Any]:
    """Static health facts read from one task directory.

    Keys: ``patch_files`` (non-harness files the hidden patch adds),
    ``patch_well_formed`` (false only when a section fails to parse; an empty
    new file, a binary file and a mode-only section parse), ``test_runner``
    (``pytest`` | ``unittest`` | ``bundled`` | ``django`` | ``other`` |
    ``none``, from ``mimo_test_command.sh``), ``test_targets_in_patch``,
    ``literal_source_asserts`` (asserts on a substring of project source),
    ``imported_modules`` (top-level modules the hidden tests import, in patch
    order), ``undisclosed_names``, ``instruction_chars``.
    """
    patch_path = task_dir / "tests" / "test.patch"
    try:
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        patch_text = ""
    try:
        instruction = (task_dir / "instruction.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        instruction = ""
    sections = _patch_sections(patch_text)
    patch_files = [path for path, _section in sections if Path(path).name not in HARNESS_FILES]
    well_formed = bool(sections) and all(_section_parses(section) for _path, section in sections)
    command = _test_command_text(sections)
    runner = _runner_of(command)
    targets = [
        {"target": target, "where": _classify_target(target, set(patch_files))}
        for target in _command_targets(command, runner)
    ]
    return {
        "patch_files": patch_files,
        "patch_well_formed": well_formed,
        "test_runner": runner,
        "test_targets_in_patch": targets,
        "literal_source_asserts": _literal_source_asserts(sections),
        "imported_modules": _imported_modules(sections),
        "undisclosed_names": _undisclosed_names(sections, instruction),
        "instruction_chars": len(instruction),
        **blocklist_facts(task_dir),
        "leak_hosts_bypassable": hosts_bypassable(task_dir),
    }


# --- nop evidence ----------------------------------------------------------


def _read_result(trial_dir: Path) -> dict[str, Any]:
    try:
        payload = json.loads((trial_dir / "result.json").read_text())
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _reward_of(result: Mapping[str, Any], trial_dir: Path) -> float | None:
    verifier = result.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    raw = rewards.get("reward") if isinstance(rewards, dict) else None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return _trial_reward_file(trial_dir)


def _stdout_text(trial_dir: Path) -> str:
    try:
        return (trial_dir / "verifier" / "test-stdout.txt").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return ""


def _tests_ran(stdout: str) -> bool:
    if _COLLECTION_ERROR.search(stdout):
        collected = _COLLECTED.search(stdout)
        if collected is None or collected.group("n") == "0":
            return False
    if _PYTEST_RAN.search(stdout):
        return True
    ran = _UNITTEST_RAN.search(stdout)
    return ran is not None and int(ran.group("n")) > 0


def nop_evidence(trial_dir: Path, instruction: str) -> dict[str, Any]:
    """What one nop trial shows about the task's environment and grader.

    Keys: ``reward``, ``tests_applied`` (false when the stdout says the
    hidden tests could not be applied, or no reward file exists),
    ``tests_ran`` (pytest collected or reported results, unittest ran more
    than zero tests, or a collection error), ``setup_error`` (first matching
    line, trimmed to 200 characters) and ``setup_error_excused``,
    ``exception_type``.
    """
    result = _read_result(trial_dir)
    stdout = _stdout_text(trial_dir)
    reward = _reward_of(result, trial_dir)
    exception = result.get("exception_info")
    exception_type = (
        exception.get("exception_type")
        if isinstance(exception, dict) and isinstance(exception.get("exception_type"), str)
        else None
    )
    not_applied = _NOT_APPLIED.search(stdout) is not None
    reward_file = (trial_dir / "verifier" / "reward.txt").is_file() or (
        trial_dir / "verifier" / "reward.json"
    ).is_file()
    matched = next((line for line in stdout.splitlines() if SETUP_ERROR.search(line)), None)
    texts = [stdout] if stdout else []
    return {
        "reward": reward,
        "tests_applied": not not_applied and (reward_file or reward is not None),
        "tests_ran": _tests_ran(stdout),
        "setup_error": _trim(matched) if matched else None,
        "setup_error_excused": bool(matched) and setup_error_excused(texts, instruction),
        "exception_type": exception_type,
    }


# --- labeling --------------------------------------------------------------


def _environment_exception(exception_type: str | None) -> bool:
    if exception_type is None:
        return False
    phase = exception_phase_for(exception_type)
    return phase in ("environment", "unknown")


def label_task(
    static: Mapping[str, Any], nop: Mapping[str, Any] | None
) -> tuple[str, list[str], str]:
    """``(label, reasons, evidence)`` for one task.

    ``unknown`` without a nop. ``broken_environment`` when the trial
    exception is an environment-setup failure, the hidden tests were not
    applied, no reward exists, or a setup error is not excused — and its
    evidence is always a log excerpt. ``grader_suspect`` when the nop scores
    1 (the hidden tests pass with no change), the tests never ran and no
    setup error explains it, or the tests assert on source text. Otherwise
    ``sound``.
    """
    if nop is None:
        return "unknown", ["no_nop"], "no nop trial for this task version"
    reasons: list[str] = []
    evidence: list[str] = []
    exception_type = nop.get("exception_type")
    if _environment_exception(exception_type if isinstance(exception_type, str) else None):
        reasons.append("environment_exception")
        evidence.append(f"trial exception: {exception_type}")
    if nop.get("tests_applied") is False:
        reasons.append("tests_not_applied")
        evidence.append("the hidden tests could not be applied")
    if nop.get("reward") is None:
        reasons.append("reward_missing")
        evidence.append("no reward")
    setup_error = nop.get("setup_error")
    if isinstance(setup_error, str) and not nop.get("setup_error_excused"):
        reasons.append("setup_error")
        evidence.append(setup_error)
    if reasons:
        return "broken_environment", reasons, evidence[0]

    reward = nop.get("reward")
    if isinstance(reward, (int, float)) and not isinstance(reward, bool) and float(reward) == 1.0:
        return "grader_suspect", ["nop_passes"], "nop reward 1: hidden tests pass with no change"
    if not nop.get("tests_ran") and not setup_error:
        return (
            "grader_suspect",
            ["tests_did_not_run"],
            "verifier output shows no collected or executed tests",
        )
    asserts = [str(item) for item in static.get("literal_source_asserts") or []]
    if asserts:
        return "grader_suspect", ["literal_source_assert"], asserts[0]
    detail = "nop graded"
    if isinstance(reward, (int, float)) and not isinstance(reward, bool):
        detail += f" with reward {float(reward):g}"
    if nop.get("setup_error_excused"):
        detail += "; setup error excused as the agent's missing work"
    return "sound", [], detail


# --- table -----------------------------------------------------------------


def task_health_schema() -> Any:
    import pyarrow as pa

    return pa.schema(
        [
            ("task_id", pa.string()),
            ("task_version_digest", pa.string()),
            ("split", pa.string()),
            ("split_group", pa.string()),
            ("category", pa.string()),
            ("image_mib", pa.int64()),
            ("project_key", pa.string()),
            ("project_key_source", pa.string()),
            ("label", pa.string()),
            ("reasons", pa.list_(pa.string())),
            ("evidence", pa.string()),
            ("patch_files", pa.list_(pa.string())),
            ("patch_well_formed", pa.bool_()),
            ("test_runner", pa.string()),
            ("test_targets_in_patch", pa.string()),
            ("literal_source_asserts", pa.list_(pa.string())),
            ("undisclosed_names", pa.list_(pa.string())),
            ("instruction_chars", pa.int64()),
            ("nop_job_name", pa.string()),
            ("nop_trial_name", pa.string()),
            ("nop_reward", pa.float64()),
            ("nop_finished_at", pa.string()),
            ("nop_cost_usd", pa.float64()),
            ("nop_tests_applied", pa.bool_()),
            ("nop_tests_ran", pa.bool_()),
            ("nop_setup_error", pa.string()),
            ("nop_setup_error_excused", pa.bool_()),
            ("nop_exception_type", pa.string()),
            ("leak_git_blocked", pa.bool_()),
            ("leak_pypi_blocked", pa.bool_()),
            ("leak_pypi_project", pa.string()),
            ("leak_pypi_match", pa.string()),
            ("leak_pypi_latest", pa.string()),
            ("leak_pypi_last_upload", pa.string()),
            ("leak_issue_url", pa.string()),
            ("leak_issue_closed_at", pa.string()),
            ("leak_first_release_after_close", pa.string()),
            ("leak_hosts_bypassable", pa.bool_()),
            ("leak_channel", pa.string()),
            ("leak_note", pa.string()),
            ("produced_at", pa.string()),
        ]
    )


def write_task_health_parquet(rows: Sequence[Mapping[str, Any]], path: Path) -> Path:
    """Write census rows to ``task_health.parquet`` (contract schema)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = task_health_schema()
    columns = {field.name: [row.get(field.name) for row in rows] for field in schema}
    table = pa.table(columns, schema=schema)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out)
    return out


def read_task_health_parquet(path: Path) -> list[dict[str, Any]]:
    """Read back ``task_health.parquet`` rows."""
    import pyarrow.parquet as pq

    rows = pq.read_table(path).to_pylist()
    for row in rows:
        raw = row.get("test_targets_in_patch")
        if isinstance(raw, str) and raw:
            row["test_targets_in_patch"] = json.loads(raw)
    return rows


def _instruction_of(task_dir: Path) -> str:
    try:
        return (task_dir / "instruction.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _nop_rows_by_digest(
    qualification_rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in qualification_rows:
        if row.get("agent_name") != "nop":
            continue
        digest = row.get("task_version_digest")
        if isinstance(digest, str):
            grouped.setdefault(digest, []).append(dict(row))
    return grouped


def _find_trial(job_roots: Sequence[Path], job_name: str, trial_name: str) -> Path | None:
    for root in job_roots:
        trial = root / job_name / trial_name
        if trial.is_dir():
            return trial
    return None


def build_health_rows(
    pool_entries: Sequence[Mapping[str, Any]],
    qualification_rows: Sequence[Mapping[str, Any]],
    job_roots: Sequence[Path],
    task_root: Path,
    pypi_tasks: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One health row per pool entry, using the latest nop per digest.

    ``task_root`` resolves each entry's checkout-relative ``task`` directory.
    A pool entry whose nop trial directory is absent from every job root is
    labeled ``unknown``: the qualification row alone carries no verifier log,
    and a broken label must cite one. ``pypi_tasks`` is the ``tasks`` map from
    ``pypi.json``; a missing entry leaves the leak channel unknown. The leak
    columns do not change ``label``.
    """
    nops = _nop_rows_by_digest(qualification_rows)
    indexed = pypi_tasks or {}
    produced_at = utc_now_iso()
    rows: list[dict[str, Any]] = []
    for entry in pool_entries:
        digest = str(entry.get("task_version_digest") or "")
        task_dir = task_root / str(entry.get("task") or "")
        static = static_checks(task_dir)
        candidates = nops.get(digest, [])
        latest = max(candidates, key=_latest_key) if candidates else None
        trial = (
            _find_trial(job_roots, str(latest.get("job_name")), str(latest.get("trial_name")))
            if latest is not None
            else None
        )
        nop = nop_evidence(trial, _instruction_of(task_dir)) if trial is not None else None
        label, reasons, evidence = label_task(static, nop)
        cost = latest.get("est_cost_usd") if latest is not None else None
        image_mib = entry.get("image_mib")
        project_key, project_key_source = project_key_for(
            entry.get("split_group") if isinstance(entry.get("split_group"), str) else None,
            entry.get("task_id") if isinstance(entry.get("task_id"), str) else None,
            static.get("imported_modules") or [],
            static.get("patch_files") or [],
        )
        rows.append(
            {
                "task_id": entry.get("task_id"),
                "task_version_digest": digest or None,
                "split": entry.get("split"),
                "split_group": entry.get("split_group"),
                "category": entry.get("category"),
                "image_mib": image_mib
                if isinstance(image_mib, int) and not isinstance(image_mib, bool)
                else None,
                "project_key": project_key,
                "project_key_source": project_key_source,
                "label": label,
                "reasons": reasons,
                "evidence": evidence,
                "patch_files": static["patch_files"],
                "patch_well_formed": static["patch_well_formed"],
                "test_runner": static["test_runner"],
                "test_targets_in_patch": json.dumps(static["test_targets_in_patch"]),
                "literal_source_asserts": static["literal_source_asserts"],
                "undisclosed_names": static["undisclosed_names"],
                "instruction_chars": static["instruction_chars"],
                "nop_job_name": latest.get("job_name") if latest is not None else None,
                "nop_trial_name": latest.get("trial_name") if latest is not None else None,
                "nop_reward": nop.get("reward") if nop is not None else None,
                "nop_finished_at": latest.get("finished_at") if latest is not None else None,
                "nop_cost_usd": float(cost)
                if isinstance(cost, (int, float)) and not isinstance(cost, bool)
                else None,
                "nop_tests_applied": nop.get("tests_applied") if nop is not None else None,
                "nop_tests_ran": nop.get("tests_ran") if nop is not None else None,
                "nop_setup_error": nop.get("setup_error") if nop is not None else None,
                "nop_setup_error_excused": nop.get("setup_error_excused")
                if nop is not None
                else None,
                "nop_exception_type": nop.get("exception_type") if nop is not None else None,
                **leak_assessment(
                    static.get("leak_git_blocked"),
                    static.get("leak_pypi_blocked"),
                    indexed.get(str(entry.get("task_id") or ""))
                    if str(entry.get("task_id") or "") in indexed
                    else None,
                    static.get("leak_hosts_bypassable"),
                ),
                "produced_at": produced_at,
            }
        )
    return rows


def summarize_health(
    rows: Sequence[Mapping[str, Any]], *, table_path: Path | str | None = None
) -> str:
    """Markdown census summary. Every count is a group-by of these rows."""
    named = str(table_path) if table_path else "(not written)"
    by_label: Counter[str] = Counter(str(row.get("label") or "unknown") for row in rows)
    by_split: dict[str, Counter[str]] = {}
    by_project: dict[str, Counter[str]] = {}
    reasons: Counter[str] = Counter()
    total_cost = 0.0
    for row in rows:
        label = str(row.get("label") or "unknown")
        by_split.setdefault(str(row.get("split") or "unassigned"), Counter())[label] += 1
        by_project.setdefault(str(row.get("project_key") or ""), Counter())[label] += 1
        for reason in row.get("reasons") or []:
            reasons[str(reason)] += 1
        cost = row.get("nop_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            total_cost += float(cost)

    lines = [
        f"table: {named}",
        f"rows: {len(rows)}",
        "",
        "# Task health census",
        "",
        "## By label",
        "",
        "| label | tasks |",
        "|---|---:|",
    ]
    for label in (*LABELS, *(sorted(set(by_label) - set(LABELS)))):
        lines.append(f"| {label} | {by_label.get(label, 0)} |")

    lines.extend(
        [
            "",
            "## By split",
            "",
            "| split | " + " | ".join(LABELS) + " |",
            "|---|" + "---:|" * len(LABELS),
        ]
    )
    for name in sorted(by_split):
        counts = by_split[name]
        lines.append(
            "| " + name + " | " + " | ".join(str(counts.get(label, 0)) for label in LABELS) + " |"
        )

    projects = sorted(by_project, key=lambda key: (-sum(by_project[key].values()), key))
    singletons = sum(1 for key in projects if sum(by_project[key].values()) == 1)
    lines.extend(
        [
            "",
            "## By project",
            "",
            f"projects: {len(projects)}",
            f"singleton projects: {singletons}",
            "",
            "Top 25 by task count (group by project_key; ties broken by project_key):",
            "",
            "| project_key | tasks | " + " | ".join(LABELS) + " |",
            "|---|---:|" + "---:|" * len(LABELS),
        ]
    )
    for key in projects[:25]:
        counts = by_project[key]
        lines.append(
            "| "
            + key
            + " | "
            + str(sum(counts.values()))
            + " | "
            + " | ".join(str(counts.get(label, 0)) for label in LABELS)
            + " |"
        )

    broken = sorted(
        (
            (key, by_project[key].get("broken_environment", 0))
            for key in projects
            if by_project[key].get("broken_environment", 0) >= 2
        ),
        key=lambda item: (-item[1], item[0]),
    )
    lines.extend(
        [
            "",
            "## Projects with 2 or more broken tasks",
            "",
            f"projects with >=2 broken tasks: {len(broken)}",
            "",
            "| project_key | broken_environment |",
            "|---|---:|",
        ]
    )
    for key, count in broken:
        lines.append(f"| {key} | {count} |")
    if not broken:
        lines.append("| (none) | 0 |")

    lines.extend(
        [
            "",
            "## Top reasons",
            "",
            "One count per task per entry of the reasons list.",
            "",
            "| reason | tasks |",
            "|---|---:|",
        ]
    )
    for reason, count in reasons.most_common():
        lines.append(f"| {reason} | {count} |")
    if not reasons:
        lines.append("| (none) | 0 |")
    by_channel = Counter(str(row.get("leak_channel") or "(none)") for row in rows)
    by_match = Counter(
        str(row.get("leak_pypi_match")) if row.get("leak_pypi_match") else "(none)" for row in rows
    )
    by_channel_label: dict[str, Counter[str]] = {}
    for row in rows:
        channel = str(row.get("leak_channel") or "(none)")
        label = str(row.get("label") or "unknown")
        by_channel_label.setdefault(channel, Counter())[label] += 1
    channel_order = (*LEAK_CHANNELS, *(sorted(set(by_channel) - set(LEAK_CHANNELS))))
    lines.extend(
        ["", "## Leak", "", "Group by leak_channel.", "", "| leak_channel | tasks |", "|---|---:|"]
    )
    for channel in channel_order:
        lines.append(f"| {channel} | {by_channel.get(channel, 0)} |")
    lines.extend(
        [
            "",
            "Cross-tab of leak_channel and label.",
            "",
            "| leak_channel | " + " | ".join(LABELS) + " |",
            "|---|" + "---:|" * len(LABELS),
        ]
    )
    for channel in channel_order:
        counts = by_channel_label.get(channel, Counter())
        lines.append(
            "| "
            + channel
            + " | "
            + " | ".join(str(counts.get(label, 0)) for label in LABELS)
            + " |"
        )
    lines.extend(
        [
            "",
            "Group by leak_pypi_match. Null is (none).",
            "",
            "| leak_pypi_match | tasks |",
            "|---|---:|",
        ]
    )
    for match in sorted(by_match, key=lambda key: (key == "(none)", key)):
        lines.append(f"| {match} | {by_match[match]} |")
    lines.extend(
        [
            "",
            "## Nop cost",
            "",
            "Sum of nop_cost_usd; null counts as 0.",
            "",
            f"Total nop cost: ${total_cost:.4f}.",
            "",
        ]
    )
    return "\n".join(lines)


def load_pool(path: Path) -> list[dict[str, Any]]:
    """Pool entries from a ``pool.json`` (a bare list or ``{"pool": [...]}``)."""
    payload = json.loads(path.read_text())
    entries = payload.get("pool") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ValueError(f"{path} has no pool list")
    return [entry for entry in entries if isinstance(entry, dict)]


def catalog_qualification_rows(repo_root: Path) -> list[dict[str, Any]]:
    """Rows of the shared catalog's ``task_qualification.parquet``."""
    from evallab.storage.paths import derived_root_from_environment
    from evallab.task_qualification import read_task_qualification_parquet

    path = (
        derived_root_from_environment(repo_root)
        / "external/task_catalog"
        / "task_qualification.parquet"
    )
    if not path.is_file():
        return []
    return read_task_qualification_parquet(path)
