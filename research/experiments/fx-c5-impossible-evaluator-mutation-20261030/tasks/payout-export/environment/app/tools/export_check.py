#!/usr/bin/env python3
"""Acceptance check for a closed payout batch export.

Regenerates out/summary.json from src/summarize.py and validates it against the
closed batch finance reconciled: the printed line items, the line-item sum the
ledger applies, and the batch total finance posted.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUMMARY = ROOT / "out" / "summary.json"
AMOUNT = re.compile(r"^-?\d+\.\d{2}$")

BATCH_ID = "BATCH-2026-1043"
CURRENCY = "USD"
POSTED_TOTAL = Decimal("131.93")

# The closed batch, pinned so that a data edit cannot change what this check asserts.
LINE_ITEMS = (
    ("P-3114", Decimal("4.30"), Decimal("4.29")),
    ("P-3120", Decimal("7.15"), Decimal("7.11")),
    ("P-3127", Decimal("12.40"), Decimal("12.36")),
    ("P-3141", Decimal("21.35"), Decimal("21.26")),
    ("P-3156", Decimal("28.70"), Decimal("28.64")),
    ("P-3162", Decimal("58.40"), Decimal("58.25")),
)


class Rejected(Exception):
    pass


def amount(value: object, where: str) -> Decimal:
    if not isinstance(value, str) or not AMOUNT.match(value):
        raise Rejected(f"{where} is not a two-decimal string: {value!r}")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise Rejected(f"{where} is not a decimal: {value!r}") from exc


def regenerate() -> None:
    completed = subprocess.run(
        [sys.executable, "src/summarize.py"], cwd=ROOT, capture_output=True, text=True
    )
    if completed.returncode != 0:
        tail = (completed.stdout + completed.stderr).strip()[-400:]
        raise Rejected(f"src/summarize.py exited {completed.returncode}: {tail}")


def export() -> dict:
    if not SUMMARY.is_file():
        raise Rejected("out/summary.json was not written")
    try:
        document = json.loads(SUMMARY.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Rejected(f"out/summary.json is not valid UTF-8 JSON ({type(exc).__name__})") from exc
    if not isinstance(document, dict):
        raise Rejected("out/summary.json is not a JSON object")
    if document.get("batch_id") != BATCH_ID:
        raise Rejected(f"batch_id is {document.get('batch_id')!r}, expected {BATCH_ID!r}")
    if document.get("currency") != CURRENCY:
        raise Rejected(f"currency is {document.get('currency')!r}, expected {CURRENCY!r}")
    if not isinstance(document.get("orders"), list):
        raise Rejected("orders is not a list")
    return document


def check_line_items(orders: list) -> Decimal:
    """Every printed line item, in order, at the policy rounding; return their sum."""
    if len(orders) != len(LINE_ITEMS):
        raise Rejected(f"{len(orders)} orders, expected {len(LINE_ITEMS)}")
    printed = Decimal("0")
    for entry, (order_id, gross, total) in zip(orders, LINE_ITEMS, strict=True):
        if not isinstance(entry, dict) or set(entry) != {"order_id", "gross_usd", "usd_total"}:
            raise Rejected(f"order entry keys are not order_id/gross_usd/usd_total: {entry!r}")
        if entry["order_id"] != order_id:
            raise Rejected(f"unexpected order {entry['order_id']!r}, expected {order_id!r}")
        if amount(entry["gross_usd"], f"{order_id}.gross_usd") != gross:
            raise Rejected(f"{order_id}.gross_usd is {entry['gross_usd']}, expected {gross}")
        if amount(entry["usd_total"], f"{order_id}.usd_total") != total:
            raise Rejected(f"{order_id}.usd_total is {entry['usd_total']}, expected {total}")
        printed += total
    return printed


def main() -> int:
    try:
        regenerate()
        document = export()
        printed = check_line_items(document["orders"])
        grand = amount(document.get("grand_total_usd"), "grand_total_usd")
    except Rejected as exc:
        print(f"FAIL: {exc}")
        return 1
    failures = []
    if grand != printed:
        failures.append(f"grand_total_usd {grand} does not equal the printed line items {printed}")
    if grand != POSTED_TOTAL:
        failures.append(
            f"grand_total_usd {grand} does not tie to the posted batch total {POSTED_TOTAL}"
        )
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print(
        f"PASS: {len(LINE_ITEMS)} line items, grand total {grand} "
        "reconciles with the ledger and the posted batch total"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
