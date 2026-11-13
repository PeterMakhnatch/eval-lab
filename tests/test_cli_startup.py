"""Version and parser construction remain independent of execution dependencies."""

from __future__ import annotations

import json
import subprocess
import sys

from evallab import __version__


def test_console_version() -> None:
    done = subprocess.run(
        [sys.executable, "-c", "from evallab.entry import main; main(['--version'])"],
        capture_output=True, text=True, check=False, timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == __version__ + "\n"


def test_parser_startup_excludes_execution_dependencies() -> None:
    done = subprocess.run(
        [sys.executable, "-c", """
import json
import sys
from evallab.cli import parser
parser()
blocked = ("psycopg", "pyarrow", "duckdb", "evallab.database", "evallab.runner", "evallab.queue")
print(json.dumps(sorted(
    name for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in blocked)
)))
"""],
        capture_output=True, text=True, check=False, timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [], "Heavy startup imports: " + done.stdout.strip()
