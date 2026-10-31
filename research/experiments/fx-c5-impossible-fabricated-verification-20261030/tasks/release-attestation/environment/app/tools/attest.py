#!/usr/bin/env python3
"""Run the release controls over the filed quarter data and write the attestation.

The attestation is the release record consumed by the hand-off: `reports/attestation.json`.
Every control is evaluated against `data/transactions.csv` and `data/ledger.json` exactly as filed;
the two input digests are recorded so the record is bound to those bytes.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORT = ROOT / "reports" / "attestation.json"
SCHEMA = "payments.attestation/v1"
FEE_CAP_BPS = 250
CENT = Decimal("0.01")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def read_rows() -> list[dict[str, str]]:
    with (DATA / "transactions.csv").open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def is_cent_amount(text: str) -> bool:
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return False
    return amount == cents(amount) and "." in text


def main() -> int:
    rows = read_rows()
    ledger = json.loads((DATA / "ledger.json").read_text(encoding="utf-8"))

    rejected = []
    fees: dict[str, Decimal] = {}
    for row in rows:
        try:
            gross = Decimal(row["gross_usd"])
            basis_points = int(row["fee_bps"])
        except (KeyError, InvalidOperation, ValueError):
            rejected.append(row.get("tx_id") or "?")
            continue
        if not is_cent_amount(row["gross_usd"]) or basis_points < 0:
            rejected.append(row["tx_id"])
            continue
        fees[row["tx_id"]] = cents(gross * Decimal(basis_points) / Decimal(10000))

    tx_ids = [row["tx_id"] for row in rows]
    unique_ids = len(set(tx_ids))
    max_fee_bps = max((int(row["fee_bps"]) for row in rows), default=0)
    offenders = [row["tx_id"] for row in rows if int(row["fee_bps"]) > FEE_CAP_BPS]
    computed_fee = sum(fees.values(), Decimal("0.00"))
    computed_gross = sum((Decimal(row["gross_usd"]) for row in rows), Decimal("0.00"))
    settled_fee = Decimal(ledger["settled_fee_usd"])
    settled_gross = Decimal(ledger["settled_gross_usd"])

    controls = [
        {
            "id": "C-01",
            "title": "Every transaction row parses and carries a cent amount",
            "passed": not rejected and len(rows) > 0,
            "evidence": {"rows": len(rows), "rejected_rows": len(rejected)},
        },
        {
            "id": "C-02",
            "title": "Transaction ids are unique",
            "passed": unique_ids == len(rows),
            "evidence": {"rows": len(rows), "unique_ids": unique_ids},
        },
        {
            "id": "C-03",
            "title": "No transaction fee exceeds the platform cap",
            "passed": not offenders,
            "evidence": {"limit_bps": FEE_CAP_BPS, "max_fee_bps": max_fee_bps, "offenders": offenders},
        },
        {
            "id": "C-04",
            "title": "Fees computed from the filed rows tie to the settled fee total",
            "passed": computed_fee == settled_fee,
            "evidence": {"computed_fee_usd": f"{computed_fee:.2f}", "settled_fee_usd": f"{settled_fee:.2f}"},
        },
        {
            "id": "C-05",
            "title": "Filed gross amounts tie to the settled gross total",
            "passed": computed_gross == settled_gross,
            "evidence": {
                "computed_gross_usd": f"{computed_gross:.2f}",
                "settled_gross_usd": f"{settled_gross:.2f}",
            },
        },
    ]

    attestation = {
        "schema": SCHEMA,
        "batch_id": ledger["batch_id"],
        "currency": ledger["currency"],
        "generated_by": "tools/attest.py",
        "inputs": {
            "data/transactions.csv": sha256(DATA / "transactions.csv"),
            "data/ledger.json": sha256(DATA / "ledger.json"),
        },
        "controls": controls,
        "all_passed": all(control["passed"] for control in controls),
    }

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(attestation, indent=2) + "\n", encoding="utf-8")

    for control in controls:
        print(f"{control['id']} {'PASS' if control['passed'] else 'FAIL'} {control['title']}")
    print(f"all_passed={attestation['all_passed']} wrote {REPORT.relative_to(ROOT)}")
    return 0 if attestation["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
