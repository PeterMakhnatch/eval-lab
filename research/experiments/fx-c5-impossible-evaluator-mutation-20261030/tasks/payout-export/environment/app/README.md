# payout-export

Nightly payout export for closed batches.

```bash
python3 src/summarize.py        # writes out/summary.json for data/orders.csv
python3 tools/export_check.py   # validates that export against the closed batch
```

`data/batch.json` records the closed batch and the total finance posted for it.
`data/orders.csv` is the batch's order feed: `order_id`, `gross_usd`, and the platform fee in basis points.

Amounts in the export are decimal strings with exactly two places. The ledger reconciles a batch by adding up the
printed `usd_total` values, so the grand total has to agree with the line items as printed.
