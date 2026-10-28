"""Staged cross-test adapter: synthetic adapter modules and bundles only; no staged data is read."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.production import trace_harness_staged as staged
from evallab.production.trace_harness import HarnessRun

AGENT = {"step_id": 3, "source": "agent", "message": "ran the tests", "tool_calls": []}
PROMPT = {"step_id": 1, "source": "user", "message": "fix the bug"}

ADAPTER = """
import json
from pathlib import Path

def specs(root):
    for line in (Path(root) / "specs.json").read_text().splitlines():
        yield json.loads(line)
"""


def install(tmp_path: Path, name: str, specs: list[dict[str, Any]]) -> dict[str, Any]:
    """A synthetic adapter at the registered repo-relative path and a bundle holding its specs."""
    repo = tmp_path / "repo"
    adapter = repo / staged.STAGED_SETS[name].adapter
    adapter.parent.mkdir(parents=True, exist_ok=True)
    adapter.write_text(ADAPTER, encoding="utf-8")
    bundle = tmp_path / "bundle"
    bundle.mkdir(exist_ok=True)
    (bundle / "specs.json").write_text("\n".join(json.dumps(spec) for spec in specs), encoding="utf-8")
    return {"root": bundle, "repo_root": repo}


def beam(key: str, state: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {"identity": {"trace_key": key * 64}, "labels": {"state": state}, "_steps": steps}


def test_runs_carry_original_step_ids_and_actor_flag(tmp_path):
    where = install(tmp_path, "beam-tripwire", [
        beam("a", "positive", [PROMPT, AGENT]),
        beam("b", "negative", [PROMPT]),
    ])
    runs = list(staged.staged_runs("beam-tripwire", **where))
    assert [run.run_id for run in runs] == ["staged:beam-tripwire:" + "a" * 16, "staged:beam-tripwire:" + "b" * 16]
    assert all(isinstance(run, HarnessRun) for run in runs)
    assert {(run.source, run.split) for run in runs} == {("staged:beam-tripwire", "CROSS-TEST")}
    assert runs[0].step_ids == [1, 3]
    assert [run.actor_inputs_available for run in runs] == [True, False]


def test_limit_takes_the_first_rows(tmp_path):
    where = install(tmp_path, "beam-tripwire", [beam(k, "negative", [AGENT]) for k in "abc"])
    assert len(list(staged.staged_runs("beam-tripwire", limit=2, **where))) == 2
    assert list(staged.staged_runs("beam-tripwire", limit=0, **where)) == []


def test_labels_keep_considered_only_rows_out_of_the_negatives(tmp_path):
    where = install(tmp_path, "beam-tripwire", [
        beam("a", "positive", [AGENT]),
        beam("b", "negative", [AGENT]),
        beam("c", "ambiguous", [AGENT]),
        beam("d", "excluded", [AGENT]),
    ])
    run_ids, positive, steps = staged.staged_labels("beam-tripwire", **where)
    prefix = "staged:beam-tripwire:"
    assert run_ids == {prefix + k * 16 for k in "abcd"}
    assert positive == {
        prefix + "a" * 16: True, prefix + "b" * 16: False, prefix + "c" * 16: None, prefix + "d" * 16: None,
    }
    assert steps == {}
    assert set(positive) == {run.run_id for run in staged.staged_runs("beam-tripwire", **where)}


def test_boolean_positive_and_hashed_native_ids(tmp_path):
    flag = {"trace_key": "f" * 16, "positive": True, "_steps": [AGENT]}
    where = install(tmp_path, "kernelbench-hard-runs", [flag, {**flag, "trace_key": "0" * 16, "positive": False}])
    _, positive, _ = staged.staged_labels("kernelbench-hard-runs", **where)
    assert sorted(positive.values()) == [False, True]

    cheat = {"identity": {"run_id": "native/run-1"}, "gold": {"label_state": "positive"}, "_steps": [AGENT]}
    where = install(tmp_path, "agent-cheat-index", [cheat])
    [run] = staged.staged_runs("agent-cheat-index", **where)
    assert run.run_id == "staged:agent-cheat-index:" + staged._hash16("native/run-1")
    assert "native" not in run.run_id


def test_a_repeated_row_id_is_refused(tmp_path):
    where = install(tmp_path, "beam-tripwire", [beam("a", "positive", [AGENT]), beam("a", "negative", [AGENT])])
    with pytest.raises(ValueError, match="duplicate row id"):
        list(staged.staged_runs("beam-tripwire", **where))


def test_census_counts_labels_steps_and_missing_actor_input(tmp_path):
    where = install(tmp_path, "beam-tripwire", [
        beam("a", "positive", [PROMPT, AGENT]),
        beam("b", "negative", [PROMPT]),
        beam("c", "negative", [PROMPT, AGENT, AGENT]),
        beam("d", "ambiguous", [AGENT]),
    ])
    assert staged.staged_census("beam-tripwire", **where) == {
        "set": "beam-tripwire", "rows": 4, "positives": 1, "negatives": 2, "considered_only": 1,
        "steps_median": 1.5, "no_actor_input": 1,
    }


def test_unknown_excluded_and_absent_sets_fail_clearly(tmp_path):
    with pytest.raises(ValueError, match="excluded: .*segment"):
        staged.staged_set("sleight-bench")
    with pytest.raises(ValueError, match="qualifying sets"):
        staged.staged_set("nope")
    with pytest.raises(FileNotFoundError, match="not on this machine"):
        next(staged.staged_runs("beam-tripwire", root=tmp_path / "absent", repo_root=tmp_path))


def test_registered_sets_are_consistent():
    assert set(staged.STAGED_SETS) == set(staged._ACCESSORS)
    assert not set(staged.STAGED_SETS) & set(staged.EXCLUDED_SETS)
    for name, entry in staged.STAGED_SETS.items():
        assert entry.name == name
        assert entry.source == f"staged:{name}"
        assert entry.licence
        assert entry.bundle.count("/") == 1
        assert entry.positives > 0
        assert entry.negatives > 0
        assert entry.considered_only >= 0
        assert entry.established_negatives <= entry.negatives
        assert entry.negative_basis in {"established", "mixed"}
