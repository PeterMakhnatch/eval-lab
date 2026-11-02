"""DEV-only line specifications; paid execution is never implicit."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from evallab.hack_drop import check_dev_path_allowed
from evallab.line.io import read_json


def load_spec(path: Path) -> dict[str, Any]:
    path = check_dev_path_allowed(path)
    spec = read_json(path)
    if spec.get("schema") != "evallab.line_spec/v1" or spec.get("split") != "dev":
        raise ValueError("line spec must use evallab.line_spec/v1 and split=dev")
    if not isinstance(spec.get("batch_id"), str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*", spec["batch_id"]
    ):
        raise ValueError("batch_id must be a safe single directory name")

    def absolute(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("path must be nonempty text")
        p = Path(value).expanduser()
        return str(check_dev_path_allowed(p if p.is_absolute() else path.parent / p))

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
    if spec.get("task_action", "review") not in ("review", "drop"):
        raise ValueError("task_action must be review or drop")
    spec.setdefault("task_action", "review")
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
    if ar["mode"] == "live" and (
        not isinstance(ar.get("approval"), str)
        or not ar["approval"].strip()
        or cap <= 0
        or ar.get("route") not in allowed
    ):
        raise ValueError("live AutoReview requires an allowed route, approval and positive max_usd")
    ar["cache"] = absolute(ar["cache"]) if ar.get("cache") else None
    if ar["mode"] == "cached" and not ar["cache"]:
        raise ValueError("cached AutoReview requires a cache path")
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
    spec["out_root"] = absolute(spec.get("out_root", "~/Developer/eval-lab-results/line"))
    return spec
