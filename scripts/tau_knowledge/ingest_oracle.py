#!/usr/bin/env python3
"""Normalize one successful tau oracle trial into shared typed facts.

No ATIF is synthesized: Harbor's oracle trial exposes a tau2 runtime state and
verifier result but not agent/trajectory.json. The missing ATIF remains explicit
in EvidenceCoverage, keeping this trial unavailable for behavior-rate analysis.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from evallab.semantic_facts import CapabilityOpportunity, NormalizedFactBundle, project_fact_bundle


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trial_dir", type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--criteria-digest", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    trial = args.trial_dir.resolve()
    state = trial / "agent/tau3_runtime_state.json"
    verifier = trial / "verifier/result.json"
    result = trial / "result.json"
    for path in (state, verifier, result):
        if not path.is_file():
            raise SystemExit(f"missing oracle evidence: {path}")
    state_payload = json.loads(state.read_text(encoding="utf-8"))
    verifier_payload = json.loads(verifier.read_text(encoding="utf-8"))
    result_payload = json.loads(result.read_text(encoding="utf-8"))
    trial_id = str(result_payload["id"])
    messages = state_payload.get("messages")
    observed_actions = []
    if isinstance(messages, list):
        for message in messages:
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("name"):
                    observed_actions.append(str(call["name"]))
    reward = verifier_payload.get("reward")
    required = ("source_task", "runtime_state", "verifier_reward", "atif_trajectory", "valid_termination", "observed_action")
    missing_list = []
    if not (trial / "agent/trajectory.json").is_file():
        missing_list.append("atif_trajectory")
    if state_payload.get("termination_reason") not in {"agent_stop", "user_stop"}:
        missing_list.append("valid_termination")
    if not observed_actions:
        missing_list.append("observed_action")
    missing = tuple(missing_list)
    opportunity = CapabilityOpportunity(
        opportunity_id=f"{trial_id}:credit_card_action",
        trial_id=trial_id,
        benchmark="tau-Knowledge",
        construct="credit_card_action",
        start_step=0,
        end_step=max(0, len(messages or []) - 1),
        eligible=True if observed_actions else None,
        required_evidence=required,
        missing_evidence=missing,
        source_ref=str(state),
        source_digest=digest(state),
        provenance_kind="mechanical",
    )
    bundle = NormalizedFactBundle(capability_opportunities=(opportunity,))
    output = args.output.resolve()
    paths = project_fact_bundle(bundle, output)
    (output / "oracle-observed.json").write_text(
        json.dumps(
            {
                "trial_id": trial_id,
                "task_id": args.task_id,
                "criteria_digest": args.criteria_digest,
                "source_ref": str(state),
                "source_digest": digest(state),
                "verifier_ref": str(verifier),
                "verifier_digest": digest(verifier),
                "verifier_reward": reward,
                "observed_action_names": sorted(set(observed_actions)),
                "atif_path": str(trial / "agent/trajectory.json") if (trial / "agent/trajectory.json").is_file() else None,
                "atif_observed": (trial / "agent/trajectory.json").is_file(),
                "missing_evidence": list(missing),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({name: str(path) for name, path in paths.items()}, indent=2))
    return 0


if __name__ == "__main__":
    main()
