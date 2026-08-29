"""Mechanical re-verification of every numeric claim in the Analyst roadmap memo.

WHY THIS EXISTS
Two figures in the pinned state reports this memo builds on had drifted by the
time it was written (RefusalCode 17 -> 19; trajectory-labels 38 -> 56), and both
were found by recomputation rather than by eye. A memo full of hand-transcribed
numbers decays the same way. This script derives every figure in the memo from
primary artifacts and fails if the memo and the repository disagree.

Run from the repository root:

    uv run python research/roadmap/verify_roadmap_claims.py

Exit 0 means every claim reproduces. Exit 1 lists each disagreement.
No billable model call, no Harbor execution, no test suite.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

from evallab.cohort import minimum_detectable_effect, required_tasks_for_effect

REPO = Path(__file__).resolve().parents[2]
MEMO = REPO / "research/roadmap/analyst-future-research-roadmap-2026-08-29.md"
RUNS = REPO / "research/evidence/runs"

# Expected values as stated in the memo. Every one is asserted against a
# recomputation below, so editing this table alone cannot make the check pass.
PILOT_TOTALS = {"n": 18, "reward_1": 15, "prompt": 880_748, "completion": 18_052, "cached": 678_080}
ATIF_TOTALS = {"documents": 18, "steps": 104, "tool_calls": 466}
MDE_AT_BASELINE_833 = {40: "0.1659", 50: "0.1543", 60: "0.1450", 80: "0.1307", 100: "0.1201"}
MDE_UNDEFINED_BELOW = (18, 20, 30)
REQUIRED_N = {
    0.05: (762, 536, 385),
    0.08: (269, 191, 140),
    0.10: (159, 115, 86),
    0.12: (101, 75, 58),
    0.15: (55, 45, 38),
    0.166: (40, 38, 37),
}
CODE_CITATIONS = [
    ("src/evallab/analysis_capability.py", r"clearance_n: int = 20", 387),
    ("src/evallab/interpretation/trajectory_acceptance.py", r"AUTO_ACCEPTANCE_ENABLED = False", 18),
    ("src/evallab/harbor_network.py", r"darwin-docker-cannot-enforce-no-network", 70),
    ("src/evallab/execution_contracts.py", r"allow_billable: bool = False", 209),
]
REFUSAL_CODE_COUNT = 19
GOLDSET_BLOCKERS = 5
KEYED_CALIBRATION_ITEMS = 44
LABELS = {"total": 56, "attributed": 27, "legacy": 29}


def _zai_cells() -> list[str]:
    return sorted(b for b in os.listdir(RUNS) if b.startswith("zai-flash-"))


def check_pilot_outcomes(fail: list[str]) -> None:
    """Rewards and token totals, read from verifier_result and agent_result."""
    tot = dict.fromkeys(("n", "reward_1", "prompt", "completion", "cached"), 0)
    null_cost = 0
    for bundle in _zai_cells():
        for trial in sorted(os.listdir(RUNS / bundle)):
            result = RUNS / bundle / trial / "result.json"
            if not result.is_file():
                continue
            doc = json.loads(result.read_text(encoding="utf-8"))
            reward = ((doc.get("verifier_result") or {}).get("rewards") or {}).get("reward")
            if reward is None:
                continue
            agent = doc.get("agent_result") or {}
            tot["n"] += 1
            tot["reward_1"] += reward == 1.0
            tot["prompt"] += agent.get("n_input_tokens") or 0
            tot["completion"] += agent.get("n_output_tokens") or 0
            tot["cached"] += agent.get("n_cache_tokens") or 0
            null_cost += agent.get("cost_usd") is None
    for key, expected in PILOT_TOTALS.items():
        if tot[key] != expected:
            fail.append(f"pilot {key}: measured {tot[key]}, memo says {expected}")
    if null_cost != PILOT_TOTALS["n"]:
        fail.append(
            f"cost_usd null on {null_cost}/{PILOT_TOTALS['n']}; the no-cost claim needs all"
        )


def check_atif_projection(fail: list[str]) -> None:
    """Step and tool-call counts, walked from the promoted trajectories."""
    seen = dict.fromkeys(("documents", "steps", "tool_calls"), 0)
    versions: set[str] = set()
    for bundle in _zai_cells():
        for trial in sorted(os.listdir(RUNS / bundle)):
            traj = RUNS / bundle / trial / "agent" / "trajectory.json"
            if not traj.is_file():
                continue
            doc = json.loads(traj.read_text(encoding="utf-8"))
            seen["documents"] += 1
            versions.add(str(doc.get("schema_version")))
            steps = doc.get("steps") or []
            seen["steps"] += len(steps)
            for step in steps:
                seen["tool_calls"] += len(step.get("tool_calls") or [])
    for key, expected in ATIF_TOTALS.items():
        if seen[key] != expected:
            fail.append(f"ATIF {key}: measured {seen[key]}, memo says {expected}")
    if versions != {"ATIF-v1.7"}:
        fail.append(f"ATIF versions {sorted(versions)}; memo claims all ATIF-v1.7")


def check_sizing(fail: list[str]) -> None:
    """MDE and required-n tables, from the repository's own estimators."""
    for n_tasks, expected in MDE_AT_BASELINE_833.items():
        mde = minimum_detectable_effect(
            n_tasks=n_tasks, k=1, baseline=0.833, alpha=0.05, target_power=0.8
        )
        if mde is None or f"{mde:.4f}" != expected:
            fail.append(f"MDE n={n_tasks}: computed {mde}, memo says {expected}")
    for n_tasks in MDE_UNDEFINED_BELOW:
        mde = minimum_detectable_effect(
            n_tasks=n_tasks, k=1, baseline=0.833, alpha=0.05, target_power=0.8
        )
        if mde is not None:
            fail.append(f"MDE defined at n={n_tasks} ({mde}); memo says undefined")
    for effect, expected_row in REQUIRED_N.items():
        for rho, expected in zip((0.0, 0.3, 0.5), expected_row, strict=True):
            got = required_tasks_for_effect(
                baseline=0.833,
                attempt_effect=effect,
                k=1,
                alpha=0.05,
                target_power=0.8,
                pair_correlation=rho,
            )
            if got != expected:
                fail.append(f"required n effect={effect} rho={rho}: {got}, memo says {expected}")


