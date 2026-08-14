#!/bin/bash
set -euo pipefail

# Reconcile every ledger entry whose stored settlement amount disagrees with
# the authoritative settlement feed.
sqlite3 /app/data/ledger.db <<'SQL'
BEGIN;

UPDATE ledger_entries
SET
    settled_amount_cents = (
        SELECT amount_cents
        FROM settlement_feed
        WHERE settlement_feed.external_id = ledger_entries.external_id
    ),
    reconciliation_status = 'reconciled'
WHERE EXISTS (
    SELECT 1
    FROM settlement_feed
    WHERE settlement_feed.external_id = ledger_entries.external_id
      AND settlement_feed.amount_cents != ledger_entries.settled_amount_cents
);

COMMIT;
SQL
