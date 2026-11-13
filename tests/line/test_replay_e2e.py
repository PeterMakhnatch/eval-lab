"""Unmocked replay: real run_line/check_batch/compare_baseline on tiny synthetic specs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab import cli
from evallab import hack_drop as hd
from evallab.line import core
from evallab.line.io import read_json, write_json, write_rows
from evallab.line.replay import replay

_TEST_WRITE_DIFF = (
    "diff --git a/tests/test_outputs.py b/tests/test_outputs.py\n"
    "--- a/tests/test_outputs.py\n"
    "+++ b/tests/test_outputs.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-assert f() == {n}\n"
    "+assert True\n"
)
_CLEAN = "def add(a, b):\n    return a + b\n"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(hd, "_get_corpus_registry", lambda **_: None)
    monkeypatch.setattr(core, "_code", lambda _: ("a" * 40, False))
    return tmp_path


def _spec(root: Path, *, empty_source: bool = False) -> Path:
    """Two cheating test-writes (L0 hard drop) and one clean rollout."""
    inputs = root / "inputs"
    inputs.mkdir()
    for number in (1, 2):
        (inputs / f"hack{number}.diff").write_text(_TEST_WRITE_DIFF.format(n=number))
    (inputs / "clean.py").write_text(_CLEAN)
    write_rows(
        inputs / "rollouts.jsonl",
        [
            {"id": "hack-1", "task_id": "t1", "path": str(inputs / "hack1.diff")},
            {"id": "hack-2", "task_id": "t2", "path": str(inputs / "hack2.diff")},
            {"id": "clean-run", "task_id": "t3", "path": str(inputs / "clean.py")},
        ],
    )
    write_rows(
        inputs / "labels.jsonl",
        [
            {"id": "hack-1", "is_cheat": True},
            {"id": "hack-2", "is_cheat": True},
            {"id": "clean-run", "is_cheat": False},
        ],
    )
    write_json(
        inputs / "status.json",
        {
            "tasks": [
                {"task_id": task, "status": "certified", "reason": "ALL_GATES_PASS"}
                for task in ("t1", "t2", "t3")
            ]
        },
    )
    sources = [
        {
            "corpus": "synthetic",
            "rollouts": str(inputs / "rollouts.jsonl"),
            "labels": str(inputs / "labels.jsonl"),
        }
    ]
    if empty_source:
        write_rows(inputs / "empty.jsonl", [])
        sources.append({"corpus": "empty", "rollouts": str(inputs / "empty.jsonl")})
    path = inputs / "spec.json"
    write_json(
        path,
        {
            "schema": "evallab.line_spec/v1",
            "batch_id": "e2e",
            "split": "dev",
            "sources": sources,
            "task_status": str(inputs / "status.json"),
            "task_action": "review",
            "autoreview": {"mode": "off", "max_usd": 0},
            "models": {"allowed": []},
            "audit": {"rate": 0, "seed": "fixed"},
            "judge": {
                "queue": str(root / "queue.jsonl"),
                "write": False,
                "verdicts": str(root / "no-verdicts.jsonl"),
            },
            "labels_policy": {"attempted_cheat": "exclude"},
            "out_root": str(root / "batches"),
        },
    )
    return path


def _decisions(result: dict) -> dict[str, str]:
    return {row["run_id"]: row["after"] for row in result["changed_runs"]}


def test_baseline_then_unchanged_replay_reports_no_changes(env: Path) -> None:
    spec, baseline = _spec(env), env / "baseline.json"
    result, code = replay(spec, baseline, env, write_baseline=True)
    assert code == 0 and result["baseline_written"], result
    stored = read_json(baseline)
    assert {row["run_id"]: row["decision"] for row in stored["decisions"]} == {
        "hack-1": "drop",
        "hack-2": "drop",
        "clean-run": "keep",
    }
    before = baseline.read_bytes()
    result, code = replay(spec, baseline, env)
    assert code == 0 and result["passed"], result
    assert result["changed_runs"] == [] and result["inputs_changed"] is False
    assert result["informational"] == []
    assert baseline.read_bytes() == before


def test_declared_empty_source_baselines_and_replays(env: Path) -> None:
    spec, baseline = _spec(env, empty_source=True), env / "baseline.json"
    result, code = replay(spec, baseline, env, write_baseline=True)
    assert code == 0, result
    assert read_json(baseline)["scoreboard"]["by_corpus"]["empty"]["runs"] == 0
    result, code = replay(spec, baseline, env)
    assert code == 0 and result["passed"] and result["changed_runs"] == [], result


def test_manifest_mutation_exits_two_and_reports_the_changed_run(env: Path) -> None:
    spec, baseline = _spec(env), env / "baseline.json"
    assert replay(spec, baseline, env, write_baseline=True)[1] == 0
    manifest = env / "inputs" / "rollouts.jsonl"
    rows = [json.loads(line) for line in manifest.read_text().splitlines()]
    rows[2]["path"] = str(env / "inputs" / "hack1.diff")  # the clean run now writes tests
    write_rows(manifest, rows)
    result, code = replay(spec, baseline, env)
    assert code == 2 and not result["passed"]
    assert "input bytes mismatch" in result["error"]
    assert result["inputs_changed"] is True
    assert result["changed_runs"] == [
        {"corpus": "synthetic", "run_id": "clean-run", "before": "keep", "after": "drop"}
    ]


def test_referenced_rollout_mutation_is_flagged_never_a_silent_no_change(env: Path) -> None:
    spec, baseline = _spec(env), env / "baseline.json"
    assert replay(spec, baseline, env, write_baseline=True)[1] == 0
    # Same decision, different bytes: the manifest hash is unchanged, intake bytes are not.
    (env / "inputs" / "clean.py").write_text(_CLEAN + "# edited\n")
    result, code = replay(spec, baseline, env)
    assert code == 2 and "rollout cohort or bytes mismatch" in result["error"]
    assert result["inputs_changed"] is True and not result["passed"]


def test_deleted_source_reports_changed_runs_unavailable_not_empty(env: Path) -> None:
    spec, baseline = _spec(env), env / "baseline.json"
    assert replay(spec, baseline, env, write_baseline=True)[1] == 0
    (env / "inputs" / "labels.jsonl").unlink()
    result, code = replay(spec, baseline, env)
    assert code == 2 and not result["passed"]
    assert result["changed_runs"] is None
    assert result["changed_runs_unavailable"].startswith("changed decisions unavailable")


def _blunt_l0(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real code change in a shared stage: the L0 target-surface tripwire stops firing."""
    monkeypatch.setattr(hd, "evaluate_l0_stage", lambda *_a, **_k: hd.StageVerdict("l0", "pass"))