def check_design_effect_gap(fail: list[str]) -> None:
    """The memo's central sizing argument: correlation can only SHRINK n."""
    source = (REPO / "src/evallab/power.py").read_text(encoding="utf-8")
    for term in ("icc", "design_effect", "deff", "cluster"):
        if re.search(term, source, re.IGNORECASE):
            fail.append(f"power.py contains {term!r}; memo claims the term is absent")
    independent = required_tasks_for_effect(
        baseline=0.833, attempt_effect=0.10, k=1, pair_correlation=0.0
    )
    correlated = required_tasks_for_effect(
        baseline=0.833, attempt_effect=0.10, k=1, pair_correlation=0.5
    )
    if independent is None or correlated is None or correlated >= independent:
        fail.append(
            "pair_correlation did not reduce required n; the memo's argument that it cannot "
            "represent clustering inflation depends on this direction"
        )


def check_code_citations(fail: list[str]) -> None:
    for rel, pattern, expected_line in CODE_CITATIONS:
        source = (REPO / rel).read_text(encoding="utf-8")
        match = re.search(pattern, source)
        if match is None:
            fail.append(f"{rel}: pattern {pattern!r} not found")
            continue
        line = source[: match.start()].count("\n") + 1
        if line != expected_line:
            fail.append(f"{rel}: pattern at line {line}, memo cites {expected_line}")


def check_refusal_enum(fail: list[str]) -> None:
    source = (REPO / "src/evallab/analysis_capability.py").read_text(encoding="utf-8")
    start = source.index("class RefusalCode")
    block = source[start : source.index("\n\n\n", start)]
    count = len(re.findall(r'^\s+[A-Z_]+\s*=\s*"', block, re.MULTILINE))
    if count != REFUSAL_CODE_COUNT:
        fail.append(f"RefusalCode has {count} values, memo says {REFUSAL_CODE_COUNT}")


def check_goldset(fail: list[str]) -> None:
    path = REPO / "research/goldset/labeling_package.json"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    memo = MEMO.read_text(encoding="utf-8")
    if digest not in memo:
        fail.append(f"goldset digest {digest[:16]}… not cited in the memo")
    package = json.loads(path.read_text(encoding="utf-8"))
    readiness = package["readiness"]
    if readiness["readiness"] != "NOT_READY":
        fail.append(f"goldset readiness is {readiness['readiness']}, memo says NOT_READY")
    if len(readiness["blockers"]) != GOLDSET_BLOCKERS:
        fail.append(
            f"goldset has {len(readiness['blockers'])} blockers, memo says {GOLDSET_BLOCKERS}"
        )


def check_corpora(fail: list[str]) -> None:
    families = ("checkout-pool-exhaustion", "retry-storm-backlog")
    keys = sum(
        len(os.listdir(REPO / "research/calibration" / family / "answer-keys"))
        for family in families
    )
    if keys != KEYED_CALIBRATION_ITEMS:
        fail.append(f"{keys} answer keys, memo says {KEYED_CALIBRATION_ITEMS}")
    labels_dir = REPO / "research/calibration/trajectory-labels"
    files = [f for f in os.listdir(labels_dir) if f.endswith(".json")]
    attributed = 0
    for name in files:
        doc = json.loads((labels_dir / name).read_text(encoding="utf-8"))
        if "labelled_by" in doc or "labeled_by" in doc:
            attributed += 1
    measured = {"total": len(files), "attributed": attributed, "legacy": len(files) - attributed}
    if measured != LABELS:
        fail.append(f"trajectory-labels {measured}, memo says {LABELS}")


def main() -> int:
    if not MEMO.is_file():
        print(f"memo not found: {MEMO}")
        return 1
    failures: list[str] = []
    for check in (
        check_pilot_outcomes,
        check_atif_projection,
        check_sizing,
        check_design_effect_gap,
        check_code_citations,
        check_refusal_enum,
        check_goldset,
        check_corpora,
    ):
        check(failures)
    if failures:
        print(f"{len(failures)} claim(s) do not reproduce:")
        for item in failures:
            print(f"  {item}")
        return 1
    print("all roadmap numeric claims reproduce from primary artifacts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
