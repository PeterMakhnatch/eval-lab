"""Line coverage of the audited files during a task's own verifier run.

``evallab hack mutate`` edits files the reference solution leaves behind. A
mutant on a line the hidden tests never execute keeps its reward by
construction, so its survival says nothing about the grader. This module
instruments one extra reference run so the verifier's Python processes record
which lines of the audited files actually run:

* :func:`instrument_package` copies the task package and puts a prologue at
  the top of ``tests/test.sh``. The prologue writes a small tracer to
  :data:`COVERAGE_HOME` (outside every task path) and a ``.pth`` hook into the
  site-packages of each Python 3.8+ interpreter it finds, so every Python
  process the verifier starts loads the tracer. It works the same in shared
  and separate verifier modes because ``tests/test.sh`` is what the verifier
  container runs.
* The tracer (``sys.monitoring`` on 3.12+, ``sys.settrace`` before) records
  executed lines of files whose path or content matches a target, keyed by
  the SHA-256 of the bytes the process saw, and writes one JSON record per
  process at exit to :data:`COVERAGE_OUT`. It never writes to stdout or
  stderr and leaves the process untraced on any error.
* :func:`load_trace` and :func:`executed_lines` read those records on the
  host. Only records whose digest matches the captured reference bytes count,
  so a verifier that first runs the tests on the original code (a fail-to-pass
  baseline) cannot mark lines of the fixed file as executed.

Line semantics (:class:`LineMap`): a line counts as executed when any line of
its innermost statement ran; for compound statements (``if``, ``for``,
``def`` ...) only the header counts, so a multi-line expression is judged as
one statement and an ``if`` body is not credited for its header.
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_TRACE = "evallab.verifier_coverage.trace/v1"

#: Where the prologue writes the tracer and its target list inside the verifier.
COVERAGE_HOME = "/var/lib/envcheck-coverage"
#: Where each traced process writes its record; Harbor downloads ``/logs/verifier``.
COVERAGE_OUT = "/logs/verifier/envcheck-coverage"
_COVERAGE_HOST = ("verifier", "envcheck-coverage")
_PTH_NAME = "zz-envcheck-coverage.pth"
_PROLOGUE_BEGIN = "# >>> envcheck coverage (evallab hack mutate)"
_PROLOGUE_END = "# <<< envcheck coverage"

#: The tracer, written verbatim into the verifier. Python 3.8-compatible on
#: purpose: it is imported by every interpreter the prologue hooks.
_TRACER = r'''"""envcheck line tracer (evallab hack mutate): records which lines of the
audited files run in this process and writes one JSON record at exit.
Writes nothing to stdout or stderr; any error leaves the process untraced."""


def _envcheck_install():
    import atexit
    import hashlib
    import json
    import os
    import sys

    home = "__ENVCHECK_HOME__"
    out = "__ENVCHECK_OUT__"
    with open(os.path.join(home, "targets.json"), encoding="utf-8") as handle:
        config = json.load(handle)
    by_path = {}
    by_sha = {}
    sizes = set()
    for target in config["targets"]:
        by_path[os.path.realpath(target["path"])] = target["path"]
        by_sha.setdefault(target["sha256"], target["path"])
        sizes.add(target["size"])
    hits = {}
    keys = {}

    def wanted(filename):
        key = keys.get(filename)
        if key is not None:
            return key
        key = ""
        try:
            real = os.path.realpath(os.path.abspath(filename))
            size = os.stat(real).st_size
        except (OSError, TypeError, ValueError):
            real, size = "", -1
        target = by_path.get(real)
        if target is not None or size in sizes:
            try:
                with open(real, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
            except OSError:
                digest = ""
            if target is None:
                target = by_sha.get(digest)
            if target is not None and digest:
                key = target + "\0" + digest
        keys[filename] = key
        return key

    mode = None
    tracer = None
    monitoring = getattr(sys, "monitoring", None)
    if monitoring is not None and os.environ.get("ENVCHECK_COVERAGE_TRACER") != "settrace":
        for tool in (3, 4):
            try:
                monitoring.use_tool_id(tool, "envcheck-coverage")
            except ValueError:
                continue
            mode = tool
            break
    if mode is not None:
        tool = mode
        events = monitoring.events
        disable = monitoring.DISABLE

        def on_start(code, offset):
            if wanted(code.co_filename):
                monitoring.set_local_events(tool, code, events.LINE)
            return disable

        def on_line(code, line):
            key = wanted(code.co_filename)
            if key:
                hits.setdefault(key, set()).add(line)
            return disable

        monitoring.register_callback(tool, events.PY_START, on_start)
        monitoring.register_callback(tool, events.LINE, on_line)
        monitoring.set_events(tool, events.PY_START)
        mode = "sys.monitoring"
    else:
        import threading

        def tracer(frame, event, arg):
            key = wanted(frame.f_code.co_filename)
            if not key:
                return None
            lines = hits.setdefault(key, set())

            def local(frame, event, arg):
                if event == "line":
                    lines.add(frame.f_lineno)
                return local

            return local

        sys.settrace(tracer)
        threading.settrace(tracer)
        mode = "settrace"

    def dump():
        try:
            intact = mode == "sys.monitoring" or sys.gettrace() is tracer
            files = []
            for key, lines in hits.items():
                path, digest = key.split("\0", 1)
                files.append({"path": path, "sha256": digest, "lines": sorted(lines)})
            record = {
                "schema": "__ENVCHECK_SCHEMA__",
                "pid": os.getpid(),
                "argv": [str(part)[:200] for part in list(getattr(sys, "argv", []))[:6]],
                "python": sys.version.split()[0],
                "mode": mode,
                "intact": intact,
                "files": files,
            }
            os.makedirs(out, exist_ok=True)
            name = os.path.join(out, "%d-%s.json" % (os.getpid(), os.urandom(4).hex()))
            with open(name + ".tmp", "w", encoding="utf-8") as handle:
                json.dump(record, handle)
            os.replace(name + ".tmp", name)
        except Exception:
            pass

    atexit.register(dump)


try:
    _envcheck_install()
except Exception:
    pass
'''


def tracer_source(home: str = COVERAGE_HOME, out: str = COVERAGE_OUT) -> str:
    """The tracer module; ``home`` holds ``targets.json``, records land in ``out``."""
    return (
        _TRACER.replace("__ENVCHECK_HOME__", home)
        .replace("__ENVCHECK_OUT__", out)
        .replace("__ENVCHECK_SCHEMA__", SCHEMA_TRACE)
    )


def targets_document(files: dict[str, bytes]) -> str:
    """``targets.json``: each audited container path with the reference bytes' digest."""
    return json.dumps(
        {
            "targets": [
                {
                    "path": path,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
                for path, content in sorted(files.items())
            ]
        },
        indent=1,
        sort_keys=True,
    )


def coverage_prologue(files: dict[str, bytes]) -> str:
    """Shell that installs the tracer before the verifier runs; silent, never fails."""
    tracer = tracer_source()
    targets = targets_document(files)
    for tag in ("ENVCHECK_COV_EOF_TRACER", "ENVCHECK_COV_EOF_TARGETS"):
        if tag in tracer or tag in targets:
            raise ValueError(f"heredoc delimiter {tag} collides with the tracer or a target path")
    interpreters = " ".join(
        (
            "/usr/bin/python3*",
            "/usr/local/bin/python3*",
            "/opt/*/bin/python3*",
            "/opt/conda/envs/*/bin/python3*",
            "/root/.venv/bin/python3*",
            "/root/.local/bin/python3*",
            "/*/.venv/bin/python3*",
            "/*/*/.venv/bin/python3*",
        )
    )
    return (
        f"{_PROLOGUE_BEGIN}: record which lines of the audited files the tests run\n"
        "envcheck_coverage_install() {\n"
        f"  envcheck_home={COVERAGE_HOME}\n"
        '  mkdir -p "$envcheck_home" || return 0\n'
        "  cat > \"$envcheck_home/envcheck_coverage.py\" <<'ENVCHECK_COV_EOF_TRACER'\n"
        f"{tracer}"
        "ENVCHECK_COV_EOF_TRACER\n"
        "  cat > \"$envcheck_home/targets.json\" <<'ENVCHECK_COV_EOF_TARGETS'\n"
        f"{targets}\n"
        "ENVCHECK_COV_EOF_TARGETS\n"
        f"  for envcheck_py in {interpreters} $(command -v python python3 2>/dev/null); do\n"
        '    case "$envcheck_py" in *-config) continue ;; esac\n'
        '    [ -x "$envcheck_py" ] || continue\n'
        '    "$envcheck_py" -c \'import site, sys\n'
        "if sys.version_info >= (3, 8):\n"
        '    print("\\n".join(site.getsitepackages()))\' 2>/dev/null\n'
        "  done | sort -u | while IFS= read -r envcheck_site; do\n"
        '    [ -d "$envcheck_site" ] || continue\n'
        f"    printf '%s\\nimport envcheck_coverage\\n' \"$envcheck_home\" "
        f'> "$envcheck_site/{_PTH_NAME}"\n'
        "  done\n"
        "  return 0\n"
        "}\n"
        "envcheck_coverage_install >/dev/null 2>&1 || true\n"
        "unset -f envcheck_coverage_install 2>/dev/null || true\n"
        "unset envcheck_home envcheck_py envcheck_site 2>/dev/null || true\n"
        f"{_PROLOGUE_END}\n"
    )


def instrument_package(package: Path, destination: Path, files: dict[str, bytes]) -> Path:
    """Copy ``package`` to ``destination`` with the coverage prologue atop ``tests/test.sh``.

    ``files`` maps each audited container path to the reference bytes captured
    from it. Raises ``ValueError`` when the package has no ``tests/test.sh``.
    """
    test_sh = package / "tests" / "test.sh"
    if not test_sh.is_file():
        raise ValueError(f"{package}: no tests/test.sh to instrument")
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(package, destination, symlinks=True)
    script = test_sh.read_text(encoding="utf-8")
    shebang, _, body = script.partition("\n") if script.startswith("#!") else ("", "", script)
    prologue = coverage_prologue(files)
    instrumented = f"{shebang}\n{prologue}{body}" if shebang else prologue + body
    (destination / "tests" / "test.sh").write_text(instrumented, encoding="utf-8")
    return destination


# --------------------------------------------------------------------------- #
# Host side: records and line semantics
# --------------------------------------------------------------------------- #


@dataclass
class Trace:
    """Records from one instrumented trial: executed lines per (path, digest)."""

    processes: int = 0
    #: False when a process lost the tracer (another tool replaced ``sys.settrace``).
    intact: bool = True
    modes: set[str] = field(default_factory=set)
    lines: dict[tuple[str, str], set[int]] = field(default_factory=dict)


def trace_dir(trial: Path) -> Path:
    return trial.joinpath(*_COVERAGE_HOST)


def load_trace(trials: Iterable[Path]) -> Trace | None:
    """Merge every record under each trial's ``verifier/envcheck-coverage``; None if none."""
    trace = Trace()
    for trial in trials:
        folder = trace_dir(trial)
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("schema") != SCHEMA_TRACE:
                continue
            trace.processes += 1
            trace.intact = trace.intact and record.get("intact") is True
            if isinstance(record.get("mode"), str):
                trace.modes.add(record["mode"])
            for item in record.get("files") or []:
                if not isinstance(item, dict):
                    continue
                key = (str(item.get("path")), str(item.get("sha256")))
                lines = {line for line in item.get("lines") or [] if isinstance(line, int)}
                trace.lines.setdefault(key, set()).update(lines)
    return trace if trace.processes else None


def executed_lines(trace: Trace, path: str, content: bytes) -> set[int]:
    """Lines of ``path`` that ran while the file held exactly ``content``."""
    return set(trace.lines.get((path, hashlib.sha256(content).hexdigest()), set()))


class LineMap:
    """Statement structure of one Python file, for judging which lines ran."""

    def __init__(self, source: bytes) -> None:
        self.spans: dict[int, tuple[int, int]] = {}
        self.definitions: set[int] = set()
        self.docstrings: set[int] = set()
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            self.parsed = False
            return
        self.parsed = True
        for node in ast.walk(tree):
            if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                body = node.body
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    end = body[0].end_lineno or body[0].lineno
                    self.docstrings.update(range(body[0].lineno, end + 1))
            if not isinstance(node, ast.stmt):
                continue
            start, end = self._header(node)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                self.definitions.update(range(start, end + 1))
            for line in range(start, end + 1):
                current = self.spans.get(line)
                if current is None or end - start < current[1] - current[0]:
                    self.spans[line] = (start, end)

    @staticmethod
    def _header(node: ast.stmt) -> tuple[int, int]:
        decorators = getattr(node, "decorator_list", None) or []
        start = min([node.lineno, *(item.lineno for item in decorators)])
        end = node.end_lineno or node.lineno
        children = getattr(node, "body", None)
        if isinstance(node, ast.Match) and node.cases:
            first = node.cases[0].pattern.lineno
        elif isinstance(children, list) and children and isinstance(children[0], ast.stmt):
            first = children[0].lineno
        else:
            return start, end
        return start, max(node.lineno, first - 1)

    def code_lines(self) -> set[int]:
        """Lines that belong to a statement and are not a docstring."""
        return set(self.spans) - self.docstrings

    def ran(self, line: int, hits: set[int]) -> bool:
        start, end = self.spans.get(line, (line, line))
        return any(number in hits for number in range(start, end + 1))

    def ran_any(self, lines: Iterable[int], hits: set[int]) -> bool:
        return any(self.ran(line, hits) for line in lines)


def hunk_ran(lines: Sequence[int], line_map: LineMap, hits: set[int]) -> bool:
    """Did the tests run any behaviour in these oracle lines?

    Definition headers (``def``/``class``/decorators) run at import, so they
    count only when the hunk has no other code; a hunk with no code at all is
    judged by its neighbours.
    """
    code_lines = line_map.code_lines()
    code = [line for line in lines if line in code_lines]
    behaviour = [line for line in code if line not in line_map.definitions]
    if behaviour:
        return line_map.ran_any(behaviour, hits)
    if code:
        return line_map.ran_any(code, hits)
    neighbours = {min(lines, default=1) - 1, max(lines, default=0) + 1}
    return line_map.ran_any(neighbours, hits)


def line_ranges(lines: Iterable[int]) -> str:
    """``[3, 4, 5, 9]`` -> ``"3-5, 9"``."""
    ordered = sorted(set(lines))
    parts: list[str] = []
    for line in ordered:
        if parts and int(parts[-1].rsplit("-", 1)[-1]) == line - 1:
            head = parts[-1].split("-", 1)[0]
            parts[-1] = f"{head}-{line}"
        else:
            parts.append(str(line))
    return ", ".join(parts)
