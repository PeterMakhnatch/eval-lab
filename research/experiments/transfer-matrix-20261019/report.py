"""Render ``results.json`` into ``report.md`` tables and the robust-feature verdicts.

Feature verdict rule. The *transfer* rule was written before the first test
numbers were read: a feature is *selected* when its univariate AUROC 95%
interval excludes 0.5 in the same direction on at least two dev splits and is
not significant in the opposite direction on any dev split; it *transfers* when
that also holds on at least two test splits. The non-transfer labels were split
after the first run (previously one "dataset artifact" bucket):
*direction flips* (significant in opposite directions on different keys),
*dataset-specific* (significant on one key, flat on another key where it is
available), *single key, transfer untestable* (significant on one key and absent
elsewhere), and *uninformative here*.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
KEYS = (
    "cheatbench-actions-v1",
    "hacktrace-act-v1",
    "trajectory-judge-400",
    "trace-paired-v1",
    "mimo-v7-canonical-pass70",
)
SHORT = {
    "cheatbench-actions-v1": "CheatBench",
    "hacktrace-act-v1": "HackTrace",
    "trajectory-judge-400": "TrajJudge",
    "trace-paired-v1": "Paired",
    "mimo-v7-canonical-pass70": "MiMo70",
}


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.0f}%"


def num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def ci(values: list[float] | None, fmt=num) -> str:
    return "" if not values else f" [{fmt(values[0])}, {fmt(values[1])}]"


def direction(cell: dict[str, Any] | None) -> int:
    """+1 / -1 when the AUROC interval excludes 0.5, else 0."""
    if not cell or not cell.get("ci95") or not cell["ci95"].get("auroc"):
        return 0
    low, high = cell["ci95"]["auroc"]
    return 1 if low > 0.5 else -1 if high < 0.5 else 0


def verdicts(univariate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for name, cells in univariate.items():
        dev = {k: direction(cells.get(f"{k}/dev")) for k in KEYS if f"{k}/dev" in cells}
        test = {k: direction(cells.get(f"{k}/test")) for k in KEYS if f"{k}/test" in cells}
        signs = {d for d in list(dev.values()) + list(test.values()) if d}
        keys_sig = {k for k, d in dev.items() if d} | {k for k, d in test.items() if d}
        verdict = "uninformative here"
        for sign in (1, -1):
            dev_hits = [k for k, d in dev.items() if d == sign]
            test_hits = [k for k, d in test.items() if d == sign]
            if len(dev_hits) >= 2 and -sign not in dev.values():
                verdict = (
                    "transfers"
                    if len(test_hits) >= 2 and -sign not in test.values()
                    else "selected on dev, not confirmed on test"
                )
        if verdict == "uninformative here":
            available = [k for k in KEYS if cells.get(f"{k}/dev") or cells.get(f"{k}/test")]
            if len(signs) == 2:
                verdict = "direction flips (dataset artifact)"
            elif len(keys_sig) == 1:
                verdict = (
                    "dataset-specific (flat elsewhere)"
                    if len(available) > 1
                    else "single key, transfer untestable"
                )
            elif len(keys_sig) >= 2:
                verdict = "same direction on 2+ keys, failed the dev selection rule"
        out[name] = {
            "dev": dev,
            "test": test,
            "verdict": verdict,
            "significant_keys": sorted(keys_sig),
        }
    return out


def main() -> None:
    result = json.loads((HERE / "results.json").read_text())
    lines = ["# Transfer matrix — generated tables", ""]
    lines += ["## Populations", "", "| key/split | n | positives | view |", "|---|---:|---:|---|"]
    for name, p in result["population"].items():
        lines.append(f"| {name} | {p['n']} | {p['n_pos']} | {', '.join(p['view'])} |")
    lines += [
        "",
        "## Feature-family availability (N/A = no item in the key has an opportunity)",
        "",
    ]
    families = list(next(iter(result["feature_availability"].values())))
    lines += [
        "| family | " + " | ".join(SHORT[k] for k in KEYS) + " |",
        "|---|" + "---|" * len(KEYS),
    ]
    for family in families:
        cells = ["yes" if result["feature_availability"][k][family] else "N/A" for k in KEYS]
        lines.append(f"| {family} | " + " | ".join(cells) + " |")
    lines += ["", "## (a) Tripwires alone, test splits", ""]
    lines += [
        "| key | variant | recall | false alarms | AUROC |",
        "|---|---|---|---|---|",
    ]
    for key in KEYS:
        for variant, v in result["tripwires"][key].items():
            ev = v["evaluation"]
            lines.append(
                f"| {SHORT[key]} | {variant} | {v['hits_pos']}/{ev['n_pos']} = {pct(v['recall'])}"
                f"{ci(v['recall_wilson95'], pct)} | {v['hits_neg']}/{ev['n_neg']} = {pct(v['false_alarm'])}"
                f"{ci(v['false_alarm_wilson95'], pct)} | {num(ev['auroc'])}{ci(ev['ci95'].get('auroc'))} |"
            )
    rule = result["rule_list"]
    lines += [
        "",
        f"## (c) Dev-selected rule list: alarm if any of {', '.join(rule['features'])} > 0",
        "",
        "| key | recall | false alarms |",
        "|---|---|---|",
    ]
    for key in KEYS:
        r = rule["keys"][key]
        lines.append(
            f"| {SHORT[key]} | {r['hits_pos']}/{r['n_pos']} = {pct(r['recall'])}{ci(r['recall_wilson95'], pct)} "
            f"| {r['hits_neg']}/{r['n_neg']} = {pct(r['false_alarm'])}{ci(r['false_alarm_wilson95'], pct)} |"
        )
    lines += ["", "## (b) Logistic regression: train on X dev → test on Y test", ""]
    for metric, label in (
        ("auroc", "AUROC"),
        ("recall_at_2pct_fpr", "Recall at 2% false alarms (test-negative threshold)"),
        ("recall_at_5pct_fpr", "Recall at 5% false alarms (test-negative threshold)"),
    ):
        lines += [f"### {label}", ""]
        lines += ["| train ↓ / test → | " + " | ".join(SHORT[k] for k in KEYS) + " |"]
        lines += ["|---|" + "---|" * len(KEYS)]
        fmt = num if metric == "auroc" else pct
        for train, row in result["matrix"].items():
            cells = [f"{fmt(row[k][metric])}{ci(row[k]['ci95'].get(metric), fmt)}" for k in KEYS]
            lines.append(f"| {SHORT[train]} | " + " | ".join(cells) + " |")
        lines.append("")
    lines += ["### Frozen dev threshold (5% dev false alarms), applied unchanged", ""]
    lines += ["| train ↓ / test → | " + " | ".join(SHORT[k] for k in KEYS) + " |"]
    lines += ["|---|" + "---|" * len(KEYS)]
    for train, row in result["matrix"].items():
        cells = []
        for k in KEYS:
            f = row[k]["frozen_dev_threshold"]["5pct"]
            cells.append(f"R {pct(f['recall'])} / FA {pct(f['false_alarm'])}")
        lines.append(f"| {SHORT[train]} | " + " | ".join(cells) + " |")
    lines += ["", "## Within-stratum AUROC (benchmark family / editable-test stratum)", ""]
    strata = result["strata"]
    trains = list(result["matrix"])
    focus = (
        "history_cmds",
        "log_calls",
        "read_test",
        "test_runs",
        "write_source",
        "literal_branches",
    )
    header = [f"LR train={SHORT[t]}" for t in trains] + list(focus)
    lines += [
        "| stratum | pos/neg | " + " | ".join(header) + " |",
        "|---|---|" + "---|" * len(header),
    ]
    for name, cell in strata.items():
        cells = [
            f"{num(cell['models'][t]['auroc'])}{ci(cell['models'][t]['ci95'].get('auroc'))}"
            for t in trains
        ]
        for feature in focus:
            f = cell["features"].get(feature)
            cells.append("N/A" if f is None else f"{num(f['auroc'])}{ci(f['ci95'].get('auroc'))}")
        lines.append(f"| {name} | {cell['n_pos']}/{cell['n_neg']} | " + " | ".join(cells) + " |")
    lines += ["", "## Univariate feature AUROC (test splits) and verdicts", ""]
    v = verdicts(result["univariate"])
    lines += ["| feature | " + " | ".join(SHORT[k] for k in KEYS) + " | verdict |"]
    lines += ["|---|" + "---|" * (len(KEYS) + 1)]
    for name, cells in result["univariate"].items():
        row = []
        for k in KEYS:
            cell = cells.get(f"{k}/test")
            row.append(
                "N/A" if cell is None else f"{num(cell['auroc'])}{ci(cell['ci95'].get('auroc'))}"
            )
        lines.append(f"| {name} | " + " | ".join(row) + f" | {v[name]['verdict']} |")
    lines += ["", "## Univariate feature AUROC (dev splits)", ""]
    dev_keys = [k for k in KEYS if any(f"{k}/dev" in c for c in result["univariate"].values())]
    lines += ["| feature | " + " | ".join(SHORT[k] for k in dev_keys) + " |"]
    lines += ["|---|" + "---|" * len(dev_keys)]
    for name, cells in result["univariate"].items():
        row = []
        for k in dev_keys:
            cell = cells.get(f"{k}/dev")
            row.append(
                "N/A" if cell is None else f"{num(cell['auroc'])}{ci(cell['ci95'].get('auroc'))}"
            )
        lines.append(f"| {name} | " + " | ".join(row) + " |")
    (HERE / "report.md").write_text("\n".join(lines) + "\n")
    (HERE / "verdicts.json").write_text(json.dumps(v, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
