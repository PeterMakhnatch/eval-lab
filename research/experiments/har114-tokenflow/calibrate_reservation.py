"""Calibrate the proxy input-token reservation divisor (HAR-114 follow-up).

$0: read-only over committed evidence worktrees; stdlib only. No trials,
no Modal deploys, no model calls.

Method: for each of the 82 runs in runs.jsonl, read the job-level
lab-metadata.json provider_usage.calls ledger (each settled call carries
both reserved_input_tokens — the proxy's byte-length estimate — and the
real input_tokens). Report the reserved/actual distribution, check
candidate divisors with ceil() so the bound stays an upper bound, and
replay the input-token gate to compare old vs new stop points.

Writes reservation-calibration.json next to this script.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import mean, median

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]

# Evidence roots. Read-only; mirrors analyze_runs.py in this directory.
HAR110 = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har110-live/runs")
HAR104 = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har104-runs/runs")
HAR81A = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs")
HAR81B = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs")
ROOTS = (HAR110, HAR104, HAR81A, HAR81B)

OUT = HERE / "reservation-calibration.json"


def find_job(job: str, trial: str) -> Path:
    candidates: list[Path] = []
    for root in ROOTS:
        candidate = root / job
        if candidate.is_dir():
            candidates.append(candidate)
    aborted = HAR110 / "_aborted-har110-verifier-digest"
    if aborted.is_dir():
        for sub in sorted(aborted.iterdir()):
            if sub.is_dir() and sub.name == job:
                candidates.append(sub)
    # Two records can share a job name (df7dfd07: one trial in the live
    # job, one in the aborted leftover); prefer the dir holding the trial.
    for candidate in candidates:
        if (candidate / trial).is_dir():
            return candidate
    if candidates:
        return candidates[0]
    raise FileNotFoundError(f"job dir not found in evidence roots: {job}")


def main() -> None:
    rows = [json.loads(line) for line in (HERE / "runs.jsonl").read_text().splitlines()]
    assert len(rows) == 82, f"expected 82 runs, got {len(rows)}"

    pairs: list[dict] = []  # one per reconciled call with real usage
    n_unresolved = 0
    n_zero_usage = 0
    jobs: dict[str, dict] = {}
    seen_ledgers: set[str] = set()
    for row in rows:
        job_dir = find_job(row["job"], row["trial"])
        with open(job_dir / "lab-metadata.json") as handle:
            usage = json.load(handle)["provider_usage"]
        # Key by directory: two records may share a job name with
        # distinct ledgers (live vs aborted leftover).
        jobs[str(job_dir)] = {"usage": usage, "stop": (row["token_flow"] or {}).get("stop")}
        if str(job_dir) in seen_ledgers:
            continue
        seen_ledgers.add(str(job_dir))
        for call in usage["calls"]:
            if call.get("state") != "reconciled":
                n_unresolved += 1
                continue
            actual = call.get("input_tokens", 0)
            if not actual:
                n_zero_usage += 1
                continue
            reserved = call["reserved_input_tokens"]
            pairs.append(
                {
                    "job": row["job"],
                    "call_id": call["call_id"],
                    "reserved": reserved,
                    "actual": actual,
                    "ratio": reserved / actual,
                }
            )

    ratios = sorted(pair["ratio"] for pair in pairs)

    def quantile(q: float) -> float:
        return ratios[min(len(ratios) - 1, int(q * len(ratios)))]

    stats = {
        "n_calls": len(pairs),
        "n_unresolved": n_unresolved,
        "n_zero_usage": n_zero_usage,
        "min": min(ratios),
        "p01": quantile(0.01),
        "p05": quantile(0.05),
        "median": median(ratios),
        "mean": mean(ratios),
        "max": max(ratios),
    }

    worst = sorted(pairs, key=lambda pair: pair["ratio"])[:10]

    # Candidate divisors with ceil(): new = ceil(reserved / divisor).
    divisors = {}
    for divisor in (2, 3):
        bad = [pair for pair in pairs if -(-pair["reserved"] // divisor) < pair["actual"]]
        divisors[str(divisor)] = {
            "under_reserved_calls": len(bad),
            "worst_margin": min(ratios) / divisor,
            "examples": bad[:5],
        }

    # Stop replay: for input-exhausted runs, the gate trips when
    # settled + held_unresolved + next_reservation > max_input_tokens.
    # Use each run's last settled call as the next-call proxy; under the
    # new rule both the held and the next reservation shrink by /2.
    divisor = 2
    old_settled: list[int] = []
    continued = 0
    input_runs = 0
    gate_explained = 0
    for _job, entry in jobs.items():
        stop = entry["stop"] or {}
        if stop.get("stop_reason") != "ceiling:input_tokens":
            continue
        input_runs += 1
        usage = entry["usage"]
        limit = usage["limits"]["max_input_tokens"]
        calls = usage["calls"]
        settled = sum(
            call.get("input_tokens", 0) for call in calls if call.get("state") == "reconciled"
        )
        old_settled.append(settled)
        held_old = sum(
            call["reserved_input_tokens"] for call in calls if call.get("state") != "reconciled"
        )
        held_new = sum(
            -(-call["reserved_input_tokens"] // divisor)
            for call in calls
            if call.get("state") != "reconciled"
        )
        metered = [
            call
            for call in calls
            if call.get("state") == "reconciled" and call.get("input_tokens", 0)
        ]
        last = metered[-1]
        if settled + held_old + last["reserved_input_tokens"] > limit:
            gate_explained += 1
        new_next = -(-last["reserved_input_tokens"] // divisor)
        if settled + held_new + new_next <= limit:
            continued += 1

    record = {
        "method": (
            "reserved_input_tokens vs input_tokens per settled call from "
            "job-level lab-metadata.json provider_usage.calls across the "
            "82 runs in runs.jsonl; stop replay uses last settled call as "
            "the next-call proxy"
        ),
        "model": "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B (all calls)",
        "ratio_reserved_over_actual": stats,
        "worst_calls": [
            {
                "job": pair["job"],
                "call_id": pair["call_id"],
                "reserved": pair["reserved"],
                "actual": pair["actual"],
                "ratio": pair["ratio"],
            }
            for pair in worst
        ],
        "divisors": divisors,
        "chosen_divisor": divisor,
        "stop_replay": {
            "input_exhausted_runs": input_runs,
            "old_gate_explained_by_last_call_proxy": gate_explained,
            "median_settled_input_at_old_stop": median(old_settled),
            "runs_continuing_past_observed_stop_under_new": continued,
        },
    }
    OUT.write_text(json.dumps(record, indent=2) + "\n")

    print(
        f"runs: {len(rows)} calls: {stats['n_calls']} "
        f"unresolved: {n_unresolved} zero-usage: {n_zero_usage}"
    )
    print(
        f"ratio min={stats['min']:.3f} p01={stats['p01']:.3f} "
        f"median={stats['median']:.3f} max={stats['max']:.3f}"
    )
    for name, div in divisors.items():
        print(
            f"divisor {name}: under-reserved={div['under_reserved_calls']} "
            f"worst_margin={div['worst_margin']:.3f}"
        )
    print(
        f"input-exhausted: {input_runs}, old stop explained: {gate_explained}, "
        f"continue under new: {continued}, "
        f"median settled at old stop: {median(old_settled):,.0f}"
    )
    print(f"wrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    sys.exit(main())
