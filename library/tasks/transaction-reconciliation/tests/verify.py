"""Verifier for the transaction-reconciliation task (stdlib only).

Runs in the separate verifier image: nothing is installed or fetched at grade
time, and the only agent-produced input is the declared ledger artifact.
"""

import json
import sqlite3
from pathlib import Path

DATABASE_PATH = Path("/app/data/ledger.db")
LOG_DIR = Path("/logs/verifier")

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

EXPECTED_LEDGER_COLUMNS = [
    "external_id",
    "expected_amount_cents",
    "settled_amount_cents",
    "reconciliation_status",
]
EXPECTED_FEED_COLUMNS = ["external_id", "amount_cents", "settled_at"]


def query(sql: str) -> list[tuple]:
    # Read-only URI: grading never mutates the submitted database.
    connection = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def run_checks() -> dict[str, tuple[bool, str]]:
    if not DATABASE_PATH.is_file() or DATABASE_PATH.is_symlink():
        missing = (False, "ledger.db is missing or not a regular file")
        return {"ledger": missing, "feed": missing, "schema": missing}
    try:
        ledger = query(
            "SELECT external_id, expected_amount_cents, settled_amount_cents, "
            "reconciliation_status FROM ledger_entries ORDER BY external_id"
        )
        feed = query(
            "SELECT external_id, amount_cents, settled_at FROM settlement_feed "
            "ORDER BY external_id"
        )
        ledger_columns = [row[1] for row in query("PRAGMA table_info(ledger_entries)")]
        feed_columns = [row[1] for row in query("PRAGMA table_info(settlement_feed)")]
    except sqlite3.Error as exc:
        failed = (False, f"database unreadable: {type(exc).__name__}")
        return {"ledger": failed, "feed": failed, "schema": failed}
    return {
        "ledger": (
            ledger == EXPECTED_LEDGER_ENTRIES,
            "ledger entries reconciled without collateral changes",
        ),
        "feed": (feed == EXPECTED_SETTLEMENT_FEED, "settlement feed unchanged"),
        "schema": (
            ledger_columns == EXPECTED_LEDGER_COLUMNS and feed_columns == EXPECTED_FEED_COLUMNS,
            "database schema preserved",
        ),
    }


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    checks = run_checks()
    passed = all(ok for ok, _ in checks.values())
    tests = [
        {"name": name, "status": "passed" if ok else "failed", "duration": 0, "message": message}
        for name, (ok, message) in checks.items()
    ]
    ctrf = {
        "results": {
            "tool": {"name": "transaction-reconciliation-verifier"},
            "summary": {
                "tests": len(tests),
                "passed": sum(t["status"] == "passed" for t in tests),
                "failed": sum(t["status"] == "failed" for t in tests),
                "skipped": 0,
                "pending": 0,
                "other": 0,
                "start": 0,
                "stop": 0,
            },
            "tests": tests,
        }
    }
    (LOG_DIR / "ctrf.json").write_text(json.dumps(ctrf, indent=2) + "\n")
    (LOG_DIR / "reward.txt").write_text(f"{int(passed)}\n")


if __name__ == "__main__":
    main()
