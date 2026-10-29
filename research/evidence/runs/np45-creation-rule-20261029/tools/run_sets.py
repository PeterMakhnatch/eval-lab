"""NP45-TW-1 step 1: classify every ``grader_tamper`` alarm event of one set (label-free), one set per process.

usage: run_sets.py <registry id | cm18 | np21 | np33>

Writes ``<state>/events-<id>.json``: counts of rows, rows without agent steps, errors, and per run the kind of each alarm event plus the
raw label fields (``state``, ``positive``, ``stratum``) that only ``census.py`` reads. Aborts unless ``classify.py`` has the frozen digest.
A read that exceeds 20 minutes is written as ``not_completed`` with no rows.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pickle
import signal
import sys
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("EVALLAB_DISABLE_LOCAL_DOCKER", "1")

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "research/evidence/runs/np43-grader-tamper-kinds-20261029/tools"))

import classify  # noqa: E402
import convert  # noqa: E402

FROZEN = "0e5dc144b1741269cd3cbe28a7d3f1da3e20aa672d1fdf73b639d621730f6302"
STATE = Path.home() / ".local/state"
OUT = STATE / "evallab-worker-evidence/np45-tw1-20261029"
REGISTRY = (
    Path.home() / "Developer/research-context/bridge/2026-10-27-run-atlas/cross-test-registry.json"
)
LIMIT_SECONDS = 20 * 60


class TimedOut(Exception):
    pass


def _alarm(_signum: int, _frame: Any) -> None:
    raise TimedOut


def cm18() -> Any:
    for row in pickle.loads((STATE / "evallab-cm18/runs.pkl").read_bytes()):
        yield (
            row["steps"],
            {
                "set": f"cm18:{row['set']}",
                "stratum": row["stratum"],
                "positive": bool(row["label"]),
            },
        )


def np21() -> Any:
    for row in pickle.loads((STATE / "evallab-np21-cm1/runs.pkl").read_bytes()):
        yield (
            row["steps"],
            {
                "set": f"np21:{row['set']}",
                "stratum": row["stratum"],
                "positive": bool(row["label"]),
            },
        )


def np33() -> Any:
    root = STATE / "evallab-np33-cm1"
    for row in json.loads((root / "sample.json").read_text())["runs"]:
        steps = pickle.loads((root / "runs" / f"{row['key']}.pkl").read_bytes())["steps"]
        yield (
            steps,
            {
                "set": f"np33:{row['set']}",
                "stratum": row["stratum"],
                "positive": bool(row["label"]),
            },
        )


def adapter_rows(set_id: str) -> Any:
    entry = next(e for e in json.loads(REGISTRY.read_text())["entries"] if e["id"] == set_id)
    path = REPO / entry["adapter"]["module_path"]
    spec = importlib.util.spec_from_file_location("np45_adapter", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    source = (
        getattr(module, "specs", None) or getattr(module, "iter_specs", None) or module.iter_rows
    )
    for row in source():
        steps = row.get("_steps")
        if steps is None and hasattr(module, "to_steps"):
            steps = module.to_steps(row)
        gold = row.get("gold") if isinstance(row.get("gold"), dict) else {}
        labels = row.get("labels") if isinstance(row.get("labels"), dict) else {}
        yield (
            steps,
            {
                "set": set_id,
                "state": gold.get("state")
                or gold.get("label_state")
                or labels.get("state")
                or labels.get("label_state"),
                "positive": row.get("positive") if isinstance(row.get("positive"), bool) else None,
            },
        )


def main() -> int:
    digest = hashlib.sha256(Path(classify.__file__).read_bytes()).hexdigest()
    if digest != FROZEN:
        raise SystemExit(f"classifier digest {digest} is not the frozen one")
    set_id = sys.argv[1]
    dumped = (
        OUT / f"{set_id}-rows.pkl"
    )  # rows an adapter needing Python 3.14 (zstd zip) wrote with dump314.py
    if dumped.exists():
        rows_source = lambda: iter(pickle.loads(dumped.read_bytes()))  # noqa: E731
    else:
        rows_source = {"cm18": cm18, "np21": np21, "np33": np33}.get(set_id) or (
            lambda: adapter_rows(set_id)
        )
    OUT.mkdir(parents=True, exist_ok=True)
    started = time.time()
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(LIMIT_SECONDS)
    result: dict[str, Any] = {
        "id": set_id,
        "status": "ok",
        "rows": 0,
        "no_steps": 0,
        "errors": {},
        "runs": [],
    }
    try:
        for steps, label in rows_source():
            result["rows"] += 1
            atif = convert.to_atif(list(steps)) if steps else None
            if not atif:
                result["no_steps"] += 1
                result["runs"].append({**label, "agent_steps": 0, "events": []})
                continue
            try:
                out = classify.run_events(atif)
            except Exception as exc:  # noqa: BLE001 - counted, never dropped
                name = type(exc).__name__
                result["errors"][name] = result["errors"].get(name, 0) + 1
                continue
            result["runs"].append(
                {
                    **label,
                    "agent_steps": out["agent_steps"],
                    "events": [{"kind": e["kind"], "sub": e["sub"]} for e in out["events"]],
                }
            )
    except TimedOut:
        result.update(status="not_completed", runs=[])
    except Exception as exc:  # noqa: BLE001 - the set is reported as unreadable with the reason class
        result.update(status=f"unreadable: {type(exc).__name__}: {str(exc)[:160]}", runs=[])
    finally:
        signal.alarm(0)
    result["seconds"] = round(time.time() - started)
    (OUT / f"events-{set_id}.json").write_text(json.dumps(result, sort_keys=True))
    print(
        set_id,
        result["status"],
        "rows",
        result["rows"],
        "no_steps",
        result["no_steps"],
        "errors",
        result["errors"],
        f"{result['seconds']}s",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
