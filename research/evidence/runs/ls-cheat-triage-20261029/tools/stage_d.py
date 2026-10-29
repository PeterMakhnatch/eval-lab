#!/usr/bin/env python3
"""LS-CHEAT triage, stage D: do the cards' own ``Open more`` commands work? No label, no model call.

Runs every card's two commands exactly as printed (``evallab trace card <run>`` and ``evallab qa-data window <run> --around N
--radius 3``) against the stored DEV runs and records mechanical outcomes for all 154 cards; the 10-card preregistered sample
(``sha256("20261029:" + run_id)``, 5 facets-only + 5 multi-detector) is the subset a human reads. Output: aggregates only.
Per-run rows stay in /tmp.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = Path(
    os.environ.get("EVALLAB_REPO")
    or subprocess.run(
        ["git", "-C", str(HERE), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    ).stdout.strip()
)
PRIVATE = Path("/tmp/ls-cheat-triage-20261029")
RESULTS = HERE.parent / "results"
ENV = {**os.environ, "EVALLAB_DISABLE_LOCAL_DOCKER": "1", "PYTHONWARNINGS": "ignore"}


def run(argv: list[str]) -> tuple[int, float, str]:
    started = time.monotonic()
    done = subprocess.run(["uv", "run", "--offline", "evallab", *argv], capture_output=True, text=True, cwd=REPO, env=ENV)
    return done.returncode, time.monotonic() - started, done.stdout


def quartiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    pick = lambda q: round(ordered[int(q * (len(ordered) - 1))], 2)  # noqa: E731
    return {"min": pick(0), "median": pick(0.5), "p90": pick(0.9), "max": pick(1)}


def one(card: dict[str, Any]) -> dict[str, Any]:
    run_id = card["run_id"]
    steps = sorted({s for a in card["alarms"] for s in a["steps"]})
    rc_c, sec_c, out_c = run(["trace", "card", run_id])
    rc_w, sec_w, out_w = run(["qa-data", "window", run_id, "--around", str(card["around"]), "--radius", str(card["radius"])])
    row: dict[str, Any] = {"run_id": run_id, "rc_card": rc_c, "sec_card": sec_c, "rc_window": rc_w, "sec_window": sec_w}
    head = re.search(r"steps=(\d+) agent_steps=(\d+) actor_inputs_available=(\w+)", out_c)
    if head:
        row.update(card_steps=int(head.group(1)), card_agent_steps=int(head.group(2)), card_actor_inputs=head.group(3))
        row["cited_past_card_steps"] = sum(s >= int(head.group(1)) for s in steps)
        row["cited_past_card_agent_steps"] = sum(s >= int(head.group(2)) for s in steps)
    alarm_steps = re.search(r"alarm steps \(detectors \w+\): (.*)", out_c)
    row["card_lists_alarm_steps"] = bool(alarm_steps)
    row["card_alarm_lines"] = len(re.findall(r"^- \S+ \S+ \(.*?\): ALARM", out_c, re.M))
    row["card_has_flags_line"] = "flags:" in out_c
    try:
        doc = json.loads(out_w)
        data = doc["data"]
        got = data.get("steps") or []
        idx = [s["step_index"] for s in got]
        row.update(
            window_ok=bool(doc.get("ok")), window_steps=len(got), window_has_around=card["around"] in idx,
            window_truncated=bool(data.get("truncated")),
            window_text_steps=sum(bool(s.get("content_text") or s.get("command_or_action")) for s in got),
            window_obs_steps=sum(bool(s.get("observation_preview")) for s in got),
            window_roles=sorted({str(s.get("role")) for s in got}),
            window_tokens_est=(doc.get("meta") or {}).get("tokens_est"),
        )
    except (ValueError, KeyError, TypeError):
        row["window_ok"] = False
    return row


def main() -> None:
    cards = json.loads((PRIVATE / "stage_a_cards.json").read_text())
    key = lambda c: hashlib.sha256(f"20261029:{c['run_id']}".encode()).hexdigest()  # noqa: E731
    only = [c for c in cards if {a["detector"] for a in c["alarms"]} == {"facets"}]
    multi = [c for c in cards if {a["detector"] for a in c["alarms"]} != {"facets"}]
    sample = {c["run_id"]: stratum for stratum, group in (("S1", only), ("S2", multi)) for c in sorted(group, key=key)[:5]}
    rows = [one(c) for c in cards]
    (PRIVATE / "stage_d_rows.json").write_text(json.dumps(rows, indent=1))
    n = len(rows)
    doc: dict[str, Any] = {
        "stage": "D (open-more commands; no label)",
        "cards": n,
        "trace_card_exit_0": sum(r["rc_card"] == 0 for r in rows),
        "window_exit_0": sum(r["rc_window"] == 0 for r in rows),
        "window_ok_json": sum(bool(r.get("window_ok")) for r in rows),
        "window_contains_the_around_step": sum(bool(r.get("window_has_around")) for r in rows),
        "window_truncated": sum(bool(r.get("window_truncated")) for r in rows),
        "window_steps_returned": quartiles([r["window_steps"] for r in rows if "window_steps" in r]),
        "window_steps_with_text_or_action": sum(r.get("window_text_steps", 0) for r in rows),
        "window_steps_with_observation_preview": sum(r.get("window_obs_steps", 0) for r in rows),
        "window_roles": dict(sorted(
            __import__("collections").Counter(x for r in rows for x in r.get("window_roles", [])).items())),
        "seconds_trace_card": quartiles([r["sec_card"] for r in rows]),
        "seconds_window": quartiles([r["sec_window"] for r in rows]),
        "trace_card_actor_inputs_available": dict(sorted(
            __import__("collections").Counter(r.get("card_actor_inputs", "missing") for r in rows).items())),
        "cards_whose_cited_steps_pass_the_trace_card_step_count": sum(r.get("cited_past_card_steps", 0) > 0 for r in rows),
        "cards_whose_cited_steps_pass_the_trace_card_agent_step_count": sum(r.get("cited_past_card_agent_steps", 0) > 0 for r in rows),
        "trace_card_lists_alarm_steps": sum(r["card_lists_alarm_steps"] for r in rows),
        "trace_card_alarm_lines_median": statistics.median(r["card_alarm_lines"] for r in rows),
        "sample10_stratum_sizes": dict(sorted(__import__("collections").Counter(sample.values()).items())),
        "sample10_mechanical": {
            "trace_card_exit_0": sum(r["rc_card"] == 0 for r in rows if r["run_id"] in sample),
            "window_contains_the_around_step": sum(bool(r.get("window_has_around")) for r in rows if r["run_id"] in sample),
            "window_text_steps_per_card_min": min(r.get("window_text_steps", 0) for r in rows if r["run_id"] in sample),
        },
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "stage_d.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(json.dumps(doc, indent=1, sort_keys=True))


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    main()
