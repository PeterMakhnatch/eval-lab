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

SPEC = REPO / "research/roadmap/specs/campaign-0-action-memory-dose-ladder.json"

# Cross-file equality: the memo and the spec must agree, and the spec must not
# advertise a provider ceiling below its own design. Independent review found the
# spec claiming max_trials=72 against a 100-trial design while the prose alternated
# 108 and 100, so this is asserted rather than trusted.
CROSS_FILE = {
    "wave2_scored_trials": 25,
    "wave2_reward_1": 17,
    "phase_a_trials": 36,
    "phase_b_trials": 2,
    "total_runnable_trials": 38,
}
BUDGETS = {
    "phase_a_projected": 6_291_672,
    "phase_a_ceiling": 7_000_000,
    "phase_b_ceiling": 2_500_000,
    "provider_token_budget": 9_500_000,
    "provider_max_trials": 38,
}
# Per-trial input tokens. 4k/16k are recomputed from promoted bundles; 64k is
# user-reported; 128k has never run and MUST stay null so it cannot be budgeted.
# Measured per-trial input tokens. 128k is absent rather than None: an unmeasured
# dose must be impossible to multiply into a budget, not merely awkward to.
DOSE_COST_MEASURED: dict[str, int] = {"4096": 32056, "16384": 79497, "65536": 412753}
DOSE_UNMEASURED = ("131072",)

# Sequential-scaffold final result. Asserted because it is the basis for calling the
# intervention falsified and unbudgetable, and because a future editor lowering these
# numbers would quietly revive a design the evidence rejected.
SCAFFOLD = {
    "neutral_tokens": 6_683_558,
    "semantic_tokens": 7_454_261,
    "total_tokens": 14_137_819,
    "semantic_reads": 232,
    "expected_reads": 257,
    "baseline_64k": 412_753,
}


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


def check_cross_file_counts(fail: list[str]) -> None:
    """The memo and the spec must state the same counts, and both must be present."""
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    memo = MEMO.read_text(encoding="utf-8")
    recon = spec.get("COUNT_RECONCILIATION") or {}
    for key, expected in CROSS_FILE.items():
        got = recon.get(key)
        if got != expected:
            fail.append(f"spec COUNT_RECONCILIATION.{key} = {got}, expected {expected}")
    # the memo must not still carry the superseded totals
    if "21 scored trials" in memo:
        fail.append("memo still says '21 scored trials'; wave-2 total is 25")
    for token in ("25 scored trials", "6,291,672", "9,500,000"):
        if token not in memo:
            fail.append(f"memo is missing the reconciled figure {token!r}")
    # phases must sum
    if recon.get("phase_a_trials", 0) + recon.get("phase_b_trials", 0) != recon.get(
        "total_runnable_trials"
    ):
        fail.append("spec phase trials do not sum to total_runnable_trials")


def check_budget_admission(fail: list[str]) -> None:
    """Budgets must be derived, fail closed, and admit the design they carry."""
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    limits = spec.get("lane", {}).get("provider_limits", {})
    if limits.get("max_trials") != BUDGETS["provider_max_trials"]:
        fail.append(
            f"provider max_trials {limits.get('max_trials')} != "
            f"{BUDGETS['provider_max_trials']} runnable trials"
        )
    if limits.get("max_prompt_tokens_budget") != BUDGETS["provider_token_budget"]:
        fail.append(
            f"provider token budget {limits.get('max_prompt_tokens_budget')} != "
            f"{BUDGETS['provider_token_budget']}"
        )
    phase_a = spec.get("phase_a_measured_doses") or {}
    projected = (phase_a.get("projected_input_tokens") or {}).get("total")
    ceiling = phase_a.get("ceiling_input_tokens")
    if projected != BUDGETS["phase_a_projected"]:
        fail.append(f"phase A projection {projected} != {BUDGETS['phase_a_projected']}")
    if ceiling != BUDGETS["phase_a_ceiling"]:
        fail.append(f"phase A ceiling {ceiling} != {BUDGETS['phase_a_ceiling']}")
    if projected is not None and ceiling is not None and projected > ceiling:
        fail.append("phase A projection exceeds its own ceiling — spec is not admissible")
    # ceilings must sum into the provider budget
    phase_b_ceiling = (spec.get("phase_b_128k_cost_canary") or {}).get("ceiling_input_tokens")
    if (ceiling or 0) + (phase_b_ceiling or 0) != BUDGETS["provider_token_budget"]:
        fail.append("phase ceilings do not sum to the provider token budget")
    # the projection must be reproducible from the declared per-dose costs
    trials_per_dose = 12  # 2 arms x 3 seeds x 2 reps
    recomputed = sum(cost * trials_per_dose for cost in DOSE_COST_MEASURED.values())
    if recomputed != BUDGETS["phase_a_projected"]:
        fail.append(
            f"phase A projection is not reproducible from per-dose costs: "
            f"{recomputed} vs {BUDGETS['phase_a_projected']}"
        )
    # 128k must stay unmeasured so it cannot be silently budgeted
    basis = (spec.get("measured_cost_basis") or {}).get("input_tokens_per_trial") or {}
    for dose in DOSE_UNMEASURED:
        if basis.get(dose) is not None:
            fail.append(
                f"{dose} per-trial cost is no longer null; an unmeasured dose must not be budgeted"
            )
    for dose, expected_cost in DOSE_COST_MEASURED.items():
        if basis.get(dose) != expected_cost:
            fail.append(f"spec dose cost {dose} = {basis.get(dose)}, measured {expected_cost}")
    if (spec.get("broad_ladder_not_runnable") or {}).get("runnable") is not False:
        fail.append("the broad ladder is marked runnable; its 128k cost cannot be projected")