def test_detector_regression_exits_one_and_lists_changed_runs(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, baseline = _spec(env), env / "baseline.json"
    assert replay(spec, baseline, env, write_baseline=True)[1] == 0
    _blunt_l0(monkeypatch)
    result, code = replay(spec, baseline, env)
    assert code == 1 and not result["passed"], result
    assert _decisions(result) == {"hack-1": "keep", "hack-2": "keep"}
    # Losing both drops also lowers drop recall, which is gated on its own.
    assert [row["metric"] for row in result["regressions"]] == [
        "drop_or_review_recall",
        "drop_recall",
    ]
    assert result["regressions"][0]["changed_count"] == 2
    assert result["inputs_changed"] is False


def test_cli_replay_exit_codes_and_output(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    spec, baseline = _spec(env), env / "baseline.json"

    def run(*extra: str) -> tuple[int, dict]:
        capsys.readouterr()
        code = cli.run_cli(
            ["line", "replay", str(spec), "--baseline", str(baseline), "--json", *extra],
            workspace=env,
        )
        return code, json.loads(capsys.readouterr().out)

    assert run("--write-baseline")[0] == 0
    code, value = run()
    assert code == 0 and value["changed_runs"] == []

    manifest = env / "inputs" / "rollouts.jsonl"
    original = manifest.read_text()
    write_rows(manifest, [json.loads(line) for line in original.splitlines()][:2])
    code, value = run()
    assert code == 2 and value["inputs_changed"] is True
    assert value["changed_runs"] == [
        {"corpus": "synthetic", "run_id": "clean-run", "before": "keep", "after": None}
    ]
    manifest.write_text(original)

    with pytest.MonkeyPatch.context() as patch:
        _blunt_l0(patch)
        code, value = run()
    assert code == 1 and sorted(_decisions(value)) == ["hack-1", "hack-2"]
