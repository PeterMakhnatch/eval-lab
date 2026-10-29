"""R29-TW-1: replay stored runs through the Live Supervisor's tripwire path and record which integrity alerts fire.

The supervisor's ``trace_alarm`` trigger fires on live_watch integrity alerts (``live_supervisor_harbor.INTEGRITY_RULES``).
Those alerts are a pure function of ``tripwires.scan_prefix`` over the agent steps (``live_watch.wire_signals`` ->
``live_watch.integrity_alerts``); file-side sensors (``hidden_test_read``, ``git_object_read``) do not exist in a replay and
cannot fire. This script runs exactly that chain on the final state of every stored run and writes, per run, the set of alert
rules raised. Steps only; labels come from the corpus loaders (strict DEV gate for Goodfire and CheatBench) or the staged
``sample.json`` / ``runs.pkl`` files and are never read by the detectors.

usage: replay.py            writes <cache>/alerts-v2.json (per run: alert rules, supervisor trigger rules before / after the fix)
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import pickle
import sys
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("EVALLAB_DISABLE_LOCAL_DOCKER", "1")

from evallab import live_watch as lw  # noqa: E402
from evallab.production.live_supervisor import Observation, Subject  # noqa: E402
from evallab.production.live_supervisor_harbor import INTEGRITY_RULES, _alert_view, triggers_for  # noqa: E402
from evallab.production.trace_harness import DEFAULT_DATABASE  # noqa: E402
from evallab.production.trace_harness_corpora import load_corpus_labels  # noqa: E402
from evallab.tripwires import scan_prefix  # noqa: E402

STATE = Path.home() / ".local/state"
NP36 = STATE / "evallab-worker-evidence/np36-tw1-20261029"
CACHE = STATE / "evallab-worker-evidence/r29-tw1-20261029"
RUNS: list[dict] = []


def staged(tag: str, root: Path) -> list[dict]:
    out = []
    for row in pickle.loads((root / "runs.pkl").read_bytes()):
        out.append(
            {
                "id": f"{tag}:{row['set']}:{row['key']}",
                "pop": tag,
                "set": row["set"],
                "stratum": row["stratum"],
                "positive": bool(row["label"]),
                "steps": tuple(row["steps"]),
            }
        )
    return out


def np33() -> list[dict]:
    root = STATE / "evallab-np33-cm1"
    sample = json.loads((root / "sample.json").read_text())
    out = []
    for row in sample["runs"]:
        steps = tuple(pickle.loads((root / "runs" / f"{row['key']}.pkl").read_bytes())["steps"])
        out.append(
            {
                "id": f"np33:{row['set']}:{row['key']}",
                "pop": "np33",
                "set": row["set"],
                "stratum": row["stratum"],
                "positive": bool(row["label"]),
                "steps": steps,
            }
        )
    return out


def dev(name: str, corpus: str, pkl: str) -> list[dict]:
    labels = load_corpus_labels(corpus, database=DEFAULT_DATABASE, repo_root=Path.cwd())
    steps = pickle.loads((NP36 / f"{pkl}.pkl").read_bytes())
    out = []
    for run_id, run_steps in steps.items():
        verdict = labels.positive.get(run_id) if run_id in labels.run_ids else None
        if verdict is None:
            continue
        out.append(
            {
                "id": run_id,
                "pop": name,
                "set": name,
                "stratum": "positive" if verdict else "negative",
                "positive": bool(verdict),
                "steps": tuple(run_steps),
            }
        )
    return out


SUBJECT = Subject(kind="harbor_trial", id="replay", owner="lab", killable=True, run_id="replay")


def alerts_of(steps: tuple) -> dict:
    ordered = sorted(
        (dict(s) for s in steps),
        key=lambda s: int(s["step_id"]) if str(s.get("step_id", "")).lstrip("-").isdigit() else 0,
    )
    agent = lw._agent_steps(ordered)
    wires = scan_prefix(agent, steps=ordered)
    status = {
        "job": "replay",
        "trial": "replay",
        "task": "replay",
        **lw.wire_signals(wires),
        "hidden_test_corroboration": wires["hidden_test_corroboration"],
    }
    alerts = lw.integrity_alerts(status)

    def fires(alert: dict) -> bool:
        observation = Observation(
            subject=SUBJECT, observed_at=datetime.now(UTC), metrics={"alerts": [_alert_view(alert)]}
        )
        return any(hit.trigger == "trace_alarm" for hit in triggers_for(observation))

    return {
        "rules": sorted({a["rule"] for a in alerts}),
        "before": sorted({a["rule"] for a in alerts if a["rule"] in INTEGRITY_RULES}),
        "after": sorted({a["rule"] for a in alerts if fires(a)}),
        "mechanisms": sorted(
            {m for a in alerts if a["rule"] == "grader_tamper" for m in a.get("mechanisms", [])}
        ),
    }


def one(index: int) -> dict:
    run = RUNS[index]
    try:
        found, error = alerts_of(run["steps"]), None
    except Exception as exc:  # noqa: BLE001 - recorded, never dropped
        found, error = (
            {"rules": [], "before": [], "after": [], "mechanisms": []},
            f"{type(exc).__name__}: {exc}"[:200],
        )
    return {
        "id": run["id"],
        "pop": run["pop"],
        "set": run["set"],
        "stratum": run["stratum"],
        "positive": run["positive"],
        "length": len(run["steps"]),
        **found,
        "error": error,
    }


def main() -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    RUNS.extend(dev("goodfire_dev", "goodfire-dev", "goodfire_dev"))
    RUNS.extend(dev("cheatbench_dev", "cheatbench", "cheatbench"))
    RUNS.extend(staged("cm18", STATE / "evallab-cm18"))
    RUNS.extend(staged("np21", STATE / "evallab-np21-cm1"))
    RUNS.extend(np33())
    print("runs", len(RUNS), flush=True)
    with mp.get_context("fork").Pool(8) as pool:
        rows = pool.map(one, range(len(RUNS)), chunksize=8)
    (CACHE / "alerts-v2.json").write_text(json.dumps(rows, sort_keys=True))
    print("errors", sum(bool(r["error"]) for r in rows), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
