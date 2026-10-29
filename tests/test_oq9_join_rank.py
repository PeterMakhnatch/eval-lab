import importlib.util
import math
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "oq9_join_rank",
    Path(__file__).resolve().parents[1] / "research/experiments/oq9-atlas-facet-join/join_rank.py",
)
join_rank = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(join_rank)


def test_wilson_matches_reference_values_and_handles_empty():
    low, high = join_rank.wilson(10, 10)
    assert 0.72 < low < 0.73 and math.isclose(high, 1.0, abs_tol=1e-9)
    assert all(math.isnan(x) for x in join_rank.wilson(0, 0))


def test_order_breaks_ties_by_task_hash_not_by_input_order():
    tasks = ["t-a", "t-b", "t-c", "t-d"]
    first = join_rank.order(tasks, {"t-c": 2.0})
    second = join_rank.order(list(reversed(tasks)), {"t-c": 2.0})
    assert first == second
    assert first[0] == "t-c"
    assert first[1:] == sorted(tasks[:2] + tasks[3:], key=join_rank.task_hash)


def test_precision_at_reports_only_reachable_k_with_wilson_interval():
    ranked = [f"t{i}" for i in range(12)]
    result = join_rank.precision_at(ranked, {"t0", "t1", "t11"})
    assert set(result) == {"10"}
    assert result["10"]["hits"] == 2
    low, high = result["10"]["wilson95"]
    assert low < 0.2 < high


def test_fisher_is_one_sided_and_extreme_table_is_small():
    assert join_rank.fisher_greater(5, 0, 0, 5) < 0.01
    assert math.isclose(join_rank.fisher_greater(0, 5, 5, 0), 1.0)
