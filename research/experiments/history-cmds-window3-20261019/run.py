"""One-shot blind test of the frozen dev-selected rule on ``mimo-fresh-window3``.

Declared before the first run (no reruns with changes):

* Detectors, unchanged from PR #1091: the dev-selected rule
  ``history_cmds > 0 OR tripwire_any`` and ``history_cmds > 0`` alone, from
  ``evallab.action_features`` (sha 88cf3b998fd3…); ``tripwire_any`` is the frozen
  evidence-only tripwire OR. The rule was selected on dev splits under tripwire rules
  v1. v2 only adds ``execute_bash``, and the window-3 traces use the mimoagent
  ``bash`` tool; the run asserts that no ``execute_bash`` call is present, so both
  versions give the same decisions on this key.
* Primary estimand: among v6 passes, recall on ``wrong_pass`` and false alarms on
  ``real_pass``. Secondary: any incorrect submission (``wrong_pass`` + ``not_a_pass``)
  vs ``real_pass``. Views: actions-only primary, full reported for parity. Wilson
  95% intervals. No fitting, thresholds or model calls.
"""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "research/answer-keys"))

from external_traces import file_digest, fired_rules, wilson  # noqa: E402

from evallab import action_features as af  # noqa: E402
from evallab import tripwires  # noqa: E402
from evallab.results_home import results_root  # noqa: E402

KEY = REPO / "research/answer-keys/mimo-fresh-window3"
FROZEN_ACTION_FEATURES = "88cf3b998fd3"


def record_scoreboard(result: dict[str, Any]) -> None:
    """Rows for the frozen rule and the tripwire OR (fail = wrong_pass + not_a_pass)."""
    import csv
    import datetime

    path = REPO / "research/qa-scoreboard/scoreboard.csv"
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        columns = reader.fieldnames
        existing = list(reader)
    if columns is None:
        raise ValueError("scoreboard header missing")
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = sum(result["counts"].values())
    systems = {
        "rule": (
            "Deterministic action features",
            f"action-rule@{result['action_features_sha256'][:12]}/history_cmds-or-tripwire",
        ),
        "tripwire_any": (
            "Deterministic tripwires",
            f"tripwires@{result['tripwires_sha256'][:12]}",
        ),
    }
    rows = []
    for view, detectors in result["views"].items():
        for detector, (agent, system) in systems.items():
            m = detectors[detector]
            wrong = m["wrong_pass_recall"]
            row = {
                "date_utc": stamp,
                "key_id": "mimo-fresh-window3",
                "split": "test",
                "category": "all",
                "agent": agent,
                "system": f"{system}/{view}",
                "recall": str(m["incorrect_recall"]["rate"]),
                "false_alarm_rate": str(m["real_pass_false_alarm"]["rate"]),
                "root_cause_match": "n/a",
                "paired_discrimination": "n/a",
                "cost_per_item_usd": "0.0",
                "n_items": str(n),
                "n_measured": str(n),
                "n_pairs": "0",
                "n_pairs_measured": "0",
                "status": "complete",
                "notes": f"One-shot blind test, frozen before scoring; {view} view; wrong-pass "
                f"recall {wrong['hits']}/{wrong['n']}; fail = wrong_pass + not_a_pass; "
                "research/experiments/history-cmds-window3-20261019/results.json; 0 model calls",
            }
            identity = ("key_id", "split", "category", "agent", "system")
            prior = [old for old in existing if all(old[k] == row[k] for k in identity)]
            if any(old[k] != row[k] for old in prior for k in ("recall", "false_alarm_rate")):
                raise ValueError(f"{row['system']}: existing metrics differ")
            if not prior:
                rows.append(row)
    with path.open("a", newline="") as stream:
        csv.DictWriter(stream, fieldnames=columns, lineterminator="\n").writerows(rows)


def main() -> None:
    if "--scoreboard" in sys.argv[1:]:
        record_scoreboard(json.loads((HERE / "results.json").read_text()))
        return
    warnings.filterwarnings("ignore", category=SyntaxWarning)
    sha = file_digest(REPO / "src/evallab/action_features.py")
    if not sha.startswith(FROZEN_ACTION_FEATURES):
        raise ValueError("action_features.py differs from the frozen #1091 version")
    root = results_root()
    items = [json.loads(line) for line in (KEY / "items.jsonl").read_text().splitlines()]
    rows = []
    for item in items:
        row: dict[str, Any] = {
            "item_id": item["item_id"],
            "source_label": item["source_label"],
            "source": item["source"],
        }
        for view, ref in (("actions", item["actions_only_ref"]), ("full", item["full_ref"])):
            doc = json.loads((root / ref["path"] / "agent/trajectory.json").read_text())
            names = {
                str(c.get("function_name") or "").lower()
                for s in doc["steps"]
                for c in s.get("tool_calls") or []
            }
            if "execute_bash" in names:
                raise ValueError(f"{item['item_id']}: execute_bash present; v1 and v2 could differ")
            steps = doc["steps"]
            agent = [s for s in steps if s["source"] == "agent"]
            rules = fired_rules(doc)
            feats = af.trace_features(steps, rules, tripwires.scan_trace(agent, steps=steps))
            events, _ = af.action_events(steps)
            first = next((e for e in events if e.kind == "history"), None)
            row[view] = {
                "history_cmds": bool(feats["history_cmds"]),
                "tripwire_any": bool(feats["tripwire_any"]),
                "rule": bool(feats["history_cmds"] or feats["tripwire_any"]),
                "tripwire_rules": rules,
                "history_evidence": None
                if first is None
                else {"step_id": first.step_id, "command": first.target},
            }
        rows.append(row)
    result: dict[str, Any] = {
        "schema": "evallab.window3_blind_rule/v1",
        "llm_calls": 0,
        "cost_usd": 0.0,
        "action_features_sha256": sha,
        "tripwires_sha256": file_digest(REPO / "src/evallab/tripwires.py"),
        "key_items_sha256": file_digest(KEY / "items.jsonl"),
        "counts": {
            label: sum(r["source_label"] == label for r in rows)
            for label in ("wrong_pass", "real_pass", "not_a_pass")
        },
        "views": {},
    }
    for view in ("actions", "full"):
        out = {}
        for detector in ("rule", "history_cmds", "tripwire_any"):

            def rate(label: set[str], rows=rows, view=view, detector=detector) -> dict[str, Any]:
                group = [r for r in rows if r["source_label"] in label]
                return wilson(sum(r[view][detector] for r in group), len(group))

            out[detector] = {
                "wrong_pass_recall": rate({"wrong_pass"}),
                "real_pass_false_alarm": rate({"real_pass"}),
                "incorrect_recall": rate({"wrong_pass", "not_a_pass"}),
                "not_a_pass_flag_rate": rate({"not_a_pass"}),
            }
        result["views"][view] = out
    (HERE / "results.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    (HERE / "predictions.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    )
    print(json.dumps(result["views"]["actions"], indent=1))


if __name__ == "__main__":
    main()
