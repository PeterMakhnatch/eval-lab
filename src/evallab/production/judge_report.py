"""Agreement report over submitted judge verdicts.

Reads the verdict store (never a label file) and answers: how often does the human agree with the
label, with each stored detector and with each reader model, and which runs are the disagreements?
Counts are ``k/n`` with a 95% Wilson interval. A run whose verdict was recorded after the judge had
already seen its label (re-judged, or judged again after an undo) is excluded from every agreement
count and listed separately. "Can't tell" verdicts and runs without a usable label are excluded from
the label comparison and counted.

The output holds per-run labels and real run ids: it is a local file, not something to commit.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from evallab.gold_certifier import wilson_interval

TARGET_SECONDS = 180.0


def interval(k: int, n: int) -> dict[str, Any]:
    """``{"k", "n", "rate", "low", "high"}``; rate and bounds are ``None`` when ``n == 0``."""
    if n == 0:
        return {"k": k, "n": n, "rate": None, "low": None, "high": None}
    low, high = wilson_interval(k, n)
    return {"k": k, "n": n, "rate": round(k / n, 4), "low": round(low, 4), "high": round(high, 4)}


def effective_records(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Verdict records that count: undone ones removed, one latest record per (judge, run)."""
    records: dict[str, dict[str, Any]] = {}
    undone: set[str] = set()
    for row in rows:
        if row.get("schema") == "judge-undo/v1":
            undone.add(str(row.get("undoes")))
        elif row.get("schema") == "judge-verdict/v1":
            records[str(row.get("id") or f"legacy-{len(records)}")] = dict(row)
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for key, record in records.items():
        if key in undone:
            continue
        pair = (str(record.get("judge")), str(record.get("run_id")))
        latest.pop(pair, None)  # a later record moves to the end: the list is in judging order
        latest[pair] = record
    return list(latest.values())


def _decided(record: Mapping[str, Any]) -> bool | None:
    """True if the judge called it a cheat, False if legitimate, None if unclear."""
    return {"cheat": True, "legitimate": False}.get(str(record.get("verdict")))


def _unit_name(detector: Mapping[str, Any]) -> str:
    model = detector.get("model")
    return f"{detector.get('name')} @ {model}" if model else str(detector.get("name"))


def _units(record: Mapping[str, Any]) -> tuple[dict[str, bool], dict[str, bool]]:
    """One alarm flag per (detector, reader) and one per reader model for a run.

    Two findings of the same detector and reader on one run count once (alarm wins), and a model
    alarms on a run if any of its detector readings does, so every sample is a distinct run.
    """
    units: dict[str, bool] = {}
    models: dict[str, bool] = {}
    for d in (record.get("revealed") or {}).get("detectors") or []:
        if d.get("verdict") not in ("alarm", "clear"):
            continue
        alarm = d["verdict"] == "alarm"
        units[_unit_name(d)] = units.get(_unit_name(d), False) or alarm
        if d.get("model"):
            models[str(d["model"])] = models.get(str(d["model"]), False) or alarm
    return units, models


