#!/usr/bin/env python3
"""Run the CheatBench consumer-fidelity / blinding audit.

Inputs are the committed ``cb_subset.json`` index, the pinned local Parquet,
materialized detector ATIFs, and existing Kev JSONL windows.  Outputs are
deterministic audit JSONL under ``derived/`` (ignored)
plus a compact committed summary under this experiment's ``results/``.

No model, Docker, GPU, or paid calls are used.  Missing inputs are recorded
as unavailable and make the process exit nonzero; they are never passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

EXPERIMENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXPERIMENT_DIR.parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from evallab.cheatbench_fidelity import (  # noqa: E402
    POSITIVE_LABELS,
    SCHEMA,
    audit_kev_chunks,
    audit_materialized_atif,
    family_split,
    load_cb_subset,
    load_parquet_anchors,
    load_source_anchor_matches,
    summarize_atif_rows,
    summarize_kev,
)

PRIMARY = Path("/Users/petermakhnatch/Developer/eval-lab")
DEFAULT_SUBSET = REPO_ROOT / "research/calibration/detectors-v1/cb_subset.json"
DEFAULT_PARQUET = PRIMARY / "derived/external/cheatbench/full.parquet"
DEFAULT_ATIF_ROOT = PRIMARY / "derived/detector-roots/cb"
DEFAULT_KEV_ROOT = PRIMARY / "derived/kev/chunks"
DEFAULT_OUT_DIR = REPO_ROOT / "derived/trace-consumer-audit-v1"
DEFAULT_SUMMARY_OUT = EXPERIMENT_DIR / "results/summary.json"
_SHA_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _declared_pin_sha(parquet_path: Path) -> str | None:
    pin_path = parquet_path.parent / "PIN"
    if not pin_path.is_file():
        return None
    match = _SHA_RE.search(pin_path.read_text(encoding="utf-8", errors="replace"))
    return match.group(0).lower() if match else None


def _counter(counter: Counter[Any]) -> dict[str, int]:
    return {str(key): int(counter[key]) for key in sorted(counter, key=lambda k: str(k))}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subset", type=Path, default=DEFAULT_SUBSET)
    parser.add_argument("--parquet", type=Path, default=DEFAULT_PARQUET)
    parser.add_argument("--atif-root", type=Path, default=DEFAULT_ATIF_ROOT)
    parser.add_argument("--kev-root", type=Path, default=DEFAULT_KEV_ROOT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--summary-out", type=Path, default=DEFAULT_SUMMARY_OUT)
    parser.add_argument("--consumers", default="atif,kev")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--verify-source-hash", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    consumers = {item.strip() for item in args.consumers.split(",") if item.strip()}
    unknown_consumers = consumers - {"atif", "kev"}
    if unknown_consumers:
        raise SystemExit(f"unknown consumers: {sorted(unknown_consumers)}")
    if not args.subset.is_file():
        raise SystemExit(f"subset not found: {args.subset}")

    all_rows = load_cb_subset(args.subset)
    subset_rows = all_rows[: args.limit] if args.limit is not None else all_rows
    subset_sha = _sha256_file(args.subset)
    positive_ids = [
        str(row.get("trace_id") or row.get("id"))
        for row in subset_rows
        if row.get("label") in POSITIVE_LABELS
    ]

    source: dict[str, Any] = {
        "dataset": "steinad/CheatBench",
        "subset_path": str(args.subset),
        "subset_sha256": subset_sha,
        "parquet_path": str(args.parquet),
        "parquet_present": args.parquet.is_file(),
        "pin_sha256": _declared_pin_sha(args.parquet),
        "parquet_sha256": None,
        "pin_match": None,
    }
    exit_code = 0
    if args.verify_source_hash and args.parquet.is_file():
        source["parquet_sha256"] = _sha256_file(args.parquet)
        if source["pin_sha256"] is not None:
            source["pin_match"] = source["parquet_sha256"] == source["pin_sha256"]
            if not source["pin_match"]:
                exit_code = 3

    anchors: dict[str, Any] = {}
    source_matches: dict[str, Any] = {}
    parquet_error: str | None = None
    source_error: str | None = None
    if args.parquet.is_file():
        try:
            anchors = load_parquet_anchors(args.parquet, wanted_ids=positive_ids)
            source_matches = load_source_anchor_matches(args.parquet, anchors)
        except Exception as exc:  # recorded below; anchors stay unavailable
            parquet_error = f"{type(exc).__name__}: {exc}"
            source_error = parquet_error
            exit_code = exit_code or 2
    else:
        exit_code = exit_code or 2

    availability = {
        "parquet": bool(args.parquet.is_file() and parquet_error is None),
        "source_verification": bool(source_matches),
        "atif_root": bool(args.atif_root.is_dir()),
        "kev_root": bool(args.kev_root.is_dir()),
        "parquet_error": parquet_error,
        "source_error": source_error,
    }
    if "atif" in consumers and not availability["atif_root"]:
        exit_code = exit_code or 2
    if "kev" in consumers and not availability["kev_root"]:
        exit_code = exit_code or 2

    args.out_dir.mkdir(parents=True, exist_ok=True)
    consumer_summaries: dict[str, Any] = {}

    if "atif" in consumers:
        atif_rows = list(
            audit_materialized_atif(
                args.atif_root, subset_rows, anchors, source_matches=source_matches
            )
        )
        _write_jsonl(args.out_dir / "audit-atif.jsonl", atif_rows)
        consumer_summaries["materialized_atif"] = summarize_atif_rows(atif_rows)

    if "kev" in consumers:
        wanted_ids = {
            str(row.get("trace_id") or row.get("id")) for row in subset_rows
        }
        kev_result = audit_kev_chunks(
            args.kev_root, anchors, wanted_ids=wanted_ids, source_matches=source_matches
        )
        kev_rows = [
            {"schema": SCHEMA, "consumer": "kev_windows", **kev_result.per_trace[key]}
            for key in sorted(kev_result.per_trace)
        ]
        _write_jsonl(args.out_dir / "audit-kev.jsonl", kev_rows)
        consumer_summaries["kev_windows"] = summarize_kev(kev_result, subset_rows)

    labels = Counter(row.get("label") for row in subset_rows)
    benchmarks = Counter(row.get("benchmark") for row in subset_rows)
    splits = Counter(family_split(row.get("benchmark")) for row in subset_rows)
    summary: dict[str, Any] = {
        "schema": SCHEMA,
        "source": source,
        "availability": availability,
        "population": {
            "subset_rows_all": len(all_rows),
            "subset_rows_audited": len(subset_rows),
            "limit": args.limit,
            "labels": _counter(labels),
            "benchmarks": _counter(benchmarks),
            "family_split": _counter(splits),
            "positive_rows": len(positive_ids),
            "positive_rows_with_loc": sum(
                1
                for row in subset_rows
                if row.get("label") in POSITIVE_LABELS and row.get("loc")
            ),
            "anchors_loaded": len(anchors),
            "measurable_anchors": sum(1 for anchor in anchors.values() if anchor.measurable),
            "source_verified_measurable_anchors": sum(
                1
                for trace_id, anchor in anchors.items()
                if anchor.measurable
                and (source_matches.get(trace_id) is not None)
                and source_matches[trace_id].status == "preserved"
            ),
        },
        "consumers": consumer_summaries,
        "outputs": {
            "out_dir": str(args.out_dir),
            "atif_rows": "audit-atif.jsonl" if "atif" in consumers else None,
            "kev_rows": "audit-kev.jsonl" if "kev" in consumers else None,
        },
        "caveats": [
            "This validates evidence projection and blinding, not detector recall.",
            "Measurable snippets must first be found in the resolved source trace field; otherwise they are unknown, not consumer losses.",
            "Latent or unlocalized CheatBench rows have no measurable in-trace anchor.",
            "Kev trace/chunk label columns are supervised targets; only state text is audited as input.",
            "Materialized ATIF metadata currently carries evaluator labels; blind_atif is the proposed projection boundary.",
            "Heldout benchmark families are reported separately and were not used to choose search variants.",
        ],
    }
    _write_json(args.summary_out, summary)
    _write_json(args.out_dir / "summary.json", summary)

    print(
        "cheatbench consumer-fidelity audit: "
        f"rows={len(subset_rows)} anchors={len(anchors)} "
        f"measurable={summary['population']['measurable_anchors']}"
    )
    for name, payload in consumer_summaries.items():
        statuses = (payload.get("anchors") or {}).get("status") or {}
        print(
            f"  {name}: anchors preserved={statuses.get('preserved', 0)} "
            f"lost={statuses.get('lost', 0)} unknown={statuses.get('unknown', 0)} "
            f"not_applicable={statuses.get('not_applicable', 0)}"
        )
    print(f"summary: {args.summary_out}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
