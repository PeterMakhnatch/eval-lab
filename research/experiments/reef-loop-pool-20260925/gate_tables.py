#!/usr/bin/env python3
"""Regenerate gate-design.json for the reef-loop-pool proposal.

Deterministic: pure computation over src/evallab/power.py and the recorded
evidence anchors cited below. No host state, no model calls.

Anchors:
- exp04-check gate_table (research-context/reef/experiments/work/04-check/summary.json),
  pass rates [sieve] 32/60, [fib] 34/60, [csv] 39/60;
- HAR-72 DeepSeek A/A (har72-reef-gate-20260925/runs/har72-aa-deepseek-api/summary.json),
  pooled rates [1.0, 0.9799, 1.0], 5 repeats x 3 tasks, 0/30 publishes;
- exp05 round 1 (research-context/reef/experiments/work/05/summary.json),
  qwen3-coder:30b per-task outcomes 1/1, 1/1, 0/1, 0/1 on the four search tasks.

Usage: uv run python research/experiments/reef-loop-pool-20260925/gate_tables.py
"""

from __future__ import annotations

import json
from pathlib import Path

from evallab.power import (
    PairedGateRule,
    paired_gate_plan_grid,
    paired_gate_publish_probability,
)

HERE = Path(__file__).resolve().parent

EXP04_CHECK_RATES = [32 / 60, 34 / 60, 39 / 60]
HAR72_CEILING_RATES = [1.0, 0.98, 1.0]
EXP05_THIN_RATES = [1.0, 1.0, 0.0, 0.0]
POOLED_RATES = [0.5, 0.5, 0.5, 0.5]
MIDRANGE_RATES = [0.533, 0.567, 0.65, 0.6]

REEF_DEFAULT = PairedGateRule.reef_default()
SIGN_GATE = PairedGateRule.sign_gate(alpha=0.05, veto=1)

#: Recorded exp04-check gate_table rows for exact-match validation.
EXP04_CHECK_TABLE = [
    ("null (A/A)", "wins>losses", 1, 0.34),
    ("null (A/A)", "wins>losses", 5, 0.426),
    ("null (A/A)", "sign test p<0.05", 5, 0.019),
    ("null (A/A)", "sign test p<0.05", 10, 0.03),
    ("+0.2 per task", "wins>losses", 1, 0.543),
    ("+0.2 per task", "wins>losses", 5, 0.845),
    ("+0.2 per task", "sign test p<0.05", 5, 0.169),
    ("+0.2 per task", "sign test p<0.05", 10, 0.404),
    ("+0.4 per task", "wins>losses", 1, 0.761),
    ("+0.4 per task", "wins>losses", 5, 0.996),
    ("+0.4 per task", "sign test p<0.05", 5, 0.681),
    ("+0.4 per task", "sign test p<0.05", 10, 0.977),
]


def publish(
    rates: list[float], lift: float, repeats: int, rule: PairedGateRule
) -> float:
    return paired_gate_publish_probability(
        current_pass_rates=rates,
        candidate_pass_rates=[min(1.0, rate + lift) for rate in rates],
        repeats=repeats,
        rule=rule,
    ).publish_probability


def exp04_validation() -> dict[str, object]:
    rows = []
    for label, gate, repeats, recorded in EXP04_CHECK_TABLE:
        lift = {"null (A/A)": 0.0, "+0.2 per task": 0.2, "+0.4 per task": 0.4}[label]
        rule = REEF_DEFAULT if gate.startswith("wins>losses") else PairedGateRule.sign_gate(veto=None)
        computed = publish(EXP04_CHECK_RATES, lift, repeats, rule)
        rows.append(
            {
                "candidate": label,
                "gate": f"{gate}, {repeats} ep/task",
                "recorded_p_publish": recorded,
                "computed_p_publish": round(computed, 6),
                "matches": abs(round(computed, 3) - recorded) < 5e-4,
            }
        )
    return {
        "source": "research-context/reef/experiments/work/04-check/summary.json gate_table",
        "rates": {"[sieve]": "32/60", "[fib]": "34/60", "[csv]": "39/60"},
        "rows": rows,
        "all_match": all(row["matches"] for row in rows),
    }