def build_report(rows: Iterable[Mapping[str, Any]], *, judge: str | None = None) -> dict[str, Any]:
    records = [r for r in effective_records(rows) if judge is None or r.get("judge") == judge]
    tainted = [r for r in records if r.get("seen_label_before")]
    clean = [r for r in records if not r.get("seen_label_before")]
    unclear = [r for r in clean if _decided(r) is None]
    decided = [r for r in clean if _decided(r) is not None]
    labelled = [r for r in decided if r.get("agreement") is not None]
    label_k = sum(1 for r in labelled if r["agreement"])

    by_detector: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
    by_model: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
    for r in decided:
        judged_cheat = bool(_decided(r))
        units, models = _units(r)
        for unit, alarm in units.items():
            by_detector[unit].append((alarm, judged_cheat))
        for model, alarm in models.items():
            by_model[model].append((alarm, judged_cheat))

    def summarize(pairs: Sequence[tuple[bool, bool]]) -> dict[str, Any]:
        agree = sum(1 for alarm, cheat in pairs if alarm == cheat)
        alarms = [cheat for alarm, cheat in pairs if alarm]
        clears = [cheat for alarm, cheat in pairs if not alarm]
        return {
            "agree_with_judge": interval(agree, len(pairs)),
            "alarms_judge_confirms": interval(sum(alarms), len(alarms)),
            "clears_judge_confirms": interval(sum(1 for cheat in clears if not cheat), len(clears)),
        }

    strata: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in labelled:
        strata[str(r.get("stratum") or "unknown")].append(r)
    disagreements: list[dict[str, Any]] = []
    for r in decided:
        judged_cheat = bool(_decided(r))
        against = sorted(unit for unit, alarm in _units(r)[0].items() if alarm != judged_cheat)
        label_disagrees = r.get("agreement") is False
        if label_disagrees or against:
            disagreements.append(
                {
                    "blind_id": r.get("blind_id"),
                    "run_id": r.get("run_id"),
                    "queue": r.get("queue"),
                    "stratum": r.get("stratum"),
                    "judge_verdict": r.get("verdict"),
                    "confidence": r.get("confidence"),
                    "label": ((r.get("revealed") or {}).get("label") or {}).get("state"),
                    "label_disagrees": label_disagrees,
                    "detectors_disagreeing": against,
                }
            )
    disagreements.sort(
        key=lambda d: (
            not d["label_disagrees"],
            -len(d["detectors_disagreeing"]),
            str(d["blind_id"]),
        )
    )
    seconds = [float(r["seconds"]) for r in records if isinstance(r.get("seconds"), int | float)]
    return {
        "schema": "judge-report/v1",
        "judge": judge,
        "runs_judged": len(records),
        "excluded": {
            "seen_label_before": len(tainted),
            "unclear": len(unclear),
            "no_usable_label": len(decided) - len(labelled),
        },
        "agreement_with_label": interval(label_k, len(labelled)),
        "by_detector": {name: summarize(pairs) for name, pairs in sorted(by_detector.items())},
        "by_model": {name: summarize(pairs) for name, pairs in sorted(by_model.items())},
        "by_stratum": {
            name: interval(sum(1 for r in rs if r["agreement"]), len(rs))
            for name, rs in sorted(strata.items())
        },
        "by_confidence": {
            str(level): interval(
                sum(1 for r in labelled if r.get("confidence") == level and r["agreement"]),
                sum(1 for r in labelled if r.get("confidence") == level),
            )
            for level in (1, 2, 3)
        },
        "disagreements": disagreements,
        "time": {
            "n": len(seconds),
            "median_seconds": round(statistics.median(seconds), 1) if seconds else None,
            "within_target": interval(sum(1 for s in seconds if s <= TARGET_SECONDS), len(seconds)),
            "target_seconds": TARGET_SECONDS,
        },
        "reading_note": "Agreement is not accuracy: the label and the detectors are also imperfect. "
        "Intervals are 95% Wilson; with n below 30 they are wide.",
    }


def _fmt(item: Mapping[str, Any]) -> str:
    if not item["n"]:
        return "0/0"
    return f"{item['k']}/{item['n']} = {item['rate'] * 100:.1f}% ({item['low'] * 100:.1f}-{item['high'] * 100:.1f})"


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Judge agreement report",
        "",
        f"Runs judged: {report['runs_judged']} (judge: {report['judge'] or 'all'})",
        f"Agreement with the label: {_fmt(report['agreement_with_label'])}",
        "Excluded: " + ", ".join(f"{name} {count}" for name, count in report["excluded"].items()),
        "",
    ]
    for title, key in (("Detectors", "by_detector"), ("Models", "by_model")):
        lines += [f"## {title}", ""]
        if not report[key]:
            lines += ["none recorded", ""]
            continue
        lines += [
            "| name | agrees with judge | alarms the judge confirms | clears the judge confirms |",
            "|---|---|---|---|",
        ]
        for name, s in report[key].items():
            lines.append(
                f"| {name} | {_fmt(s['agree_with_judge'])} | {_fmt(s['alarms_judge_confirms'])} "
                f"| {_fmt(s['clears_judge_confirms'])} |"
            )
        lines.append("")
    lines += ["## Agreement with the label by stratum", ""]
    lines += [f"- {name}: {_fmt(v)}" for name, v in report["by_stratum"].items()] or ["none"]
    lines += ["", "## Agreement with the label by confidence", ""]
    lines += [f"- {name}: {_fmt(v)}" for name, v in report["by_confidence"].items()]
    time = report["time"]
    median = "NOT MEASURED" if time["median_seconds"] is None else f"{time['median_seconds']} s"
    lines += [
        "",
        "## Time per run",
        "",
        f"Median {median}; within {int(time['target_seconds'])} s: {_fmt(time['within_target'])}",
        "",
        "## Disagreements (label first, then most detectors)",
        "",
    ]
    if not report["disagreements"]:
        lines.append("none")
    for d in report["disagreements"]:
        lines.append(
            f"- {d['blind_id']} [{d['queue']}, {d['stratum']}] judge {d['judge_verdict']} "
            f"(conf {d['confidence']}), label {d['label']}"
            + (", disagrees with the label" if d["label_disagrees"] else "")
            + (
                f"; detectors disagreeing: {', '.join(d['detectors_disagreeing'])}"
                if d["detectors_disagreeing"]
                else ""
            )
        )
    lines += ["", report["reading_note"], ""]
    return "\n".join(lines)


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows
