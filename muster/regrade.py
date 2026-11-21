"""Recompute rewards from the event logs of finished trials (docs/DESIGN.md, section 4, rule 7).

A world-kit task grades with `score(events, instance)` in its tests/verify.py, a pure function. So the
reward of a finished trial can be recomputed from the event log that the trial left. Two things follow:

1. Replay parity. The recomputed reward must equal the recorded reward. A difference means that the
   grader is not a function of the log, or that a file changed after the run.
2. Late actions. Harbor collects the task's artifacts when play ends, and the verifier closes the world
   a moment later. A seat action that is in the graded log and not in the collected log happened after
   play ended, and the verifier counted it.

This module imports the task's tests/verify.py. Use it on tasks that you trust.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

TOLERANCE = 1e-6  # the kit rounds rewards to 6 places


def load_score(task: Path) -> tuple[Callable[[list[dict], dict], dict] | None, dict | None, str | None]:
    """The task's score function and instance, or (None, None, reason)."""
    verify, world = task / "tests" / "verify.py", task / "environment" / "world"
    if not verify.is_file():
        return None, None, "the task has no tests/verify.py"
    try:
        instance = json.loads((world / "instance.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return None, None, f"cannot read the task's instance.json: {type(error).__name__}"
    spec = importlib.util.spec_from_file_location(f"muster_verify_{abs(hash(str(task)))}", verify)
    if spec is None or spec.loader is None:
        return None, None, "cannot import tests/verify.py"
    module = importlib.util.module_from_spec(spec)
    saved_path, saved_flag = list(sys.path), sys.dont_write_bytecode
    sys.path.insert(0, str(world))  # verify.py imports worldkit from the world directory
    sys.dont_write_bytecode = True  # leave no cache files in the task directory
    sys.modules.pop("worldkit", None)  # every task carries its own copy of the kit
    try:
        spec.loader.exec_module(module)
    except Exception as error:  # the task's code decides what can go wrong here
        return None, None, f"tests/verify.py does not import: {type(error).__name__}: {error}"
    finally:
        sys.path[:] = saved_path
        sys.dont_write_bytecode = saved_flag
        sys.modules.pop("worldkit", None)
    score = getattr(module, "score", None)
    if not callable(score):
        return None, None, "tests/verify.py has no score(events, instance)"
    return score, instance, None


def replay(
    score: Callable[[list[dict], dict], dict], events: list[dict], instance: dict
) -> tuple[dict, str | None]:
    """The rewards that the log gives, rounded as the kit rounds them, or ({}, reason)."""
    try:
        raw = score(events, instance)
        rewards = {str(key): round(float(value), 6) for key, value in raw.items()}
    except Exception as error:
        return {}, f"score() failed: {type(error).__name__}: {error}"
    if not all(math.isfinite(value) for value in rewards.values()):
        return {}, "score() gave a value that is not a finite number"
    return rewards, None


def differences(recorded: dict[str, float], replayed: dict[str, float]) -> dict[str, list[float | None]]:
    """Reward keys whose recorded and replayed values differ: key -> [recorded, replayed]."""
    return {
        key: [recorded.get(key), replayed.get(key)]
        for key in sorted(set(recorded) | set(replayed))
        if key not in recorded or key not in replayed or abs(recorded[key] - replayed[key]) > TOLERANCE
    }


def late_actions(graded: list[dict], collected: list[dict]) -> list[dict]:
    """Accepted seat requests that the verifier graded and that the collected log does not have.

    They arrived after Harbor collected the artifacts, so after play ended, and before the close.
    """
    late = []
    for event in graded[len(collected) :]:
        if event.get("kind") == "admin.close" and event.get("ok"):
            break
        if event.get("ok") and event.get("seat") is not None:
            late.append({"seq": event["seq"], "seat": event["seat"], "kind": event["kind"]})
    return late


def regrade_trial(
    recorded: dict[str, float],
    graded: list[dict] | None,
    collected: list[dict] | None,
    score: Callable[[list[dict], dict], dict],
    instance: dict,
) -> dict[str, Any]:
    """One trial's replay: status, the differing keys, and the late actions."""
    log = graded if graded is not None else collected
    if log is None:
        return {"status": "no-log", "differences": {}, "late_actions": [], "log": None}
    replayed, error = replay(score, log, instance)
    if error:
        return {"status": "error", "reason": error, "differences": {}, "late_actions": [], "log": None}
    differ = differences(recorded, replayed) if recorded else {}
    late = late_actions(graded, collected) if graded is not None and collected is not None else []
    return {
        "status": "no-recorded-reward" if not recorded else "mismatch" if differ else "match",
        "differences": differ,
        "late_actions": late,
        "log": "graded" if graded is not None else "collected",
        "replayed": replayed,
    }
