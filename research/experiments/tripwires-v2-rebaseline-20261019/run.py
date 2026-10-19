"""Re-baseline every trace key under tripwire rules v2, beside the frozen v1 results.

v1 is the committed output of ``tripwires@731d849f84c0`` (the per-key
``predictions.jsonl`` written by ``external_traces.py`` / ``internal_traces.py``),
read unchanged. v2 is the same evidence-only OR (``external_traces.fired_rules``)
run by the current code, whose only behavioral change is that ``execute_bash``
counts as a shell tool. Inputs are the published full and actions-only views; no
model is called and no submitted code is executed.

``--scoreboard`` appends the v2 rows for every key and the v1 rows that were never
written (the internal keys), refusing to overwrite changed metrics.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import sys
import warnings
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
KEYS_DIR = REPO / "research/answer-keys"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(KEYS_DIR))

from external_traces import file_digest, fired_rules, wilson  # noqa: E402

from evallab import tripwires  # noqa: E402
from evallab.results_home import results_root  # noqa: E402

V1_SHA = "731d849f84c0d3824057e1d90f217bd96c889639b217096e9d10eb9b5758e432"
INTERNAL_KEYS = "2026-10-19/internal-traces/keys"
#: key -> (items manifest, committed v1 predictions, frozen test slots)
KEYS: dict[str, tuple[Path, Path, int]] = {
    "cheatbench-actions-v1": (
        KEYS_DIR / "cheatbench-actions-v1/items.jsonl",
        KEYS_DIR / "cheatbench-actions-v1/predictions.jsonl",
        150,
    ),
    "hacktrace-act-v1": (
        KEYS_DIR / "hacktrace-act-v1/items.jsonl",
        KEYS_DIR / "hacktrace-act-v1/predictions.jsonl",
        150,
    ),
    "trajectory-judge-400": (
        Path(INTERNAL_KEYS),
        KEYS_DIR / "internal-views/predictions.jsonl",
        188,
    ),
    "trace-paired-v1": (Path(INTERNAL_KEYS), KEYS_DIR / "internal-views/predictions.jsonl", 27),
    "mimo-v7-canonical-pass70": (
        KEYS_DIR / "mimo-v7-canonical-pass70/items.jsonl",
        KEYS_DIR / "internal-views/predictions.jsonl",
        70,
    ),
}


def jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_items(key: str, root: Path) -> list[dict[str, Any]]:
    manifest = KEYS[key][0]
    if manifest == Path(INTERNAL_KEYS):
        manifest = root / INTERNAL_KEYS / "full" / key / "items.jsonl"
    return [i for i in jsonl(manifest) if i["split"] == "test"]


def v1_predictions(key: str) -> dict[str, dict[str, list[str]]]:
    rows = jsonl(KEYS[key][1])
    return {
        r["item_id"]: {"full": r["full"], "actions": r["actions"]}
        for r in rows
        if r.get("dataset", key) == key and r["split"] == "test"
    }


def rates(rows: list[dict[str, Any]], version: str, view: str) -> dict[str, Any]:
    positive = [r for r in rows if r["positive"]]
    negative = [r for r in rows if not r["positive"]]
    return {
        "recall": wilson(sum(bool(r[version][view]) for r in positive), len(positive)),
        "false_alarm": wilson(sum(bool(r[version][view]) for r in negative), len(negative)),
        "rule_hits": dict(sorted(Counter(x for r in rows for x in r[version][view]).items())),
    }


def rebaseline(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    out: dict[str, Any] = {}
    predictions = []
    for key in KEYS:
        old = v1_predictions(key)
        rows = []
        for item in test_items(key, root):
            new = {}
            for view, ref in (("full", item["trial_ref"]), ("actions", item["actions_only_ref"])):
                doc = json.loads((root / ref["path"] / "agent/trajectory.json").read_text())
                new[view] = fired_rules(doc)
            rows.append(
                {
                    "key": key,
                    "item_id": item["item_id"],
                    "positive": item["expected_verdict"] == "fail",
                    "v1": old[item["item_id"]],
                    "v2": new,
                }
            )
        if len(rows) != len(old):
            raise ValueError(f"{key}: {len(rows)} measured items vs {len(old)} v1 predictions")
        out[key] = {
            "n_slots": KEYS[key][2],
            "n_measured": len(rows),
            "views": {
                view: {
                    "v1": rates(rows, "v1", view),
                    "v2": rates(rows, "v2", view),
                    "decision_flips": [
                        {
                            "item_id": r["item_id"],
                            "positive": r["positive"],
                            "v1": r["v1"][view],
                            "v2": r["v2"][view],
                        }
                        for r in rows
                        if bool(r["v1"][view]) != bool(r["v2"][view])
                    ],
                    "rule_set_changes": sum(
                        sorted(r["v1"][view]) != sorted(r["v2"][view]) for r in rows
                    ),
                }
                for view in ("full", "actions")
            },
        }
        predictions.extend(rows)
    return out, predictions


def record_scoreboard(result: dict[str, Any]) -> None:
    path = REPO / "research/qa-scoreboard/scoreboard.csv"
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        columns = reader.fieldnames
        existing = list(reader)
    if columns is None:
        raise ValueError("scoreboard header missing")
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for key, cell in result["keys"].items():
        for view, metrics in cell["views"].items():
            for version, sha in (("v1", V1_SHA), ("v2", result["tripwires_sha256"])):
                m = metrics[version]
                measured = m["recall"]["n"] + m["false_alarm"]["n"]
                row = {
                    "date_utc": stamp,
                    "key_id": key,
                    "split": "test",
                    "category": "all",
                    "agent": "Deterministic tripwires",
                    "system": f"tripwires@{sha[:12]}/{view}",
                    "recall": str(m["recall"]["rate"]),
                    "false_alarm_rate": str(m["false_alarm"]["rate"]),
                    "root_cause_match": "n/a",
                    "paired_discrimination": "n/a",
                    "cost_per_item_usd": "0.0",
                    "n_items": str(cell["n_slots"]),
                    "n_measured": str(measured),
                    "n_pairs": "0",
                    "n_pairs_measured": "0",
                    "status": "complete"
                    if measured == cell["n_slots"]
                    else f"partial: {measured} of {cell['n_slots']} measured",
                    "notes": f"Frozen evidence-only OR, rules {version}; {view} view; no tuning or "
                    "model calls; Wilson intervals, per-item flips in "
                    "research/experiments/tripwires-v2-rebaseline-20261019/results.json; "
                    "source negatives are not correctness certificates",
                }
                identity = ("key_id", "split", "category", "agent", "system")
                prior = [old for old in existing if all(old[k] == row[k] for k in identity)]
                if any(
                    old[k] != row[k]
                    for old in prior
                    for k in ("recall", "false_alarm_rate", "n_measured")
                ):
                    raise ValueError(f"{key} {row['system']}: existing metrics differ")
                if not prior:
                    rows.append(row)
    with path.open("a", newline="") as stream:
        csv.DictWriter(stream, fieldnames=columns, lineterminator="\n").writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scoreboard", action="store_true")
    args = parser.parse_args()
    if args.scoreboard:
        record_scoreboard(json.loads((HERE / "results.json").read_text()))
        return
    warnings.filterwarnings("ignore", category=SyntaxWarning)
    keys, predictions = rebaseline(results_root())
    result = {
        "schema": "evallab.tripwires_rebaseline/v1",
        "llm_calls": 0,
        "cost_usd": 0.0,
        "rules_version": tripwires.RULES_VERSION,
        "tripwires_sha256": file_digest(REPO / "src/evallab/tripwires.py"),
        "edit_signals_sha256": file_digest(REPO / "src/evallab/edit_signals.py"),
        "v1_tripwires_sha256": V1_SHA,
        "split": "test",
        "keys": keys,
        "interval_caveat": "Wilson intervals; descriptive, not cluster-adjusted.",
    }
    (HERE / "results.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    (HERE / "predictions.jsonl").write_text(
        "".join(json.dumps(p, sort_keys=True) + "\n" for p in predictions)
    )
    lines = [
        "# Tripwire rules v1 vs v2, test splits",
        "",
        f"v1 = `tripwires@{V1_SHA[:12]}` (committed predictions); "
        f"v2 = `tripwires@{result['tripwires_sha256'][:12]}` (RULES_VERSION 2: `execute_bash` is a shell tool).",
        "",
        "| key | view | v1 recall | v2 recall | v1 false alarms | v2 false alarms | decision flips | rule-set changes |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for key, cell in keys.items():
        for view, m in cell["views"].items():

            def fmt(x: dict[str, Any]) -> str:
                lo, hi = x["wilson_95"] or (0, 0)
                return f"{x['hits']}/{x['n']} = {100 * (x['rate'] or 0):.1f}% [{100 * lo:.1f}, {100 * hi:.1f}]"

            lines.append(
                f"| {key} | {view} | {fmt(m['v1']['recall'])} | {fmt(m['v2']['recall'])} | "
                f"{fmt(m['v1']['false_alarm'])} | {fmt(m['v2']['false_alarm'])} | "
                f"{len(m['decision_flips'])} | {m['rule_set_changes']} |"
            )
    lines += ["", "Decision flips (actions view):", ""]
    for key, cell in keys.items():
        for flip in cell["views"]["actions"]["decision_flips"]:
            lines.append(
                f"- {key} `{flip['item_id']}` ({'positive' if flip['positive'] else 'negative'}): "
                f"{flip['v1'] or '[]'} → {flip['v2']}"
            )
    (HERE / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
