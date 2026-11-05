"""DEV-only line specifications; paid execution is never implicit."""

from __future__ import annotations

import math
import os
import re
import stat
from pathlib import Path
from typing import Any

from evallab.hack_drop import check_dev_path_allowed
from evallab.line.io import file_keys, read_json


def _refuse_final_link(spelled: Path) -> None:
    """A declared input that is itself a symlink is refused BEFORE the path is resolved:
    ``check_dev_path_allowed`` resolves it, which would hand the no-follow reader the target."""
    try:
        mode = os.lstat(spelled).st_mode
    except OSError:
        return  # absent: the reader reports it
    if stat.S_ISLNK(mode):
        raise ValueError(f"declared input is a symlink: {spelled}")


def load_spec(path: Path) -> dict[str, Any]:
    path = Path(path).expanduser().absolute()
    _refuse_final_link(path)
    path = check_dev_path_allowed(path)
    spec = read_json(path)
    if spec.get("schema") != "evallab.line_spec/v1" or spec.get("split") != "dev":
        raise ValueError("line spec must use evallab.line_spec/v1 and split=dev")
    if not isinstance(spec.get("batch_id"), str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*", spec["batch_id"]
    ):
        raise ValueError("batch_id must be a safe single directory name")
    if "copy_history" in spec and not isinstance(spec["copy_history"], bool):
        raise ValueError("copy_history must be a boolean")

    def absolute(value: Any, *, link: bool = True) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("path must be nonempty text")
        p = Path(value).expanduser()
        spelled = p if p.is_absolute() else path.parent / p
        if link:
            _refuse_final_link(spelled)
        return str(check_dev_path_allowed(spelled))

    sources = spec.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources must be a nonempty list")
    for source in sources:
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("corpus"), str)
            or not source["corpus"]
        ):
            raise ValueError("every source needs a corpus name")
        source["rollouts"] = absolute(source.get("rollouts"))
        source["labels"] = absolute(source["labels"]) if source.get("labels") else None
    spec["task_status"] = absolute(spec["task_status"]) if spec.get("task_status") else None
    explicit_action = spec.get("task_action")
    if spec.get("task_action", "review") not in ("review", "drop"):
        raise ValueError("task_action must be review or drop")
    policy = spec.setdefault("task_policy", {})
    statuses = ("certified", "provisional", "weak", "uncertified")
    if (
        not isinstance(policy, dict)
        or set(policy) - set(statuses)
        or any(action not in ("pass", "review", "drop") for action in policy.values())
    ):
        raise ValueError(
            "task_policy must map certified/provisional/weak/uncertified to pass, review or drop"
        )
    if explicit_action is not None and policy.get("weak", explicit_action) != explicit_action:
        raise ValueError("task_action and task_policy.weak disagree; set only one")
    # Keep the optional legacy alias absent when the policy supplies weak's action.
    # Inserting a default alias would turn a valid policy into a conflict on reload.
    if "weak" not in policy:
        spec.setdefault("task_action", "review")
    policy.setdefault("weak", spec.get("task_action", "review"))
    for status in ("certified", "provisional", "uncertified"):
        policy.setdefault(status, "pass")
    floor = spec.get("goodfire_review_floor", True)
    if not isinstance(floor, bool):
        raise ValueError("goodfire_review_floor must be boolean")
    ar = spec.setdefault("autoreview", {"mode": "off"})
    if not isinstance(ar, dict) or ar.get("mode") not in ("off", "cached", "live"):
        raise ValueError("autoreview.mode must be off, cached or live")
    cap = ar.setdefault("max_usd", 0)
    if (
        isinstance(cap, bool)
        or not isinstance(cap, (int, float))
        or not math.isfinite(cap)
        or cap < 0
    ):
        raise ValueError("autoreview.max_usd must be finite and nonnegative")
    models = spec.setdefault("models", {"allowed": []})
    allowed = models.get("allowed") if isinstance(models, dict) else None
    if not isinstance(allowed, list) or any(
        not isinstance(route, str) or not route for route in allowed
    ):
        raise ValueError("models.allowed must be a list of route strings")
    if "unattested" in allowed:
        raise ValueError("models.allowed may not list 'unattested': it names calls with no proof")
    if ar["mode"] == "live":
        call_cap = ar.get("max_call_usd")
        if (
            not isinstance(ar.get("approval"), str)
            or not ar["approval"].strip()
            or cap <= 0
            or ar.get("route") not in allowed
        ):
            raise ValueError(
                "live AutoReview requires an allowed route, approval and positive max_usd"
            )
        if (
            isinstance(call_cap, bool)
            or not isinstance(call_cap, (int, float))
            or not math.isfinite(call_cap)
            or not 0 < call_cap <= cap
        ):
            raise ValueError(
                "live AutoReview requires autoreview.max_call_usd, a per-call bound in (0, max_usd]"
            )
    ar["cache"] = absolute(ar["cache"]) if ar.get("cache") else None
    if ar["mode"] == "cached" and not ar["cache"]:
        raise ValueError("cached AutoReview requires a cache path")
    from evallab.line import j3

    config = ar.get("j3", {"mode": "off"})
    if (
        not isinstance(config, dict)
        or set(config) - {"mode", "cache"}
        or config.get("mode") not in {"off", "cached", "live"}
    ):
        raise ValueError("autoreview.j3 requires frozen off/cached/live configuration")
    if config["mode"] == "cached":
        config["cache"] = absolute(config.get("cache"))
    elif config.get("cache") is not None:
        raise ValueError("only cached J3 accepts a cache path")
    if config["mode"] != "off" and j3.ROUTE not in allowed:
        raise ValueError("J3 requires its frozen subscription route in models.allowed")
    audit = spec.setdefault("audit", {"rate": 0.05, "seed": "line"})
    if not isinstance(audit, dict):
        raise ValueError("audit must be an object")
    rate = audit.get("rate")
    if (
        isinstance(rate, bool)
        or not isinstance(rate, (int, float))
        or not 0 <= rate <= 1
        or not isinstance(audit.get("seed"), str)
    ):
        raise ValueError("audit needs rate in [0,1] and a string seed")
    judge = spec.get("judge")
    if not isinstance(judge, dict) or not isinstance(judge.get("write"), bool):
        raise ValueError("judge needs queue and boolean write")
    judge["queue"] = absolute(judge.get("queue"))
    # Optional Station export override; otherwise use the Station's canonical state.
    if judge.get("verdicts"):
        judge["verdicts"] = absolute(judge["verdicts"])
    policy_section = spec.get("labels_policy")
    policy = policy_section.get("attempted_cheat") if isinstance(policy_section, dict) else None
    if policy not in ("exclude", "cheat", "clean"):
        raise ValueError("labels_policy.attempted_cheat must be exclude, cheat or clean")
    spec["out_root"] = absolute(
        spec.get("out_root", "~/Developer/eval-lab-results/line"), link=False
    )
    _refuse_shared_files(spec)
    return spec


