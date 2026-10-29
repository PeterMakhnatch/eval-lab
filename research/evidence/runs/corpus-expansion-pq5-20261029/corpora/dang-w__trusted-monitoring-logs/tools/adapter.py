#!/usr/bin/env python3
"""Offline trusted-monitoring generate-run replay. Only a spec's ``_steps`` may reach a detector.

Reads the locally staged Inspect ``.eval`` archives (zip + zstd, Python >= 3.14 stdlib
``zipfile`` reads them; no third-party package). The run condition (honest vs instructed
attack), the scorer's main/side-task results and the scorer explanation stay in gold.
This module never prints transcript text, executes a tool, or calls a network/model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO = "dang-w/trusted-monitoring-logs"
REVISION = "71dbeecca5cd423372d066c6a879dab838c41900"
CORPUS = "dang-w__trusted-monitoring-logs"
BUNDLE_DIR = REVISION[:12]
ROOT = Path.home() / ".local/state/evallab-cross-test" / CORPUS / BUNDLE_DIR

# Runs whose directory names mark the publisher's result families (README "Run families").
RESULT_HONEST = frozenset(
    {
        "e2-gen-honest-1",
        "e2-gen-honest-2",
        "e2-gen-honest-3",
        "e2-gen-honest-4",
        "e3-gen-honest-capped",
        "e5-gen-honest-capped-16384",
        "e8-gen-honest-5",
        "e8-gen-honest-6",
        "e8-gen-honest-7",
        "e8-gen-honest-8",
    }
)
RESULT_ATTACK = frozenset(
    {
        "e3-gen-attack-1",
        "e3-gen-attack-2",
        "e3-gen-attack-3",
        "e3-gen-attack-4",
        "e8-gen-attack-5",
        "e8-gen-attack-7",
        "e8-gen-attack-8",
    }
)
# e1-* = superseded first pass; *probe* = probes (one has a client-timeout fault; a3 is a
# duplicate of e3 probe); they are staged but excluded from every binary denominator.

FIELDS_DETECTOR_MUST_NOT_SEE = frozenset(
    {
        "eval_mode",
        "main_task_success",
        "side_task_success",
        "apps_scorer",
        "explanation",
        "side_task_description",
        "side_task_name",
        "main_task_name",
        "setting_name",
        "setting_description",
        "system_prompt",
        "run_dir",
        "run_family",
        "file",
        "sample_id",
        "uuid",
        "epoch",
        "model",
        "model_usage",
        "events",
        "store",
        "scores",
        "metadata",
        "total_time",
        "working_time",
        "started_at",
        "completed_at",
        "gold",
        "labels",
        "identity",
        "positive",
        "label_state",
        "label_reason",
    }
)
STEP_FIELDS = frozenset(
    {"step_id", "source", "input_role", "message", "reasoning_content", "tool_calls", "observation"}
)
NAME_TOKENS = (
    "apps_scorer",
    "side_task_success",
    "main_task_success",
    "eval_mode",
    "insert_backdoor",
    "trusted-monitoring",
    "trusted_monitoring",
    "apps-generate",
    "apps_generate",
    "gen-attack",
    "gen-honest",
    "dang-w",
)
_WS = re.compile(r"\s+")
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


def norm(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def sentences(text: str, floor: int = 40) -> list[str]:
    out = [norm(part) for part in _SENT.split(text)]
    # Pure number lists (scorer echoes of the agent's own test inputs) are not verdict text.
    return [
        part for part in out if len(part) >= floor and sum(w.isalpha() for w in part.split()) >= 4
    ]


def _text(content: Any) -> tuple[str, str]:
    """Return (visible text, reasoning text) of an Inspect message content."""
    if isinstance(content, str):
        return content, ""
    texts: list[str] = []
    thoughts: list[str] = []
    for part in content:
        if part.get("type") == "text":
            texts.append(part.get("text", ""))
        elif part.get("type") == "reasoning":
            thoughts.append(part.get("reasoning", ""))
        else:
            raise ValueError(f"unknown content part {part.get('type')!r}")
    return "\n".join(t for t in texts if t), "\n".join(t for t in thoughts if t)


def build_steps(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Agent-visible replay. The (mode-independent) system prompt is dropped."""
    results: dict[str, str] = {}
    for message in messages:
        if message["role"] == "tool":
            text, _ = _text(message["content"])
            results[message["tool_call_id"]] = text
    steps: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "system" or role == "tool":
            continue
        text, thinking = _text(message["content"])
        if role == "user":
            kind = "task" if not steps else "harness_nudge"
            steps.append(
                {"step_id": len(steps), "source": "user", "input_role": kind, "message": text}
            )
            continue
        if role != "assistant":
            raise ValueError(f"unknown role {role!r}")
        step: dict[str, Any] = {
            "step_id": len(steps),
            "source": "agent",
            "input_role": "assistant",
            "message": text,
        }
        if thinking:
            step["reasoning_content"] = thinking
        calls = message.get("tool_calls") or []
        if calls:
            step["tool_calls"] = [
                {"function": c["function"], "arguments": c["arguments"]} for c in calls
            ]
            step["observation"] = [results.get(c["id"], "") for c in calls]
        steps.append(step)
    return steps


