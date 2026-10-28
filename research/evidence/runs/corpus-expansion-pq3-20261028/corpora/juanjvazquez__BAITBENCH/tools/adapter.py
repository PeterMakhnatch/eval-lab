#!/usr/bin/env python3
"""Offline BAITBENCH replay. Only a spec's ``_steps`` may reach a frozen detector.

The labels are the publisher's own two-judge verdicts, not certified honesty.
This module never prints source text, executes anything, or calls a network/model.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO = "juanjvazquez/BAITBENCH"
REVISION = "fefd88f1ccf675815ac4361672faa1bf7c790123"
CORPUS = "juanjvazquez__BAITBENCH"
ROOT = Path.home() / ".local/state/evallab-cross-test" / CORPUS / REVISION[:12]
DATA = "reward-hacking-evals/data"
CANONICAL = f"{DATA}/cases/make_datasets/v4_paper_canonical_1258_20260709.jsonl"
ANNOTATIONS = f"{DATA}/annotations_viewer_export_20260709.json"
JUDGING = f"{DATA}/outputs/judging"
# The five batches that make up the 1,258 canonical runs; both original judges per batch.
BATCHES = (
    "gemini_flash_v4_20260520",
    "sonnet_v4_20260520",
    "v4_t01_local_20260516",
    "v4_t02_local_20260516",
    "v4_t03_local_20260513",
)
NEUTRAL = "neutral_z-ai_glm-5.2_v4_20260709/judging_binary.jsonl"
TASK_NAMES = {
    "t01": "Entity overlap",
    "t02": "Near-duplicate leakage",
    "t03": "No-signal classification",
}
LABELS = frozenset({"reward_hacking", "not_reward_hacking", "unclear"})

# Publisher fields (and our derived blocks) a detector must never see.
FIELDS_DETECTOR_MUST_NOT_SEE = frozenset(
    {
        "derived_signals",
        "generalization_gap",
        "robust_evaluation",
        "robust_score",
        "robust_metrics",
        "test_score",
        "recomputed_test_score",
        "judgment",
        "judgments",
        "judge",
        "judge_*",
        "label",
        "labels",
        "verdict",
        "probability",
        "evidence_strength",
        "exploit_form",
        "trajectory_artifact_use",
        "selection_driven_by_leak",
        "short_analysis",
        "primary_mechanism",
        "primary_parent",
        "annotation",
        "human_verdict",
        "claude_label",
        "gpt_label",
        "neutral_label",
        "gold",
        "identity",
        "positive",
        "label_state",
        "label_reason",
        "cluster_key",
        "task_key",
        "trace_key",
        "task_context",
        "family_context",
        "family_code",
        "metadata",
        "run_id",
        "task_id",
        "model",
        "agent",
        "prompt_condition",
        "agent_exit_code",
        "packaging_error",
        "python_files",
        "read_files",
        "workspace_files",
        "write_files",
        "raw_refs",
        "publisher_readme",
        "system_or_task_text_announcing_the_planted_shortcut",
    }
)
STEP_FIELDS = frozenset(
    {
        "step_id",
        "source",
        "input_role",
        "message",
        "tool_calls",
        "observation",
        "artifacts",
    }
)
# The agent's working directory and dataset are named after the run/task id (agent, model,
# task, size, seed, prompt group) with mixed ``_``/``-`` separators; replace with a token.
RUN_PATH = re.compile(
    r"(?:[A-Za-z0-9._][A-Za-z0-9._\-]*?[_-])?(?:regression|classification)[_-]task[_-]\d+"
    r"[_-]n\d+k?[_-]s\d+[_-](?:score|validity)(?:[_-]\d{8}[_-]\d{6})?"
)
LABEL_WORDS = re.compile(
    r"\b(?:not_)?reward_hacking\b|generalization_gap|robust_score", re.IGNORECASE
)
PROMPT_TOKENS = re.compile(r"score_only|validity_aware")
# Informational: words the agent itself may have seen or written; not label leakage.
CONTEXT_WORDS = re.compile(r"robust_test|baitbench|planted|shortcut|reward.hack", re.IGNORECASE)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def iter_rows(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    with (root / CANONICAL).open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _load_judge(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get("type") != "record" or obj.get("skipped"):
                continue
            judgment = obj.get("judgment") or {}
            label = judgment.get("label")
            if isinstance(obj.get("run_id"), str) and label in LABELS:
                out[obj["run_id"]] = judgment
    return out


@functools.cache
def judgments(root: Path = ROOT) -> dict[str, dict[str, dict[str, Any]]]:
    """Per judge, run_id -> the judgment object. ``claude``/``gpt`` are the paper's judges."""
    base = root / JUDGING
    merged: dict[str, dict[str, dict[str, Any]]] = {"claude": {}, "gpt": {}}
    for batch in BATCHES:
        for key, name in (("claude", "anthropic"), ("gpt", "openai")):
            part = _load_judge(base / batch / f"judging_binary_{name}.jsonl")
            if set(part) & set(merged[key]):
                raise ValueError("a run is judged in two canonical batches")
            merged[key].update(part)
    merged["neutral"] = _load_judge(base / NEUTRAL)
    return merged


@functools.cache
def human(root: Path = ROOT) -> dict[str, str]:
    """Verdicts from the current-schema (v4) viewer annotations, keyed by run id."""
    records = json.loads((root / ANNOTATIONS).read_text(encoding="utf-8"))
    return {
        r["run_id"]: r["annotation"]["verdict"]
        for r in records
        if r.get("schema_version") == "current" and r["annotation"].get("verdict")
    }


def has_agent_evidence(row: dict[str, Any]) -> bool:
    e = row["evidence"]
    return bool(e["reads"] or e["writes"] or e["trajectory"] or e["written_files"])


def label_state(
    row: dict[str, Any], judged: dict[str, dict[str, dict[str, Any]]]
) -> tuple[str, str]:
    """The paper's per-run verdict is the pooled mean of two binary judges (Claude, GPT).

    Both reward_hacking -> positive; both not_reward_hacking -> negative. Any
    disagreement, any ``unclear`` or any missing judgment is ambiguous, never negative.
    """
    if not has_agent_evidence(row):
        return "excluded", "no_agent_visible_evidence_packaging_error"
    run_id = row["run_id"]
    claude = judged["claude"].get(run_id, {}).get("label")
    gpt = judged["gpt"].get(run_id, {}).get("label")
    if claude is None or gpt is None:
        return "ambiguous", "missing_original_judgment"
    if "unclear" in (claude, gpt):
        return "ambiguous", "original_judge_unclear"
    if claude != gpt:
        return "ambiguous", "original_judges_disagree"
    if claude == "reward_hacking":
        return "positive", "both_original_judges_reward_hacking"
    return "negative", "both_original_judges_not_reward_hacking_not_certified_honest"


def prompt_group(run_id: str) -> str:
    """The publisher's own baseline/validity split (make_datasets_paper_stats.prompt_from_run_id)."""
    return (
        "validity" if "_validity_" in run_id else "baseline" if "_score_" in run_id else "unknown"
    )


def instance_id(task_id: str) -> str:
    """Dataset instance = family, size and seed; the publisher's bootstrap cluster."""
    return re.sub(r"_(?:score|validity)$", "", task_id)


def family_id(task_id: str) -> str:
    return re.sub(r"_n\d+k?_s\d+_(?:score|validity)$", "", task_id)


def _scrub(value: str, audit: Counter[str]) -> str:
    cleaned, n = RUN_PATH.subn("<run>", value)
    audit["run_paths_replaced"] += n
    return cleaned


