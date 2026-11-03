"""Synthetic replay-gate coverage; no retained corpora or model calls."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from evallab.line.core import CHECK_NAMES
from evallab.line.io import canonical_hash, read_json, write_json, write_rows
from evallab.line.replay import (
    BASELINE_SCHEMA,
    POOLED,
    _frozen_hash,
    compare_baseline,
    replay,
)


def _checks(first: str) -> list[dict[str, str]]:
    """All eight line checks; the first one carries the requested status."""
    return [
        {"name": name, "status": first if index == 0 else "PASS", "details": "synthetic"}
        for index, name in enumerate(CHECK_NAMES)
    ]


def _fixture(cheats=10, clean=10, recall=None, false_alarms=0, clean_reviews=0, cheat_reviews=0):
    recall = cheats if recall is None else recall
    rows = [
        {
            "run_id": f"cheat-{i}",
            "corpus": "synthetic",
            "decision": (
                "keep" if i >= recall else "review" if i >= recall - cheat_reviews else "drop"
            ),
        }
        for i in range(cheats)
    ] + [
        {
            "run_id": f"clean-{i}",
            "corpus": "synthetic",
            "decision": (
                "drop"
                if i < false_alarms
                else "review"
                if i < false_alarms + clean_reviews
                else "keep"
            ),
        }
        for i in range(clean)
    ]
    group = {
        "runs": cheats + clean,
        "labelled_runs": cheats + clean,
        "cheats": cheats,
        "clean": clean,
        "attempted_cheat": 0,
        "cohort_hash": canonical_hash(sorted(row["run_id"] for row in rows)),
        "labels_hash": canonical_hash({"cheats": cheats, "clean": clean}),
        "metrics": {
            "drop_or_review_recall": {
                "k": recall,
                "n": cheats,
                "rate": recall / cheats if cheats else None,
            },
            "drop_recall": {
                "k": recall - cheat_reviews,
                "n": cheats,
                "rate": (recall - cheat_reviews) / cheats if cheats else None,
            },
            "clean_false_alarm_rate": {
                "k": false_alarms,
                "n": clean,
                "rate": false_alarms / clean if clean else None,
            },
            "clean_drop_or_review_rate": {
                "k": false_alarms + clean_reviews,
                "n": clean,
                "rate": (false_alarms + clean_reviews) / clean if clean else None,
            },
        },
    }
    scoreboard = {
        "schema": "evallab.line_scoreboard/v1",
        "overall": deepcopy(group),
        "by_corpus": {"synthetic": group},
        "labels_policy": {"attempted_cheat": "exclude"},
        "quality_checks": _checks("PASS"),
    }
    return {
        "schema": BASELINE_SCHEMA,
        "frozen_spec_hash": "synthetic",
        "scoreboard": scoreboard,
        "decisions": rows,
    }


@pytest.mark.parametrize(
    ("n", "lost", "passed"),
    [(10, 1, True), (10, 2, False), (100, 2, True), (100, 3, False), (51, 1, True), (51, 2, False)],
)
def test_recall_gate_strict_one_run_or_two_points(n, lost, passed):
    baseline = _fixture(cheats=n)
    current = _fixture(cheats=n, recall=n - lost)
    result = compare_baseline(baseline, current["scoreboard"], current["decisions"])
    assert result["passed"] is passed
    assert len(result["changed_runs"]) == lost
    if not passed:
        assert result["regressions"][0]["metric"] == "drop_or_review_recall"


@pytest.mark.parametrize(
    ("n", "added", "passed"),
    [
        (10, 1, True),
        (10, 2, False),
        (200, 2, True),
        (200, 3, False),
        (101, 1, True),
        (101, 2, False),
    ],
)
def test_clean_drop_gate_strict_one_run_or_one_point(n, added, passed):
    baseline = _fixture(clean=n)
    current = _fixture(clean=n, false_alarms=added)
    result = compare_baseline(baseline, current["scoreboard"], current["decisions"])
    assert result["passed"] is passed
    if not passed:
        assert result["regressions"][0]["metric"] == "clean_false_alarm_rate"


@pytest.mark.parametrize(("cheats", "clean"), [(0, 10), (10, 0), (0, 0)])
def test_zero_denominators_and_no_changes(cheats, clean):
    baseline = _fixture(cheats=cheats, clean=clean)
    if not baseline["decisions"]:
        baseline["scoreboard"]["by_corpus"] = {}
    assert compare_baseline(baseline, baseline["scoreboard"], baseline["decisions"]) == {
        "passed": True,
        "regressions": [],
        "informational": [],
        "changed_runs": [],
    }


def _empty_group():
    return {
        "runs": 0,
        "labelled_runs": 0,
        "cheats": 0,
        "clean": 0,
        "attempted_cheat": 0,
        "cohort_hash": canonical_hash([]),
        "labels_hash": canonical_hash({"cheats": 0, "clean": 0}),
        "metrics": {
            name: {"k": 0, "n": 0, "rate": None}
            for name in (
                "drop_or_review_recall",
                "drop_recall",
                "clean_false_alarm_rate",
                "clean_drop_or_review_rate",
            )
        },
    }


def test_declared_empty_corpus_is_comparable_but_unknown_decision_corpus_is_not():
    baseline = _fixture()
    baseline["scoreboard"]["by_corpus"]["empty"] = _empty_group()
    result = compare_baseline(baseline, baseline["scoreboard"], baseline["decisions"])
    assert result["passed"] and result["regressions"] == []
    stray = deepcopy(baseline)
    stray["decisions"].append({"run_id": "x", "corpus": "undeclared", "decision": "keep"})
    with pytest.raises(ValueError, match="cohorts disagree"):
        compare_baseline(stray, baseline["scoreboard"], stray["decisions"])


def test_clean_reviews_within_allowance_are_reported_beyond_it_regress():
    baseline = _fixture(clean=20)
    within = _fixture(clean=20, clean_reviews=1)
    result = compare_baseline(baseline, within["scoreboard"], within["decisions"])
    assert result["passed"] and result["regressions"] == []
    assert result["informational"] == [
        {
            "corpus": "synthetic",
            "metric": "clean_drop_or_review_rate",
            "before_k": 0,
            "after_k": 1,
            "denominator": 20,
        }
    ]
    over = _fixture(clean=20, clean_reviews=2)
    result = compare_baseline(baseline, over["scoreboard"], over["decisions"])
    assert not result["passed"] and result["informational"] == []
    assert [r["metric"] for r in result["regressions"]] == ["clean_drop_or_review_rate"]
    assert len(result["changed_runs"]) == 2
    # Fewer reviews than the baseline is an improvement, never a regression.
    noisy = _fixture(clean=20, clean_reviews=10)
    assert compare_baseline(noisy, baseline["scoreboard"], baseline["decisions"])["passed"]
    # Harm (clean drops) still gates.
    harmed = _fixture(clean=20, false_alarms=2)
    assert not compare_baseline(baseline, harmed["scoreboard"], harmed["decisions"])["passed"]


def test_review_counts_as_recall_but_not_clean_false_alarm():
    baseline = _fixture()
    current = deepcopy(baseline)
    current["decisions"][0]["decision"] = "review"
    current["decisions"][-1]["decision"] = "review"
    result = compare_baseline(baseline, current["scoreboard"], current["decisions"])
    assert result["passed"]
    assert result["changed_runs"] == [
        {"corpus": "synthetic", "run_id": "cheat-0", "before": "drop", "after": "review"},
        {"corpus": "synthetic", "run_id": "clean-9", "before": "keep", "after": "review"},
    ]


@pytest.mark.parametrize(
    "field",
    ["runs", "labelled_runs", "cheats", "clean", "attempted_cheat", "cohort_hash", "labels_hash"],
)
def test_mismatched_denominators_or_identity_are_invalid(field):
    baseline = _fixture()
    current = deepcopy(baseline["scoreboard"])
    group = current["by_corpus"]["synthetic"]
    group[field] = group[field] + 1 if isinstance(group[field], int) else "changed"
    with pytest.raises(ValueError, match="mismatch"):
        compare_baseline(baseline, current, baseline["decisions"])


def test_equal_size_but_different_decision_cohort_invalid():
    baseline = _fixture()
    decisions = deepcopy(baseline["decisions"])
    decisions[0]["run_id"] = "different-run"
    with pytest.raises(ValueError, match="cohort mismatch"):
        compare_baseline(baseline, baseline["scoreboard"], decisions)


def test_label_policy_change_invalid():
    baseline = _fixture()
    current = deepcopy(baseline["scoreboard"])
    current["labels_policy"]["attempted_cheat"] = "clean"
    with pytest.raises(ValueError, match="labels policy mismatch"):
        compare_baseline(baseline, current, baseline["decisions"])


def test_metric_denominator_change_invalid():
    baseline = _fixture()
    current = deepcopy(baseline["scoreboard"])
    current["by_corpus"]["synthetic"]["metrics"]["drop_or_review_recall"]["n"] = 11
    with pytest.raises(ValueError, match="denominator mismatch"):
        compare_baseline(baseline, current, baseline["decisions"])


def test_quality_failure_regresses_without_rate_changes():
    baseline = _fixture()
    current = deepcopy(baseline["scoreboard"])
    current["quality_checks"][0]["status"] = "FAIL"
    result = compare_baseline(baseline, current, baseline["decisions"])
    assert not result["passed"]
    assert result["regressions"][0]["kind"] == "quality_check"
    assert result["changed_runs"] == []


def test_duplicate_decision_invalid():
    baseline = _fixture()
    with pytest.raises(ValueError, match="duplicate"):
        compare_baseline(baseline, baseline["scoreboard"], baseline["decisions"] * 2)


def test_regression_in_one_corpus_is_not_hidden_by_another():
    baseline = _fixture()
    current = _fixture(recall=8)
    for value in (baseline, current):
        value["scoreboard"]["by_corpus"]["other"] = deepcopy(
            baseline["scoreboard"]["by_corpus"]["synthetic"]
        )
        value["decisions"] += [
            dict(row, corpus="other")
            for row in baseline["decisions"]
            if row["corpus"] == "synthetic"
        ]
    result = compare_baseline(baseline, current["scoreboard"], current["decisions"])
    assert not result["passed"]
    assert {row["corpus"] for row in result["regressions"]} == {"synthetic", POOLED}


def _merged(**corpora):
    """Combine single-corpus fixtures into one multi-corpus baseline-shaped fixture."""
    merged = {"schema": BASELINE_SCHEMA, "frozen_spec_hash": "synthetic", "decisions": []}
    groups = {}
    for name, fixture in corpora.items():
        groups[name] = fixture["scoreboard"]["by_corpus"]["synthetic"]
        merged["decisions"] += [dict(row, corpus=name) for row in fixture["decisions"]]
    merged["scoreboard"] = {
        "by_corpus": groups,
        "labels_policy": {"attempted_cheat": "exclude"},
        "quality_checks": _checks("PASS"),
    }
    return merged


@pytest.mark.parametrize(("moved", "passed"), [(1, True), (2, False)])
def test_drop_to_review_collapse_regresses_even_when_recall_holds(moved, passed):
    baseline = _fixture(cheats=20)
    current = _fixture(cheats=20, cheat_reviews=moved)
    result = compare_baseline(baseline, current["scoreboard"], current["decisions"])
    assert result["passed"] is passed
    if not passed:
        assert [row["metric"] for row in result["regressions"]] == ["drop_recall"]
    # Pure collapse of every drop into review: drop-or-review recall is unchanged.
    collapsed = _fixture(cheats=20, cheat_reviews=20)
    result = compare_baseline(baseline, collapsed["scoreboard"], collapsed["decisions"])
    assert (
        collapsed["scoreboard"]["by_corpus"]["synthetic"]["metrics"]["drop_or_review_recall"]["k"]
        == 20
    )
    assert [row["metric"] for row in result["regressions"]] == ["drop_recall"]


def test_send_everything_to_review_cannot_pass():
    baseline = _fixture(cheats=10, clean=10)
    everything = _fixture(cheats=10, clean=10, cheat_reviews=10, clean_reviews=10)
    result = compare_baseline(baseline, everything["scoreboard"], everything["decisions"])
    assert not result["passed"]
    assert {row["metric"] for row in result["regressions"]} == {
        "drop_recall",
        "clean_drop_or_review_rate",
    }


@pytest.mark.parametrize(("lost_each", "passed"), [(1, True), (2, False)])
def test_small_losses_in_every_corpus_are_gated_together(lost_each, passed):
    names = ["a", "b", "c", "d", "e"]
    baseline = _merged(**{name: _fixture(cheats=100, clean=0) for name in names})
    current = _merged(
        **{name: _fixture(cheats=100, clean=0, recall=100 - lost_each) for name in names}
    )
    result = compare_baseline(baseline, current["scoreboard"], current["decisions"])
    assert result["passed"] is passed
    if not passed:
        # Each corpus stays within its own two-point allowance; only the total fails.
        assert [(row["corpus"], row["metric"]) for row in result["regressions"]] == [
            (POOLED, "drop_or_review_recall")
        ]


def test_corpus_missing_from_the_run_is_a_regression_not_a_crash():
    baseline = _merged(a=_fixture(), b=_fixture())
    current = _merged(a=_fixture())
    result = compare_baseline(baseline, current["scoreboard"], current["decisions"])
    assert not result["passed"]
    assert result["regressions"] == [
        {
            "kind": "corpus_missing",
            "corpus": "b",
            "details": "corpus b is in the baseline but missing from this run",
        }
    ]
    assert len(result["changed_runs"]) == 20
    assert all(row["after"] is None for row in result["changed_runs"])
    with pytest.raises(ValueError, match="baseline lacks"):
        compare_baseline(current, baseline["scoreboard"], baseline["decisions"])


def _spec(tmp_path):
    source = tmp_path / "inputs" / "runs.jsonl"
    write_rows(source, [{"run_id": "synthetic"}])
    spec_path = tmp_path / "inputs" / "frozen.json"
    spec = {
        "schema": "evallab.line_spec/v1",
        "batch_id": "synthetic",
        "split": "dev",
        "sources": [{"corpus": "terminal-wrench", "rollouts": "runs.jsonl", "labels": None}],
        "task_status": None,
        "task_action": "review",
        "autoreview": {"mode": "off", "cache": None, "route": None, "approval": None, "max_usd": 0},
        "models": {"allowed": []},
        "audit": {"rate": 0.05, "seed": "synthetic"},
        "judge": {"queue": str(tmp_path / "external-queue.jsonl"), "write": True},
        "labels_policy": {"attempted_cheat": "exclude"},
        "out_root": str(tmp_path / "batches"),
    }
    write_json(spec_path, spec)
    return spec_path


def _mock_line(monkeypatch, tmp_path, fixture=None, failed=False, intake_sha="synthetic-bytes"):
    from evallab.line import core

    fixture = fixture or _fixture()
    seen = []
    checks = _checks("FAIL" if failed else "PASS")

    def run_line(spec_path, repo_root):
        normalized = read_json(spec_path)
        batch = Path(normalized["out_root"]) / normalized["batch_id"]
        write_json(batch / "scoreboard.json", fixture["scoreboard"])
        write_rows(batch / "decisions.jsonl", fixture["decisions"])
        write_rows(
            batch / "runs.jsonl",
            [
                {"run_id": row["run_id"], "corpus": row["corpus"], "sha256": intake_sha}
                for row in fixture["decisions"]
            ],
        )
        seen.append((spec_path, normalized, repo_root))
        return batch

    monkeypatch.setattr(core, "run_line", run_line)
    monkeypatch.setattr(
        core, "check_batch", lambda batch, repo: {"checks": checks, "passed": not failed}
    )
    return seen


def test_explicit_baseline_then_replay_preserves_spec_and_paths(monkeypatch, tmp_path):
    spec_path = _spec(tmp_path)
    original = spec_path.read_bytes()
    seen = _mock_line(monkeypatch, tmp_path)
    baseline = tmp_path / "baseline.json"
    result, code = replay(spec_path, baseline, tmp_path, write_baseline=True)
    assert code == 0 and result["baseline_written"]
    saved = read_json(baseline)
    assert saved["schema"] == BASELINE_SCHEMA
    assert saved["decisions"] == _fixture()["decisions"]
    before = baseline.read_bytes()
    result, code = replay(spec_path, baseline, tmp_path)
    assert code == 0 and result["passed"] and result["changed_runs"] == []
    assert baseline.read_bytes() == before
    assert spec_path.read_bytes() == original
    assert seen[0][1]["batch_id"] != seen[1][1]["batch_id"]
    for scratch_path, spec, _ in seen:
        assert not scratch_path.exists()
        assert not scratch_path.is_relative_to(tmp_path)
        assert spec["sources"][0]["rollouts"] == str(spec_path.parent / "runs.jsonl")
        assert spec["batch_id"].startswith("synthetic-replay-")
        assert spec["judge"]["write"] is False
    assert not (tmp_path / "external-queue.jsonl").exists()


@pytest.mark.parametrize(
    ("mode", "max_usd"), [("live", 0), ("live", 1), ("off", 1), ("cached", 0.01)]
)
def test_live_and_nonzero_caps_refused_before_running(monkeypatch, tmp_path, mode, max_usd):
    spec_path = _spec(tmp_path)
    spec = read_json(spec_path)
    spec["autoreview"].update(mode=mode, max_usd=max_usd)
    if mode == "cached":
        cache = tmp_path / "cache.jsonl"
        write_rows(cache, [])
        spec["autoreview"]["cache"] = str(cache)
    if mode == "live":
        spec["autoreview"].update(route="synthetic/model", approval="synthetic refusal test")
        spec["models"]["allowed"] = ["synthetic/model"]
    write_json(spec_path, spec)
    seen = _mock_line(monkeypatch, tmp_path)
    result, code = replay(spec_path, tmp_path / "baseline.json", tmp_path, write_baseline=True)
    assert code == 2 and not result["passed"]
    assert result["changed_runs"] is None
    assert result["changed_runs_unavailable"].startswith("changed decisions unavailable")
    assert seen == []


def test_baseline_not_created_implicitly(monkeypatch, tmp_path):
    spec_path = _spec(tmp_path)
    seen = _mock_line(monkeypatch, tmp_path)
    baseline = tmp_path / "baseline.json"
    result, code = replay(spec_path, baseline, tmp_path)
    assert code == 2 and "error" in result
    assert not baseline.exists() and not seen


def test_quality_failure_never_refreshes_baseline(monkeypatch, tmp_path):
    spec_path = _spec(tmp_path)
    baseline = tmp_path / "baseline.json"
    write_json(baseline, {"preserve": "previous baseline"})
    before = baseline.read_bytes()
    _mock_line(monkeypatch, tmp_path, failed=True)
    result, code = replay(spec_path, baseline, tmp_path, write_baseline=True)
    assert code == 1 and result["regressions"][0]["kind"] == "quality_check"
    assert baseline.read_bytes() == before


def test_api_quality_regression_exit_one(monkeypatch, tmp_path):
    spec_path = _spec(tmp_path)
    baseline = tmp_path / "baseline.json"
    _mock_line(monkeypatch, tmp_path)
    assert replay(spec_path, baseline, tmp_path, write_baseline=True)[1] == 0
    _mock_line(monkeypatch, tmp_path, failed=True)
    result, code = replay(spec_path, baseline, tmp_path)
    assert code == 1 and result["changed_runs"] == []


def test_input_bytes_changed_still_reruns_and_reports_changed_runs(monkeypatch, tmp_path):
    spec_path = _spec(tmp_path)
    baseline = tmp_path / "baseline.json"
    seen = _mock_line(monkeypatch, tmp_path)
    assert replay(spec_path, baseline, tmp_path, write_baseline=True)[1] == 0
    write_rows(spec_path.parent / "runs.jsonl", [{"run_id": "changed"}])
    seen = _mock_line(monkeypatch, tmp_path, fixture=_fixture(recall=9))
    result, code = replay(spec_path, baseline, tmp_path)
    assert code == 2 and "input bytes mismatch" in result["error"]
    assert result["inputs_changed"] is True and not result["passed"]
    assert result["changed_runs"] == [
        {"corpus": "synthetic", "run_id": "cheat-9", "before": "drop", "after": "keep"}
    ]
    assert "changed_runs_unavailable" not in result
    assert len(seen) == 1


def test_input_mismatch_with_unrunnable_batch_says_changed_runs_unavailable(monkeypatch, tmp_path):
    from evallab.line import core

    spec_path = _spec(tmp_path)
    baseline = tmp_path / "baseline.json"
    _mock_line(monkeypatch, tmp_path)
    assert replay(spec_path, baseline, tmp_path, write_baseline=True)[1] == 0
    write_rows(spec_path.parent / "runs.jsonl", [{"run_id": "changed"}])

    def unrunnable(spec_path, repo_root):
        raise ValueError("synthetic rollouts unreadable")

    monkeypatch.setattr(core, "run_line", unrunnable)
    result, code = replay(spec_path, baseline, tmp_path)
    assert code == 2 and result["inputs_changed"] is True
    assert result["changed_runs"] is None
    assert result["changed_runs_unavailable"] == (
        "changed decisions unavailable: frozen inputs mismatch"
    )


def test_invalid_comparison_keeps_changed_run_list(monkeypatch, tmp_path):
    spec_path = _spec(tmp_path)
    baseline = tmp_path / "baseline.json"
    _mock_line(monkeypatch, tmp_path)
    assert replay(spec_path, baseline, tmp_path, write_baseline=True)[1] == 0
    changed = _fixture(recall=9)
    changed["scoreboard"]["by_corpus"]["synthetic"]["labels_hash"] = "different labels"
    _mock_line(monkeypatch, tmp_path, fixture=changed)
    result, code = replay(spec_path, baseline, tmp_path)
    assert code == 2
    assert result["changed_runs"] == [
        {"corpus": "synthetic", "run_id": "cheat-9", "before": "drop", "after": "keep"}
    ]


def test_frozen_hash_excludes_only_output_locations():
    original = {
        "batch_id": "a",
        "out_root": "/one",
        "judge": {"queue": "/q", "write": True},
        "audit": {"seed": "fixed"},
    }
    changed = deepcopy(original)
    changed.update(batch_id="b", out_root="/two")
    changed["judge"].update(queue="/other", write=False)
    assert _frozen_hash(original) == _frozen_hash(changed)
    changed["audit"]["seed"] = "different"
    assert _frozen_hash(original) != _frozen_hash(changed)


def test_cached_zero_spend_replay(monkeypatch, tmp_path):
    spec_path = _spec(tmp_path)
    cache = tmp_path / "cache.jsonl"
    write_rows(cache, [])
    spec = read_json(spec_path)
    spec["autoreview"].update(mode="cached", cache=str(cache))
    write_json(spec_path, spec)
    seen = _mock_line(monkeypatch, tmp_path)
    baseline = tmp_path / "baseline.json"
    assert replay(spec_path, baseline, tmp_path, write_baseline=True)[1] == 0
    assert replay(spec_path, baseline, tmp_path)[1] == 0
    assert seen[1][1]["autoreview"]["mode"] == "cached"
    assert seen[1][1]["autoreview"]["cache"] == str(cache)


def test_referenced_rollout_bytes_changed_is_invalid(monkeypatch, tmp_path):
    spec_path = _spec(tmp_path)
    baseline = tmp_path / "baseline.json"
    _mock_line(monkeypatch, tmp_path)
    assert replay(spec_path, baseline, tmp_path, write_baseline=True)[1] == 0
    _mock_line(monkeypatch, tmp_path, intake_sha="different-rollout-bytes")
    result, code = replay(spec_path, baseline, tmp_path)
    assert code == 2 and "rollout cohort or bytes mismatch" in result["error"]
    assert result["changed_runs"] == [] and result["inputs_changed"] is True


def test_frozen_spec_change_still_reruns_and_reports_changed_runs(monkeypatch, tmp_path):
    spec_path = _spec(tmp_path)
    baseline = tmp_path / "baseline.json"
    _mock_line(monkeypatch, tmp_path)
    assert replay(spec_path, baseline, tmp_path, write_baseline=True)[1] == 0
    spec = read_json(spec_path)
    spec["audit"]["seed"] = "different"
    write_json(spec_path, spec)
    seen = _mock_line(monkeypatch, tmp_path, fixture=_fixture(recall=9))
    result, code = replay(spec_path, baseline, tmp_path)
    assert code == 2 and "frozen spec mismatch" in result["error"]
    assert result["inputs_changed"] is True
    assert [row["run_id"] for row in result["changed_runs"]] == ["cheat-9"]
    assert len(seen) == 1


def test_empty_declared_source_baselines_and_replays(monkeypatch, tmp_path):
    spec_path = _spec(tmp_path)
    fixture = _fixture()
    fixture["scoreboard"]["by_corpus"]["empty"] = _empty_group()
    _mock_line(monkeypatch, tmp_path, fixture=fixture)
    baseline = tmp_path / "baseline.json"
    result, code = replay(spec_path, baseline, tmp_path, write_baseline=True)
    assert code == 0, result
    result, code = replay(spec_path, baseline, tmp_path)
    assert code == 0 and result["changed_runs"] == [] and result["inputs_changed"] is False