def har72_ceiling_validation() -> dict[str, object]:
    outcome = paired_gate_publish_probability(
        current_pass_rates=HAR72_CEILING_RATES,
        candidate_pass_rates=HAR72_CEILING_RATES,
        repeats=5,
        rule=SIGN_GATE,
    )
    return {
        "source": "har72-reef-gate-20260925/runs/har72-aa-deepseek-api/summary.json",
        "observed": "0 publishes / 30 trials; pooled rates [1.0, 0.9799, 1.0]; 5 repeats x 3 tasks",
        "computed_false_publish_rate": outcome.publish_probability,
        "computed_veto_probability": outcome.veto_probability,
        "caveat": (
            "At a ceiling pass rate almost every pair ties, so this A/A says little "
            "about noise at mid-range pass rates; the pool needs mid-range tasks for "
            "the target model. RE's independent implementation predicted 2.94e-9 "
            "from the unrounded pooled rate 0.9799."
        ),
    }


def reef_default_fpr_growth() -> list[dict[str, object]]:
    rows = []
    for tasks in (4, 8, 12, 16, 24, 32):
        rates = [MIDRANGE_RATES[i % 4] for i in range(tasks)]
        rows.append(
            {
                "tasks": tasks,
                "fpr_reps1": round(publish(rates, 0.0, 1, REEF_DEFAULT), 3),
                "fpr_reps5": round(publish(rates, 0.0, 5, REEF_DEFAULT), 3),
            }
        )
    return rows


def requirement_grids() -> dict[str, object]:
    scenarios = {
        "exp05-thin-qwen3coder30b": EXP05_THIN_RATES,
        "pooled-0.5": POOLED_RATES,
        "midrange-04check-like": MIDRANGE_RATES,
    }
    out: dict[str, object] = {}
    for name, rates in scenarios.items():
        scene: dict[str, object] = {"rates": rates}
        for lift in (0.2, 0.4):
            for rule_name, rule in (
                ("reef-default", REEF_DEFAULT),
                ("har72-sign-veto1", SIGN_GATE),
            ):
                rows = paired_gate_plan_grid(
                    pass_rates=rates,
                    per_task_lift=lift,
                    rule=rule,
                    max_tasks=16,
                    max_repeats=8,
                )
                ok = [row for row in rows if row.meets_requirements]
                per_repeats: dict[int, int] = {}
                for row in ok:
                    per_repeats.setdefault(row.repeats, row.tasks)
                scene[f"lift{lift}:{rule_name}"] = {
                    "requirement_met_within_16x8": bool(ok),
                    "min_tasks_by_repeats": dict(sorted(per_repeats.items())),
                    "min_cell": (
                        lambda best: None
                        if best is None
                        else {
                            "tasks": best.tasks,
                            "repeats": best.repeats,
                            "episodes_per_side": best.episodes_per_side,
                        }
                    )(min(ok, key=lambda row: (row.episodes_per_side, row.tasks)) if ok else None),
                }
        out[name] = scene
    return out


def first_run_cell() -> dict[str, object]:
    cell: dict[str, object] = {"design": "4 dev tasks x k=5 repeats per side"}
    for name, rates in (
        ("exp05-thin-empirical", EXP05_THIN_RATES),
        ("midrange-assumption", MIDRANGE_RATES),
    ):
        row = {
            "rates": rates,
            "reef_default": {
                "fpr": round(publish(rates, 0.0, 5, REEF_DEFAULT), 3),
                "power_at_+0.2": round(publish(rates, 0.2, 5, REEF_DEFAULT), 3),
                "power_at_+0.4": round(publish(rates, 0.4, 5, REEF_DEFAULT), 3),
            },
            "har72_sign_veto1": {
                "fpr": round(publish(rates, 0.0, 5, SIGN_GATE), 3),
                "power_at_+0.2": round(publish(rates, 0.2, 5, SIGN_GATE), 3),
                "power_at_+0.4": round(publish(rates, 0.4, 5, SIGN_GATE), 3),
            },
        }
        cell[name] = row
    return cell


