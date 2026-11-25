from __future__ import annotations

import json
import math
import operator
from pathlib import Path
from typing import Any

from muster.lab import api, episode, load, toy
from muster.lab.card import hidden_event
from muster.lab.cardfile import TASKS
from muster.lab.schedule import parse

_KINDS = (
    "wrong_pass", "oracle_fails", "hidden_string", "world_error", "nondeterminism",
    "no_end", "state_mismatch",
)
_STATUS = ("open", "fixed", "accepted")
_OPS = {">=": operator.ge, "<=": operator.le, "<": operator.lt, ">": operator.gt,
        "==": operator.eq}
_MAX_FINDINGS = 10_000


def adapter_for(family: str) -> Any:
    if family == "toy":
        return toy.family()
    if family.startswith("toy:"):
        return toy.family(family.partition(":")[2])
    return load.family(family)


def _text(data: dict, key: str, *, empty: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (not empty and not value):
        raise load.LabError(f"finding requires string {key}")
    return value


def _load(path: Path) -> dict:
    try:
        row = json.loads(path.read_bytes())
    except (OSError, UnicodeError, ValueError) as error:
        raise load.LabError(f"cannot read finding {path}: {error}") from error
    if not isinstance(row, dict):
        raise load.LabError("finding must be an object")
    for key in ("id", "family", "condition", "instance", "schedule", "kind", "status"):
        _text(row, key)
    _text(row, "claim", empty=True)
    _text(row, "note", empty=True)
    if row["kind"] not in _KINDS or row["status"] not in _STATUS:
        raise load.LabError("invalid finding kind or status")
    lineup = row.get("lineup")
    if not isinstance(lineup, dict) or not lineup or not all(
        isinstance(seat, str) and seat and isinstance(name, str) and name
        for seat, name in lineup.items()
    ):
        raise load.LabError("invalid finding lineup")
    expect = row.get("expect")
    if not isinstance(expect, dict) or _text(expect, "op") not in _OPS:
        raise load.LabError("invalid finding expect comparison")
    _text(expect, "measure")
    value = expect.get("value")
    if type(value) not in (int, float) or not math.isfinite(value):
        raise load.LabError("finding expect value must be finite")
    return row


def _instance(adapter: Any, row: dict) -> dict:
    if row["condition"] not in adapter.conditions():
        raise load.LabError(f"unknown condition {row['condition']}")
    matches = [instance for instance in adapter.instances(row["condition"])
               if instance["id"] == row["instance"]]
    if len(matches) != 1:
        raise load.LabError(f"instance must identify one instance: {row['instance']}")
    return matches[0]


def _file_policy(base: Path, name: str) -> api.Policy:
    path, separator, function = name.removeprefix("file:").rpartition(":")
    if not separator or not path or not function or Path(path).is_absolute():
        raise load.LabError(f"expected file:relative_path:policy_name, got {name!r}")
    source = base / path
    if source.suffix != ".py" or not source.is_file() or not function.isidentifier():
        raise load.LabError(f"invalid policy module or name: {name!r}")
    try:
        module = load.module(source, "finding_policy")
        policy = getattr(module, function)
    except Exception as error:
        raise load.LabError(f"cannot load policy {name}: {error}") from error
    if not callable(policy):
        raise load.LabError(f"policy is not callable: {name}")
    return policy


def _zoo(adapter: Any, instance: dict, row: dict, base: Path) -> dict[str, api.Policy]:
    lineup = row["lineup"]
    if set(lineup) != set(adapter.seats(instance)):
        raise load.LabError("finding lineup must cover all real seats exactly")
    family_zoo = adapter.policies()
    zoo = {}
    for name in sorted(set(lineup.values())):
        if name.startswith("zoo:") and name[4:] in family_zoo:
            zoo[name] = family_zoo[name[4:]]
        elif name.startswith("file:"):
            zoo[name] = _file_policy(base, name)
        else:
            raise load.LabError(f"unknown finding policy: {name}")
    return zoo


def _observe(adapter: Any, instance: dict, row: dict, result: api.Episode,
             zoo: dict[str, api.Policy]) -> tuple[bool, dict]:
    match row["kind"]:
        case "wrong_pass" | "oracle_fails":
            expect = row["expect"]
            if expect["measure"] not in result.rewards:
                raise load.LabError(f"unknown measure: {expect['measure']}")
            value = result.rewards[expect["measure"]]
            return _OPS[expect["op"]](value, expect["value"]), {"observed": value}
        case "hidden_string":
            if not hasattr(adapter, "hidden"):
                raise load.LabError("hidden_string finding requires hidden hook")
            event = hidden_event(adapter, instance, result)
            return event is not None, {"event": event}
        case "world_error":
            event = next((e for e in result.events if e["status"] == 500), None)
            return event is not None, {"event": event}
        case "nondeterminism":
            repeated = episode.run(adapter, instance, row["lineup"], parse(row["schedule"]),
                                   zoo=zoo)
            different = result != repeated
            return different, {"repeat_differs": different, "repeat_rewards": repeated.rewards,
                               "repeat_end": repeated.ended, "repeat_order": list(repeated.order)}
        case "no_end":
            return result.ended == "step_limit", {}
        case "state_mismatch":
            if not hasattr(adapter, "state") or not hasattr(adapter, "replay"):
                raise load.LabError("state_mismatch finding requires state and replay hooks")
            return result.state != result.replayed, {"state": result.state,
                                                     "replayed": result.replayed}
    raise load.LabError(f"unknown finding kind: {row['kind']}")


def verify(path: Path | str) -> dict:
    path = Path(path)
    row = _load(path)
    adapter = adapter_for(row["family"])
    instance = _instance(adapter, row)
    zoo = _zoo(adapter, instance, row, path.parent)
    result = episode.run(adapter, instance, row["lineup"], parse(row["schedule"]), zoo=zoo)
    reproduced, evidence = _observe(adapter, instance, row, result, zoo)
    return {
        "id": row["id"], "family": row["family"], "condition": result.condition,
        "instance": result.instance, "lineup": result.lineup, "kind": row["kind"],
        "status": row["status"], "claim": row["claim"],
        "verdict": "CONFIRMED" if reproduced else "NOT_REPRODUCED",
        "rewards": result.rewards, "end": result.ended, "order": list(result.order), **evidence,
    }


def regress(family: str, *, directory: Path | None = None) -> list[dict] | None:
    if family == "toy" or family.startswith("toy:"):
        if directory is None:
            return None
    elif not family or not family.replace("_", "").replace("-", "").isalnum():
        raise load.LabError(f"invalid finding family: {family!r}")
    directory = directory if directory is not None else TASKS / family / "findings"
    if not directory.exists():
        return None
    if not directory.is_dir():
        raise load.LabError(f"not a findings directory: {directory}")
    paths = sorted(directory.glob("*.json"))
    assert len(paths) <= _MAX_FINDINGS, "too many findings"
    results = []
    for path in paths:
        row = verify(path)
        if row["family"] != family:
            raise load.LabError(f"finding family differs from {family}: {path.name}")
        ok = (row["status"], row["verdict"]) in (
            ("fixed", "NOT_REPRODUCED"), ("accepted", "CONFIRMED")
        )
        results.append({**row, "file": path.name, "ok": ok})
    return results
