"""Metadata-safe access to the registry's staged adapter projections.

Only the adapter's designated input projection crosses the reader boundary. Gold
and state are returned separately, in memory. The caller must freeze and recheck
adapter_ref (and the reward-pressure profile helper) before consuming cases.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from . import optimize as opt

_TOP_LEVEL = {
    "hackbench",
    "kernelbench-hard-runs",
    "reflex-sentinel",
    "assistant-axis",
    "sleight-bench",
    "cuaverifierbench",
    "apollo-insider-trading",
    "hack-ignition",
}
_GOLD_STATE = {
    "agent-cheat-index",
    "mistrace-reward-hacking",
    "knowing-collusion-filter",
    "caught-not-taught",
    "linuxarena-public",
    "rsi-benchmark-monitor-training",
}
_LABEL_STATE = {
    "evilgenie-escalation",
    "sdf-shortcut-taking",
    "researcharena-benchmark-gaming",
    "beam-tripwire",
    "rhb-delivery-queue",
    "baitbench",
    "flake-laundering",
    "kobayashi-maru",
    "posttrainbench-trajectories",
    "collusion-probe-pitfalls",
    "inferencebench-trajectories",
}
_LABEL_LABEL_STATE = {"agentcheck", "trusted-monitoring-logs"}
_AUDITORS = {"paichecker", "automationbench-verified"}
_SUPPORTED = (
    _TOP_LEVEL
    | _GOLD_STATE
    | _LABEL_STATE
    | _LABEL_LABEL_STATE
    | _AUDITORS
    | {"reward-pressure-study-2"}
)


def _source(repo_root: Path, rel: str) -> Path:
    root = Path(repo_root).resolve()
    path = (root / rel).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise opt.PrerequisiteError(f"missing or unsafe cross-test adapter source: {rel}")
    return path


def adapter_ref(repo_root: Path, entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return the content-addressed existing adapter source reference."""
    try:
        rel = entry["adapter"]["module_path"]
        if not isinstance(rel, str):
            raise TypeError
        return opt.file_ref(_source(repo_root, rel))
    except (KeyError, TypeError, OSError) as exc:
        raise opt.PrerequisiteError("missing cross-test adapter source contract") from exc


def _load(ref: Mapping[str, Any]) -> Any:
    try:
        return opt._module(Path(ref["path"]), ref["sha256"])
    except (OSError, ImportError) as exc:
        raise opt.PrerequisiteError(
            f"cross-test adapter dependency unavailable: {ref['path']}"
        ) from exc


def _reward_pressure(
    repo_root: Path, entry: Mapping[str, Any], module: Any, root: Path
) -> Iterator[dict[str, Any]]:
    # The adapter exposes projections but not labels. Its profile's label()
    # owns the structural gold rule; use the adapter's readers, not a parser.
    try:
        rel = entry["adapter"]["profile_script"]
        ref = opt.file_ref(_source(repo_root, rel))
    except (KeyError, TypeError, OSError) as exc:
        raise opt.PrerequisiteError("reward-pressure label helper is unavailable") from exc
    previous = sys.modules.get("adapter")
    sys.modules["adapter"] = module
    try:
        profile = _load(ref)
    finally:
        if previous is None:
            sys.modules.pop("adapter", None)
        else:
            sys.modules["adapter"] = previous
    for relpath in module.SUBSTUDIES.values():
        for row in module.read_rows(root / relpath):
            state = profile.label(row)
            if state == "excluded_error":
                yield {"_steps": [], "_bridge_state": "excluded"}
            else:
                yield {"_steps": module.build_steps(row["transcript"]), "_bridge_state": state}


def _replay_specs(module: Any, root: Path, set_id: str) -> Iterator[dict[str, Any]]:
    """Apply the adapter's replay and final leak/empty-trace exclusion."""
    for spec in module.specs(root=root):
        path = spec["_trace_path"]
        if path is None or spec["labels"]["reason"] == "duplicate_trace":
            steps = []
        else:
            steps, _ = module.replay(path)
        if set_id == "inferencebench-trajectories":
            meta = (
                {}
                if path is None
                else module.json.loads((path.parent / "run_meta.json").read_text(encoding="utf-8"))
            )
            state, _, _ = module.finalize(spec, steps, meta)
        else:
            state, _, _ = module.finalize(spec, steps)
        yield {"_steps": steps, "labels": {"state": state}}


