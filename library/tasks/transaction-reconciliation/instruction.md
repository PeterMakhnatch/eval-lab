# Reconcile a settlement discrepancy

A seeded SQLite database is available at `/app/data/ledger.db`.

Use the data already in the database to find ledger entries whose recorded
settlement amount disagrees with the corresponding entry in the settlement
feed. Entries are matched by `external_id`.

For every discrepancy, update the ledger entry so that its
`settled_amount_cents` equals the feed's `amount_cents`, and set its
`reconciliation_status` to `reconciled`.

Do not change transaction identifiers, expected amounts, the settlement feed,
or the database schema. Leave entries that already agree with the feed
unchanged. Work directly in `/app/data/ledger.db`.
