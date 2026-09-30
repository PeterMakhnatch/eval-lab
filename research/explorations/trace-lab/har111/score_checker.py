"""Score checker outputs against the frozen HAR-111 hand labels.

Usage: python3 score_checker.py OUT_DIR [--field label|rule_label]

Refuses to score if the hand labels changed after the freeze. Reports a 3x3 confusion matrix, exact agreement,
the gate (catches 002259 and 002407; under 1 in 4 'broken' calls wrong), and flagged-vs-sound as a binary
view (suspect+broken vs sound).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
LABELS = ("sound", "suspect", "broken")
MUST_CATCH = ("format-code-task-002259", "format-code-task-002407")


def frozen_labels(label_set: str = "hand") -> dict[str, str]:
    """label_set: hand (the 30), hand_holdout (the 20), or hand_rater2 (second rater on the 30)."""
    out = {}
    for line in (HERE / f"{label_set}.sha256").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        path = HERE / label_set / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise SystemExit(f"{name} changed after the freeze")
        out[path.stem] = json.loads(path.read_text())["label"]
    return out


def score(out_dir: Path, field: str, label_set: str = "hand") -> dict:
    hand = frozen_labels(label_set)
    pred = {}
    for task in hand:
        path = out_dir / f"{task}.json"
        pred[task] = json.loads(path.read_text()).get(field) if path.is_file() else None
    matrix = {h: {p: 0 for p in (*LABELS, None)} for h in LABELS}
    for task, h in hand.items():
        matrix[h][pred[task]] += 1
    exact = sum(pred[t] == h for t, h in hand.items())
    broken_calls = [t for t, p in pred.items() if p == "broken"]
    broken_wrong = [t for t in broken_calls if hand[t] == "sound"]
    flagged = {t for t, p in pred.items() if p in {"suspect", "broken"}}
    truly = {t for t, h in hand.items() if h in {"suspect", "broken"}}
    caught = {t: pred[t] in {"suspect", "broken"} for t in MUST_CATCH if t in hand}
    return {
        "field": field,
        "n": len(hand),
        "exact": exact,
        "matrix": matrix,
        "caught": caught,
        "broken_calls": len(broken_calls),
        "broken_calls_on_sound_tasks": broken_wrong,
        "gate_pass": all(caught.values())
        and (not broken_calls or len(broken_wrong) / len(broken_calls) < 0.25),
        "flag_found": sorted(flagged & truly),
        "flag_missed": sorted(truly - flagged),
        "flag_false_alarm": sorted(flagged - truly),
        "disagreements": {t: f"hand {hand[t]} / checker {pred[t]}" for t in hand if pred[t] != hand[t]},
    }


def render(s: dict) -> str:
    rows = ["| hand \\ checker | sound | suspect | broken | none |", "|---|---|---|---|---|"]
    for h in LABELS:
        m = s["matrix"][h]
        rows.append(f"| {h} | {m['sound']} | {m['suspect']} | {m['broken']} | {m[None]} |")
    return "\n".join(
        [
            f"### `{s['field']}`: exact {s['exact']}/{s['n']}",
            "",
            *rows,
            "",
            f"- Must-catch: {s['caught']}",
            f"- 'broken' calls: {s['broken_calls']}; of them on hand-sound tasks: {s['broken_calls_on_sound_tasks'] or 'none'}",
            f"- **Gate pass: {s['gate_pass']}**",
            f"- Flagged (suspect or broken) vs sound: found {len(s['flag_found'])}, missed {len(s['flag_missed'])} "
            f"{s['flag_missed']}, false alarms {len(s['flag_false_alarm'])} {s['flag_false_alarm']}",
            "",
            "Disagreements:",
            *[f"- {t}: {v}" for t, v in s["disagreements"].items()],
            "",
        ]
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--field", default="label")
    ap.add_argument("--labels", default="hand")
    a = ap.parse_args()
    print(render(score(a.out_dir, a.field, a.labels)))
