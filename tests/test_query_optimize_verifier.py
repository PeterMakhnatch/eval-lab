"""Host contract for the shipped query-optimize hidden verifier.

The verifier runs inside the task image against the 100 MB OEWN SQLite database
at ``/app/oewn.sqlite``. This module seeds a synthetic WordNet database under
``tmp_path`` (words, senses and synsets only) and imports a tmp copy of
``library/tasks/query-optimize/tests/test_outputs.py`` with every absolute path
redirected into ``tmp_path``. The shipped checks then run unchanged on the host.

The contract covers the checks that need only those three tables: both
correctness checks, the single-statement check and the size check. The
database-hash check pins the real 100 MB file, and the runtime check times the
real corpus, so neither is part of the pass contract here.

The task package bytes are frozen by the gym-v1 manifest, so the contract lives
here instead of ``library/tasks/query-optimize``.
"""

from __future__ import annotations

import importlib.util
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "library" / "tasks" / "query-optimize"
VERIFIER = PACKAGE / "tests" / "test_outputs.py"
GOLDEN = PACKAGE / "tests" / "golden.sql"
MY_SQL_QUERY = PACKAGE / "environment" / "task-deps" / "my-sql-query.sql"
SOLVE = PACKAGE / "solution" / "solve.sh"

HASH_CHECK = "test_check_for_db_modifications"
RUNTIME_CHECK = "test_compare_golden_vs_solution_runtime"
CHECKS = (
    "test_compare_golden_vs_my_sql_query_correctness",
    "test_outputs_match_exactly",
    "test_solution_contains_single_sql_query",
    "test_solution_is_small",
)

_SCHEMA = """
CREATE TABLE words (wordid INTEGER PRIMARY KEY, word TEXT NOT NULL);
CREATE TABLE synsets (
    synsetid INTEGER PRIMARY KEY,
    domainid INTEGER NOT NULL,
    posid INTEGER NOT NULL
);
CREATE TABLE senses (wordid INTEGER NOT NULL, synsetid INTEGER NOT NULL);
"""

_SEED = """
INSERT INTO words (wordid, word) VALUES
    (1, 'alpha'), (2, 'beta'), (3, 'gamma'), (4, 'delta'), (5, 'zeta');
INSERT INTO synsets (synsetid, domainid, posid) VALUES
    (101, 10, 1), (102, 20, 2), (103, 10, 1),
    (104, 10, 1), (105, 20, 1),
    (106, 11, 2), (107, 21, 2), (108, 11, 2), (109, 21, 2),
    (110, 12, 3), (111, 22, 3), (112, 12, 3),
    (113, 30, 4), (114, 40, 4);
INSERT INTO senses (wordid, synsetid) VALUES
    (1, 101), (1, 102), (1, 103),
    (2, 104), (2, 105),
    (3, 106), (3, 107), (3, 108), (3, 109),
    (4, 110), (4, 111), (4, 112),
    (5, 113), (5, 113), (5, 113), (5, 114);
"""

# The shipped golden query's final ORDER BY, used to build a row-order variant.
_FINAL_ORDER_BY = """ORDER BY
  ws.total_senses DESC,
  ws.total_synsets DESC,
  ws.distinct_domains DESC,
  ws.wordid ASC"""

_REVERSED_ORDER_BY = """ORDER BY
  ws.total_senses ASC,
  ws.total_synsets ASC,
  ws.distinct_domains ASC,
  ws.wordid DESC"""


