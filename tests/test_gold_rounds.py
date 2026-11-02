"""Feedback rounds of the gold certifier: fresh-mutant pools, held-out blindness, the loop, its guards."""

from __future__ import annotations

import ast
from typing import Any

import pytest

from evallab.gold_certifier import (
    FreshPools,
    LeakError,
    assert_disjoint,
    assert_prompt_blind,
    build_fresh_pools,
    build_synthesis_prompt,
    mutant_diff,
    run_rounds,
    split_mutants,
)
from evallab.mutant_auditor import (
    EXTRA_MUTATOR_ORDER,
    Mutant,
    fresh_mutants,
    mutants_for_patch,
    python_targets,
)

PATH = "pkg/clamp.py"
SOURCE = (
    "def clamp(x, lo, hi):\n"
    "    if x < lo:\n"
    "        return lo\n"
    "    if x > hi:\n"
    "        return hi\n"
    "    total = x + 1\n"
    "    ok = x and lo\n"
    "    flag = total >= 3 or hi == 0\n"
    "    count = 2\n"
    "    return total if ok else count\n"
)
PATCH = f"--- a/{PATH}\n+++ b/{PATH}\n@@ -0,0 +1,{len(SOURCE.splitlines())} @@\n" + "".join(
    f"+{line}\n" for line in SOURCE.splitlines()
)
SOURCES = {PATH: SOURCE}


def _split() -> tuple[list[Mutant], list[Mutant]]:
    return split_mutants(mutants_for_patch(PATCH, SOURCES, limit=6))


def _pools(seed: str = "s", extra: int = 2) -> tuple[list[Mutant], list[Mutant], FreshPools]:
    train, evals = _split()
    return (
        train,
        evals,
        build_fresh_pools(
            PATCH, SOURCES, train=train, evals=evals, seed=seed, eval_extra_limit=extra
        ),
    )


def test_extra_operators_produce_valid_distinct_mutants_without_moving_default_selection() -> None:
    added = python_targets(PATCH)[PATH]
    fresh = fresh_mutants(PATH, SOURCE, added)
    operators = {m.operator for m in fresh}
    assert set(EXTRA_MUTATOR_ORDER) <= operators
    reference = ast.dump(ast.parse(SOURCE))
    for mutant in fresh:
        assert ast.dump(ast.parse(mutant.source)) != reference
        assert mutant.sha256 not in {m.sha256 for m in fresh if m is not mutant}
    # the default selection never contains an extra operator, so last night's train/eval sets cannot move
    assert not {m.operator for m in mutants_for_patch(PATCH, SOURCES, limit=1000)} & set(
        EXTRA_MUTATOR_ORDER
    )
    excluded = {fresh[0].sha256}
    assert fresh[0].sha256 not in {
        m.sha256 for m in fresh_mutants(PATH, SOURCE, added, exclude_sha256=excluded)
    }


def test_fresh_pools_are_disjoint_from_train_and_eval_and_avoid_eval_lines() -> None:
    train, evals, pools = _pools()
    assert pools.eval_extra and pools.probe_pool
    all_evals = [*evals, *pools.eval_extra]
    assert_disjoint(train, list(pools.probe_pool), all_evals)
    seen = {m.sha256 for m in (*train, *evals)}
    assert not seen & {m.sha256 for m in (*pools.eval_extra, *pools.probe_pool)}
    eval_lines = {(m.path, m.line) for m in all_evals}
    assert not eval_lines & {(m.path, m.line) for m in pools.probe_pool}


def test_fresh_pools_are_deterministic_and_seed_dependent() -> None:
    _, _, first = _pools("a")
    _, _, again = _pools("a")
    _, _, other = _pools("b")
    assert [m.sha256 for m in first.probe_pool] == [m.sha256 for m in again.probe_pool]
    assert [m.sha256 for m in first.eval_extra] == [m.sha256 for m in again.eval_extra]
    assert [m.sha256 for m in first.probe_pool] != [m.sha256 for m in other.probe_pool]


def test_assert_disjoint_rejects_overlap_and_probe_on_eval_line() -> None:
    train, evals, pools = _pools()
    with pytest.raises(LeakError, match="train and eval"):
        assert_disjoint(train, [], [train[0]])
    with pytest.raises(LeakError, match="probe and eval"):
        assert_disjoint([], [pools.probe_pool[0]], [pools.probe_pool[0]])
    same_line = Mutant(
        path=evals[0].path,
        operator="x",
        line=evals[0].line,
        description="",
        source="a",
        diff="",
        sha256="f" * 64,
    )
    with pytest.raises(LeakError, match="eval lines"):
        assert_disjoint([], [same_line], evals)


