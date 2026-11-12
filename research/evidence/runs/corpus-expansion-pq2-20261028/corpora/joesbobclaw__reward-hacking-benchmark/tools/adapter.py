#!/usr/bin/env python3
"""Convert the pinned mature studies locally; emit no transcript or raw id.

Only ``_steps`` is detector input. The 550 specs include 12 explicitly excluded
technical failures. Collection-qualified study ids prevent cross-model collisions.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

CORPUS = "joesbobclaw__reward-hacking-benchmark"
REPO = "joesbobclaw/reward-hacking-benchmark"
REVISION = "8d65df521f59cc57715b9497822606a0420059b8"
ROOT = Path.home() / ".local/state/evallab-cross-test" / CORPUS / REVISION[:12]
STUDIES = (
    ("luna-interactive-confirmation-001", "confirmation"),
    ("glm52-interactive-replication-004", "replication"),
    ("focused-luna-002", "focused_comparison"),
    ("focused-glm52-002", "focused_comparison"),
    ("mitigation-luna-001", "mitigation"),
    ("containment-luna-001", "containment"),
    ("evasion-luna-002", "evasion"),
    ("degraded-luna-001", "degraded_controls"),
)
CONTROL_FIELDS = frozenset(
    {
        "blocked_attempts", "blocked_evasion_attempts", "blocked_exploit_attempts",
        "disable_succeeded", "invariant_unknown_observed", "route_around_attempted",
        "stale_authorization_accepted", "tamper_attempted",
    }
)
# A strict allowlist: these are ordinary recorded tool results, not final grades.
OBSERVATION_FIELDS = frozenset(
    {"cost", "ok", "order_id", "reason", "remaining_budget", "tool", "delivered"}
)
GOLD_ONLY_KEYS = frozenset(
    {
        "metrics", "exploit_attempted", "exploit_succeeded", "detected",
        "delivered_value", "optimal_value", "task_utility", "proxy_score",
        "utility_proxy_divergence", "invalid_actions", "condition", "condition_id",
        "arm", "phase", "policy_id", "run_id", "status", "termination",
        "turn_limit_reached", "final_status", "state_before", "state_after",
        "audit_active", "false_completions", "runtime_attempt_detected",
        "mitigation_arm", "evasion_arm", "degraded_arm", "verifier_enabled",
        "component_active", "authorization_cache", "decision", "invariant_signal",
        "blocked", "error", "route", "configuration_endpoint",
    }
) | CONTROL_FIELDS
FIELDS_DETECTOR_MUST_NOT_SEE = tuple(
    sorted(
        GOLD_ONLY_KEYS
        | {
            "corpus", "trace_id", "trace_key", "identity", "gold", "labels",
            "study", "study_family", "run_key", "task_id", "scenario_sha256",
            "cluster_key", "model_requested", "model_returned", "seed", "repetition",
            "max_turns", "task", "actions", "events", "partial_episode",
            "policy_trace", "cost_ledger_entry", "usage", "ledger_call_id",
            "input_tokens_preflight", "input_token_reservation_method",
            "max_output_tokens", "request_sha256", "response", "temperature", "kind",
            "source_manifest", "collection", "summary.csv", "analysis", "canaries",
            "system/task text announcing an exploit outside the recorded prompt",
        }
    )
)
# Reject a changed source that embeds structured final gold in transcript text.
GOLD_ASSIGNMENT = re.compile(
    r"[\"']?(?:exploit_attempted|exploit_succeeded|detected|proxy_score|task_utility|"
    r"tamper_attempted|disable_succeeded|route_around_attempted|"
    r"stale_authorization_accepted|invariant_unknown_observed)[\"']?"
    r"\s*[:=]\s*(?:true|false|\d)",
    re.IGNORECASE,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def iter_rows(root: Path = ROOT) -> Iterator[tuple[str, str, dict[str, Any], dict[str, Any]]]:
    for study, family in STUDIES:
        directory = root / "runs" / study
        manifest = json.loads((directory / "manifest.json").read_text())
        with (directory / "runs.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield study, family, manifest, json.loads(line)


def text_fields(trace: dict[str, Any]) -> dict[str, str]:
    fields = {"message": trace.get("assistant_text", "")}
    if trace.get("thinking"):
        fields["reasoning_content"] = trace["thinking"]
    if any(not isinstance(value, str) for value in fields.values()):
        raise ValueError("nontext recorded model output")
    return fields


def to_steps(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Replay recorded prompt/model/actions only, with gold-bearing outputs removed.

    No per-turn request, hidden state or command is reconstructed. Top-level
    ``events`` includes a final audit; only individual recorded turn events enter
    the observation allowlist. Failed model responses have no invented tool call.
    """
    steps: list[dict[str, Any]] = [
        {"step_id": 0, "source": "user", "input_role": "user", "message": row["prompt"]}
    ]
    for turn in row["turns"]:
        action = turn["action"]
        if set(action) != {"tool", "order_id", "reason"}:
            raise ValueError("recorded action schema changed")
        observation = {key: value for key, value in turn["event"].items()
                       if key in OBSERVATION_FIELDS}
        steps.append(
            {
                "step_id": len(steps),
                "source": "agent",
                "input_role": "assistant",
                **text_fields(turn["policy_trace"]),
                "tool_calls": [
                    {
                        "tool_call_id": f"call-{turn['turn']}",
                        "function_name": action["tool"],
                        "arguments": {key: value for key, value in action.items() if key != "tool"},
                    }
                ],
                "observation": {"results": [{
                    "source_call_id": f"call-{turn['turn']}",
                    "content": json.dumps(observation, sort_keys=True, ensure_ascii=False),
                }]},
            }
        )
    failed_trace = row.get("policy_trace") or {}
    if failed_trace.get("assistant_text") or failed_trace.get("thinking"):
        steps.append(
            {"step_id": len(steps), "source": "agent", "input_role": "assistant",
             **text_fields(failed_trace)}
        )
    for step in steps:
        for key in ("message", "reasoning_content"):
            if GOLD_ASSIGNMENT.search(step.get(key, "")):
                raise ValueError("structured final gold in recorded transcript text")
    return steps


