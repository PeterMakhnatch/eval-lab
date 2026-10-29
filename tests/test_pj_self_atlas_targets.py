import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "pj_atlas_targets",
    Path(__file__).resolve().parents[1] / "research/experiments/pj-self-cb2a4d-1-atlas-targets/atlas_targets.py",
)
targets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(targets)


def test_auc_is_one_for_perfect_ranking_half_for_ties_and_none_without_both_classes():
    scores = {"a": 3.0, "b": 2.0, "c": 1.0, "d": 0.0}
    assert targets.auc(scores, {"a", "b"}, ["a", "b", "c", "d"]) == 1.0
    assert targets.auc({"a": 1.0, "b": 1.0}, {"a"}, ["a", "b"]) == 0.5
    assert targets.auc(scores, {"a"}, ["a"]) is None


def test_cluster_shares_use_only_training_tasks_and_skip_small_clusters():
    world = {"clusters": {"t1": {"c1"}, "t2": {"c1"}, "t3": {"c1"}, "t4": {"c2"}, "t5": {"c1", "c2"}}}
    shares, prior = targets.fit_cluster_shares(["t1", "t2", "t3", "t4"], {"t1", "t2", "t5"}, world)
    assert shares == {"c1": (2 + 1) / (3 + 2)}  # c2 has one training task, below the minimum
    assert prior == (2 + 1) / (4 + 2)  # t5 is outside training and cannot leak in


def test_unseen_cluster_falls_back_to_the_training_prior_and_exposure_scores_run_count():
    world = {"clusters": {"x": {"cNew"}}, "runs": {"x": 7}}
    scored = targets.scores_for("cluster_share", ["x"], world, {"c1": 0.9}, 0.4)
    assert scored == {"x": 0.4}
    assert targets.scores_for("runs_exposure", ["x"], world, {}, 0.0) == {"x": 7.0}


def test_rule_helps_only_when_both_directions_clear_chance_and_exposure():
    rows = [{"task_id": f"t{i}", "keyed": i % 2 == 0} for i in range(40)]
    # Every task has the same exposure, so a rule tracking keyed status must beat that baseline in both directions.
    world = {
        "runs": {r["task_id"]: 1 for r in rows},
        "clusters": {r["task_id"]: {"A" if r["keyed"] else "B"} for r in rows},
        "facets": {},
    }
    out = targets.evaluate(rows, world)
    assert out["helps_by_preregistered_rule"]["cluster_share"] is True
    assert out["helps_by_preregistered_rule"]["tests_changed"] is False