def decision_cost() -> dict[str, object]:
    trials = {"4x5": 40, "12x8": 192, "11x8": 176}
    routes = {
        "local-ollama-qwen3-coder-30b (exp05-observed)": {
            "seconds_per_episode": 76.1,
            "seconds_per_episode_basis": "exp05 round-1 traffic seconds 60.6/56.6/91.5/119.0, median 76.05",
            "usd_per_trial": 0.0,
            "concurrency": 2,
        },
        "metered-zai-glm-5.3-flash (estimate)": {
            "input_tokens_per_trial": 17000,
            "output_tokens_per_trial": 2500,
            "input_tokens_per_trial_basis": "HAR-71 native runs: 14.7k-20.0k in / 2.0k-3.3k out per trial",
            "usd_per_trial": round((17000 * 0.15 + 2500 * 0.5) / 1e6, 4),
            "price_basis": "execution_contracts.py ZAI_OPENAPI_{INPUT,OUTPUT}_COST_MICROS_PER_MILLION = 150k/500k ($0.15/M in, $0.50/M out)",
            "seconds_per_episode": None,
            "wall_time": "not yet measured on this pool [INFERENCE]",
            "concurrency": 2,
        },
    }
    out: dict[str, object] = {}
    for label, route in routes.items():
        per_episode = route.get("seconds_per_episode")
        entry: dict[str, object] = {"usd_per_trial": route["usd_per_trial"]}
        if per_episode is not None:
            entry["wall_hours_by_design"] = {
                k: round(2 * t * per_episode / route["concurrency"] / 3600, 2)
                for k, t in trials.items()
            }
        entry["usd_by_design"] = {k: round(2 * t * route["usd_per_trial"], 2) for k, t in trials.items()}
        entry["details"] = {k: v for k, v in route.items() if k not in ("seconds_per_episode", "usd_per_trial", "concurrency")}
        out[label] = entry
    out["trials_per_decision"] = {k: 2 * t for k, t in trials.items()}
    out["deepseek_note"] = (
        "DeepSeek V4.1 flash is wired for mini-swe-agent (container-side proxy) in main, "
        "not for Terminus 2. RE's HAR-72 A/A drove DeepSeek through Reef's own serve "
        "(31.3 s median per 30-episode gate evaluation on tutorial tasks). A Terminus 2 "
        "DeepSeek/GLM-Flash host-side route requires runner work owned by ReefTraffic."
    )
    return out


def main() -> int:
    report = {
        "schema_version": 1,
        "generated_by": "research/experiments/reef-loop-pool-20260925/gate_tables.py",
        "calculator": "evallab.power.paired_gate_publish_probability (exact; never simulated)",
        "rules": {
            "reef-default": "publish iff wins - losses > 0 (Reef score_comparison; backend.py ScoreComparisonMixin.decide)",
            "har72-sign-veto1": (
                "one-sided exact sign test at alpha=0.05 on valid pairs, ties dropped, "
                "plus per-task regression veto (current passed every repeat and >=1 "
                "candidate failure on that task)"
            ),
        },
        "assumptions": [
            "Episodes are independent Bernoulli(pass) per side per repeat; the gate reads only valid episodes, and this planner assumes full validity (infra failures only shrink evidence).",
            "Grid task counts tile the rate profile cyclically: a row's 'tasks' is a rate mix, not a claim that specific new tasks exist.",
        ],
        "exp04_check_validation": exp04_validation(),
        "har72_ceiling_validation": har72_ceiling_validation(),
        "reef_default_fpr_growth_midrange": reef_default_fpr_growth(),
        "requirement_grids_fpr_le_0.05_power_ge_0.8": requirement_grids(),
        "first_run_cell": first_run_cell(),
        "decision_cost": decision_cost(),
    }
    target = HERE / "gate-design.json"
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