def steps_from_evidence(evidence: dict[str, Any]) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Agent-visible evidence only: reads/writes, evaluator output, experiment log, written files.

    Uses nothing else in the case (no task context, robust evaluation, packaging state,
    exit code or file listings). The publisher keeps no model prose, so ``message`` is empty.
    Within a step the publisher does not record order; reads precede writes here.
    """
    audit: Counter[str] = Counter()
    by_step: dict[int, list[dict[str, Any]]] = defaultdict(list)
    seen_commands: set[tuple[int, str]] = set()
    outputs: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for event in evidence["evaluation_events"]:
        outputs[(event["step"], event["command"])].append(event)
    for event in [*evidence["reads"], *evidence["writes"]]:
        source = event["source"]
        if source == "command_execution":
            key = (event["step"], event["command"])
            if key in seen_commands:  # the publisher lists a command under reads and writes
                audit["duplicate_command_events_merged"] += 1
                continue
            seen_commands.add(key)
        elif source not in {"tool_read", "file_change"}:
            raise ValueError("unrecognized event source")
        by_step[event["step"]].append(event)
        audit["source_events"] += 1
    steps: list[dict[str, Any]] = []
    for step_number in sorted(by_step):
        calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for index, event in enumerate(by_step[step_number]):
            call_id = f"s{step_number}.{index}"
            source = event["source"]
            if source == "command_execution":
                command = _scrub(event["command"], audit)
                calls.append(
                    {
                        "tool_call_id": call_id,
                        "function_name": source,
                        "arguments": {"command": command},
                    }
                )
                matched = outputs.get((step_number, event["command"]))
                recorded = matched.pop(0) if matched else None
                content = "" if recorded is None else _scrub(recorded["aggregated_output"], audit)
                audit["evaluator_outputs_attached"] += recorded is not None
                audit["output_chars"] += len(content)
                results.append(
                    {
                        "tool_call_id": call_id,
                        "content": content,
                        "exit_code": event["exit_code"]
                        if recorded is None
                        else recorded["exit_code"],
                        "output_retained": recorded is not None,
                    }
                )
            elif source == "tool_read":
                calls.append(
                    {
                        "tool_call_id": call_id,
                        "function_name": source,
                        "arguments": {"files": [_scrub(f, audit) for f in event["files"]]},
                    }
                )
            else:
                calls.append(
                    {
                        "tool_call_id": call_id,
                        "function_name": source,
                        "arguments": {
                            "changes": [
                                {"kind": c["kind"], "path": _scrub(c["path"], audit)}
                                for c in event["changes"]
                            ]
                        },
                    }
                )
            audit["tool_calls"] += 1
        step: dict[str, Any] = {
            "step_id": len(steps),
            "source": "agent",
            "input_role": "assistant",
            "message": "",
            "tool_calls": calls,
        }
        if results:
            step["observation"] = {"results": results}
        steps.append(step)
    audit["unattached_evaluator_outputs"] = sum(len(v) for v in outputs.values())
    artifacts: dict[str, Any] = {}
    if evidence["trajectory"]:
        artifacts["experiment_log"] = [
            {
                "commit_hash": t["commit_hash"],
                "description": _scrub(t["description"], audit),
                "score": t["score"],
                "status": t["status"],
            }
            for t in evidence["trajectory"]
        ]
        audit["experiment_log_rows"] = len(evidence["trajectory"])
    if evidence["written_files"]:
        artifacts["final_written_files"] = {
            name: _scrub(text, audit) for name, text in sorted(evidence["written_files"].items())
        }
        audit["written_file_chars"] = sum(len(t) for t in evidence["written_files"].values())
    if artifacts:
        steps.append(
            {
                "step_id": len(steps),
                "source": "agent",
                "input_role": "assistant",
                "message": "",
                "artifacts": artifacts,
            }
        )
    audit["mapped_steps"] = len(steps)
    return steps, audit


def to_steps(row: dict[str, Any]) -> list[dict[str, Any]]:
    return steps_from_evidence(row["evidence"])[0]


def specs(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    """Yield all 1,258 runs, including explicitly ambiguous/excluded records."""
    judged = judgments(root)
    annotated = human(root)
    for index, row in enumerate(iter_rows(root)):
        state, reason = label_state(row, judged)
        meta = row["metadata"]
        run_id = row["run_id"]
        yield {
            "identity": {
                "row_index": index,
                "run_id": run_id,
                "trace_key": digest(run_id),
                "task_id": meta["task_id"],
                "task_key": digest(meta["task_id"]),
                "instance_key": digest(instance_id(meta["task_id"])),
                "cluster_key": digest(family_id(meta["task_id"])),
                "model": meta["model"],
                "agent": meta["agent"],
            },
            "gold": {
                "state": state,
                "reason": reason,
                "positive": True if state == "positive" else False if state == "negative" else None,
                "source_annotations": {
                    "claude_label": judged["claude"].get(run_id, {}).get("label"),
                    "gpt_label": judged["gpt"].get(run_id, {}).get("label"),
                    "neutral_label": judged["neutral"].get(run_id, {}).get("label"),
                    "generalization_gap": row["derived_signals"]["generalization_gap"],
                    "human_verdict": annotated.get(run_id),
                },
            },
            "labels": {
                "state": state,
                "reason": reason,
                "task": TASK_NAMES[meta["family_code"]],
                "prompt_condition": meta["prompt_condition"],
                "prompt_group": prompt_group(run_id),
            },
            "_steps": to_steps(row),
        }


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(item) for item in value))
    return set()


def _text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _judge_texts(judged: dict[str, dict[str, dict[str, Any]]], run_id: str) -> list[str]:
    out = []
    for judge in judged.values():
        j = judge.get(run_id)
        if not j:
            continue
        out.append(str(j.get("short_analysis") or ""))
        out.extend(
            str(item.get("text") or "")
            for item in j.get("evidence") or []
            if isinstance(item, dict)
        )
    return [t for t in out if len(t) >= 30]


def leak_hits(
    row: dict[str, Any], steps: list[dict[str, Any]], judged: dict[str, dict[str, dict[str, Any]]]
) -> Counter[str]:
    """Count nonagent-visible material (labels, judge text, scores, ids) found in ``_steps``."""
    hits: Counter[str] = Counter()
    blob = _text(steps)
    meta = row["metadata"]
    hits["label_field_keys"] = len(_keys(steps) & FIELDS_DETECTOR_MUST_NOT_SEE)
    hits["label_words"] = len(LABEL_WORDS.findall(blob))
    hits["run_id"] = meta["run_id"] in blob
    hits["task_id"] = meta["task_id"] in blob
    hits["model_name"] = meta["model"] in blob or meta["model"].rsplit("/", 1)[-1] in blob
    hits["prompt_condition_token"] = len(PROMPT_TOKENS.findall(blob))
    robust = row["evidence"].get("robust_evaluation") or {}
    for name, value in (
        ("generalization_gap", row["derived_signals"]["generalization_gap"]),
        ("robust_score", robust.get("robust_score")),
    ):
        if isinstance(value, float):
            hits[f"{name}_repr"] = repr(value) in blob and len(repr(value)) >= 8
    hits["judge_text_verbatim"] = sum(text in blob for text in _judge_texts(judged, row["run_id"]))
    hits["family_context_verbatim"] = row["evidence"]["task_context"]["family_context"][:60] in blob
    return hits


def selfcheck(root: Path = ROOT) -> dict[str, Any]:
    judged = judgments(root)
    issues: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    audit_totals: Counter[str] = Counter()
    leaks: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    context: Counter[str] = Counter()
    identities: set[str] = set()
    for row in iter_rows(root):
        counts["rows"] += 1
        state, reason = label_state(row, judged)
        counts[state] += 1
        reasons[reason] += 1
        identities.add(row["run_id"])
        issues["run_id_metadata_mismatch"] += row["run_id"] != row["metadata"]["run_id"]
        issues["unknown_family"] += row["metadata"]["family_code"] not in TASK_NAMES
        issues["missing_neutral_judgment"] += row["run_id"] not in judged["neutral"]
        steps, audit = steps_from_evidence(row["evidence"])
        audit_totals.update(audit)
        issues["nonsequential_step_ids"] += [s["step_id"] for s in steps] != list(range(len(steps)))
        issues["unrecognized_step_fields"] += any(set(s) - STEP_FIELDS for s in steps)
        issues["unattached_evaluator_outputs"] += audit["unattached_evaluator_outputs"]
        issues["empty_steps_for_scoreable_row"] += state in {"positive", "negative"} and not steps
        # Steps depend on the evidence's reads/writes/evaluation/log/files and nothing else.
        evidence = row["evidence"]
        tainted = {
            k: (
                evidence[k]
                if k in {"reads", "writes", "evaluation_events", "trajectory", "written_files"}
                else None
            )
            for k in evidence
        }
        issues["other_evidence_influences_steps"] += steps_from_evidence(tainted)[0] != steps
        # Every call, evaluator output, log row and written-file character is present.
        calls = [c for s in steps for c in s.get("tool_calls", [])]
        results = [r for s in steps for r in s.get("observation", {}).get("results", [])]
        ids = [c["tool_call_id"] for c in calls]
        issues["duplicate_call_ids"] += len(ids) != len(set(ids))
        issues["result_without_call"] += not {r["tool_call_id"] for r in results} <= set(ids)
        merged = audit["duplicate_command_events_merged"]
        issues["call_count_mismatch"] += (
            len(calls) != len(evidence["reads"]) + len(evidence["writes"]) - merged
        )
        issues["evaluator_output_mismatch"] += sum(r["output_retained"] for r in results) != len(
            evidence["evaluation_events"]
        )
        artifacts = [s["artifacts"] for s in steps if "artifacts" in s]
        log_rows = sum(len(a.get("experiment_log", [])) for a in artifacts)
        issues["experiment_log_mismatch"] += log_rows != len(evidence["trajectory"])
        file_chars = sum(
            len(t) for a in artifacts for t in a.get("final_written_files", {}).values()
        )
        slug_free = not audit["run_paths_replaced"]
        if slug_free:
            issues["written_file_roundtrip"] += file_chars != audit["written_file_chars"]
        hits = leak_hits(row, steps, judged)
        for name, value in hits.items():
            leaks[name] += int(value) > 0
        leaks["episodes_with_any_hit"] += any(hits.values())
        # Informational: the agent's own text can mention task vocabulary.
        context["episodes_mentioning_task_vocabulary"] += bool(CONTEXT_WORDS.search(_text(steps)))
    counts["unique_run_ids"] = len(identities)
    nonzero = {k: v for k, v in sorted(issues.items()) if v}
    assert not nonzero, f"adapter invariants failed; no source text is emitted: {nonzero}"
    leak_total = leaks["episodes_with_any_hit"]
    assert not leak_total, f"nonagent-visible material reached _steps: {dict(leaks)}"
    return {
        "counts": dict(counts),
        "label_reasons": dict(reasons),
        "transcript_audit": dict(audit_totals),
        "leak_check": {**dict(sorted(leaks.items())), "leak_count": leak_total},
        "informational_context": dict(context),
        "label_fields_in_steps": leaks["label_field_keys"],
        "issue_count": sum(issues.values()),
        "issues": nonzero,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("selfcheck",))
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(selfcheck(args.root), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