def _refuse_shared_files(spec: dict[str, Any]) -> None:
    """No two declared inputs of different kinds may be one file (hard link, case variant).

    A labels file that is also the task-status file, a manifest or a cache would feed answers to
    a decision stage. Repeating one file within a kind (a labels file shared by two sources) is
    fine.
    """
    from evallab.production.judge_station import STATE_DIR

    kinds: dict[str, list[str]] = {
        "rollouts": [s["rollouts"] for s in spec["sources"]],
        "labels": [s["labels"] for s in spec["sources"] if s["labels"]],
        "task_status": [spec["task_status"]] if spec["task_status"] else [],
        "cache": [spec["autoreview"]["cache"]] if spec["autoreview"].get("cache") else [],
        "j3 cache": (
            [spec["autoreview"]["j3"]["cache"]]
            if spec["autoreview"].get("j3", {}).get("mode") == "cached"
            else []
        ),
        "judge queue": [spec["judge"]["queue"]],
        "judge verdicts": [spec["judge"].get("verdicts") or str(STATE_DIR / "verdicts.jsonl")],
    }
    keyed = [
        (kind, path, file_keys([Path(path)])) for kind, paths in kinds.items() for path in paths
    ]
    for index, (kind, path, keys) in enumerate(keyed):
        for other_kind, other, other_keys in keyed[index + 1 :]:
            if kind != other_kind and keys & other_keys:
                raise ValueError(
                    f"declared inputs are the same file: {kind} {path} and {other_kind} {other}"
                )
