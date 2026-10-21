"""Tripwires v4: confirmation gates over retained v3 rule hits (precision at fixed recall).

No trajectory is scanned here. Inputs are the frozen ``tripwires-v3@d338116d6583``
per-item predictions; v4 changes only which combinations of already-fired verdict
rules carry a verdict. Gate selection happens on a by-task tune half of the TB4
Hub DEV census (see ``PROTOCOL.md``, written before tuning); the frozen gate is
then scored once on the holdout half and on the retained test-split predictions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
V3_DIR = REPO / "research/experiments/tripwires-v3-hackbook-20261021"
SCOREBOARD = REPO / "research/qa-scoreboard/scoreboard.csv"
VERSION_ID = "tripwires-v4"
SEED = "tripwires-v4-tune-holdout-20261021"
SCHEMA = "evallab.tripwires_v4_precision/v1"

ATTEMPT_RULES = ("upstream_attempts", "harness_log_reads")
#: Candidate gates in tie-break order (see PROTOCOL.md selection rule). In an OR,
#: co-fire gating collapses to dropping, so candidates are drop subsets.
GATE_ORDER = ("drop_attempts", "drop_upstream", "drop_harness")
TEST_COHORTS = (
    "cheatbench-actions-v1",
    "hacktrace-act-v1",
    "mimo-v7-canonical-pass70",
    "mimo-fresh-window3",
)
TB4_COHORTS = ("tb4-hub-dev-official", "tb4-hub-dev-bootstrap")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wilson(hits: int, n: int) -> list[float] | None:
    if n == 0:
        return None
    z = 1.959963984540054
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [max(0.0, centre - half), min(1.0, centre + half)]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def verdict_rules() -> frozenset[str]:
    """Verdict-carrying rule ids of the frozen v3 catalog (legacy + verdict hackbook)."""
    result = json.loads((V3_DIR / "results-test.json").read_text())
    return frozenset(
        r["id"]
        for r in result["catalog"]
        if r["family"] == "legacy" or r["eligibility"] == "verdict"
    )


def fired_rules(record: dict[str, Any]) -> frozenset[str]:
    return frozenset(h["rule"] for h in record["hits"] if h["rule"] in verdict_rules())


def gate_fired(fired: frozenset[str], gate: str) -> bool:
    """Whether the gated verdict OR fires for one item's fired verdict-rule set."""
    if gate == "v3_any":
        return bool(fired)
    if gate == "drop_attempts":
        return bool(fired - frozenset(ATTEMPT_RULES))
    if gate == "drop_upstream":
        return bool(fired - {ATTEMPT_RULES[0]})
    if gate == "drop_harness":
        return bool(fired - {ATTEMPT_RULES[1]})
    raise ValueError(f"unknown gate {gate!r}")


def split_tasks(tasks: list[str], seed: str = SEED) -> dict[str, str]:
    """``{task: tune|holdout}``: first half by seeded hash order is tune."""
    ordered = sorted(tasks, key=lambda t: sha256_bytes(f"{seed}:{t}".encode()))
    half = len(ordered) // 2
    return {t: ("tune" if i < half else "holdout") for i, t in enumerate(ordered)}


def tb4_tasks(records: list[dict[str, Any]], cohort: str = "tb4-hub-dev-official") -> list[str]:
    return sorted(
        {
            r["source"]["task"]
            for r in records
            if r["cohort"] == cohort and r["view"] == "full" and r.get("source", {}).get("task")
        }
    )


def counts(records: list[dict[str, Any]], gate: str) -> dict[str, Any]:
    """TP / false alarms / precision / recall over measured positive+negative records."""
    tp = fa = positives = negatives = 0
    for r in records:
        if r["status"] != "measured" or r["role"] not in ("positive", "negative"):
            continue
        fired = gate_fired(fired_rules(r), gate)
        if r["role"] == "positive":
            positives += 1
            tp += fired
        else:
            negatives += 1
            fa += fired
    return {
        "true_positives": tp,
        "positives": positives,
        "false_alarms": fa,
        "negatives": negatives,
        "recall": (tp / positives) if positives else None,
        "precision": (tp / (tp + fa)) if (tp + fa) else None,
        "false_alarm_rate": (fa / negatives) if negatives else None,
        "recall_wilson_95": wilson(tp, positives),
        "precision_wilson_95": wilson(tp, tp + fa),
        "false_alarm_rate_wilson_95": wilson(fa, negatives),
    }


