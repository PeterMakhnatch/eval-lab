"""Create the deterministic starting state for the reconciliation task."""

import sqlite3
from pathlib import Path

database_path = Path("/app/data/ledger.db")
database_path.parent.mkdir(parents=True, exist_ok=True)

connection = sqlite3.connect(database_path)
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
    """
    INSERT INTO ledger_entries (
        external_id, expected_amount_cents, settled_amount_cents, reconciliation_status
    ) VALUES (?, ?, ?, ?)
    """,
    [
        ("txn_1001", 12500, 12500, "reconciled"),
        ("txn_1002", 4999, 4999, "reconciled"),
        ("txn_1003", 8750, 8750, "reconciled"),
        # Deliberately wrong: the feed records 7,500 cents for this transaction.
        ("txn_1004", 7500, 7050, "pending"),
        ("txn_1005", 1299, 1299, "reconciled"),
        ("txn_1006", 42000, 42000, "reconciled"),
    ],
)

connection.executemany(
    """
    INSERT INTO settlement_feed (external_id, amount_cents, settled_at)
    VALUES (?, ?, ?)
    """,
    [
        ("txn_1001", 12500, "2026-08-01T09:15:00Z"),
        ("txn_1002", 4999, "2026-08-01T10:30:00Z"),
        ("txn_1003", 8750, "2026-08-01T11:45:00Z"),
        ("txn_1004", 7500, "2026-08-01T13:00:00Z"),
        ("txn_1005", 1299, "2026-08-01T14:20:00Z"),
        ("txn_1006", 42000, "2026-08-01T15:35:00Z"),
    ],
)

connection.commit()
connection.close()