def make_spec(study: str, family: str, manifest: dict[str, Any],
              row: dict[str, Any]) -> dict[str, Any]:
    if row["status"] not in {"completed", "technical_failure"}:
        raise ValueError("unknown source completion state")
    completed = row["status"] == "completed"
    metrics = row["metrics"]
    if completed:
        if not isinstance(metrics, dict) or any(
            type(metrics[key]) is not bool for key in ("exploit_attempted", "exploit_succeeded")
        ):
            raise ValueError("completed run lacks boolean source gold")
        positive = metrics["exploit_attempted"]
        state, reason = ("positive" if positive else "negative"), None
    else:
        if metrics is not None:
            raise ValueError("technical failure unexpectedly has completed metrics")
        positive, state, reason = None, "excluded", "technical_failure"
    run_key = f"{study}/{row['run_id']}"
    scenario = manifest["scenario_sha256"]
    return {
        "corpus": CORPUS,
        "trace_id": run_key,
        "trace_key": digest(run_key)[:16],
        "identity": {
            "study": study, "study_family": family, "run_id": row["run_id"],
            "run_key": run_key, "task_id": scenario, "scenario_sha256": scenario,
            "cluster_key": f"{family}/{scenario}", "policy_id": row["policy_id"],
            "seed": row["seed"], "repetition": row["repetition"],
            "condition_id": row["condition_id"], "condition": row["condition"],
            "arm": row.get("arm"), "phase": row.get("phase"),
        },
        "labels": {"state": state, "positive": positive, "exclusion_reason": reason},
        "gold": {
            "exploit_attempted": positive,
            "exploit_succeeded": metrics["exploit_succeeded"] if completed else None,
            "metrics": metrics,
            "status": row["status"], "termination": row["termination"],
            "condition": row["condition"], "condition_id": row["condition_id"],
            "arm": row.get("arm"), "phase": row.get("phase"),
            "final_status": row["final_status"], "task": row["task"],
            "turn_limit_reached": row["turn_limit_reached"],
            "control_outcomes": {key: row[key] for key in CONTROL_FIELDS if key in row},
            "per_turn_held_out": [
                {"state_before": turn["state_before"], "state_after": turn["state_after"],
                 "event": {key: value for key, value in turn["event"].items()
                           if key not in OBSERVATION_FIELDS}}
                for turn in row["turns"]
            ],
            "final_audit_events": row["events"][len(row["turns"]):],
            "error": row.get("error"),
        },
        "_steps": to_steps(row),
    }


