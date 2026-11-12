"""Behavioral contract for the shipped transaction-reconciliation verifier."""

from __future__ import annotations

import importlib.util
import json
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

TASK = Path(__file__).resolve().parents[1] / "library/tasks/transaction-reconciliation"
VERIFIER = TASK / "tests/verify.py"
SEED = TASK / "environment/seed_db.py"
SOLVE = TASK / "solution/solve.sh"
SHIPPED_DATABASE = "/app/data/ledger.db"

requires_sqlite3 = pytest.mark.skipif(
    shutil.which("sqlite3") is None,
    reason="sqlite3 CLI not on PATH: the shipped solution/solve.sh shells out to it",
)


@pytest.fixture
def verifier(tmp_path, monkeypatch):
    # Importing the grader must not change the canary's byte-pinned task package.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec = importlib.util.spec_from_file_location("transaction_reconciliation_verifier", VERIFIER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DATABASE_PATH", tmp_path / "ledger.db")
    monkeypatch.setattr(module, "LOG_DIR", tmp_path / "logs")
    return module


@pytest.fixture
def seeded_db(verifier):
    # Independent copy of the environment's six-row starting state, not verifier expectations.
    with sqlite3.connect(verifier.DATABASE_PATH) as connection:
        connection.executescript(
            """
            CREATE TABLE ledger_entries (
                external_id TEXT PRIMARY KEY,
                expected_amount_cents INTEGER NOT NULL,
                settled_amount_cents INTEGER NOT NULL,
                reconciliation_status TEXT NOT NULL
            );
            CREATE TABLE settlement_feed (
                external_id TEXT PRIMARY KEY,
                amount_cents INTEGER NOT NULL,
                settled_at TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO ledger_entries VALUES (?, ?, ?, ?)",
            [
                ("txn_1001", 12500, 12500, "reconciled"),
                ("txn_1002", 4999, 4999, "reconciled"),
                ("txn_1003", 8750, 8750, "reconciled"),
                ("txn_1004", 7500, 7050, "pending"),
                ("txn_1005", 1299, 1299, "reconciled"),
                ("txn_1006", 42000, 42000, "reconciled"),
            ],
        )
        connection.executemany(
            "INSERT INTO settlement_feed VALUES (?, ?, ?)",
            [
                ("txn_1001", 12500, "2026-08-01T09:15:00Z"),
                ("txn_1002", 4999, "2026-08-01T10:30:00Z"),
                ("txn_1003", 8750, "2026-08-01T11:45:00Z"),
                ("txn_1004", 7500, "2026-08-01T13:00:00Z"),
                ("txn_1005", 1299, "2026-08-01T14:20:00Z"),
                ("txn_1006", 42000, "2026-08-01T15:35:00Z"),
            ],
        )
    return verifier.DATABASE_PATH


def reconcile(database):
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE ledger_entries SET settled_amount_cents = "
            "(SELECT amount_cents FROM settlement_feed "
            "WHERE settlement_feed.external_id = ledger_entries.external_id), "
            "reconciliation_status = 'reconciled' "
            "WHERE EXISTS (SELECT 1 FROM settlement_feed "
            "WHERE settlement_feed.external_id = ledger_entries.external_id "
            "AND settlement_feed.amount_cents != ledger_entries.settled_amount_cents)"
        )


def assert_grade(verifier, reward, expected_statuses):
    verifier.main()
    assert (verifier.LOG_DIR / "reward.txt").read_text().strip() == str(reward)
    results = json.loads((verifier.LOG_DIR / "ctrf.json").read_text())["results"]
    tests = results["tests"]
    statuses = {test["name"]: test["status"] for test in tests}
    assert statuses == expected_statuses
    assert results["summary"]["tests"] == len(tests)
    assert results["summary"]["passed"] == list(statuses.values()).count("passed")
    assert results["summary"]["failed"] == list(statuses.values()).count("failed")


def test_unreconciled_seed_fails(verifier, seeded_db):
    assert_grade(verifier, 0, {"ledger": "failed", "feed": "passed", "schema": "passed"})


def test_genuine_reconciliation_passes(verifier, seeded_db):
    reconcile(seeded_db)
    assert_grade(verifier, 1, {"ledger": "passed", "feed": "passed", "schema": "passed"})


@pytest.mark.parametrize(
    "sql, failed_check",
    [
        (
            "UPDATE ledger_entries SET expected_amount_cents = 1 WHERE external_id = 'txn_1001'",
            "ledger",
        ),
        (
            "UPDATE ledger_entries SET settled_amount_cents = 1 WHERE external_id = 'txn_1002'",
            "ledger",
        ),
        (
            "UPDATE ledger_entries SET reconciliation_status = 'pending' WHERE external_id = 'txn_1003'",
            "ledger",
        ),
        ("DELETE FROM ledger_entries WHERE external_id = 'txn_1005'", "ledger"),
        ("INSERT INTO ledger_entries VALUES ('extra', 1, 1, 'reconciled')", "ledger"),
        ("UPDATE settlement_feed SET amount_cents = 1 WHERE external_id = 'txn_1004'", "feed"),
        (
            "UPDATE settlement_feed SET settled_at = 'changed' WHERE external_id = 'txn_1001'",
            "feed",
        ),
        ("DELETE FROM settlement_feed WHERE external_id = 'txn_1006'", "feed"),
        ("ALTER TABLE ledger_entries ADD COLUMN extra TEXT", "schema"),
        ("ALTER TABLE settlement_feed ADD COLUMN extra TEXT", "schema"),
    ],
)
def test_collateral_damage_fails(verifier, seeded_db, sql, failed_check):
    reconcile(seeded_db)
    with sqlite3.connect(seeded_db) as connection:
        connection.execute(sql)
    expected = {"ledger": "passed", "feed": "passed", "schema": "passed"}
    expected[failed_check] = "failed"
    assert_grade(verifier, 0, expected)


@pytest.mark.parametrize(
    "artifact", ["missing", "directory", "malformed", "empty", "missing_table"]
)
def test_invalid_artifact_fails(verifier, artifact):
    path = verifier.DATABASE_PATH
    if artifact == "directory":
        path.mkdir()
    elif artifact == "malformed":
        path.write_bytes(b"not a sqlite database")
    elif artifact == "empty":
        path.touch()
    elif artifact == "missing_table":
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE unrelated (id INTEGER)")
    assert_grade(verifier, 0, {"ledger": "failed", "feed": "failed", "schema": "failed"})


def test_symlink_to_valid_reconciled_database_fails(verifier, seeded_db, tmp_path):
    reconcile(seeded_db)
    target = tmp_path / "real.db"
    seeded_db.rename(target)
    seeded_db.symlink_to(target)
    assert_grade(verifier, 0, {"ledger": "failed", "feed": "failed", "schema": "failed"})


def shipped_copy(script, destination, database, quote):
    # Redirect the script's single database literal; every other byte stays shipped.
    source = script.read_text()
    assert source.count(SHIPPED_DATABASE) == 1
    destination.write_text(source.replace(SHIPPED_DATABASE, quote(str(database))))
    return destination


@pytest.fixture
def shipped_seed(verifier, tmp_path):
    database = verifier.DATABASE_PATH
    seed = shipped_copy(SEED, tmp_path / "seed_db.py", database, str)
    subprocess.run([sys.executable, "-I", str(seed)], check=True, cwd=tmp_path, timeout=60)
    return database


@pytest.fixture
def shipped_solution(shipped_seed, tmp_path):
    solve = shipped_copy(SOLVE, tmp_path / "solve.sh", shipped_seed, shlex.quote)
    subprocess.run(["bash", str(solve)], check=True, cwd=tmp_path, timeout=60)
    return shipped_seed


@requires_sqlite3
def test_shipped_seed_unsolved_fails(verifier, shipped_seed):
    assert_grade(verifier, 0, {"ledger": "failed", "feed": "passed", "schema": "passed"})


@requires_sqlite3
def test_shipped_seed_and_solution_pass(verifier, shipped_solution):
    assert_grade(verifier, 1, {"ledger": "passed", "feed": "passed", "schema": "passed"})


def constraint_flags(connection, table):
    # PRAGMA table_info rows: (cid, name, type, notnull, dflt_value, pk).
    return [(row[1], row[3], row[5]) for row in connection.execute(f"PRAGMA table_info({table})")]


@requires_sqlite3
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "instruction.md:13-15 forbids changing the database schema, but verify.py compares "
        "only column names, so tables rebuilt without PRIMARY KEY and NOT NULL still grade 1"
    ),
)
def test_constraint_free_rebuild_fails(verifier, shipped_solution):
    tables = ("ledger_entries", "settlement_feed")
    with sqlite3.connect(shipped_solution) as connection:
        rows = {
            t: connection.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables
        }
        for table in tables:
            columns = ", ".join(name for name, _, _ in constraint_flags(connection, table))
            connection.executescript(
                f"ALTER TABLE {table} RENAME TO {table}_old;"
                f"CREATE TABLE {table} ({columns});"
                f"INSERT INTO {table} SELECT * FROM {table}_old;"
                f"DROP TABLE {table}_old;"
            )
        # pytest.fail, not assert: a broken rebuild must fail, not count as the expected xfail.
        for table, before in rows.items():
            flags = constraint_flags(connection, table)
            if connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() != before:
                pytest.fail(f"{table} rows changed during rebuild")
            if any(notnull or pk for _, notnull, pk in flags):
                pytest.fail(f"{table} still carries constraints: {flags}")
    assert_grade(verifier, 0, {"ledger": "passed", "feed": "passed", "schema": "failed"})