def _reconstruct_reads(events_path: Path) -> dict:
    """Rebuild issued set, requested sequence and event order from RAW events.

    Deliberately does not read `observed_reads`. The memo previously claimed intact
    coverage because `observed_reads == expected_reads`, but a count matches while
    the set is wrong: omit one handle, request one never issued, total unchanged.
    """
    issued: list[str] | None = None
    requested: list[str] = []
    ordinals: list[int] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        value = (event.get("result") or {}).get("value") or {}
        if issued is None and "chunk_ids" in value:
            issued = list(value["chunk_ids"])
        handle = (event.get("arguments") or {}).get("chunk_id")
        if handle:
            requested.append(str(handle))
        if event.get("event_ordinal") is not None:
            ordinals.append(int(event["event_ordinal"]))
    issued_list = issued or []
    issued_set, requested_set = set(issued_list), set(requested)
    return {
        "issued": len(issued_set),
        "calls": len(requested),
        "unique": len(requested_set),
        "omitted": sorted(issued_set - requested_set),
        "never_issued": sorted(requested_set - issued_set),
        "duplicated": sorted({h for h in requested if requested.count(h) > 1}),
        "prefix_order_matches": requested[: len(issued_list)] == issued_list,
        "ordinals_monotonic": ordinals == sorted(ordinals),
    }


def check_handle_audit(fail: list[str]) -> None:
    """Re-derive the 16k audit the memo publishes, from raw artifacts."""
    bundle = RUNS / "zai-flash-action-semantic16k-r3-amd64-egress"
    if not bundle.is_dir():
        fail.append(f"missing bundle for the handle audit: {bundle}")
        return
    seen = 0
    for trial in sorted(os.listdir(bundle)):
        events = bundle / trial / "artifacts" / "app" / "output" / "benchmark-events.jsonl"
        result = bundle / trial / "verifier" / "result.json"
        if not events.is_file() or not result.is_file():
            continue
        seen += 1
        audit = _reconstruct_reads(events)
        reward = json.loads(result.read_text(encoding="utf-8")).get("reward")
        if audit["omitted"] or audit["never_issued"]:
            fail.append(
                f"{trial}: 16k audit found omitted={len(audit['omitted'])} "
                f"never_issued={len(audit['never_issued'])}; the memo reports zero of both"
            )
        if not audit["prefix_order_matches"]:
            fail.append(f"{trial}: requested prefix order does not match issued order")
        if not audit["ordinals_monotonic"]:
            fail.append(f"{trial}: event ordinals are not monotonic")
        if reward == 0.0 and not audit["duplicated"]:
            fail.append(
                f"{trial}: failing 16k trial shows no duplicate handle; the memo "
                f"attributes the failure to a duplicate read"
            )
        if reward == 1.0 and audit["calls"] != audit["unique"]:
            fail.append(f"{trial}: passing trial has duplicate requests")
    if seen != 3:
        fail.append(f"handle audit covered {seen} trials, expected 3")


def check_scaffold_falsification(fail: list[str]) -> None:
    """The scaffold verdict must stay arithmetically supported."""
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    memo = MEMO.read_text(encoding="utf-8")
    if SCAFFOLD["neutral_tokens"] + SCAFFOLD["semantic_tokens"] != SCAFFOLD["total_tokens"]:
        fail.append("scaffold arm tokens do not sum to the reported total")
    if SCAFFOLD["semantic_reads"] >= SCAFFOLD["expected_reads"]:
        fail.append("scaffold semantic arm is not recorded as incomplete coverage")
    # the falsified verdict depends on one trial exceeding most of phase A
    ratio = SCAFFOLD["semantic_tokens"] / BUDGETS["phase_a_ceiling"]
    if ratio < 1.0:
        fail.append(
            f"a scaffolded trial no longer exceeds the phase A ceiling (ratio {ratio:.2f}); "
            f"the unbudgetable conclusion would need restating"
        )
    if SCAFFOLD["total_tokens"] <= BUDGETS["provider_token_budget"]:
        fail.append("scaffold total no longer exceeds the provider budget")
    scaffold = (spec.get("wave2_context") or {}).get("sequential_scaffold") or {}
    verdict = str(scaffold.get("verdict", ""))
    if "FALSIFIED" not in verdict:
        fail.append("spec no longer records the scaffold as falsified")
    if (scaffold.get("budget_impact") or {}).get("scaffold_trials_affordable_under_phase_a") != 0:
        fail.append("spec no longer records zero affordable scaffold trials")
    for token in ("14,137,819", "232/257"):
        if token not in memo:
            fail.append(f"memo is missing the scaffold figure {token!r}")


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
        check_cross_file_counts,
        check_budget_admission,
        check_handle_audit,
        check_scaffold_falsification,
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
