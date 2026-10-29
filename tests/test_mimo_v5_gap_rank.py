"""Boundaries of the HAR-224 gap ranking: coverage join, allocation and sampling invariants."""

from __future__ import annotations

import importlib.util
import math
from collections import Counter
from pathlib import Path
from typing import Any

PATH = (
    Path(__file__).resolve().parents[1]
    / "research/experiments/mimo-v5-gap-rank-20261029/rank_gap.py"
)
SPEC = importlib.util.spec_from_file_location("mimo_v5_gap_rank", PATH)
assert SPEC is not None and SPEC.loader is not None
RANK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RANK)


def census_row(
    task: str, digest: str, verdict: str, version: str = "mimo-clean-v5"
) -> dict[str, str]:
    return {
        "task_id": task,
        "manifest_version": version,
        "final_digest": digest,
        "ladder_verdict": verdict,
    }


def test_coverage_counts_only_scored_v5_ladders_and_reports_digest_binding() -> None:
    manifest = {t: {"final_digest": f"new-{t}"} for t in "abcde"}
    refresh = {t: {"old_digest": f"old-{t}"} for t in "abcd"}
    results = [
        census_row("a", "old-a", "clean"),
        census_row("b", "old-b", "unscored"),  # no verdict: still a gap
        census_row("c", "old-c", "clean", version="mimo-clean-v4"),  # other generation: not v5
        census_row("d", "other", "cracked"),  # scored, but on neither digest
        census_row("e", "new-e", "clean"),  # unrefreshed row: bound to the current digest
        census_row("a", "old-a", ""),  # control-only rows never count
    ]
    cover = RANK.ladder_coverage(manifest, results, refresh)
    assert cover["scored_tasks"] == {"a", "d", "e"}
    assert cover["unscored_tasks"] == {"b"}
    assert cover["cracked_tasks"] == {"d"}
    assert cover["bound_to_prerefresh_digest"] == 1
    assert cover["bound_to_current_v5_digest"] == 1


def test_unscored_row_does_not_mask_a_later_clean_row() -> None:
    manifest = {"a": {"final_digest": "new-a"}}
    results = [census_row("a", "old-a", "unscored"), census_row("a", "old-a", "clean")]
    cover = RANK.ladder_coverage(manifest, results, {"a": {"old_digest": "old-a"}})
    assert cover["scored_tasks"] == {"a"}
    assert cover["unscored_tasks"] == set()


def test_largest_remainder_respects_total_caps_and_floor() -> None:
    weights = {"x": 10.0, "y": 1.0, "z": 1.0}
    caps = {"x": 4, "y": 5, "z": 0}
    alloc = RANK.largest_remainder(7, weights, caps, floor=1)
    assert "z" not in alloc  # empty stratum gets nothing
    assert sum(alloc.values()) == 7
    assert alloc["x"] == 4  # capped; the surplus flows to the open stratum
    assert alloc["y"] == 3
    assert RANK.largest_remainder(50, weights, caps) == {"x": 4, "y": 5}  # never above population


def test_spearman_is_one_for_equal_order_and_minus_one_for_reversed() -> None:
    values = [3.0, 1.0, 2.0, 2.0]
    assert math.isclose(RANK.spearman(values, [v * 10 for v in values]), 1.0)
    assert math.isclose(RANK.spearman([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]), -1.0)


def ranked_rows(n: int, groups: int, seed: int = 1) -> list[dict[str, Any]]:
    families = ("python", "go", "js_ts")
    classes = ("known-defect", "none")
    rows = []
    for i in range(n):
        task = f"t{i:03d}"
        rows.append(
            {
                "task_id": task,
                "rank": i + 1,
                "hold": "",
                "repo_group": f"g{i % groups}",
                "repo_band": RANK.repo_band(n // groups),
                "domain_family": families[i % 3],
                "finding_class": classes[i % 2],
                "tie_key": RANK.tie_key(seed, task),
            }
        )
    return rows


def test_plan_never_mixes_repo_groups_across_waves_or_repeats_them_in_wave_b() -> None:
    plan, strata = RANK.plan_sample(ranked_rows(300, 60), seed=1, top_n=20, sample_n=30)
    a = [p for p in plan if p["wave"] == "A"]
    b = [p for p in plan if p["wave"] == "B"]
    assert [p["task_id"] for p in a] == sorted(p["task_id"] for p in a)  # rank order is input order
    assert max(Counter(p["repo_group"] for p in a).values()) <= RANK.MAX_PER_GROUP_A
    assert max(Counter(p["repo_group"] for p in b).values()) == 1
    assert not {p["repo_group"] for p in a} & {p["repo_group"] for p in b}
    assert len(a) == 20 and len(b) == 30
    assert sum(s["selected"] for s in strata) == len(b)


def test_plan_skips_held_rows_and_is_seed_deterministic() -> None:
    rows = ranked_rows(200, 80)
    for row in rows[:10]:
        row["hold"] = "excluded-from-training"
    first, _ = RANK.plan_sample(rows, seed=1, top_n=10, sample_n=20)
    again, _ = RANK.plan_sample(rows, seed=1, top_n=10, sample_n=20)
    assert [p["task_id"] for p in first] == [p["task_id"] for p in again]
    assert not {p["task_id"] for p in first} & {r["task_id"] for r in rows[:10]}
    other_rows = ranked_rows(200, 80, seed=2)
    for row in other_rows[:10]:
        row["hold"] = "excluded-from-training"
    other, _ = RANK.plan_sample(other_rows, seed=2, top_n=10, sample_n=20)
    wave_b = lambda plan: {p["task_id"] for p in plan if p["wave"] == "B"}  # noqa: E731
    assert wave_b(first) != wave_b(other)