def _seed_database(path: Path) -> None:
    """Seed five words that all clear the golden query's HAVING filters.

    Gamma has four synsets, so its ``total_senses`` key is 4. Zeta repeats one
    sense row three times, so ``top_synset_sense_count`` is 3 instead of the
    trivial 1. The four ordering keys together are unique, so reversing the
    ORDER BY reverses every output row.
    """
    connection = sqlite3.connect(path)
    try:
        connection.executescript(_SCHEMA + _SEED)
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def verifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import a tmp copy of the shipped verifier with its absolute paths redirected."""
    app = tmp_path / "app"
    tests = tmp_path / "tests"
    app.mkdir()
    tests.mkdir()
    db_path = app / "oewn.sqlite"
    _seed_database(db_path)
    golden_path = tests / "golden.sql"
    shutil.copyfile(GOLDEN, golden_path)
    my_sql_query_path = app / "my-sql-query.sql"
    shutil.copyfile(MY_SQL_QUERY, my_sql_query_path)
    sol_path = app / "sol.sql"
    summary_path = app / "bench_summary.json"

    redirects = {
        "/app/oewn.sqlite": db_path,
        "/tests/golden.sql": golden_path,
        "/app/sol.sql": sol_path,
        "/app/bench_summary.json": summary_path,
        "/app/my-sql-query.sql": my_sql_query_path,
    }
    source = VERIFIER.read_text(encoding="utf-8")
    counts = {}
    for literal, replacement in redirects.items():
        quoted = f'"{literal}"'
        counts[literal] = source.count(quoted)
        source = source.replace(quoted, repr(str(replacement)))
    # The hash check re-declares DB_PATH locally, so the database literal
    # appears twice; every absolute path must still point into tmp_path.
    assert counts["/app/oewn.sqlite"] == 2, counts
    assert all(count == 1 for literal, count in counts.items() if literal != "/app/oewn.sqlite"), (
        counts
    )
    assert '"/app/' not in source and '"/tests/' not in source

    copy_path = tmp_path / "verifier" / "test_outputs.py"
    copy_path.parent.mkdir()
    copy_path.write_text(source, encoding="utf-8")

    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec = importlib.util.spec_from_file_location("query_optimize_verifier", copy_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def shipped_solution(verifier: ModuleType) -> Path:
    """Run a tmp copy of the shipped solve.sh that writes into tmp_path."""
    source = SOLVE.read_text(encoding="utf-8")
    assert source.count("/app/sol.sql") == 1
    script = verifier.SOL_SQL_PATH.parent / "solve.sh"
    script.write_text(
        source.replace("/app/sol.sql", shlex.quote(str(verifier.SOL_SQL_PATH))),
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["bash", str(script)],
        cwd=script.parent,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert verifier.SOL_SQL_PATH.is_file()
    return verifier.SOL_SQL_PATH


def write_solution(verifier: ModuleType, sql: str) -> None:
    verifier.SOL_SQL_PATH.write_text(sql, encoding="utf-8")


def _wrong_solution(verifier: ModuleType, case: str) -> str:
    golden = verifier._read_sql(verifier.GOLDEN_SQL_PATH)
    if case == "two_statements":
        return f"{golden}\nSELECT 1;"
    if case == "update_statement":
        return "UPDATE words SET word = 'changed';"
    if case == "no_final_semicolon":
        assert golden.endswith(";")
        return golden[:-1]
    if case == "over_size_limit":
        sql = "SELECT 1 AS " + "a" * 1988 + ";"
        assert len(sql) == 2001
        return sql
    if case == "renamed_column":
        assert golden.count("ws.wordid AS word_id") == 1
        return golden.replace("ws.wordid AS word_id", "ws.wordid AS word_identifier")
    if case == "reversed_row_order":
        assert golden.count(_FINAL_ORDER_BY) == 1
        return golden.replace(_FINAL_ORDER_BY, _REVERSED_ORDER_BY)
    raise AssertionError(case)


def test_shipped_solution_passes_the_host_checks(
    verifier: ModuleType, shipped_solution: Path
) -> None:
    """The oracle's sol.sql clears every check in the host contract."""
    missing = [name for name in CHECKS if not callable(getattr(verifier, name, None))]
    assert missing == [], f"shipped verifier no longer defines {missing}"
    for name in CHECKS:
        getattr(verifier, name)()

    _, rows = verifier._execute_get_columns_and_rows(
        verifier.DB_PATH, verifier._read_sql(verifier.GOLDEN_SQL_PATH)
    )
    assert len(rows) >= 3, "the synthetic database must return rows for the compare checks"
    assert max(row[-1] for row in rows) == 3, "a top synset must have more than one sense"


def test_synthetic_database_excludes_hash_and_runtime_checks(verifier: ModuleType) -> None:
    """The pass contract does not require the two real-corpus checks.

    ``test_check_for_db_modifications`` pins the sha256 of the real 100 MB
    database, and ``test_compare_golden_vs_solution_runtime`` times the real
    corpus, so neither belongs to this host run. The hash check still rejects
    the synthetic file, which is why the contract cannot include it.
    """
    assert HASH_CHECK not in CHECKS
    assert RUNTIME_CHECK not in CHECKS
    assert callable(getattr(verifier, HASH_CHECK))
    assert callable(getattr(verifier, RUNTIME_CHECK))
    with pytest.raises(AssertionError, match="Database has been modified"):
        verifier.test_check_for_db_modifications()


@pytest.mark.parametrize(
    ("case", "check", "message"),
    [
        (
            "two_statements",
            "test_solution_contains_single_sql_query",
            "exactly one statement",
        ),
        ("update_statement", "test_solution_contains_single_sql_query", "must be a SELECT"),
        (
            "no_final_semicolon",
            "test_solution_contains_single_sql_query",
            "terminated by a semicolon",
        ),
        ("over_size_limit", "test_solution_is_small", "too large"),
        ("renamed_column", "test_outputs_match_exactly", "Column names/order differ"),
        ("reversed_row_order", "test_outputs_match_exactly", "Row 0 differs"),
    ],
)
def test_wrong_solution_fails_its_check(
    verifier: ModuleType, case: str, check: str, message: str
) -> None:
    sql = _wrong_solution(verifier, case)
    write_solution(verifier, sql)
    with pytest.raises(AssertionError, match=message):
        getattr(verifier, check)()

    if case not in ("renamed_column", "reversed_row_order"):
        return
    golden_columns, golden_rows = verifier._execute_get_columns_and_rows(
        verifier.DB_PATH, verifier._read_sql(verifier.GOLDEN_SQL_PATH)
    )
    columns, rows = verifier._execute_get_columns_and_rows(verifier.DB_PATH, sql)
    if case == "renamed_column":
        assert columns != golden_columns
        assert rows == golden_rows, "only the column name differs"
    else:
        assert columns == golden_columns
        assert rows == list(reversed(golden_rows)), "only the row order differs"


@pytest.mark.xfail(
    strict=True,
    reason="the single-statement check strips comments, so a commented answer passes",
)
def test_comment_annotated_solution_is_rejected(verifier: ModuleType) -> None:
    """instruction.md forbids comments ("no comments, just one single sql query").

    ``test_solution_contains_single_sql_query`` strips ``--`` and ``/* */``
    comments before checking, so the golden query with a leading ``-- note``
    line is accepted. This strict xfail records the gap; when the check starts
    rejecting comments, the body passes and the marker must be removed.
    """
    golden = verifier._read_sql(verifier.GOLDEN_SQL_PATH)
    write_solution(verifier, "-- note\n" + golden)
    with pytest.raises(AssertionError):
        verifier.test_solution_contains_single_sql_query()