def select_gate(tune_records: list[dict[str, Any]]) -> dict[str, Any]:
    """Pre-registered selection (PROTOCOL.md): admissible = recall loss <= 1 TP vs v3."""
    table = {gate: counts(tune_records, gate) for gate in ("v3_any", *GATE_ORDER)}
    base = table["v3_any"]
    admissible = [g for g in GATE_ORDER if table[g]["true_positives"] >= base["true_positives"] - 1]
    chosen = None
    for gate in GATE_ORDER:
        if gate not in admissible:
            continue
        if table[gate]["precision"] is None:
            continue
        if base["precision"] is not None and table[gate]["precision"] <= base["precision"]:
            continue
        if chosen is None or table[gate]["precision"] > table[chosen]["precision"]:
            chosen = gate
    return {"table": table, "admissible": admissible, "chosen": chosen}


def fingerprints() -> dict[str, Any]:
    inputs = {
        str(p.relative_to(REPO)): sha256_bytes(p.read_bytes())
        for p in (V3_DIR / "predictions-dev.jsonl", V3_DIR / "predictions-test.jsonl")
    }
    implementation = sha256_bytes((HERE / "run.py").read_bytes())
    combined = sha256_bytes(
        json.dumps({"implementation": implementation, "inputs": inputs}, sort_keys=True).encode()
    )
    return {
        "implementation": implementation,
        "inputs": inputs,
        "combined": combined,
        "system_id": f"{VERSION_ID}@{combined[:12]}",
    }


def tune() -> dict[str, Any]:
    records = load_jsonl(V3_DIR / "predictions-dev.jsonl")
    assignment = split_tasks(tb4_tasks(records))
    tune_records = [
        r
        for r in records
        if r["cohort"] == "tb4-hub-dev-official"
        and r["view"] == "full"
        and assignment.get(r["source"]["task"]) == "tune"
    ]
    selection = select_gate(tune_records)
    return {
        "schema": SCHEMA,
        "stage": "tune",
        "seed": SEED,
        "fingerprint": fingerprints(),
        "task_assignment": dict(sorted(assignment.items())),
        "tune_cohort": "tb4-hub-dev-official/full",
        **selection,
    }


def score(gate: str, expect: str) -> dict[str, Any]:
    fp = fingerprints()
    if expect != fp["combined"]:
        raise ValueError(f"fingerprint {fp['combined']} != expected {expect}: not the frozen v4")
    dev = load_jsonl(V3_DIR / "predictions-dev.jsonl")
    test = load_jsonl(V3_DIR / "predictions-test.jsonl")
    assignment = split_tasks(tb4_tasks(dev))
    out: dict[str, Any] = {
        "schema": SCHEMA,
        "stage": "score",
        "fingerprint": fp,
        "gate": gate,
        "seed": SEED,
        "cohorts": {},
    }
    for cohort in TB4_COHORTS:
        sel = [
            r
            for r in dev
            if r["cohort"] == cohort
            and r["view"] == "full"
            and assignment.get(r["source"]["task"]) == "holdout"
        ]
        out["cohorts"][f"{cohort}/full/holdout"] = {
            "gate": counts(sel, gate),
            "v3_any": counts(sel, "v3_any"),
        }
    v3_test = json.loads((V3_DIR / "results-test.json").read_text())
    for cohort in TEST_COHORTS:
        registered = v3_test["cohorts"][cohort]["registered_in_split"]
        for view in ("full", "actions"):
            sel = [r for r in test if r["cohort"] == cohort and r["view"] == view]
            cell = counts(sel, gate)
            cell["registered"] = registered
            out["cohorts"][f"{cohort}/{view}/test"] = {
                "gate": cell,
                "v3_any": counts(sel, "v3_any"),
            }
    decisions = []
    for r in dev + test:
        if r["status"] != "measured":
            continue
        decisions.append(
            {
                "cohort": r["cohort"],
                "item_id": r["item_id"],
                "view": r["view"],
                "role": r["role"],
                "gate_fired": gate_fired(fired_rules(r), gate),
                "v3_any_fired": gate_fired(fired_rules(r), "v3_any"),
            }
        )
    decisions.sort(key=lambda d: (d["cohort"], d["item_id"], d["view"]))
    return {"result": out, "decisions": decisions}


def fmt(x: dict[str, Any]) -> str:
    recall = (
        "n/a"
        if x["recall"] is None
        else f"{x['true_positives']}/{x['positives']} = {x['recall']:.1%}"
    )
    precision = "n/a" if x["precision"] is None else f"{x['precision']:.1%}"
    return f"recall {recall}, precision {precision} (FA {x['false_alarms']}/{x['negatives']})"


def render(result: dict[str, Any]) -> str:
    lines = [
        f"# Tripwire rules v4 ({result['fingerprint']['system_id']}), gate `{result['gate']}`",
        "",
        "Confirmation gate over retained `tripwires-v3@d338116d6583` rule hits; gate selected "
        "on the by-task tune half only (PROTOCOL.md). $0, 0 model calls.",
        "",
        "| cohort | v4 gate | v3 ANY (same items) |",
        "|---|---|---|",
    ]
    for name, cell in result["cohorts"].items():
        lines.append(f"| {name} | {fmt(cell['gate'])} | {fmt(cell['v3_any'])} |")
    return "\n".join(lines) + "\n"


