#!/usr/bin/env python3
"""B7 smoke: replay the 150 labelled Goodfire DEV runs through ``cascade_stream``.

No model call: the detector runs without ``--allow-model`` on a private copy of the
CM-11b Kimi verdict cache, so every judge miss is an unavailable judgment (abstain),
never a negative. Two inputs, same detector, same cache:

``harness``  the runs exactly as ``evallab trace run --runs goodfire`` hands them to a
             detector (``resolve_runs``: Run Atlas ``cloud_doc`` steps).
``replay``   the step conversion the stored v8 replay (#1353, CM-11b ``e2e.py``) fed
             the worker: native assistant messages and tool rows from the same
             database rows (``replay_steps`` below, a copy of the CM-2 ``lib.load_steps``),
             checked byte-for-byte against each stored row's ``steps_sha256``.

Each run's record is compared with the stored v8 row (``comparisons/cm15/dev-v8/
after-goodfire.jsonl``): verdict, the card set (rule, flagged step, status, card
prefix) and the first alarm's ``card_prefix`` and agent turns, the turns counted
on the harness steps exactly as ID-CM-1 (``latency.py``) counts them. Labels
(stratum) are read only at scoring time. Writes counts and run-id hashes only.

    stream_smoke.py prepare   # copy cache + stored rows into the private state dir, sha256 receipt
    stream_smoke.py run       # both arms -> results/smoke.json, results/per-run.jsonl
    stream_smoke.py cards     # replay-arm runs whose cards differ: Stage 1 only (no judge), rule diff
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import shutil
import sqlite3
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
sys.path.insert(0, str(REPO / "src"))
os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
sys.dont_write_bytecode = True

COMPARISONS = Path.home() / ".local/state/evallab-prod-live/run-atlas/comparisons"
STATE = COMPARISONS / "b7-stream"
STORED_SRC = COMPARISONS / "cm15/dev-v8/after-goodfire.jsonl"
CACHE_SRC = Path("/tmp/cm11b/c-goodfire.jsonl")  # the CM-11b Goodfire Kimi verdict cache
STORED = STATE / "after-goodfire.jsonl"
CACHE = STATE / "judge-cache-goodfire.jsonl"
RECEIPT = STATE / "prepare.json"
RESULTS = HERE.parent / "results"
JOBS = 4


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hid(trace_id: str) -> str:
    return hashlib.sha256(trace_id.encode()).hexdigest()[:16]


def prepare() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {}
    for src, dest in ((STORED_SRC, STORED), (CACHE_SRC, CACHE)):
        if dest.exists() and sha(dest) != sha(src):
            raise SystemExit(f"{dest} exists with different bytes")
        shutil.copyfile(src, dest)
        out[dest.name] = {"source": str(src), "sha256": sha(dest), "bytes": dest.stat().st_size}
    snapshot = Path.home() / ".local/state/evallab-prod-live/trace-harness/sources/cm11b/manifest.json"
    pinned = json.loads(snapshot.read_text())["files"]["after-goodfire.jsonl"]["sha256"]
    out["stored_rows_match_harness_snapshot"] = pinned == out[STORED.name]["sha256"]
    RECEIPT.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(json.dumps(out, indent=1))


# ---------------------------------------------------------------- replay input


def replay_steps(con: sqlite3.Connection, trace_id: str) -> list[dict[str, Any]]:
    """CM-2 ``lib.load_steps``: native conversation -> ATIF-like steps (no label columns)."""
    rows = con.execute(
        "select step_index, role, content_text, flags_json from trace_steps where trace_id=? order by step_index",
        (trace_id,),
    ).fetchall()
    steps: list[dict[str, Any]] = []
    by_call: dict[str, dict[str, Any]] = {}
    pending: dict[str, Any] | None = None
    for index, role, content, flags_json in rows:
        flags = json.loads(flags_json)
        message = (flags.get("native_node_residual") or {}).get("message") or {}
        if role == "assistant":
            calls = []
            for call in message.get("tool_calls") or []:
                try:
                    arguments = json.loads(call.get("arguments") or "{}")
                except (TypeError, ValueError):
                    arguments = {"raw": str(call.get("arguments"))}
                if not isinstance(arguments, dict):
                    arguments = {"raw": arguments}
                calls.append({"tool_call_id": str(call.get("id")), "function_name": str(call.get("name") or ""),
                              "arguments": arguments})  # fmt: skip
            step: dict[str, Any] = {"step_id": index, "source": "agent", "message": content or "",
                                    "tool_calls": calls, "observation": {"results": []}}  # fmt: skip
            if flags.get("reasoning_content"):
                step["reasoning_content"] = flags["reasoning_content"]
            steps.append(step)
            for call in calls:
                by_call[call["tool_call_id"]] = step
            pending = step
        elif role == "tool":
            owner = by_call.get(str(flags.get("tool_call_id"))) or pending
            if owner is not None:
                owner["observation"]["results"].append(
                    {"source_call_id": str(flags.get("tool_call_id")), "content": content or ""}
                )
                owner["available_at"] = index
        else:
            steps.append({"step_id": index, "source": role, "message": content or ""})
    for step in steps:
        step.setdefault("available_at", step["step_id"])
        step["available_at"] = max(step["available_at"], step["step_id"])
    return sorted(steps, key=lambda s: int(s["step_id"]))


# ---------------------------------------------------------------- per run

_G: dict[str, Any] = {}


def _init() -> None:
    from evallab.production import trace_harness_stream as ts

    _G["detector"] = ts.CascadeStreamDetector(cache=ts.VerdictCache(CACHE))


def card_set(cards: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return sorted((c["rule"], c["flagged_step"], c["status"], c["card_prefix"]) for c in cards)


def stored_first(row: dict[str, Any]) -> dict[str, Any] | None:
    """ID-CM-1 ``first_alarm`` on the stored row."""
    from evallab.production.trace_harness_stream import PASS_THROUGH

    alarming = [c for c in row["cards"] if c["status"] == "confirmed" or c["rule"] in PASS_THROUGH]
    return min(alarming, key=lambda c: (c["card_prefix"], c["flagged_step"])) if alarming else None


def job(args: tuple[str, str, list[dict[str, Any]], list[int], dict[str, Any]]) -> dict[str, Any]:
    from evallab.production.trace_harness import HarnessRun
    from evallab.production.trace_harness_stream import first_alarm

    arm, trace_id, steps, harness_agent_ids, row = args
    detector = _G["detector"]
    run = HarnessRun(trace_id, "goodfire", "DEV", "unknown", tuple(steps), True)
    started = time.perf_counter()
    (record,) = detector.detect([run])
    info = detector.runs[trace_id]
    mine = first_alarm(info["alarms"])
    theirs = stored_first(row)
    stored_unavailable = int(row.get("triage_unavailable") or 0)
    stored_verdict = "alarm" if theirs else "clear" if stored_unavailable == 0 else "abstain"
    out = {
        "run": hid(trace_id), "arm": arm, "verdict": record["verdict"], "stored_verdict": stored_verdict,
        "cards_equal": card_set(info["cards"]) == card_set(row["cards"]),
        "cards": len(info["cards"]), "stored_cards": len(row["cards"]),
        "alarms_at_eof": info["alarms_at_eof"], "first_alarm_at_eof": bool(mine and mine["at_eof"]),
        "hits": info["hits"], "misses": info["misses"], "unavailable": info["triage_unavailable"],
        "stored_unavailable": stored_unavailable, "input_tokens": info["input_tokens"],
        "output_tokens": info["output_tokens"], "seconds": round(time.perf_counter() - started, 2),
    }
    if mine is not None:
        out |= {"card_prefix": mine["card_prefix"], "flagged_step": mine["flagged_step"],
                "turns_stream": mine["turns"], "emitted_step": mine["emitted_step"],
                "emitted_turns": mine["emitted_turns"],
                "emitted_after_prefix": (mine["emitted_step"] or 0) - (mine["card_prefix"] or 0),
                "turns_idcm1": bisect.bisect_right(harness_agent_ids, mine["card_prefix"])}  # fmt: skip
    if theirs is not None:
        out |= {"stored_card_prefix": theirs["card_prefix"], "stored_flagged_step": theirs["flagged_step"],
                "stored_turns_idcm1": bisect.bisect_right(harness_agent_ids, theirs["card_prefix"])}  # fmt: skip
    return out


# ---------------------------------------------------------------- run


def quart(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    q = statistics.quantiles(values, n=4, method="inclusive") if len(values) > 1 else [values[0]] * 3
    return {"n": len(values), "median": round(q[1], 2), "q1": round(q[0], 2), "q3": round(q[2], 2)}


def summarize(rows: list[dict[str, Any]], positive: dict[str, bool]) -> dict[str, Any]:
    out: dict[str, Any] = {"runs": len(rows)}
    for group, want in (("enacted", True), ("clean", False)):
        sel = [r for r in rows if positive[r["run"]] is want]
        out[group] = {
            "n": len(sel),
            "verdicts": dict(sorted(Counter(r["verdict"] for r in sel).items())),
            "stored_verdicts": dict(sorted(Counter(r["stored_verdict"] for r in sel).items())),
            "alarm": sum(r["verdict"] == "alarm" for r in sel),
            "stored_alarm": sum(r["stored_verdict"] == "alarm" for r in sel),
            "first_alarm_turns_idcm1": quart([r["turns_idcm1"] for r in sel if "turns_idcm1" in r]),
        }
    alarmed = [r for r in rows if r["verdict"] == "alarm" or r["stored_verdict"] == "alarm"]
    out["agreement"] = {
        "verdict_equal": sum(r["verdict"] == r["stored_verdict"] for r in rows),
        "cards_equal": sum(r["cards_equal"] for r in rows),
        "alarm_set_equal": {r["run"] for r in rows if r["verdict"] == "alarm"}
        == {r["run"] for r in rows if r["stored_verdict"] == "alarm"},
        "first_alarm_prefix_equal": sum(
            r.get("card_prefix") == r.get("stored_card_prefix") and r.get("flagged_step") == r.get("stored_flagged_step")
            for r in alarmed
        ),
        "first_alarm_turns_equal": sum(r.get("turns_idcm1") == r.get("stored_turns_idcm1") for r in alarmed),
        "stream_turns_equal_idcm1_turns": sum(r.get("turns_stream") == r.get("turns_idcm1") for r in alarmed),
        "first_alarm_at_eof": sum(r["first_alarm_at_eof"] for r in alarmed),
        "first_alarm_emitted_after_prefix_steps": quart(
            [r["emitted_after_prefix"] for r in alarmed if "emitted_after_prefix" in r]
        ),
        "alarmed_either": len(alarmed),
    }
    out["judge"] = {k: sum(r[k] for r in rows) for k in ("hits", "misses", "unavailable", "stored_unavailable",
                                                          "input_tokens", "output_tokens")}  # fmt: skip
    out["runs_with_misses"] = sum(r["misses"] > 0 for r in rows)
    return out


def run() -> None:
    from evallab.production import window_cascade as wc
    from evallab.production.run_atlas import DEFAULT_DATABASE
    from evallab.production.trace_harness import resolve_runs

    assert wc.CASCADE_VERSION == "window-cascade/v8"
    receipt = json.loads(RECEIPT.read_text())
    for path in (STORED, CACHE):
        if sha(path) != receipt[path.name]["sha256"]:
            raise SystemExit(f"{path} changed after prepare")
    stored = {}
    for line in STORED.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            if row["stratum"] != "considered_only":  # the 50 considered-only runs are never scored
                stored[row["trace_id"]] = row
    runs = {r.run_id: r for r in resolve_runs("goodfire", repo_root=REPO)}  # DEV admission + cross-key
    missing = sorted(set(stored) - set(runs))
    if missing:
        raise SystemExit(f"{len(missing)} stored runs not admitted by the harness loader")
    positive = {hid(t): row["positive"] for t, row in stored.items()}  # scoring only
    con = sqlite3.connect(f"file:{DEFAULT_DATABASE}?mode=ro", uri=True)
    jobs: list[tuple[str, str, list[dict[str, Any]], list[int], dict[str, Any]]] = []
    step_hash_match = 0
    for trace_id in sorted(stored):
        harness = runs[trace_id]
        agent_ids = sorted(int(s["step_id"]) for s in harness.steps if s.get("source") == "agent")
        steps = replay_steps(con, trace_id)
        digest = hashlib.sha256(json.dumps(steps, sort_keys=True).encode()).hexdigest()
        step_hash_match += digest == stored[trace_id]["steps_sha256"]
        jobs.append(("harness", trace_id, [dict(s) for s in harness.steps], agent_ids, stored[trace_id]))
        jobs.append(("replay", trace_id, steps, agent_ids, stored[trace_id]))
    con.close()
    started = time.time()
    with ProcessPoolExecutor(max_workers=JOBS, initializer=_init) as pool:
        rows = list(pool.map(job, jobs, chunksize=4))
    by_arm = {arm: [r for r in rows if r["arm"] == arm] for arm in ("harness", "replay")}
    result = {
        "schema": "b7-stream-smoke/v1",
        "detector": "cascade_stream", "cascade_version": wc.CASCADE_VERSION,
        "judge": "kimi-code/k3.1-pro-highspeed (stored verdicts only; allow_model off; 0 model calls)",
        "inputs": {"stored_rows_sha256": receipt[STORED.name]["sha256"],
                   "cache_sha256": receipt[CACHE.name]["sha256"],
                   "stored_rows_match_harness_snapshot": receipt["stored_rows_match_harness_snapshot"],
                   "labelled_runs": len(stored), "enacted": sum(r["positive"] for r in stored.values()),
                   "replay_steps_sha256_match": step_hash_match},  # fmt: skip
        "arms": {arm: summarize(arm_rows, positive) for arm, arm_rows in by_arm.items()},
        "seconds": round(time.time() - started, 1),
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "smoke.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    with (RESULTS / "per-run.jsonl").open("w") as handle:
        # Labels are joined only into the aggregates above; a run row carries none.
        for row in sorted(rows, key=lambda r: (r["arm"], r["run"])):
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    print(json.dumps(result, indent=1, sort_keys=True))


def _stage1(job: tuple[str, list[dict[str, Any]], list[dict[str, Any]]]) -> tuple[list[Any], list[Any]]:
    from evallab.production.trace_harness_stream import replay

    trace_id, steps, stored_cards = job
    now = Counter((c["rule"], c["flagged_step"]) for c in replay(trace_id, steps, None).cards())
    then = Counter((c["rule"], c["flagged_step"]) for c in stored_cards)
    return sorted((then - now).elements()), sorted((now - then).elements())


def cards() -> None:
    """Why do card sets differ on the replay arm? Stage 1 alone (no judge) vs the stored cards."""
    from evallab.production.run_atlas import DEFAULT_DATABASE

    with (RESULTS / "per-run.jsonl").open() as handle:
        differ = {r["run"] for r in map(json.loads, handle) if r["arm"] == "replay" and not r["cards_equal"]}
    stored = {}
    for line in STORED.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            if hid(row["trace_id"]) in differ:
                stored[row["trace_id"]] = row["cards"]
    con = sqlite3.connect(f"file:{DEFAULT_DATABASE}?mode=ro", uri=True)
    jobs = [(t, replay_steps(con, t), stored[t]) for t in sorted(stored)]
    con.close()
    with ProcessPoolExecutor(max_workers=JOBS) as pool:
        diffs = list(pool.map(_stage1, jobs))
    result = {
        "runs": len(diffs),
        "stored_cards_not_raised_now": sum(len(gone) for gone, _ in diffs),
        "cards_raised_now_not_stored": sum(len(new) for _, new in diffs),
        "new_cards_by_rule": dict(Counter(rule for _, new in diffs for rule, _ in new).most_common()),
    }
    (RESULTS / "card-diff.json").write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))



def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cmd", choices=("prepare", "run", "cards"))
    args = parser.parse_args()
    {"prepare": prepare, "run": run, "cards": cards}[args.cmd]()


if __name__ == "__main__":
    main()
