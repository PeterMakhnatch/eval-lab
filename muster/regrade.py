from __future__ import annotations

import copy
import importlib.util
import json
import math
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

TOLERANCE = 1e-6
_TRANSIENT_MODS = ("worldkit", "codec")


def _wrap_task_scorer(
    raw_score: Callable[[list[dict], dict], dict], world: Path, task_mods: dict[str, ModuleType]
) -> Callable[[list[dict], dict], dict]:
    world_dir = str(world)

    def isolated_score(events: list[dict], instance: dict) -> dict:
        saved_path, saved_flag = list(sys.path), sys.dont_write_bytecode
        caller_mods = {k: sys.modules[k] for k in _TRANSIENT_MODS if k in sys.modules}
        for name in _TRANSIENT_MODS:
            sys.modules.pop(name, None)
        sys.modules.update(task_mods)
        if world_dir in sys.path:
            sys.path.remove(world_dir)
        sys.path.insert(0, world_dir)
        sys.dont_write_bytecode = True
        try:
            result = raw_score(copy.deepcopy(events), copy.deepcopy(instance))
            for name in _TRANSIENT_MODS:
                if name in sys.modules:
                    task_mods[name] = sys.modules[name]
            return result
        finally:
            sys.path[:] = saved_path
            sys.dont_write_bytecode = saved_flag
            for name in _TRANSIENT_MODS:
                sys.modules.pop(name, None)
            sys.modules.update(caller_mods)

    return isolated_score


def load_score(task: Path) -> tuple[Callable[[list[dict], dict], dict] | None, dict | None, str | None]:
    verify, world = task / "tests" / "verify.py", task / "environment" / "world"
    if not verify.is_file():
        return None, None, "the task has no tests/verify.py"
    try:
        instance = json.loads((world / "instance.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as error:
        return None, None, f"cannot read the task's instance.json: {type(error).__name__}"
    if not isinstance(instance, dict):
        return None, None, "the task's instance.json is not a JSON object"
    mod_name = f"muster_verify_{abs(hash(str(task.resolve())))}"
    spec = importlib.util.spec_from_file_location(mod_name, verify)
    if spec is None or spec.loader is None:
        return None, None, "cannot import tests/verify.py"
    module = importlib.util.module_from_spec(spec)
    saved_path, saved_flag = list(sys.path), sys.dont_write_bytecode
    caller_mods = {k: sys.modules[k] for k in _TRANSIENT_MODS if k in sys.modules}
    had_prev, prev_mod = (mod_name in sys.modules), sys.modules.get(mod_name)
    task_mods: dict[str, ModuleType] = {}
    for name in _TRANSIENT_MODS:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(world))
    sys.dont_write_bytecode = True
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
        for name in _TRANSIENT_MODS:
            if name in sys.modules:
                task_mods[name] = sys.modules[name]
    except (Exception, SystemExit) as error:
        if had_prev and prev_mod is not None:
            sys.modules[mod_name] = prev_mod
        else:
            sys.modules.pop(mod_name, None)
        return None, None, f"tests/verify.py does not import: {type(error).__name__}: {error}"
    finally:
        sys.path[:] = saved_path
        sys.dont_write_bytecode = saved_flag
        for name in _TRANSIENT_MODS:
            sys.modules.pop(name, None)
        sys.modules.update(caller_mods)
    raw_score = getattr(module, "score", None)
    if not callable(raw_score):
        return None, None, "tests/verify.py has no score(events, instance)"
    return _wrap_task_scorer(raw_score, world, task_mods), copy.deepcopy(instance), None


def replay(
    score: Callable[[list[dict], dict], dict], events: list[dict], instance: dict
) -> tuple[dict, str | None]:
    if not isinstance(instance, dict):
        return {}, "instance is not a JSON object"
    caller_mods = {k: sys.modules[k] for k in _TRANSIENT_MODS if k in sys.modules}
    try:
        raw = score(copy.deepcopy(events), copy.deepcopy(instance))
    except (Exception, SystemExit) as error:
        return {}, f"score() failed: {type(error).__name__}: {error}"
    finally:
        for name in _TRANSIENT_MODS:
            if name not in caller_mods:
                sys.modules.pop(name, None)
        sys.modules.update(caller_mods)
    if not isinstance(raw, dict):
        return {}, f"score() returned {type(raw).__name__}, expected dict"
    rewards: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(value, bool):
            continue
        if not isinstance(value, int | float):
            return {}, f"score() gave non-numeric value for {key!r}"
        try:
            num = float(value)
        except OverflowError:
            return {}, "score() gave a value that is not a finite number"
        if not math.isfinite(num):
            return {}, "score() gave a value that is not a finite number"
        rewards[str(key)] = round(num, 6)
    if not rewards:
        return {}, "score() returned no numeric rewards"
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
        response = event.get("response")
        if isinstance(response, dict) and response.get("played") is True:
            continue
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
    replayed, error = replay(score, log, copy.deepcopy(instance) if isinstance(instance, dict) else instance)
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
