"""Verifier for the transaction-reconciliation task."""

import sqlite3
from pathlib import Path

DATABASE_PATH = Path("/app/data/ledger.db")

EXPECTED_LEDGER_ENTRIES = [
    ("txn_1001", 12500, 12500, "reconciled"),
    ("txn_1002", 4999, 4999, "reconciled"),
    ("txn_1003", 8750, 8750, "reconciled"),
    ("txn_1004", 7500, 7500, "reconciled"),
    ("txn_1005", 1299, 1299, "reconciled"),
    ("txn_1006", 42000, 42000, "reconciled"),
]

EXPECTED_SETTLEMENT_FEED = [
    ("txn_1001", 12500, "2026-08-01T09:15:00Z"),
    ("txn_1002", 4999, "2026-08-01T10:30:00Z"),
    ("txn_1003", 8750, "2026-08-01T11:45:00Z"),
    ("txn_1004", 7500, "2026-08-01T13:00:00Z"),
    ("txn_1005", 1299, "2026-08-01T14:20:00Z"),
    ("txn_1006", 42000, "2026-08-01T15:35:00Z"),
]


def open_database() -> sqlite3.Connection:
    assert DATABASE_PATH.is_file(), "The task database is missing."
    return sqlite3.connect(DATABASE_PATH)


def test_ledger_entries_are_reconciled_without_collateral_changes():
    with open_database() as connection:
        actual_entries = connection.execute(
            """
            SELECT external_id, expected_amount_cents, settled_amount_cents,
                   reconciliation_status
            FROM ledger_entries
            ORDER BY external_id
            """
        ).fetchall()

    assert actual_entries == EXPECTED_LEDGER_ENTRIES


def test_settlement_feed_is_unchanged():
    with open_database() as connection:
        actual_feed = connection.execute(
            """
            SELECT external_id, amount_cents, settled_at
            FROM settlement_feed
            ORDER BY external_id
            """
        ).fetchall()

    assert actual_feed == EXPECTED_SETTLEMENT_FEED


def test_database_schema_is_preserved():
    with open_database() as connection:
        ledger_columns = connection.execute("PRAGMA table_info(ledger_entries)").fetchall()
        feed_columns = connection.execute("PRAGMA table_info(settlement_feed)").fetchall()

    assert [column[1] for column in ledger_columns] == [
        "external_id",
        "expected_amount_cents",
        "settled_amount_cents",
        "reconciliation_status",
    ]
    assert [column[1] for column in feed_columns] == [
        "external_id",
        "amount_cents",
        "settled_at",
    ]