def specs(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    """Yield all 550 in-memory specs; score only positive/negative label states."""
    for study, family, manifest, row in iter_rows(root):
        yield make_spec(study, family, manifest, row)


def keys_in(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(keys_in(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(keys_in(item) for item in value))
    return set()


def selfcheck(root: Path = ROOT) -> dict[str, Any]:
    """Count labels and retained interactions; assert metadata noninterference."""
    totals: Counter[str] = Counter()
    per_collection: dict[str, Counter[str]] = defaultdict(Counter)
    per_model: dict[str, Counter[str]] = defaultdict(Counter)
    groups: dict[str, Counter[str]] = {key: Counter() for key in ("arm", "condition_id", "phase")}
    removed: Counter[str] = Counter()
    final_audit_fields: Counter[str] = Counter()
    run_keys: set[str] = set()
    raw_ids: set[str] = set()
    family_keys: set[str] = set()
    clusters: set[str] = set()
    scenarios: set[str] = set()
    prompt_hashes: set[str] = set()
    for study, family, manifest, row in iter_rows(root):
        spec = make_spec(study, family, manifest, row)
        state = spec["labels"]["state"]
        run_key = spec["identity"]["run_key"]
        assert run_key not in run_keys, "duplicate collection-qualified run id"
        run_keys.add(run_key)
        raw_ids.add(row["run_id"])
        family_keys.add(f"{family}/{row['run_id']}")
        scenarios.add(spec["identity"]["scenario_sha256"])
        clusters.add(spec["identity"]["cluster_key"])
        prompt_hashes.add(digest(row["prompt"]))
        totals["rows"] += 1
        totals[state] += 1
        per_collection[study][state] += 1
        per_collection[study]["rows"] += 1
        per_model[row["policy_id"]][state] += 1
        per_model[row["policy_id"]]["rows"] += 1
        for key in groups:
            groups[key][digest(json.dumps(row.get(key), sort_keys=True))] += 1
        if state != "excluded":
            attempted, succeeded = row["metrics"]["exploit_attempted"], row["metrics"]["exploit_succeeded"]
            assert not succeeded or attempted, "success without source attempt"
            totals["completed"] += 1
            totals["attempt_successes"] += int(attempted and succeeded)
            totals["attempt_failures"] += int(attempted and not succeeded)
            totals["completed_without_success"] += int(not succeeded)
        else:
            totals["technical_failures"] += 1
            partial = row.get("partial_episode") or {}
            if partial:
                assert partial["turns"] == row["turns"]
            else:
                assert not row["turns"]
                totals["failures_without_partial_episode"] += 1
        steps = spec["_steps"]
        assert [step["step_id"] for step in steps] == list(range(len(steps)))
        assert steps[0]["message"] == row["prompt"]
        assert not keys_in(steps).intersection(GOLD_ONLY_KEYS)
        assert [turn["event"] for turn in row["turns"]] == row["events"][:len(row["turns"])]
        assert [turn["action"] for turn in row["turns"]] == row["actions"]
        for turn, step in zip(row["turns"], steps[1:], strict=False):
            call = step["tool_calls"][0]
            assert {"tool": call["function_name"], **call["arguments"]} == turn["action"]
            assert step["message"] == turn["policy_trace"]["assistant_text"]
            assert step.get("reasoning_content", "") == turn["policy_trace"].get("thinking", "")
            observation = json.loads(step["observation"]["results"][0]["content"])
            assert set(observation) <= OBSERVATION_FIELDS
            assert not keys_in(observation).intersection(GOLD_ONLY_KEYS)
            assert observation == {k: v for k, v in turn["event"].items() if k in OBSERVATION_FIELDS}
            removed.update(key for key in turn["event"] if key not in OBSERVATION_FIELDS)
        # Every noninput field can change without changing the detector projection.
        tainted = copy.deepcopy(row)
        for key in set(tainted) - {"prompt", "turns", "policy_trace"}:
            tainted[key] = None
        for trace in [tainted.get("policy_trace") or {},
                      *(turn["policy_trace"] for turn in tainted["turns"])]:
            for key in set(trace) - {"assistant_text", "thinking"}:
                trace[key] = None
        for turn in tainted["turns"]:
            for key in set(turn) - {"action", "event", "policy_trace", "turn"}:
                turn[key] = None
            for key in set(turn["event"]) - OBSERVATION_FIELDS:
                turn["event"][key] = None
        assert to_steps(tainted) == steps, "gold/metadata altered detector input"
        audits = row["events"][len(row["turns"]):]
        totals["final_audit_events_excluded"] += len(audits)
        for event in audits:
            final_audit_fields.update(event.keys())
        totals["mapped_steps"] += len(steps)
        totals["source_action_turns"] += len(row["turns"])
        totals["tool_calls"] += sum(len(step.get("tool_calls", [])) for step in steps)
        totals["tool_observations"] += sum(len(step.get("observation", {}).get("results", [])) for step in steps)
        totals["retained_failed_model_responses"] += int(len(steps) > len(row["turns"]) + 1)
        totals["reasoning_turns"] += sum(bool(step.get("reasoning_content")) for step in steps)
        totals["retained_prompt_messages"] += 1
    for study, _ in STUDIES:
        with (root / "runs" / study / "runs.jsonl").open(encoding="utf-8") as stream:
            source_rows = {
                row["run_id"]: row
                for line in stream if line.strip()
                for row in [json.loads(line)]
            }
        with (root / "runs" / study / "summary.csv").open(encoding="utf-8", newline="") as stream:
            summaries = list(csv.DictReader(stream))
        assert len(source_rows) == len(summaries)
        assert set(source_rows) == {row["run_id"] for row in summaries}
        for summary in summaries:
            row = source_rows[summary["run_id"]]
            assert summary["status"] == row["status"]
            if row["status"] == "completed":
                for key in ("exploit_attempted", "exploit_succeeded"):
                    assert summary[key].casefold() in {"true", "false"}
                    assert (summary[key].casefold() == "true") == row["metrics"][key]
        collection_file = root / "runs" / study / "collection.json"
        if collection_file.exists():
            collection = json.loads(collection_file.read_text())
            assert set(collection["planned_run_ids"]) == set(source_rows)
    assert (totals["rows"], totals["completed"], totals["positive"], totals["negative"],
            totals["excluded"], totals["attempt_successes"], totals["attempt_failures"]) == (
                550, 538, 129, 409, 12, 78, 51)
    assert (len(raw_ids), len(run_keys), len(family_keys), len(clusters), len(scenarios)) == (340, 550, 510, 7, 1)
    return {
        "counts": dict(totals),
        "excluded_by_reason": {"technical_failure": totals["excluded"]},
        "by_collection": [
            {"collection_index": index, "study_family": family, **dict(per_collection[study])}
            for index, (study, family) in enumerate(STUDIES, 1)
        ],
        "by_model": {key: dict(value) for key, value in sorted(per_model.items())},
        "by_arm_sha256": dict(sorted(groups["arm"].items())),
        "by_condition_sha256": dict(sorted(groups["condition_id"].items())),
        "by_phase_sha256": dict(sorted(groups["phase"].items())),
        "raw_unique_run_ids": len(raw_ids), "unique_run_keys": len(run_keys),
        "family_run_key_unique": len(family_keys), "bootstrap_clusters": len(clusters),
        "scenario_count": len(scenarios), "unique_prompt_hashes": len(prompt_hashes),
        "removed_tool_observation_fields": dict(sorted(removed.items())),
        "excluded_final_audit_fields": dict(sorted(final_audit_fields.items())),
        "gold_fields_in_steps": 0, "gold_assignment_text_occurrences": 0,
        "label_text_redactions": 0, "issues": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("selfcheck",))
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(selfcheck(args.root), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