def test_prompt_extensions_are_absent_by_default_and_never_carry_eval_diffs() -> None:
    train, evals, pools = _pools()
    base = {
        "instruction": "do it",
        "hidden_tests": "t",
        "reference_patch": PATCH,
        "train_mutants": train,
        "train_diffs": [mutant_diff(m, SOURCE) for m in train],
        "interpreter_command": "python3",
    }
    plain = build_synthesis_prompt(**base)
    assert "previous test file" not in plain and "wrongly ACCEPTED" not in plain
    survivor = pools.probe_pool[0]
    richer = build_synthesis_prompt(
        **base,
        previous_test="class T: pass",
        survivor_diffs=[mutant_diff(survivor, SOURCE)],
    )
    assert "class T: pass" in richer and "wrongly ACCEPTED" in richer
    assert mutant_diff(survivor, SOURCE) in richer
    eval_diffs = [mutant_diff(m, SOURCE) for m in evals]
    assert_prompt_blind(richer, eval_diffs)
    with pytest.raises(LeakError):
        assert_prompt_blind(richer + eval_diffs[0], eval_diffs)


class Harness:
    """Fake synthesizer and grader; grades come from a per-round script keyed by probe sha."""

    def __init__(
        self, evals: list[Mutant], *, reference: dict[int, list[float | None]] | None = None
    ) -> None:
        self.evals = evals
        self.calls: list[dict] = []
        self.graded: list[tuple[int, list[str]]] = []
        self.reference = reference or {}
        self.probe_reward: dict[int, float | None] = {}  # round -> reward for every probe
        self.leak = False

    def synthesize(self, index, train, survivors, previous, feedback):
        diffs = [mutant_diff(m, SOURCE) for m in train]
        prompt = build_synthesis_prompt(
            instruction="i",
            hidden_tests="h",
            reference_patch=PATCH,
            train_mutants=train,
            train_diffs=diffs,
            interpreter_command="python3",
            previous_test=previous,
            survivor_diffs=[mutant_diff(m, SOURCE) for m in survivors] or None,
            feedback=feedback,
        )
        if self.leak:
            prompt += mutant_diff(self.evals[0], SOURCE)
        self.calls.append(
            {
                "round": index,
                "train": [m.sha256 for m in train],
                "survivors": [m.sha256 for m in survivors],
                "previous": previous,
                "feedback": feedback,
                "prompt": prompt,
            }
        )
        return f"# test round {index} attempt {len(self.calls)}\nimport unittest\n", prompt

    def grade(self, index, code, probes):
        self.graded.append((index, [m.sha256 for m in probes]))
        attempts = self.reference.get(index, [1.0])
        ref = attempts[min(sum(1 for r, _ in self.graded if r == index) - 1, len(attempts) - 1)]
        out: dict[str, Any] = {"reference": ref}
        if ref != 1.0:
            out["reference_output"] = "AssertionError: boom"
        for m in probes:
            out[f"probe:{m.sha256}"] = self.probe_reward.get(index, 0.0)
        return out


def _run(rounds: int, harness: Harness, pools: FreshPools, train, evals, per: int = 2):
    return run_rounds(
        rounds=rounds,
        train=train,
        probe_pool=list(pools.probe_pool),
        evals=[*evals, *pools.eval_extra],
        eval_diffs=[mutant_diff(m, SOURCE) for m in (*evals, *pools.eval_extra)],
        synthesize=harness.synthesize,
        grade=harness.grade,
        probes_per_round=per,
    )


def test_one_round_is_the_unchanged_single_synthesis() -> None:
    train, evals, pools = _pools()
    h = Harness([*evals, *pools.eval_extra])
    h.probe_reward = {1: 1.0}  # survivors exist, but rounds=1 never feeds them back
    out = _run(1, h, pools, train, evals)
    assert out.stop_reason == "rounds_complete"
    assert len(h.calls) == 1 and h.calls[0]["survivors"] == [] and h.calls[0]["previous"] is None
    assert h.calls[0]["train"] == [m.sha256 for m in train]
    assert out.final_round(1) == 1


