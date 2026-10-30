#!/usr/bin/env python3
"""Payout export for the closed batch: out/summary.json.

Per-order totals are rounded half-up to the cent from the order's gross and fee, and the grand total is the sum of
those printed line items, which is how the ledger reconciles a batch.
"""

from __future__ import annotations

import csv
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CENT = Decimal("0.01")


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def main() -> None:
    batch = json.loads((ROOT / "data/batch.json").read_text(encoding="utf-8"))
    orders = []
    printed_total = Decimal("0")
    with (ROOT / "data/orders.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            gross = Decimal(row["gross_usd"])
            fee = Decimal(row["fee_bps"])
            net = cents(gross * (Decimal(10000) - fee) / Decimal(10000))
            printed_total += net
            orders.append(
                {
                    "order_id": row["order_id"],
                    "gross_usd": str(cents(gross)),
                    "usd_total": str(net),
                }
            )
    document = {
        "batch_id": batch["batch_id"],
        "currency": batch["currency"],
        "orders": orders,
        "grand_total_usd": str(printed_total),
    }
    destination = ROOT / "out/summary.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
