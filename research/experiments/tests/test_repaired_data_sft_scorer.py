"""Incorrect-pass contract of the repaired-data SFT scorer (`analyze.py`).

Regression for the Verifier's 2026-10-16 synthetic case: with no original-grader replays
and 31 unresolved v6 failures, the scorer printed `incorrect_pass_rate: 0.0`, so missing
data looked like a measured zero.

The replay population is the held-out set minus the tasks excluded by design. On
2026-10-16 those are the four environment-invalid tasks that QA Trace excludes at plan as
`g1x`.
"""

from __future__ import annotations

import runpy
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ANALYZE = runpy.run_path(
    str(REPO_ROOT / "research" / "experiments" / "repaired-data-sft-v1" / "analyze.py")
)
summary = ANALYZE["incorrect_pass_summary"]

G1X = {
    t: "g1x"
    for t in (
        "format-code-task-001199",
        "format-code-task-001246",
        "format-code-task-001590",
        "format-code-task-002451",
    )
}
IN_POPULATION = [f"task-{i:02d}" for i in range(29)]  # 33 held-out tasks minus the four g1x


def _arm(
    v6_pass: dict[str, bool], replays: dict[str, bool | None], attempt: int = 1
) -> list[tuple[str, int, bool, bool | None]]:
    return [(t, attempt, v, replays.get(t)) for t, v in v6_pass.items()]


def test_unreplayed_failures_are_unmeasured_not_zero() -> None:
    # Verifier's arm B: 2 v6 passes, 31 v6 failures, no replay input, no exclusions.
    trials = [(f"t{i}", 1, i < 2, None) for i in range(33)]
    out = summary(trials, {})
    assert out["incorrect_pass_rate"] is None
    assert out["incorrect_pass_ci95"] is None
    assert out["incorrect_pass_status"] == "unmeasured"
    assert out["incorrect_pass_unresolved_failures"] == 31
    assert out["incorrect_pass_v6_passes"] == 2
    assert out["incorrect_pass_bounds"] == [0.0, round(31 / 33, 4)]


def test_all_v6_passes_are_structurally_zero() -> None:
    out = summary([(f"t{i}", 1, True, None) for i in range(33)], {})
    assert out["incorrect_pass_status"] == "complete"
    assert out["incorrect_pass_rate"] == 0.0
    assert out["incorrect_pass_unresolved_failures"] == 0
    assert out["incorrect_pass_bounds"] is None


def test_excluded_tasks_leave_the_population_and_do_not_block_the_rate() -> None:
    # Every in-population failure is replayed and the four g1x tasks fail on v6 with no
    # replay (QA Trace never plans them). The rate is measured over the 29 in-population
    # trials, and the exclusions are counted by reason.
    v6 = {t: i % 3 == 0 for i, t in enumerate(IN_POPULATION)} | dict.fromkeys(G1X, False)
    replays = {t: i % 5 == 1 for i, t in enumerate(IN_POPULATION) if not v6[t]}
    out = summary(_arm(v6, replays), G1X)
    incorrect = sum(replays.values())
    assert out["incorrect_pass_status"] == "complete"
    assert out["incorrect_pass_population"] == 29
    assert out["incorrect_pass_excluded"] == {"g1x": 4}
    assert out["incorrect_pass_unresolved_failures"] == 0
    assert out["incorrect_pass_rate"] == round(incorrect / 29, 4)

    # Without the exclusion the same data is (correctly) unmeasured-bounded, not zero.
    unexcluded = summary(_arm(v6, replays), {})
    assert unexcluded["incorrect_pass_rate"] is None
    assert unexcluded["incorrect_pass_unresolved_failures"] == 4


def test_unresolved_in_population_failure_still_bounds_the_rate() -> None:
    v6 = dict.fromkeys(IN_POPULATION, False) | dict.fromkeys(G1X, False)
    replays: dict[str, bool | None] = {t: False for t in IN_POPULATION[:-3]}
    replays[IN_POPULATION[0]] = True
    out = summary(_arm(v6, replays), G1X)
    assert out["incorrect_pass_status"] == "partial"
    assert out["incorrect_pass_rate"] is None
    assert out["incorrect_pass_excluded"] == {"g1x": 4}
    assert out["incorrect_pass_unresolved_failures"] == 3
    assert out["incorrect_pass_bounds"] == [round(1 / 29, 4), round(4 / 29, 4)]


def test_attempt_1_replay_mode_restricts_the_denominator_to_attempt_1() -> None:
    # Two attempts per task; QA Trace replayed attempt 1 only. The attempt-1 v6 failures
    # are all resolved, and the attempt-2 failures have no replay.
    v6_a1 = {t: i % 3 == 0 for i, t in enumerate(IN_POPULATION)}
    replays = {t: i % 5 == 1 for i, t in enumerate(IN_POPULATION) if not v6_a1[t]}
    v6_a2 = {t: i % 2 == 0 for i, t in enumerate(IN_POPULATION)}
    trials = _arm(v6_a1, replays, 1) + _arm(v6_a2, {}, 2)

    out = summary(trials, {}, attempt_1_only=True)
    assert out["incorrect_pass_status"] == "complete"
    assert out["incorrect_pass_population"] == 29
    assert out["incorrect_pass_excluded"] == {ANALYZE["ATTEMPT_MODE_REASON"]: 29}
    assert out["incorrect_pass_rate"] == round(sum(replays.values()) / 29, 4)

    # The same rows read in all-attempts mode: attempt-2 failures are unresolved.
    all_attempts = summary(trials, {})
    assert all_attempts["incorrect_pass_rate"] is None
    assert all_attempts["incorrect_pass_unresolved_failures"] == sum(not v for v in v6_a2.values())


def test_attempt_index_comes_from_rollout_trial_names() -> None:
    attempt_of = ANALYZE["attempt_of"]
    assert attempt_of("rollout-007-format-code-task-000371-a2") == 2
    assert attempt_of("rollout-007-format-code-task-000371-a1") == 1
    assert attempt_of("rollout-007-format-code-task-000371") == 1