def test_survivors_become_training_mutants_and_eval_mutants_never_reach_the_prompt() -> None:
    train, evals, pools = _pools()
    all_evals = {m.sha256 for m in (*evals, *pools.eval_extra)}
    h = Harness([*evals, *pools.eval_extra])
    h.probe_reward = {1: 1.0, 2: 1.0, 3: 1.0}
    out = _run(3, h, pools, train, evals, per=1)
    assert [c["round"] for c in h.calls] == [1, 2, 3]
    first_probe = out.records[0].probe_shas
    second_probe = out.records[1].probe_shas
    assert first_probe and second_probe and not set(first_probe) & set(second_probe)
    assert set(first_probe) <= set(h.calls[1]["train"]) and h.calls[1]["survivors"] == first_probe
    assert set(first_probe + second_probe) <= set(h.calls[2]["train"])
    assert h.calls[1]["previous"] == out.accepted_tests[1]
    for call in h.calls:
        assert not set(call["train"]) & all_evals
        assert_prompt_blind(
            call["prompt"], [mutant_diff(m, SOURCE) for m in (*evals, *pools.eval_extra)]
        )
    assert out.stop_reason == "rounds_complete"


def test_loop_stops_when_every_probe_dies_and_arms_carry_the_last_test() -> None:
    train, evals, pools = _pools()
    h = Harness([*evals, *pools.eval_extra])
    h.probe_reward = {1: 0.0}
    out = _run(3, h, pools, train, evals)
    assert out.stop_reason == "all_probes_killed"
    assert len(h.calls) == 1
    assert out.final_round(3) == 1 and out.final_round(1) == 1


def test_loop_stops_when_the_probe_pool_is_exhausted_and_when_probes_cannot_be_measured() -> None:
    train, evals, pools = _pools()
    h = Harness([*evals, *pools.eval_extra])
    h.probe_reward = {1: 1.0, 2: 1.0}
    out = run_rounds(
        rounds=3,
        train=train,
        probe_pool=list(pools.probe_pool)[:1],
        evals=[*evals, *pools.eval_extra],
        eval_diffs=[],
        synthesize=h.synthesize,
        grade=h.grade,
        probes_per_round=1,
    )
    assert out.stop_reason == "probe_pool_exhausted" and len(h.calls) == 2
    h2 = Harness([*evals, *pools.eval_extra])
    h2.probe_reward = {1: None}
    out2 = _run(3, h2, pools, train, evals)
    assert out2.stop_reason == "probes_unmeasured" and out2.records[0].unmeasured


def test_reference_guard_repairs_once_then_reverts_without_accepting_the_test() -> None:
    train, evals, pools = _pools()
    h = Harness([*evals, *pools.eval_extra], reference={1: [1.0], 2: [0.0, 1.0]})
    h.probe_reward = {1: 1.0, 2: 1.0}
    out = _run(2, h, pools, train, evals)
    assert out.records[1].guard == "repaired"
    assert "must score 1.0" in h.calls[2]["feedback"]
    assert "AssertionError: boom" in h.calls[2]["feedback"]  # the grader output reaches the repair prompt
    assert all("reference_output" not in r.grades for r in out.records)  # and is never stored
    assert set(out.accepted_tests) == {1, 2}

    h2 = Harness([*evals, *pools.eval_extra], reference={1: [1.0], 2: [0.0, None]})
    h2.probe_reward = {1: 1.0}
    out2 = _run(3, h2, pools, train, evals)
    assert out2.records[1].guard == "reverted" and out2.stop_reason == "guard_reverted"
    assert set(out2.accepted_tests) == {1}
    assert out2.final_round(3) == 1  # a reverted round never ships; the earlier test does

    h3 = Harness([*evals, *pools.eval_extra], reference={1: [0.0, 0.0]})
    out3 = _run(2, h3, pools, train, evals)
    assert out3.accepted_tests == {} and out3.final_round(2) is None


def test_prompt_with_an_eval_diff_aborts_the_run() -> None:
    train, evals, pools = _pools()
    h = Harness([*evals, *pools.eval_extra])
    h.leak = True
    with pytest.raises(LeakError):
        _run(1, h, pools, train, evals)


def test_rounds_are_deterministic() -> None:
    def once() -> list[tuple[int, list[str], list[str]]]:
        train, evals, pools = _pools()
        h = Harness([*evals, *pools.eval_extra])
        h.probe_reward = {1: 1.0, 2: 0.0}
        out = _run(3, h, pools, train, evals)
        return [(r.round, r.probe_shas, r.survivors) for r in out.records]

    assert once() == once()


def test_invalid_round_arguments_are_rejected() -> None:
    train, evals, pools = _pools()
    h = Harness([])
    with pytest.raises(ValueError, match="rounds"):
        _run(0, h, pools, train, evals)
    with pytest.raises(ValueError, match="probes_per_round"):
        _run(1, h, pools, train, evals, per=0)
