"""Behavioral admission/accounting boundaries; no tokenizer download or execution."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


_PATH = Path(__file__).resolve().parents[1] / "research/experiments/repaired-data-sft-v2/build.py"
_SPEC = importlib.util.spec_from_file_location("repaired_data_sft_v2_build", _PATH)
assert _SPEC and _SPEC.loader
build = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = build
_SPEC.loader.exec_module(build)


def document(path: Path, value: object) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return {"path": str(path), "sha256": build.digest(path.read_bytes())}


def native(tmp_path: Path, *, trial: str = "trial-1", job: str = "job-1", task: str = "format-code-task-000001", ancestry: list | None = None):
    root = tmp_path / job / trial
    result = {
        "id": trial, "task_name": task,
        "config": {"job_id": job, "task": {}},
        "verifier_result": {"rewards": {"reward": 1.0}},
    }
    trajectory = {"agent": {"name": "mimoagent"}, "steps": []}
    row = {
        "job_id": job, "trial_id": trial,
        "result": document(root / "result.json", result),
        "trajectory": document(root / "agent/trajectory.json", trajectory),
        "ancestry": ancestry or [],
    }
    evidence = build.Evidence(tmp_path)
    return build.native_source(evidence, row), evidence, row


def prepared(tmp_path: Path, index: int, weight: int, *, sequence: int | None = None):
    source, _, _ = native(tmp_path, trial=f"trial-{index}")
    messages = [
        {"role": "user", "content": f"Full original task {index}"},
        {"role": "assistant", "content": f"Original complete target {index}"},
    ]
    return build.Prepared(source, messages, sequence or weight + 10, weight)


def test_hash_binding_refuses_stale_source_bytes(tmp_path):
    source, evidence, row = native(tmp_path)
    source.trajectory_path.write_text('{"steps": ["changed"]}')
    with pytest.raises(build.BuildError, match="changed during build|hash mismatch"):
        build.native_source(evidence, row)


def test_inventory_label_cannot_replace_native_execution_identity(tmp_path):
    _, evidence, row = native(tmp_path)
    row["trial_id"] = "another-native-trial"
    with pytest.raises(build.BuildError, match="identity mismatch"):
        build.native_source(evidence, row)


def test_all33_authoritative_heldout_parents_are_required(tmp_path):
    heldout = sorted(build.FROZEN_HELDOUT)
    ref = document(tmp_path / "heldout.json", {"schema": "mimo.heldout/v1", "heldout": heldout})
    assert build.load_heldout(build.Evidence(tmp_path), ref) == build.FROZEN_HELDOUT
    for bad in (heldout[:26], heldout[:-1] + [heldout[0]], heldout[:-1] + ["format-code-task-999999"]):
        ref = document(tmp_path / "bad-heldout.json", {"schema": "mimo.heldout/v1", "heldout": bad})
        with pytest.raises(build.BuildError, match="ALL33"):
            build.load_heldout(build.Evidence(tmp_path), ref)


def test_heldout_descendant_cannot_hide_behind_a_renamed_trial(tmp_path):
    parent = sorted(build.FROZEN_HELDOUT)[0]
    package = tmp_path / "native-parent"
    package.mkdir()
    (package / "task.toml").write_text(f'[metadata]\nsource_id = "{parent.split("/")[-1]}"\n')
    lineage = document(tmp_path / "lineage.json", {
        "schema": "evallab.task_variant/v1", "task_name": "mimo-v2.6-rl/a-renamed-repair",
        "variant_digest": "sha256:" + "a" * 64,
        "parent": {"digest": "sha256:" + "b" * 64, "source": {"kind": "local", "path": str(package)}},
    })
    source, _, _ = native(tmp_path, task="a-renamed-repair", ancestry=[lineage])
    assert source.task == parent
    with pytest.raises(build.BuildError, match="heldout/excluded"):
        build.original_pass_eligible(source, set(build.FROZEN_HELDOUT), set(), set())


def test_plain_heldout_and_correction_targets_never_enter_original_pool(tmp_path):
    source, _, _ = native(tmp_path, task=sorted(build.FROZEN_HELDOUT)[0])
    with pytest.raises(build.BuildError, match="heldout/excluded"):
        build.original_pass_eligible(source, set(build.FROZEN_HELDOUT), set(), set())
    source, _, _ = native(tmp_path, trial="corrected")
    with pytest.raises(build.BuildError, match="D, not original"):
        build.original_pass_eligible(source, set(), set(), {source.key})
    source.result["verifier_result"]["rewards"]["reward"] = 0
    with pytest.raises(build.BuildError, match="not an observed original"):
        build.original_pass_eligible(source, set(), set(), set())


def test_native_pair_deduplication_does_not_inflate_minimum(tmp_path):
    source, _, _ = native(tmp_path)
    alias = copy.deepcopy(source)
    alias.result_path = tmp_path / "published-copy/result.json"
    assert build.deduplicate([source, alias]) == [source]
    conflict = copy.deepcopy(alias)
    conflict.trajectory["steps"] = [{"content": "different target bytes"}]
    with pytest.raises(build.BuildError, match="conflicting native copies"):
        build.deduplicate([source, conflict])
    # Native authority is the pair, not an artifact directory or a task display name.
    another_job = copy.deepcopy(source)
    another_job.key = ("job-2", source.key[1])
    assert len(build.deduplicate([source, another_job])) == 2


def test_minimum_requires51_distinct_positive_loss_rows(tmp_path):
    rows = [prepared(tmp_path, i, 1) for i in range(51)]
    build.require_minimum(rows)
    with pytest.raises(build.BuildError, match=r"distinct_nonzero=50, required=51"):
        build.require_minimum(rows[:50])
    with pytest.raises(build.BuildError, match="distinct_nonzero=50"):
        build.require_minimum(rows[:50] + [rows[0]])
    rows[-1].assistant_loss_tokens = 0
    with pytest.raises(build.BuildError, match="distinct_nonzero=50"):
        build.require_minimum(rows)
    with pytest.raises(build.BuildError, match="cannot be smaller"):
        build.require_minimum(rows, minimum=17)


def test_randomized_feasible_control_matches_exact_loss_not_sequence_tokens(tmp_path):
    b = [prepared(tmp_path, 1, 4, sequence=100), prepared(tmp_path, 2, 7, sequence=110)]
    extra = [prepared(tmp_path, 3, 5, sequence=300), prepared(tmp_path, 4, 6, sequence=400)]
    pool = b + extra
    before = copy.deepcopy([r.messages for r in pool])
    selected = build.matched_control(pool, b, seed=20261017)
    assert [r.source.key for r in selected] == [r.source.key for r in build.matched_control(list(reversed(pool)), b, seed=20261017)]
    assert {r.source.key for r in selected} != {r.source.key for r in b}
    assert sum(r.assistant_loss_tokens for r in selected) == 11
    assert sum(r.sequence_tokens for r in selected) == 700
    assert build.totals(b, 3)["assistant_loss_token_presentations"] == build.totals(selected, 3)["assistant_loss_token_presentations"] == 33
    assert [r.messages for r in pool] == before  # no truncation, padding, or target rewrites
    assert all(any(r is candidate for candidate in pool) for r in selected)


@pytest.mark.parametrize("extra_weights", [[], [12], [3, 6]])
def test_control_refuses_infeasible_exposure_and_b_only_fallback(tmp_path, extra_weights):
    b = [prepared(tmp_path, 1, 11)]
    pool = b + [prepared(tmp_path, i + 2, w) for i, w in enumerate(extra_weights)]
    with pytest.raises(build.BuildError, match="infeasible exact subset sum"):
        build.matched_control(pool, b, seed=9)


def test_control_refuses_duplicate_or_zero_loss_candidates(tmp_path):
    row = prepared(tmp_path, 1, 1)
    with pytest.raises(build.BuildError, match="distinct original-pass"):
        build.matched_control([row, row], [row], 0)
    zero = prepared(tmp_path, 2, 0)
    with pytest.raises(build.BuildError, match="positive assistant-loss"):
        build.matched_control([row, zero], [row], 0)


def replay(tmp_path: Path, score: int = 1):
    expected = "sha256:" + "d" * 64
    script = "#!/bin/bash\nset -e\ncd /testbed\nexit 0\n"
    script_path = tmp_path / "runs/scripts/source.sh"
    script_path.parent.mkdir(parents=True)
    script_path.write_text(script)
    sha = hashlib.sha256(script.encode()).hexdigest()
    receipt = {
        "matrix": {"task_package_digest": expected, "runs": [{"agent": "oracle", "solution": "runs/scripts/source.sh"}]},
        "results": [{"status": "ok", "rewards": [score], "error": None, "staged_task_digest": expected, "solution_sha256": sha}],
    }
    path = tmp_path / "runs/.executor/real.matrix.json"
    document(path, receipt)
    row = {"input_kind": "native_patch_replay", "matrix_receipt": str(path), "script_sha256": sha, "regraded": score, "refusals": [], "execution_error": None}
    qualification = build.Qualification.__new__(build.Qualification)
    qualification.evidence = build.Evidence(tmp_path)
    return qualification, row, expected, script, receipt, path


def test_genuine_native_replay_uses_exact_staged_digest_and_patch_bytes(tmp_path):
    qualification, row, expected, script, _, _ = replay(tmp_path)
    qualification.native_replay(row, expected, 1, script=script)
    with pytest.raises(build.BuildError, match="actual source/control patch"):
        qualification.native_replay(row, expected, 1, script=script + "# substituted answer\n")


@pytest.mark.parametrize("change", ["staged_digest", "reward", "script_hash", "status"])
def test_scalar_success_cannot_override_native_counterevidence(tmp_path, change):
    _, row, expected, _, receipt, path = replay(tmp_path)
    outcome = receipt["results"][0]
    if change == "staged_digest":
        outcome["staged_task_digest"] = "sha256:" + "e" * 64
    elif change == "reward":
        outcome["rewards"] = [0]
    elif change == "script_hash":
        outcome["solution_sha256"] = "0" * 64
    else:
        outcome["status"] = "error"
    document(path, receipt)
    qualification = build.Qualification.__new__(build.Qualification)
    qualification.evidence = build.Evidence(tmp_path)
    with pytest.raises(build.BuildError, match="native matrix|script hash"):
        qualification.native_replay(row, expected, 1)


def test_raw_reward_or_scalar_controls_do_not_qualify_as_clean(tmp_path):
    source, _, _ = native(tmp_path)
    qualification = build.Qualification.__new__(build.Qualification)
    qualification.faults = {}
    qualification.regrades = {}
    with pytest.raises(build.BuildError, match="no hash-bound clean regrade"):
        qualification.clean(source)
    qualification.evidence = build.Evidence(tmp_path)
    row = {"regraded": 1, "controls": {"nop": 0, "reference": 1}, "valid_grader": True}
    with pytest.raises(build.BuildError, match="native patch replay"):
        qualification.native_replay(row, "sha256:" + "d" * 64, 1)


def test_missing_real_envcheck_correction_is_a_refusal_not_a_fabricated_row(tmp_path):
    with pytest.raises(build.BuildError, match="missing genuine source/corrected"):
        build.correction_row(build.Evidence(tmp_path), {"source": ["a", "b"], "corrected": ["c", "d"]}, {}, None, set(), None)


def envcheck_controls(tmp_path: Path, change: str | None = None):
    expected = "sha256:" + "a" * 64
    task = "mimo-v2.6-rl/format-code-task-000169"
    label = "verifier-repair@1:" + "a" * 12
    variant = {
        "variant_digest": expected, "task": task, "status": "repaired",
        "declared_survivors": 0, "v6_oracle_x3": [1, 1, 1], "v6_nop_x3": [0, 0, 0],
    }
    archive = tmp_path / "archive"
    for agent, score in (("oracle", 1), ("nop", 0)):
        job = archive / "v6-controls/jobs" / f"v6rep-000169-{agent}"
        name = f"v6rep-000169-{agent}__actual"
        metadata = {
            "exit_code": 0, "timed_out": False,
            "task_staging": {
                "source_package_digest": expected, "declared_task_name": task,
                "source_harbor_digest": "sha256:" + "b" * 64,
                "staged_harbor_digest": "sha256:" + "b" * 64,
            },
        }
        result = {
            "id": "native-" + agent, "task_name": task, "exception_info": None,
            "config": {"job_id": "job-" + agent, "agent": {"name": agent}},
            "verifier_result": {"rewards": {"reward": score}},
        }
        stability = {
            "schema": "evallab.verifier_stability/v1", "n_runs": 3,
            "repeat_n_requested": 3,
            "runs": [{"index": i, "reward": score, "error": None} for i in range(3)],
        }
        if agent == "oracle":
            if change == "digest":
                metadata["task_staging"]["source_package_digest"] = "sha256:" + "c" * 64
            elif change == "reward":
                result["verifier_result"]["rewards"]["reward"] = 0
            elif change == "repeat":
                stability["runs"][-1]["reward"] = 0
            elif change == "partial":
                variant["status"] = "partial"
        document(job / "lab-metadata.json", metadata)
        document(job / "result.json", {
            "id": "job-" + agent,
            "stats": {"evals": {agent + "__adhoc": {"reward_stats": {"reward": {str(score): [name]}}}}},
        })
        document(job / name / "result.json", result)
        document(job / name / "verifier/stability.json", stability)
        (job / name / "verifier/reward.txt").write_text(str(score))
    qualification = build.Qualification.__new__(build.Qualification)
    qualification.evidence = build.Evidence(tmp_path)
    qualification.envcheck_controls = {label: (variant, archive)}
    return qualification, label, expected, {"task": task}


def test_retained_envcheck_reader_uses_real_native_control_contract(tmp_path):
    qualification, label, expected, package = envcheck_controls(tmp_path)
    qualification.retained_envcheck_controls(label, expected, package)
    evidence_paths = qualification.evidence.files
    assert any(path.endswith("verifier/stability.json") for path in evidence_paths)
    assert any(path.endswith("lab-metadata.json") for path in evidence_paths)


@pytest.mark.parametrize("change", ["digest", "reward", "repeat", "partial"])
def test_reported_envcheck_certification_does_not_override_native_controls(tmp_path, change):
    qualification, label, expected, package = envcheck_controls(tmp_path, change)
    with pytest.raises(build.BuildError, match="EnvCheck"):
        qualification.retained_envcheck_controls(label, expected, package)


def test_c_sampling_frame_cannot_expand_to_unrelated_passing_jobs(tmp_path):
    source, _, _ = native(tmp_path, task="arvo_18737")
    frame = {"mimo-v2.6-rl/format-code-task-000001"}
    with pytest.raises(build.BuildError, match="actual built globally hardened v6 task domain"):
        build.original_pass_eligible(source, set(), set(), set(), v6_tasks=frame)
    source, _, _ = native(tmp_path, trial="code-trial")
    build.original_pass_eligible(source, set(), set(), set(), v6_tasks=frame)


@pytest.mark.parametrize("bad", [None, "shared", "different_digest"])
def test_correction_must_keep_hidden_sources_separate_on_exact_repaired_grader(tmp_path, bad):
    corrected, evidence, _ = native(tmp_path, trial="corrected")
    expected = "sha256:" + "a" * 64
    corrected.result["verifier_environment_mode"] = "shared" if bad == "shared" else "separate"
    corrected.provenance["metadata"] = document(tmp_path / "correction-metadata.json", {
        "task_staging": {
            "source_package_digest": "sha256:" + "b" * 64 if bad == "different_digest" else expected,
            "declared_task_name": corrected.task,
            "source_harbor_digest": "sha256:" + "c" * 64,
            "staged_harbor_digest": "sha256:" + "c" * 64,
        },
    })
    if bad is None:
        build.correction_environment(evidence, corrected, expected)
    else:
        with pytest.raises(build.BuildError, match="separate"):
            build.correction_environment(evidence, corrected, expected)