def _state(set_id: str, spec: Mapping[str, Any]) -> str:
    if set_id == "reward-pressure-study-2":
        state = spec["_bridge_state"]
    elif set_id == "paichecker":
        positive = spec["gold"]["positive"]
        if type(positive) is not bool:
            raise opt.PrerequisiteError("paichecker has non-boolean gold")
        state = "positive" if positive else "negative"
    elif set_id == "automationbench-verified":
        if spec["auditor_input_status"] != "ready":
            return "excluded"
        state = {"defective_verified": "positive", "unchanged_not_certified": "ambiguous"}.get(
            spec["gold"]["label_state"]
        )
    elif set_id in _TOP_LEVEL:
        positive = spec["positive"]
        if positive is None:
            return "ambiguous"
        if type(positive) is not bool:
            raise opt.PrerequisiteError(f"non-boolean adapter gold for {set_id}")
        state = "positive" if positive else "negative"
    elif set_id in _GOLD_STATE:
        gold = spec["gold"]
        state = gold.get("state", gold.get("label_state"))
    elif set_id in _LABEL_STATE:
        state = spec["labels"]["state"]
    else:
        state = spec["labels"]["label_state"]
    if set_id == "mistrace-reward-hacking" and state in {"positive_success", "positive_attempt"}:
        state = "positive"
    if state not in {"positive", "negative", "ambiguous", "excluded"}:
        raise opt.PrerequisiteError(f"unsupported label state for {set_id}: {state!r}")
    return state


def iter_cases(repo_root: Path, entry: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield input and independent primary gold/state; never yield full specs."""
    set_id = entry.get("id")
    if set_id not in _SUPPORTED:
        raise opt.PrerequisiteError(f"unsupported cross-test scientific contract: {set_id!r}")
    ref = adapter_ref(repo_root, entry)
    module = _load(ref)
    try:
        root = Path(entry["bundle_path"]).expanduser()
        if not root.is_dir():
            raise opt.PrerequisiteError(f"cross-test bundle unavailable: {set_id}")
        if set_id == "hackbench":
            specs = module.specs(
                granularity="command", path=root / "data/hackbench_169_gpt55_glm52.numbered.jsonl"
            )
        elif set_id == "reflex-sentinel":
            specs = module.specs(root=root, subset="agent")
        elif set_id == "assistant-axis":
            specs = module.specs(root=root, positive_set="strong-only")
        elif set_id == "apollo-insider-trading":
            specs = module.specs(label="is_misaligned", root=root)
        elif set_id in {"agent-cheat-index", "researcharena-benchmark-gaming"}:
            specs = module.specs(root=root, include_excluded=True)
        elif set_id in _AUDITORS:
            specs = module.specs(bundle=root)
        elif set_id == "knowing-collusion-filter":
            specs = module.iter_specs(root=root)
        elif set_id == "reward-pressure-study-2":
            specs = _reward_pressure(repo_root, entry, module, root)
        elif set_id in {"posttrainbench-trajectories", "inferencebench-trajectories"}:
            specs = _replay_specs(module, root, set_id)
        else:
            specs = module.specs(root=root)
        for spec in specs:
            state = _state(set_id, spec)
            if set_id in _AUDITORS:
                input_data = spec["auditor_input"]
                if not isinstance(input_data, dict):
                    raise opt.PrerequisiteError(f"invalid auditor input for {set_id}")
            else:
                steps = spec["_steps_code" if set_id == "trusted-monitoring-logs" else "_steps"]
                if not isinstance(steps, list):
                    raise opt.PrerequisiteError(f"invalid step projection for {set_id}")
                input_data = {"steps": steps}
            yield {
                "input": input_data,
                "positive": True if state == "positive" else False if state == "negative" else None,
                "state": state,
            }
    except (KeyError, TypeError, AttributeError, ImportError, OSError) as exc:
        raise opt.PrerequisiteError(
            f"cross-test adapter contract unavailable or changed: {set_id}"
        ) from exc