def publish(result: dict[str, Any], path: Path = SCOREBOARD) -> dict[str, int]:
    """Append v4 test rows only; identical re-publication is a no-op; changed metrics refuse."""
    import csv
    import datetime
    import io

    text = path.read_text()
    rows = list(csv.DictReader(io.StringIO(text)))
    columns = list(rows[0].keys()) if rows else []
    existing = {(r["key_id"], r["system"]): r for r in rows}
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = []
    for cohort in TEST_COHORTS:
        for view in ("full", "actions"):
            cell = result["cohorts"][f"{cohort}/{view}/test"]["gate"]
            n_items = cell["positives"] + cell["negatives"]
            row = {
                "date_utc": stamp,
                "key_id": cohort,
                "split": "test",
                "category": "all",
                "agent": "Deterministic tripwires",
                "system": f"{result['fingerprint']['system_id']}/{view}",
                "recall": str(cell["recall"]),
                "false_alarm_rate": str(cell["false_alarm_rate"]),
                "root_cause_match": "n/a",
                "paired_discrimination": "n/a",
                "cost_per_item_usd": "0.0",
                "n_items": str(cell["registered"]),
                "n_measured": str(n_items),
                "n_pairs": "0",
                "n_pairs_measured": "0",
                "status": (
                    "complete"
                    if n_items == cell["registered"]
                    else f"partial: {n_items} of {cell['registered']} measured"
                ),
                "notes": (
                    f"Frozen confirmation gate `{result['gate']}` over tripwires-v3@d338116d6583 "
                    f"rule hits, selected on TB4 DEV tune half only (seed {SEED}); {view} view; "
                    "descriptive replay on reused keys, exposure disclosed in splits/exposure.jsonl; "
                    "per-cohort precision/recall vs v3 in "
                    "research/experiments/tripwires-v4-precision-20261021/results.json"
                ),
            }
            key = (cohort, row["system"])
            prior = existing.get(key)
            if prior is not None:
                if (
                    prior["recall"] != row["recall"]
                    or prior["false_alarm_rate"] != row["false_alarm_rate"]
                ):
                    raise ValueError(f"{key}: existing metrics differ")
                continue
            fresh.append(row)
    if fresh:
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=columns)
        for row in fresh:
            writer.writerow(row)
        with path.open("a") as handle:
            handle.write(out.getvalue())
    return {"appended": len(fresh), "already_present": len(TEST_COHORTS) * 2 - len(fresh)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("split", help="print the frozen by-task assignment; reads retained predictions")
    sub.add_parser("fingerprint", help="print the frozen identity; reads no predictions")
    sub.add_parser("tune", help="select the gate on the tune half and write results-tune.json")
    score_parser = sub.add_parser("score", help="score the frozen gate once")
    score_parser.add_argument("--gate", required=True)
    score_parser.add_argument("--expect-fingerprint", required=True)
    publish_parser = sub.add_parser("publish", help="append v4 test rows from results.json")
    publish_parser.add_argument("--results", type=Path, default=HERE / "results.json")
    args = parser.parse_args()
    if args.command == "split":
        records = load_jsonl(V3_DIR / "predictions-dev.jsonl")
        print(json.dumps(split_tasks(tb4_tasks(records)), indent=1, sort_keys=True))
    elif args.command == "fingerprint":
        print(json.dumps(fingerprints(), indent=1, sort_keys=True))
    elif args.command == "tune":
        out = HERE / "results-tune.json"
        if out.exists():
            raise FileExistsError(f"refusing to overwrite retained output: {out.name}")
        result = tune()
        out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
        print(json.dumps(result, indent=1, sort_keys=True))
    elif args.command == "score":
        outputs = [HERE / "results.json", HERE / "predictions.jsonl", HERE / "report.md"]
        existing = [p for p in outputs if p.exists()]
        if existing:
            raise FileExistsError(
                f"refusing to overwrite retained outputs: {[p.name for p in existing]}"
            )
        scored = score(args.gate, args.expect_fingerprint)
        outputs[0].write_text(json.dumps(scored["result"], indent=1, sort_keys=True) + "\n")
        outputs[1].write_text(
            "".join(json.dumps(d, sort_keys=True) + "\n" for d in scored["decisions"])
        )
        outputs[2].write_text(render(scored["result"]))
        print(render(scored["result"]))
    else:
        result = json.loads(args.results.read_text())
        if result.get("schema") != SCHEMA or result.get("stage") != "score":
            raise ValueError(f"{args.results} is not a {SCHEMA} score result")
        print(json.dumps(publish(result)))


if __name__ == "__main__":
    sys.exit(main())
