"""Focused tests for evallab.harbor_view (HAR-170).

Consumer-visible behavior only: overlay keeps ``reward`` identical,
``integrity`` is 0 exactly when a deterministic rule fires, ``reward_gated``
is the product, native-dims trials pass through byte-identical, unscored
trials get no dims, and sources are never mutated.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evallab import harbor_view
from evallab.harbor_view import (
    RULE_COPY_CHECK,
    RULE_GRADER_TAMPER,
    RULE_UPSTREAM_FETCH,
    build_viewer_root,
    compile_arm_pattern,
    derive_arm,
    discover_jobs,
    installed_harbor_version,
    parse_merge_spec,
    rule_versions,
    viewer_command,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _trial_result(name: str, *, rewards: dict | None) -> dict:
    return {
        "task_name": "pkg/task",
        "trial_name": name,
        "trial_uri": f"file:///jobs/job/{name}",
        "task_id": {"path": "/tmp/task"},
        "task_checksum": "abc",
        "config": {
            "task": {"path": "/tmp/task"},
            "trial_name": name,
            "trials_dir": "/tmp/trials",
            "agent": {"name": "agent", "model_name": "provider/model"},
            "environment": {"import_path": "env"},
            "job_id": "job-1",
        },
        "agent_info": {
            "name": "agent",
            "model_info": {"name": "model", "provider": "provider"},
        },
        "agent_result": {
            "n_input_tokens": 100,
            "n_cache_tokens": 0,
            "n_output_tokens": 10,
            "cost_usd": None,
        },
        "verifier_result": None if rewards is None else {"rewards": rewards},
        "exception_info": None,
        "started_at": "2026-10-01T00:00:00Z",
        "finished_at": "2026-10-01T00:01:00Z",
    }


def _make_job(
    root: Path,
    name: str,
    trial_rewards: dict[str, dict | None],
    *,
    processed: dict[str, dict] | None = None,
    trajectories: dict[str, dict] | None = None,
) -> Path:
    job = root / name
    _write_json(job / "config.json", {"job": name})
    _write_json(job / "result.json", {"job": name})
    for trial_name, rewards in trial_rewards.items():
        trial = job / trial_name
        trial.mkdir(parents=True, exist_ok=True)
        _write_json(trial / "config.json", {"trial_name": trial_name})
        _write_json(trial / "result.json", _trial_result(trial_name, rewards=rewards))
        (trial / "agent").mkdir(exist_ok=True)
    for trial_name, record in (processed or {}).items():
        _write_json(job / "processed" / f"trial-{trial_name}.json", record)
    for trial_name, trajectory in (trajectories or {}).items():
        _write_json(job / trial_name / "agent" / "trajectory.json", trajectory)
    return job


def _snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            out[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


def _stored_record(taint: list) -> dict:
    return {"taint": taint, "errors": {}}


def _benign_trajectory() -> dict:
    return {
        "steps": [
            {
                "source": "agent",
                "step_id": 0,
                "tool_calls": [
                    {
                        "function_name": "bash_command",
                        "arguments": {"keystrokes": "ls /tmp/work"},
                    }
                ],
            }
        ]
    }


def _tamper_trajectory() -> dict:
    return {
        "steps": [
            {
                "source": "agent",
                "step_id": 0,
                "tool_calls": [
                    {
                        "function_name": "bash_command",
                        "arguments": {"keystrokes": "cat tests/foo.py"},
                    }
                ],
            },
            {
                "source": "agent",
                "step_id": 1,
                "tool_calls": [
                    {
                        "function_name": "bash_command",
                        "arguments": {"keystrokes": "echo hi > tests/foo.py"},
                    }
                ],
            },
        ]
    }


def test_overlay_gates_tainted_reward_but_keeps_it(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _make_job(
        src,
        "job-a",
        {"trial-t": {"reward": 1.0}},
        processed={
            "trial-t": _stored_record(
                [
                    {
                        "kind": "copied_code",
                        "rule": "copy_check/v1",
                        "matched_lines": 7,
                        "added_lines": 40,
                        "examples": ["def solver():"],
                    }
                ]
            )
        },
    )
    out = tmp_path / "view"
    report = build_viewer_root([src / "job-a"], out)

    rewards = json.loads(
        (out / "job-a" / "trial-t" / "result.json").read_text(encoding="utf-8")
    )["verifier_result"]["rewards"]
    assert rewards["reward"] == 1.0
    assert rewards["integrity"] == 0
    assert rewards["reward_gated"] == 0.0
    details = json.loads(
        (out / "job-a" / "trial-t" / "reward-details.json").read_text(encoding="utf-8")
    )
    assert details["fired_rules"] == [RULE_COPY_CHECK]
    assert details["rule_versions"] == rule_versions()
    assert (out / "job-a" / "trial-t").is_dir() and not (
        out / "job-a" / "trial-t"
    ).is_symlink()
    job_counts = report["jobs"]["job-a"]
    assert job_counts["overlay"] == 1
    assert job_counts["integrity_0"] == ["trial-t"]
    assert job_counts["rules_fired"] == {RULE_COPY_CHECK: 1}


def test_clean_stored_trial_scores_full(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _make_job(
        src,
        "job-a",
        {"trial-c": {"reward": 1.0}},
        processed={"trial-c": _stored_record([])},
    )
    out = tmp_path / "view"
    build_viewer_root([src / "job-a"], out)

    rewards = json.loads(
        (out / "job-a" / "trial-c" / "result.json").read_text(encoding="utf-8")
    )["verifier_result"]["rewards"]
    assert rewards == {"reward": 1.0, "integrity": 1, "reward_gated": 1.0}
    details = json.loads(
        (out / "job-a" / "trial-c" / "reward-details.json").read_text(encoding="utf-8")
    )
    assert details["fired_rules"] == []
    assert details["provenance"] == "stored"


def test_native_dims_trials_pass_through_byte_identical(tmp_path: Path) -> None:
    src = tmp_path / "src"
    job = _make_job(
        src, "job-a", {"trial-n": {"reward": 0.0, "integrity": 0, "reward_gated": 0.0}}
    )
    before = (job / "trial-n" / "result.json").read_bytes()
    out = tmp_path / "view"
    report = build_viewer_root([src / "job-a"], out)

    trial_out = out / "job-a" / "trial-n"
    assert trial_out.is_symlink()
    assert (trial_out / "result.json").read_bytes() == before
    assert not (out / "job-a" / "trial-n" / "reward-details.json").exists()
    assert report["jobs"]["job-a"]["native"] == 1
    assert report["jobs"]["job-a"]["overlay"] == 0


@pytest.mark.parametrize("rewards", [None, {}])
def test_unscored_trials_get_no_dims(tmp_path: Path, rewards: dict | None) -> None:
    src = tmp_path / "src"
    if rewards == {}:
        trial_rewards: dict[str, dict | None] = {"trial-u": {}}
    else:
        trial_rewards = {"trial-u": None}
    _make_job(src, "job-a", trial_rewards)
    out = tmp_path / "view"
    report = build_viewer_root([src / "job-a"], out)

    trial_out = out / "job-a" / "trial-u"
    assert trial_out.is_symlink()
    assert not (out / "job-a" / "trial-u" / "reward-details.json").exists()
    assert report["jobs"]["job-a"]["unscored"] == 1
    assert report["jobs"]["job-a"]["scored"] == 0


def test_sources_are_never_mutated(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _make_job(
        src,
        "job-a",
        {
            "trial-t": {"reward": 1.0},
            "trial-n": {"reward": 1.0, "integrity": 1, "reward_gated": 1.0},
            "trial-u": None,
        },
        processed={"trial-t": _stored_record([])},
    )
    before = _snapshot(src)
    build_viewer_root([src / "job-a"], tmp_path / "view")
    assert _snapshot(src) == before


def test_compute_path_without_stored_record(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _make_job(
        src,
        "job-a",
        {"trial-x": {"reward": 0.0}},
        trajectories={"trial-x": _benign_trajectory()},
    )
    out = tmp_path / "view"
    build_viewer_root([src / "job-a"], out)

    rewards = json.loads(
        (out / "job-a" / "trial-x" / "result.json").read_text(encoding="utf-8")
    )["verifier_result"]["rewards"]
    assert rewards["integrity"] == 1
    assert rewards["reward_gated"] == 0.0
    details = json.loads(
        (out / "job-a" / "trial-x" / "reward-details.json").read_text(encoding="utf-8")
    )
    assert details["provenance"].startswith("computed")


def test_grader_tamper_is_computed_and_fires(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _make_job(
        src,
        "job-a",
        {"trial-g": {"reward": 1.0}},
        trajectories={"trial-g": _tamper_trajectory()},
    )
    out = tmp_path / "view"
    build_viewer_root([src / "job-a"], out)

    rewards = json.loads(
        (out / "job-a" / "trial-g" / "result.json").read_text(encoding="utf-8")
    )["verifier_result"]["rewards"]
    assert rewards["reward"] == 1.0
    assert rewards["integrity"] == 0
    assert rewards["reward_gated"] == 0
    details = json.loads(
        (out / "job-a" / "trial-g" / "reward-details.json").read_text(encoding="utf-8")
    )
    assert details["fired_rules"] == [RULE_GRADER_TAMPER]


def test_stored_upstream_attempt_without_confirmation_stays_clean(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    _make_job(
        src,
        "job-a",
        {"trial-f": {"reward": 1.0}},
        processed={
            "trial-f": _stored_record(
                [
                    {
                        "kind": "upstream_fetch",
                        "outcome": "failed",
                        "target": "https://example.com/x",
                    }
                ]
            )
        },
    )
    out = tmp_path / "view"
    build_viewer_root([src / "job-a"], out)
    rewards = json.loads(
        (out / "job-a" / "trial-f" / "result.json").read_text(encoding="utf-8")
    )["verifier_result"]["rewards"]
    assert rewards["integrity"] == 1
    assert rewards["reward_gated"] == 1.0


def test_discover_jobs_expands_roots_and_skips_strays(tmp_path: Path) -> None:
    src = tmp_path / "src"
    job = _make_job(src, "job-a", {"trial-t": {"reward": 1.0}})
    (src / "stray.txt").write_text("x", encoding="utf-8")
    (src / "empty-dir").mkdir()

    jobs, skipped = discover_jobs([src])
    assert jobs == [job.resolve()]
    assert any("empty-dir" in note for note in skipped)

    direct, _ = discover_jobs([job])
    assert direct == [job.resolve()]

    missing, skipped_missing = discover_jobs([src / "nope"])
    assert missing == []
    assert skipped_missing


def test_build_refuses_non_empty_out(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _make_job(src, "job-a", {"trial-t": {"reward": 1.0}})
    out = tmp_path / "view"
    out.mkdir()
    (out / "junk").write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        build_viewer_root([src / "job-a"], out)


def test_viewer_command_pins_024_when_installed_is_older(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harbor_view, "installed_harbor_version", lambda: (0, 21, 0))
    cmd = viewer_command(tmp_path, port=8080)
    assert cmd[:5] == ["uv", "tool", "run", "--from", "harbor==0.24.0"]
    assert cmd[5:8] == ["harbor", "view", str(tmp_path)]


def test_viewer_command_uses_installed_when_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harbor_view, "installed_harbor_version", lambda: (0, 24, 0))
    cmd = viewer_command(tmp_path, port=9000)
    assert cmd[:2] == ["harbor", "view"]
    assert "--port" in cmd and "9000" in cmd


def test_rule_versions_are_grounded() -> None:
    from evallab.copy_check import RULE as copy_rule

    versions = rule_versions()
    assert versions[RULE_COPY_CHECK] == copy_rule
    assert set(versions) == {RULE_COPY_CHECK, RULE_UPSTREAM_FETCH, RULE_GRADER_TAMPER}
    assert installed_harbor_version() is None or len(installed_harbor_version()) == 3


def test_merge_folds_jobs_preserves_reward_records_source(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _make_job(
        src,
        "exp-001-stock",
        {"trial-s": {"reward": 1.0}},
        processed={"trial-s": _stored_record([])},
    )
    _make_job(
        src,
        "exp-001-gepa",
        {"trial-g": {"reward": 0.0}},
        processed={
            "trial-g": _stored_record(
                [
                    {
                        "kind": "copied_code",
                        "rule": "copy_check/v1",
                        "matched_lines": 6,
                        "added_lines": 30,
                        "examples": ["def solver():"],
                    }
                ]
            )
        },
    )
    _make_job(src, "exp-001-tuned", {"trial-u": None})
    before = _snapshot(src)
    jobs, _ = discover_jobs([src])
    pattern = compile_arm_pattern(r"-(?P<arm>stock|tuned|gepa)$")
    out = tmp_path / "view"
    report = build_viewer_root(
        jobs, out, merges=[("merged", "exp-001-*")], arm_pattern=pattern
    )

    assert report["totals"]["jobs"] == 1
    assert report["totals"]["trials"] == 3
    merged = report["merged"]["merged"]
    assert merged["sources"] == ["exp-001-gepa", "exp-001-stock", "exp-001-tuned"]
    assert merged["arms"] == {
        "trial-g": "gepa",
        "trial-s": "stock",
        "trial-u": "tuned",
    }
    job_dir = out / "merged"
    config = json.loads((job_dir / "config.json").read_text(encoding="utf-8"))
    assert config["job_name"] == "merged"
    result = json.loads((job_dir / "result.json").read_text(encoding="utf-8"))
    assert result["n_total_trials"] == 3
    assert result["stats"]["n_completed_trials"] == 3
    assert result["stats"]["n_errored_trials"] == 0
    assert set(result["stats"]["evals"]) == {
        "agent__model__stock",
        "agent__model__gepa",
        "agent__model__tuned",
    }
    assert result["stats"]["evals"]["agent__model__tuned"]["n_trials"] == 0
    for trial_name, source_job, arm in (
        ("trial-s", "exp-001-stock", "stock"),
        ("trial-g", "exp-001-gepa", "gepa"),
        ("trial-u", "exp-001-tuned", "tuned"),
    ):
        record = json.loads(
            (job_dir / trial_name / ".evallab-source.json").read_text(
                encoding="utf-8"
            )
        )
        assert record["source_job"] == source_job
        assert record["arm"] == arm
        overlaid = json.loads(
            (job_dir / trial_name / "result.json").read_text(encoding="utf-8")
        )
        assert overlaid["source"] == arm
    scored = json.loads(
        (job_dir / "trial-s" / "result.json").read_text(encoding="utf-8")
    )["verifier_result"]["rewards"]
    assert scored["reward"] == 1.0
    assert scored["integrity"] == 1
    assert scored["reward_gated"] == 1.0
    assert _snapshot(src) == before


def test_merge_spec_and_arm_pattern_reject_bad_input() -> None:
    with pytest.raises(ValueError):
        parse_merge_spec("no-equals-sign")
    assert parse_merge_spec("name=glob*") == ("name", "glob*")
    with pytest.raises(ValueError):
        compile_arm_pattern(r"(no-named-group)")
    pattern = compile_arm_pattern(r"-(?P<arm>stock|tuned)$")
    assert derive_arm("job-stock", pattern) == "stock"
    assert derive_arm("job-other", pattern) == "job-other"
