#!/usr/bin/env python3
import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def verify(
    task_dir: Path,
    evidence_dir: Path,
    reward_dir: Path | None = None,
) -> dict[str, Any]:
    if reward_dir is None:
        reward_dir = Path("/logs/verifier")
    reward_dir.mkdir(parents=True, exist_ok=True)

    # Resolve target spec / golden specification
    spec_path = task_dir / "fixtures" / "target_spec.json"
    if not spec_path.exists():
        spec_path = task_dir / "target_spec.json"
    if not spec_path.exists() and (task_dir / "scenario.json").exists():
        scenario = json.loads((task_dir / "scenario.json").read_text(encoding="utf-8"))
        spec_data = {
            "target_entity": scenario["target_entity"],
            "target_attribute": scenario["target_attribute"],
            "expected_bound_value": scenario["latest_value"],
            "dose_bytes": scenario.get("dose_bytes", 4096),
        }
        spec_bytes = (task_dir / "scenario.json").read_bytes()
    elif spec_path.exists():
        spec_bytes = spec_path.read_bytes()
        spec_data = json.loads(spec_bytes.decode("utf-8"))
    else:
        res = {"reward": 0.0, "reason": "missing_target_spec_file"}
        _record(reward_dir, res)
        return res

    # Compute and verify truth spec digest
    truth_digest = f"sha256:{hashlib.sha256(spec_bytes).hexdigest()}"

    final_state_file = evidence_dir / "final-state.json"
    if not final_state_file.exists():
        res = {"reward": 0.0, "reason": "missing_final_state_evidence", "truth_digest": truth_digest}
        _record(reward_dir, res)
        return res

    try:
        final_state = json.loads(final_state_file.read_text(encoding="utf-8"))
    except Exception as exc:
        res = {"reward": 0.0, "reason": f"corrupt_final_state: {exc}", "truth_digest": truth_digest}
        _record(reward_dir, res)
        return res

    # Validate benchmark-events.jsonl for monotone event index and read/execute logs
    events_file = evidence_dir / "benchmark-events.jsonl"
    event_count = 0
    read_events = 0
    mutation_events = 0
    if events_file.exists():
        last_index = 0
        for line in events_file.read_text(encoding="utf-8").splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            try:
                ev = json.loads(line_str)
                event_count += 1
                ev_idx = ev.get("event_index", 0)
                if ev_idx <= last_index:
                    res = {"reward": 0.0, "reason": "non_monotone_event_indices", "truth_digest": truth_digest}
                    _record(reward_dir, res)
                    return res
                last_index = ev_idx
                if ev.get("event_type") in {"read_chunk", "get_context_chunk"}:
                    read_events += 1
                elif ev.get("event_type") in {"execute_mutation", "mutation"}:
                    mutation_events += 1
            except Exception:
                pass

    obs_entity = final_state.get("target_entity")
    obs_attr = final_state.get("target_attribute")
    obs_val = final_state.get("bound_value")

    exp_entity = spec_data.get("target_entity")
    exp_attr = spec_data.get("target_attribute")
    exp_val = spec_data.get("expected_bound_value")

    if obs_entity == exp_entity and obs_attr == exp_attr and obs_val == exp_val:
        res = {
            "reward": 1.0,
            "reason": "exact_latest_value_bound",
            "target_entity": obs_entity,
            "bound_value": obs_val,
            "truth_digest": truth_digest,
            "events_validated": event_count,
            "read_events": read_events,
            "mutation_events": mutation_events,
        }
    else:
        res = {
            "reward": 0.0,
            "reason": "mismatch",
            "expected": spec_data,
            "observed": final_state,
            "truth_digest": truth_digest,
        }

    _record(reward_dir, res)
    return res


def _record(reward_dir: Path, result: dict[str, Any]) -> None:
    reward_str = "1.0\n" if result["reward"] == 1.0 else "0.0\n"
    (reward_dir / "reward.txt").write_text(reward_str, encoding="utf-8")
    (reward_dir / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    cli_parser = argparse.ArgumentParser(description="Action-memory verifier entrypoint")
    cli_parser.add_argument("--task-dir", type=Path, default=Path("/tests"))
    cli_parser.add_argument("--evidence-dir", type=Path, default=Path("/app/output"))
    cli_parser.add_argument("--reward-dir", type=Path, default=Path("/logs/verifier"))
    cli_args = cli_parser.parse_args()

    v_res = verify(cli_args.task_dir, cli_args.evidence_dir, cli_args.reward_dir)
    sys.exit(0 if v_res["reward"] == 1.0 else 1)