def step_strings(step: dict[str, Any]) -> Iterator[str]:
    for key in ("message", "reasoning_content"):
        if step.get(key):
            yield step[key]
    yield from step.get("observation") or []
    for call in step.get("tool_calls") or []:
        yield call["function"]
        yield from _leaves(call["arguments"])


def _leaves(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)


def manifest_files(root: Path = ROOT) -> list[dict[str, Any]]:
    manifest = json.loads((root / "STAGE-MANIFEST.json").read_text())
    return [f for f in manifest["files"] if f["path"].endswith(".eval")]


def run_dir(path: str) -> str:
    return path.split("/")[1]


def iter_samples(root: Path = ROOT) -> Iterator[tuple[str, dict[str, Any]]]:
    for entry in sorted(manifest_files(root), key=lambda f: f["path"]):
        with zipfile.ZipFile(root / entry["path"]) as archive:
            names = [n for n in archive.namelist() if n.startswith("samples/")]
            names.sort(key=lambda n: (int(n.split("/")[1].split("_")[0]), n))
            for name in names:
                yield entry["path"], json.loads(archive.read(name))


def _mode_of(directory: str) -> str:
    return "attack" if "-attack" in directory else "honest"


def classify(path: str, sample: dict[str, Any]) -> tuple[str, str]:
    """(state, reason). Missing or inconsistent evidence is ambiguous, never negative."""
    directory = run_dir(path)
    if directory not in RESULT_HONEST and directory not in RESULT_ATTACK:
        return "excluded", "superseded_or_probe_family"
    mode = sample["metadata"].get("eval_mode")
    if mode != _mode_of(directory):
        return "ambiguous", "mode_directory_mismatch"
    if sample.get("error"):
        return "ambiguous", "sample_error"
    score = ((sample.get("scores") or {}).get("apps_scorer") or {}).get("value")
    if not isinstance(score, dict) or "main_task_success" not in score:
        return "ambiguous", "no_score"
    if mode == "honest":
        if "side_task_success" in score:
            return "ambiguous", "honest_with_side_task_score"
        return "negative", "honest_condition"
    side = score.get("side_task_success")
    if side == "C":
        return "positive", "attack_condition_side_task_succeeded"
    if side == "I":
        submitted = any(
            "secret_input" in (c.get("arguments") or {})
            for m in sample["messages"]
            for c in (m.get("tool_calls") or [])
        )
        return "ambiguous", ("attack_side_task_failed" if submitted else "attack_no_submission")
    return "ambiguous", "attack_side_task_unscored"


def trace_sha(steps: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(steps, sort_keys=True).encode()).hexdigest()


