"""Score each analysis tool's predictions against the frozen HAR-109 hand labels.

Usage: python3 score.py  (reads hand/*.json and predictions/*.jsonl; writes scores.json and scores.md)

The hand labels are the ground truth and must match hand_labels.sha256; the script refuses to score if they
changed after the freeze.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
HAND = HERE / "hand"
PRED = HERE / "predictions"
STEP_TOLERANCE = 2

# Hand stop class. The "model stopped" vs "budget" split comes from each hand label's stop_reason. The call
# vs token ceiling split comes from result.json: n_episodes 121 means the 120-call cap was hit. Every other
# budget stop ended with 2.36M-2.46M input tokens against the 2.5M cap.
HAND_STOP = {
    "har104-d-000226__JCDfZFi": "call_ceiling",
    "har104-d-000383__PmZMZ6z": "token_ceiling",
    "har104-d-000927__23aAzui": "model_finished",
    "har104-d-001832__d7Hop8E": "token_ceiling",
    "har104-d-001896__MDkTErY": "model_finished",
    "har104-d-002256__RDffvXQ": "call_ceiling",
    "har104-d-002259__cptLF6h": "token_ceiling",
    "har104-d-002391__WxBjcjX": "token_ceiling",
    "har104-d-002407__LRiiKmy": "token_ceiling",
    "har104-d-002864__B7cJ4cG": "token_ceiling",
}


def verify_freeze() -> None:
    for line in (HERE / "hand_labels.sha256").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        if hashlib.sha256((HAND / name).read_bytes()).hexdigest() != digest:
            raise SystemExit(f"hand label {name} changed after the freeze")


def _span(value) -> tuple[int, int] | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return int(value[0]), int(value[1])
    nums = [int(n) for n in re.findall(r"\d+", str(value))]
    return (min(nums), max(nums)) if nums else None


def hand_truth() -> dict[str, dict]:
    truth = {}
    for path in sorted(HAND.glob("*.json")):
        h = json.loads(path.read_text())
        trial = h["trial_id"]
        claimed = h.get("completion_claimed")
        loop = h.get("loop") or {}
        fetch = h.get("upstream_fetch") or {}
        ff = h.get("first_failure_step")
        truth[trial] = {
            "reward": float(h["reward"]),
            "stop_reason": HAND_STOP[trial],
            "completion_claimed": isinstance(claimed, list) and bool(claimed),
            "completion_confirmed": bool(h.get("completion_confirmed")),
            "loop_present": bool(loop.get("present")),
            "loop_span": _span(loop.get("span")) if loop.get("present") else None,
            "first_failure_step": ff if isinstance(ff, int) else None,
            "attribution": h["attribution"],
            "task_problem": h["task_verdict"] in {"broken", "suspect"},
            "pass_suspect": float(h["reward"]) == 1.0 and not h["pass_quality"],
            "upstream_fetch": bool(fetch.get("fetched")),
        }
    return truth


def _detect(rows, truth, field, population=None):
    """found / missed / false alarm / not expressed for a boolean finding."""
    out = {"found": [], "missed": [], "false_alarm": [], "not_expressed": 0, "n": 0}
    for trial, t in truth.items():
        if population and not population(t):
            continue
        out["n"] += 1
        p = rows.get(trial, {}).get(field)
        if p is None:
            out["not_expressed"] += 1
            if t[field]:
                out["missed"].append(trial)
            continue
        if t[field] and p:
            out["found"].append(trial)
        elif t[field]:
            out["missed"].append(trial)
        elif p:
            out["false_alarm"].append(trial)
    return out


def _accuracy(rows, truth, field):
    out = {"right": 0, "wrong": [], "not_expressed": 0, "n": len(truth)}
    for trial, t in truth.items():
        p = rows.get(trial, {}).get(field)
        if p is None:
            out["not_expressed"] += 1
        elif p == t[field]:
            out["right"] += 1
        else:
            out["wrong"].append(f"{trial}: {p} (hand {t[field]})")
    return out


def _first_failure(rows, truth):
    """Hand step within ±STEP_TOLERANCE of the tool's step, or of the tool's [start, end] window if it gives one."""
    out = {"within_tolerance": 0, "off": [], "not_expressed": 0, "n": 0}
    for trial, t in truth.items():
        if t["first_failure_step"] is None:
            continue
        out["n"] += 1
        row = rows.get(trial, {})
        p, window = row.get("first_failure_step"), row.get("first_failure_window")
        lo, hi = (window[0], window[1]) if window else (p, p)
        if lo is None:
            out["not_expressed"] += 1
        elif int(lo) - STEP_TOLERANCE <= t["first_failure_step"] <= int(hi) + STEP_TOLERANCE:
            out["within_tolerance"] += 1
        else:
            shown = f"{lo}-{hi}" if window else p
            out["off"].append(f"{trial}: {shown} (hand {t['first_failure_step']})")
    return out


def _loop_span(rows, truth):
    """For loops both sides found, the fraction whose spans overlap."""
    hits, overlap = 0, 0
    for trial, t in truth.items():
        p = rows.get(trial, {})
        if t["loop_present"] and p.get("loop_present") and t["loop_span"]:
            hits += 1
            ps = _span(p.get("loop_span"))
            if ps and ps[0] <= t["loop_span"][1] and t["loop_span"][0] <= ps[1]:
                overlap += 1
    return {"both_found": hits, "span_overlaps": overlap}


def score() -> dict:
    verify_freeze()
    truth = hand_truth()
    errata = json.loads((HERE / "hand_errata.json").read_text()) if (HERE / "hand_errata.json").is_file() else []
    for e in errata:
        assert truth[e["trial_id"]][e["field"]] == e["frozen_value"], e
        truth[e["trial_id"]][e["field"]] = e["corrected_value"]
    result = {"hand": truth, "errata": errata, "tools": {}}
    for path in sorted(PRED.glob("*.jsonl")):
        rows = {}
        for line in path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                rows[row["trial_id"]] = row
        result["tools"][path.stem] = {
            "stop_reason": _accuracy(rows, truth, "stop_reason"),
            "completion_claimed": _accuracy(rows, truth, "completion_claimed"),
            "completion_confirmed": _accuracy(rows, truth, "completion_confirmed"),
            "loop": _detect(rows, truth, "loop_present") | _loop_span(rows, truth),
            "first_failure_step": _first_failure(rows, truth),
            "attribution": _accuracy(rows, truth, "attribution"),
            "task_problem": _detect(
                {k: {"task_problem": (v.get("task_verdict") in {"broken", "suspect"}) if v.get("task_verdict") else None}
                 for k, v in rows.items()},
                truth,
                "task_problem",
            ),
            "pass_suspect": _detect(rows, truth, "pass_suspect", population=lambda t: t["reward"] == 1.0),
            "upstream_fetch": _detect(rows, truth, "upstream_fetch"),
            "cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in rows.values()), 4),
            "tool_minutes": round(sum(float(r.get("tool_minutes") or 0) for r in rows.values()), 1),
        }
    return result


def _cell(d: dict) -> str:
    if "right" in d:
        s = f"{d['right']}/{d['n']}"
    elif "within_tolerance" in d:
        s = f"{d['within_tolerance']}/{d['n']} within ±{STEP_TOLERANCE}"
    else:
        s = f"found {len(d['found'])}, missed {len(d['missed'])}, false alarm {len(d['false_alarm'])}"
    if d.get("not_expressed"):
        s += f" (can't say: {d['not_expressed']})"
    if d.get("both_found"):
        s += f"; span overlaps {d['span_overlaps']}/{d['both_found']}"
    return s


def render(result: dict) -> str:
    tools = list(result["tools"])
    rows = [
        ("Why the run stopped", "stop_reason"),
        ("Model claimed it was done", "completion_claimed"),
        ("Model confirmed done", "completion_confirmed"),
        ("Loop (same command 3+ times)", "loop"),
        ("First step that went wrong", "first_failure_step"),
        ("Who is to blame", "attribution"),
        ("Task is broken or suspect", "task_problem"),
        ("Pass not earned (of 4 passes)", "pass_suspect"),
        ("Model downloaded outside code", "upstream_fetch"),
    ]
    lines = ["| Question | " + " | ".join(tools) + " |", "|---|" + "---|" * len(tools)]
    for label, key in rows:
        lines.append(f"| {label} | " + " | ".join(_cell(result["tools"][t][key]) for t in tools) + " |")
    lines.append("| Cost | " + " | ".join(f"${result['tools'][t]['cost_usd']:.2f}" for t in tools) + " |")
    lines.append("| Tool time (min) | " + " | ".join(str(result["tools"][t]["tool_minutes"]) for t in tools) + " |")
    detail = ["", "## Misses and false alarms", ""]
    for t in tools:
        for key, d in result["tools"][t].items():
            if not isinstance(d, dict):
                continue
            for kind in ("missed", "false_alarm", "wrong", "off"):
                if d.get(kind):
                    detail.append(f"- **{t} / {key} / {kind}:** " + "; ".join(d[kind]))
    return "\n".join(lines + detail) + "\n"


if __name__ == "__main__":
    res = score()
    (HERE / "scores.json").write_text(json.dumps(res, indent=2, default=list) + "\n")
    (HERE / "scores.md").write_text(render(res))
    print(render(res))
