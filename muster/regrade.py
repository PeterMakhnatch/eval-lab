from __future__ import annotations

import importlib.util
import json
import math
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

TOLERANCE = 1e-6


def load_score(task: Path) -> tuple[Callable[[list[dict], dict], dict] | None, dict | None, str | None]:
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
    sys.path.insert(0, str(world))
    sys.dont_write_bytecode = True
    sys.modules.pop("worldkit", None)
    sys.modules.pop("codec", None)
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        return None, None, f"tests/verify.py does not import: {type(error).__name__}: {error}"
    finally:
        sys.path[:] = saved_path
        sys.dont_write_bytecode = saved_flag
        sys.modules.pop("worldkit", None)
        sys.modules.pop("codec", None)
    score = getattr(module, "score", None)
    if not callable(score):
        return None, None, "tests/verify.py has no score(events, instance)"
    return score, instance, None


def replay(
    score: Callable[[list[dict], dict], dict], events: list[dict], instance: dict
) -> tuple[dict, str | None]:
    try:
        raw = score(events, instance)
        rewards = {str(key): round(float(value), 6) for key, value in raw.items()}
    except Exception as error:
        return {}, f"score() failed: {type(error).__name__}: {error}"
    if not all(math.isfinite(value) for value in rewards.values()):
        return {}, "score() gave a value that is not a finite number"
    return rewards, None


def differences(recorded: dict[str, float], replayed: dict[str, float]) -> dict[str, list[float | None]]:
    return {
        key: [recorded.get(key), replayed.get(key)]
        for key in sorted(set(recorded) | set(replayed))
        if key not in recorded or key not in replayed or abs(recorded[key] - replayed[key]) > TOLERANCE
    }


def late_actions(graded: list[dict], collected: list[dict]) -> list[dict]:
    late = []
    for event in graded[len(collected) :]:
        if event.get("kind") == "admin.close" and event.get("ok"):
            break
        if event.get("ok") and event.get("seat") is not None:
            late.append({"seq": event["seq"], "seat": event["seat"], "kind": event["kind"]})
    assert len(late) <= max(0, len(graded) - len(collected))
    return late


def regrade_trial(
    recorded: dict[str, float],
    graded: list[dict] | None,
    collected: list[dict] | None,
    score: Callable[[list[dict], dict], dict],
    instance: dict,
) -> dict[str, Any]:
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