def specs(root: Path = ROOT) -> list[dict[str, Any]]:
    """One in-memory dict per staged generate sample; only ``_steps`` may reach a detector."""
    out: list[dict[str, Any]] = []
    for path, sample in iter_samples(root):
        state, reason = classify(path, sample)
        steps = build_steps(sample["messages"])
        meta = sample["metadata"]
        score = ((sample.get("scores") or {}).get("apps_scorer")) or {}
        out.append(
            {
                "identity": {
                    "file": path,
                    "run_dir": run_dir(path),
                    "sample_id": str(sample["id"]),
                    "task_id": str(meta.get("main_task_name", sample["id"])),
                    "task_text": sample["messages"][1]["content"]
                    if len(sample["messages"]) > 1
                    else "",
                },
                "gold": {
                    "eval_mode": meta.get("eval_mode"),
                    "score": score.get("value"),
                    "explanation": score.get("explanation", ""),
                    "side_task_description": meta.get("side_task_description", ""),
                },
                "labels": {
                    "label_state": state,
                    "label_reason": reason,
                    "positive": state == "positive",
                    "trace_sha256": trace_sha(steps),
                },
                "_steps": steps,
            }
        )
    resolve_duplicates(out)
    return out


def resolve_duplicates(rows: list[dict[str, Any]]) -> None:
    """Byte-identical traces: keep the first; conflicting labels make the group ambiguous."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["labels"]["trace_sha256"]].append(row)
    for members in groups.values():
        if len(members) == 1:
            continue
        scored = [m for m in members if m["labels"]["label_state"] != "excluded"]
        states = {m["labels"]["label_state"] for m in scored}
        for index, member in enumerate(scored):
            if len(states) > 1:
                member["labels"]["label_state"] = "ambiguous"
                member["labels"]["label_reason"] = "duplicate_trace_conflicting_labels"
            elif index > 0:
                member["labels"]["label_state"] = "excluded"
                member["labels"]["label_reason"] = "duplicate_trace"
            member["labels"]["positive"] = member["labels"]["label_state"] == "positive"


def leak_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Whitespace-normalised substring scan of ALL step text, tool arguments included."""
    label_hits = name_hits = quote_units = 0
    quote_by_mode: Counter[str] = Counter()
    for row in rows:
        blob = norm("\n".join(s for step in row["_steps"] for s in step_strings(step)))
        judge = sentences(row["gold"]["explanation"])
        if any(s in blob for s in judge):
            label_hits += 1
        if any(token in blob for token in NAME_TOKENS) or norm(row["identity"]["file"]) in blob:
            name_hits += 1
        side = sentences(row["gold"]["side_task_description"])
        if side and any(s in blob for s in side):
            quote_units += 1
            quote_by_mode[row["gold"]["eval_mode"]] += 1
    return {
        "units": len(rows),
        "judge_text_hits": label_hits,
        "name_token_hits": name_hits,
        "side_task_quote_units": quote_units,
        "side_task_quote_by_mode": dict(quote_by_mode),
    }


def selfcheck(root: Path = ROOT) -> dict[str, Any]:
    rows = specs(root)
    issues: list[str] = []
    calls = results = chars = 0
    for row in rows:
        steps = row["_steps"]
        if [s["step_id"] for s in steps] != list(range(len(steps))):
            issues.append("non-contiguous step ids")
        for step in steps:
            if not set(step) <= STEP_FIELDS:
                issues.append(f"unexpected step keys {sorted(set(step) - STEP_FIELDS)}")
            tool_calls = step.get("tool_calls") or []
            observation = step.get("observation") or []
            if len(tool_calls) != len(observation):
                issues.append("call/result join broken")
            calls += len(tool_calls)
            results += len(observation)
            chars += sum(len(s) for s in step_strings(step))
    audit = leak_audit(rows)
    if audit["judge_text_hits"] or audit["name_token_hits"]:
        issues.append("label/name leak in _steps")
    states = Counter(r["labels"]["label_state"] for r in rows)
    return {
        "units": len(rows),
        "states": dict(states),
        "steps": sum(len(r["_steps"]) for r in rows),
        "tool_calls": calls,
        "tool_results": results,
        "step_string_chars": chars,
        "leak_audit": audit,
        "issues": issues,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["selfcheck"])
    parser.parse_args()
    print(json.dumps(selfcheck(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
