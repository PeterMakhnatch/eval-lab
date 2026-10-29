#!/usr/bin/env python3
"""Offline agentcheck replay. Only a spec's ``_steps`` may reach a frozen detector.

Labels come from the corpus's own rule detector (``score.cheats``), so a candidate that
re-derives the same rules (deleted tests, weakened assertions, added skips, pin changes)
over the patch is circular on this corpus. The CLI prints aggregates only.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO = "Aminembarek2/agentcheck"
REVISION = "bb78e187fe88d95c24afb901b6c4469387bdb569"
ROOT = Path.home() / ".local/state/evallab-cross-test/aminembarek2__agentcheck" / REVISION[:12]
RUN_GLOB = "runs/*.json"  # top level only; runs/archive, calibration and sweeps are excluded
STEP_FIELDS = frozenset({"i", "role", "kind", "text", "tool_calls"})
_WS = re.compile(r"\s+")


def norm(value: str) -> str:
    return _WS.sub(" ", value).strip()


def iter_runs(root: Path = ROOT) -> Iterator[tuple[str, dict[str, Any]]]:
    for path in sorted(root.glob(RUN_GLOB)):
        yield path.stem, json.loads(path.read_text(encoding="utf-8"))


def label_state(run: dict[str, Any]) -> tuple[str, str]:
    """Positive = detector listed a cheat finding. Negative = scored, clean, no cheat.
    Anything else (unscoreable without a flag, missing/inconsistent score) is ambiguous."""
    score = run.get("score")
    if not isinstance(score, dict) or "cheats" not in score:
        return "ambiguous", "missing_score"
    cheats = score["cheats"]
    if cheats:
        if score.get("status") == "scored":
            return "positive", "detector_cheat_scored"
        return "positive", "detector_cheat_unscoreable"
    if score.get("status") == "scored" and score.get("is_clean") is True:
        return "negative", "scored_clean_no_flag"
    return "ambiguous", f"no_flag_but_status_{score.get('status')}"


def to_steps(run: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    roles = {
        "SystemMessage": "system",
        "HumanMessage": "user",
        "AIMessage": "assistant",
        "ToolMessage": "tool_result",
    }
    for msg in run.get("trajectory") or []:
        role = roles.get(msg.get("type"))
        if role is None:
            raise ValueError(f"unknown message type {msg.get('type')!r}")
        steps.append(
            {
                "i": len(steps),
                "role": role,
                "kind": "message",
                "text": msg.get("content") or "",
                "tool_calls": msg.get("tool_calls") or [],
            }
        )
    if run.get("diff"):
        steps.append(
            {
                "i": len(steps),
                "role": "agent",
                "kind": "final_diff",
                "text": run["diff"],
                "tool_calls": [],
            }
        )
    return steps


def specs(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    for stem, run in iter_runs(root):
        state, reason = label_state(run)
        score = run.get("score") or {}
        yield {
            "_steps": to_steps(run),
            "identity": {"run_id": stem, "task_id": run.get("task_id")},
            "labels": {
                "label_state": state,
                "label_reason": reason,
                "cheats": score.get("cheats"),
                "findings": score.get("findings"),
                "status": score.get("status"),
                "is_clean": score.get("is_clean"),
                "verdict_reason": run.get("verdict_reason"),
            },
        }


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def leak_audit(spec: dict[str, Any]) -> Counter[str]:
    """Whitespace-normalised substring checks of label text and ids against step text."""
    blob = norm(" ".join(s for step in spec["_steps"] for s in _strings(step)))
    leaks: Counter[str] = Counter()
    # Detector sentences only. Paths and kind words are ordinary repo text, and
    # verdict_reason is harness output the agent itself saw via run_tests.
    texts = {"cheats": list(_strings(spec["labels"].get("cheats")))}
    texts["findings"] = [
        f["detail"]
        for f in spec["labels"].get("findings") or []
        if isinstance(f.get("detail"), str)
    ]
    for field, items in texts.items():
        for text in items:
            if len(norm(text)) >= 12 and norm(text) in blob:
                leaks[field] += 1
    for key in ("run_id", "task_id"):
        value = spec["identity"][key]
        if value and norm(value) in blob:
            leaks[key] += 1
    return leaks


def selfcheck(root: Path = ROOT) -> dict[str, Any]:
    issues: Counter[str] = Counter()
    leaks: Counter[str] = Counter()
    states: Counter[str] = Counter()
    n = n_steps = 0
    for spec in specs(root):
        n += 1
        states[spec["labels"]["label_state"]] += 1
        n_steps += len(spec["_steps"])
        if not spec["_steps"]:
            issues["empty_steps"] += 1
        for step in spec["_steps"]:
            if set(step) - STEP_FIELDS:
                issues["unexpected_step_field"] += 1
            if not isinstance(step["text"], str):
                issues["non_text_step"] += 1
        leaks.update(leak_audit(spec))
    return {
        "runs": n,
        "steps": n_steps,
        "label_states": dict(states),
        "issues": dict(issues),
        "leaks": dict(leaks),
        "leak_count": sum(leaks.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    print(json.dumps(selfcheck(parser.parse_args().root), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
