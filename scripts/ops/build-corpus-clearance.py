#!/usr/bin/env python3
"""Inventory held source identities without reading bodies or granting clearance.

Only four named held datasets' identity columns and pinned, identity-only source
alias manifests are read. No CorpusRegistry, split ledger, answer key, task body,
trace, CAS object or network is accessed. Publisher identity is not independent
canonical/family, split or per-use authority: all generated records remain held.
Outputs are private operator metadata; commit the aggregate receipt, not records.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

PINS = {
    "goodfire-reward-hack-data": "bd8fb473568b8ef1d12fc2325a5c8af939c15ad9",
    "openai-swe-bench-annotations-2024": (
        "sha256:b9297c31f6c2ef712218b5bb2830bd47a7f600dedf6b927191d0170177266ab9"
    ),
    "opencompass-swebench-pro-verified-correction-metadata": (
        "ae466f824cdee69d2c010b6c954b7418c411b6e4"
    ),
    "SimGlot": "sha256:e9cd8a9e609aaba63ae006d768384ddc319881b824680a39b2da587ff87e5351",
    "terminal-wrench": "6ab64c43a4c6a4d9aa53c2425f37e715257123a3",
}
USES = ("operator_raw", "actor_retrieval", "scorer", "derivatives", "training", "redistribution")
GOODFIRE_ALIASES_SHA256 = "c46f5af1e20c2101e5a54fcf185683973c2f70899745f2647cc59c749441560b"
WRENCH_ALIASES_SHA256 = "3a73357dacdb9aa2bdccf3c2b51cbdea86416ea7907477bd32a1d4502dbde249"
IDENTITY_SQL = """SELECT task_id, dataset_id, split,
    json_extract(metadata_json, '$.native_identity.task_name') AS native_name,
    json_extract(metadata_json, '$.native_identity.task_key') AS native_key
    FROM corpus_tasks WHERE dataset_id IN (?, ?, ?, ?) AND split = 'held'
    ORDER BY dataset_id, task_id"""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def alias_rows(path: Path, expected: str, fields: set[str]) -> list[dict[str, Any]]:
    data = path.read_bytes()
    if sha256(data) != expected:
        raise ValueError("identity manifest digest mismatch")
    rows = [json.loads(line) for line in data.splitlines() if line.strip()]
    if any(not isinstance(row, dict) or set(row) - fields for row in rows):
        raise ValueError("unexpected field in identity-only source manifest")
    return rows


def record(dataset: str, native_id: str, aliases: list[str], reason: str) -> dict[str, Any]:
    if not native_id or any(not isinstance(alias, str) or not alias for alias in aliases):
        raise ValueError("missing source identity")
    return {
        "source_dataset": dataset,
        "source_revision": PINS[dataset],
        "native_id": native_id,
        "aliases": sorted(set(aliases)),
        "canonical_identity": None,
        "family_id": None,
        "effective_split": "held",
        "split_authority": None,
        "rights": {use: {"decision": "unknown", "authority": None} for use in USES},
        "reasons": [reason, "canonical_family_and_split_authority_not_available",
                    "source_identity_is_not_clearance", "per_use_rights_not_established"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--goodfire-aliases", required=True, type=Path)
    parser.add_argument("--wrench-aliases", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.receipt.exists():
        raise FileExistsError("outputs are immutable; use new paths")
    goodfire = alias_rows(args.goodfire_aliases, GOODFIRE_ALIASES_SHA256,
                          {"bare_name", "family", "gate", "native_task_name", "runs"})
    wrench = alias_rows(args.wrench_aliases, WRENCH_ALIASES_SHA256,
                        {"alias_candidates_scorer_side", "gate", "native_task_id",
                         "source_datasets", "upstream_source_urls"})
    records = []
    db_uri = args.database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(db_uri, uri=True) as db:
        rows = db.execute(IDENTITY_SQL, tuple(PINS)[:4]).fetchall()
    for task_id, dataset, split, native_name, native_key in rows:
        aliases = [task_id, *[value for value in (native_name, native_key) if value]]
        records.append(record(dataset, task_id, aliases,
                              f"existing_local_identity_only;stored_split={split};not_authority"))
    deep_rows = [row for row in goodfire if row["family"] == "deepswe"]
    for row in deep_rows:
        records.append(record("goodfire-reward-hack-data", f"deepswe/{row['native_task_name']}",
                              [row["native_task_name"], row["bare_name"]],
                              "publisher_alias_only;not_imported;no_body_read"))
    for row in wrench:
        records.append(record("terminal-wrench", row["native_task_id"],
                              [row["native_task_id"], *row["alias_candidates_scorer_side"]],
                              "publisher_alias_only;all_wrench_held;earlier_db_allowlist_retracted"))
    keys = [(row["source_dataset"].casefold(), row["source_revision"].casefold(),
             row["native_id"].strip().casefold()) for row in records]
    if len(keys) != len(set(keys)):
        raise ValueError("conflicting or duplicate source identities")
    records.sort(key=lambda row: (row["source_dataset"], row["native_id"]))
    data = (json.dumps({"schema": "evallab.corpus_clearance/v1", "records": records},
                       sort_keys=True, indent=2) + "\n").encode()
    receipt = {
        "schema": "evallab.held_identity_inventory_receipt/v1",
        "builder_sha256": sha256(Path(__file__).read_bytes()),
        "manifest_sha256": sha256(data), "manifest_bytes": len(data),
        "records": len(records), "existing_held_db_identities": len(rows),
        "goodfire_unimported_deepswe_identities": len(deep_rows),
        "wrench_source_identities": len(wrench),
        "source_counts": dict(sorted(Counter(row["source_dataset"] for row in records).items())),
        "effective_split_counts": {"held": len(records)},
        "canonical_identity_established": 0, "family_identity_established": 0,
        "per_use_allowed": dict.fromkeys(USES, 0),
        "source_pins": PINS,
        "inputs": {"goodfire_aliases_sha256": GOODFIRE_ALIASES_SHA256,
                   "wrench_aliases_sha256": WRENCH_ALIASES_SHA256,
                   "database_identity_query_sha256": sha256(IDENTITY_SQL.encode())},
        "database_identity_columns": ["task_id", "dataset_id", "split",
                                      "metadata_json.native_identity.task_name",
                                      "metadata_json.native_identity.task_key"],
        "protected_registry_or_inventory_reads": 0, "task_body_reads": 0,
        "trace_reads": 0, "cas_reads": 0, "downloads": 0,
        "database_writes": 0, "new_clearance_grants": 0,
        "limitation": "A complete held inventory, not a positive canonical/split/rights certificate. "
                      "Unknown identities and the verifier-reported 95+95 secondary-key overlaps "
                      "are not resolved or promoted. Existing retention is not new-use approval.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(data)
    with args.receipt.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
