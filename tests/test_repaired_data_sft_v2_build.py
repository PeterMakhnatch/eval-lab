"""Behavioral admission/accounting boundaries; no tokenizer download or execution."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_PATH = Path(__file__).resolve().parents[1] / "research/experiments/repaired-data-sft-v2/build.py"
_SPEC = importlib.util.spec_from_file_location("repaired_data_sft_v2_build", _PATH)
assert _SPEC and _SPEC.loader
build = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = build
_SPEC.loader.exec_module(build)

TASK = "mimo-v2.6-rl/format-code-task-000001"


def document(path: Path, value: object) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return {"path": str(path), "sha256": build.digest(path.read_bytes())}


def native(
    tmp_path: Path,
    *,
    trial: str = "trial-1",
    job: str = "job-1",
    task: str = "format-code-task-000001",
    ancestry: list | None = None,
):
    root = tmp_path / job / trial
    result = {
        "id": trial,
        "task_name": task,
        "config": {"job_id": job, "task": {}},
        "verifier_result": {"rewards": {"reward": 1.0}},
    }
    trajectory = {"agent": {"name": "mimoagent"}, "steps": []}
    row = {
        "job_id": job,
        "trial_id": trial,
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
    for bad in (
        heldout[:26],
        heldout[:-1] + [heldout[0]],
        heldout[:-1] + ["format-code-task-999999"],
    ):
        ref = document(tmp_path / "bad-heldout.json", {"schema": "mimo.heldout/v1", "heldout": bad})
        with pytest.raises(build.BuildError, match="ALL33"):
            build.load_heldout(build.Evidence(tmp_path), ref)


def test_heldout_descendant_cannot_hide_behind_a_renamed_trial(tmp_path):
    parent = sorted(build.FROZEN_HELDOUT)[0]
    package = tmp_path / "native-parent"
    package.mkdir()
    (package / "task.toml").write_text(f'[metadata]\nsource_id = "{parent.split("/")[-1]}"\n')
    lineage = document(
        tmp_path / "lineage.json",
        {
            "schema": "evallab.task_variant/v1",
            "task_name": "mimo-v2.6-rl/a-renamed-repair",
            "variant_digest": "sha256:" + "a" * 64,
            "parent": {
                "digest": "sha256:" + "b" * 64,
                "source": {"kind": "local", "path": str(package)},
            },
        },
    )
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
    assert [r.source.key for r in selected] == [
        r.source.key for r in build.matched_control(list(reversed(pool)), b, seed=20261017)
    ]
    assert {r.source.key for r in selected} != {r.source.key for r in b}
    assert sum(r.assistant_loss_tokens for r in selected) == 11
    assert sum(r.sequence_tokens for r in selected) == 700
    assert (
        build.totals(b, 3)["assistant_loss_token_presentations"]
        == build.totals(selected, 3)["assistant_loss_token_presentations"]
        == 33
    )
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


def replay(
    tmp_path: Path,
    score: int = 1,
    *,
    package: Path | None = None,
    script: str | None = None,
    name: str = "qa-replay-native",
):
    from evallab.registry import compute_task_digests
    from evallab.runner import staged_matrix_request
    from evallab.schemas import ExperimentMatrix, MatrixRun

    task = package or tmp_path / "variants/source"
    if package is None:
        (task / "solution").mkdir(parents=True)
        (task / "tests").mkdir()
        (task / "task.toml").write_text(
            f'[task]\nname = "{TASK}"\n[environment]\nworkdir = "/testbed"\n'
        )
        (task / "solution/solve.sh").write_text("#!/bin/bash\nexit 0\n")
        (task / "tests/test.sh").write_text("#!/bin/bash\nexit 0\n")
    digests = compute_task_digests(task)
    expected = digests.package
    if script is None:
        script = "#!/bin/bash\nset -e\ncd /testbed\nexit 0\n"
    script_path = tmp_path / "runs/scripts" / f"{name}.sh"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script)
    sha = hashlib.sha256(script.encode()).hexdigest()
    run = MatrixRun(
        name=name, agent="oracle", solution=str(script_path.relative_to(tmp_path))
    )
    matrix = ExperimentMatrix(
        matrix_id="0" * 26,
        name=run.name,
        hypothesis="A retained native patch replay qualifies a clean observation.",
        benchmark_family="qa-trace-regrade",
        task_id="format-code-task-000001",
        task=str(task),
        task_package_digest=expected,
        verifier_digest=digests.verifier,
        jobs_dir="runs",
        runs=[run],
    )
    with staged_matrix_request(matrix, run, repo_root=tmp_path) as (_, provenance):
        staged_digest = provenance["staged_task_digest"]
    assert staged_digest != expected
    receipt = {
        "matrix": matrix.model_dump(mode="json"),
        "results": [
            {"name": run.name, **provenance, "status": "ok", "rewards": [score], "error": None}
        ],
    }
    path = tmp_path / "runs/.executor" / f"{name}.matrix.json"
    document(path, receipt)
    job = tmp_path / "runs" / run.name
    document(
        job / "result.json",
        {
            "id": "native-job",
            "n_total_trials": 1,
            "stats": {},
            "finished_at": "2026-10-17T06:00:02Z",
            "exception_info": None,
        },
    )
    document(
        job / "lab-metadata.json",
        {
            "exit_code": 0,
            "timed_out": False,
            "task_staging": {
                "source_package_digest": staged_digest,
                "source_harbor_digest": "sha256:" + "c" * 64,
                "staged_harbor_digest": "sha256:" + "c" * 64,
            },
        },
    )
    trial = job / "native-trial"
    document(
        trial / "result.json",
        {
            "id": "native-trial",
            "trial_name": "native-trial",
            "task_name": "mimo-v2.6-rl/format-code-task-000001",
            "finished_at": "2026-10-17T06:00:02Z",
            "exception_info": None,
            "agent_execution": {
                "started_at": "2026-10-17T06:00:00Z",
                "finished_at": "2026-10-17T06:00:01Z",
            },
            "verifier_result": {"rewards": {"reward": score}},
        },
    )
    (trial / "agent").mkdir()
    (trial / "agent/oracle.txt").write_text("")
    row = {
        "grader": f"mimo-clean-v6:{expected[7:19]}",
        "grader_identity": {"task_name": TASK, "variant_digest": expected},
        "input_kind": "native_patch_replay",
        "matrix_receipt": str(path),
        "job_dir": str(job),
        "script_sha256": sha,
        "regraded": score,
        "refusals": [],
        "execution_error": None,
    }
    qualification = build.Qualification.__new__(build.Qualification)
    qualification.evidence = build.Evidence(tmp_path)
    return qualification, row, expected, script, receipt, path


def qualified_case(tmp_path: Path, monkeypatch, *, composed: bool = False):
    """Real /3 selections, lineage and retained native jobs; never runs an executor."""
    from evallab import task_variants
    from evallab.mimo_clean import MANIFEST_COLUMNS, write_manifest
    from evallab.qa import counterfactual as cf

    patch = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
    parent = tmp_path / "before-v6"
    (parent / "tests").mkdir(parents=True)
    (parent / "solution").mkdir()
    (parent / "environment/setup").mkdir(parents=True)
    (parent / "instruction.md").write_text("Replace old with new.\n")
    (parent / "task.toml").write_text(
        'schema_version = "1.4"\n'
        f'[task]\nname = "{TASK}"\n'
        '[environment]\nworkdir = "/testbed"\ndocker_image = "fixture:never-executed"\n'
        "[verifier]\n"
        + ("" if composed else 'environment_mode = "separate"\n')
    )
    (parent / "solution/solve.sh").write_text("#!/bin/bash\nexit 0\n")
    (parent / "tests/test.sh").write_text("#!/bin/bash\ntrue\n")
    (parent / "tests/test.patch").write_text(patch.replace("+new", "+old-test"))
    (parent / "tests/test_command.sh").write_text("pytest x.py\n")
    (parent / "environment/setup/setup.sh").write_text("#!/bin/bash\ntrue\n")
    store, records = tmp_path / "variants", tmp_path / "repair-records"
    monkeypatch.setattr(build, "REPO", tmp_path)
    monkeypatch.setattr(task_variants, "default_variants_root", lambda _: store)
    before_v6 = task_variants.derive_task(
        parent,
        changes={"tests/test.patch": patch.encode()},
        transform="verifier-repair@1",
        rationale="A genuine earlier repair can also be selected by the v6 manifest.",
        created_by="pytest",
        repo_root=tmp_path,
        records_dir=records,
        variants_root=store,
    )
    before_path = records / before_v6.task_slug / f"{before_v6.digest12}.json"
    before_package = store / before_v6.task_slug / before_v6.digest12
    final = (
        cf.separate_over(before_package, TASK, tmp_path, via="verifier-repair@1")
        if composed
        else cf.Grader(
            TASK, "verifier-repair@1", before_v6.variant_digest, before_package, str(before_path)
        )
    )
    assert final is not None
    manifest_package = tmp_path / "manifest-package"
    shutil.copytree(final.package, manifest_package)
    repaired = task_variants.derive_task(
        manifest_package,
        changes={"tests/test.patch": (patch + "# genuine v6-child regression\n").encode()},
        transform="verifier-repair@1",
        rationale="Only this test-only child has the exact v6 parent qualification.",
        created_by="pytest",
        repo_root=tmp_path,
        records_dir=records,
        variants_root=store,
    )
    repaired_path = records / repaired.task_slug / f"{repaired.digest12}.json"
    manifest = tmp_path / "v6.csv"
    manifest_row = {
        **dict.fromkeys(MANIFEST_COLUMNS, ""),
        "task_id": TASK.rsplit("/", 1)[-1],
        "status": "built",
        "chain": ">".join(sorted(build.V6_CHAIN)),
        "final_digest": final.variant_digest,
        "package_path": str(manifest_package),
    }
    write_manifest([manifest_row], manifest)
    repair_paths = [before_path, before_path, repaired_path]
    graders = cf.newer_graders(
        TASK, tmp_path, cf.repair_records(repair_paths), manifest=manifest
    )
    assert len(graders) == 2
    source, _, _ = native(tmp_path)
    trial = source.result_path.parent
    (trial / "verifier").mkdir()
    (trial / "verifier/agent.diff").write_text(patch)
    reference = patch.replace("+new", "+reference")
    monkeypatch.setattr(cf, "reference_patch", lambda *_: reference)
    controls, observations = [], []
    for index, grader in enumerate(graders):
        native_rows = {}
        for kind, score, diff in (("nop", 0, ""), ("reference", 1, reference), ("source", 1, patch)):
            kept, _ = cf.strip_binary_artifacts(diff)
            _, row, expected, _, _, _ = replay(
                tmp_path,
                score,
                package=grader.package,
                script=cf._patch_script(kept, "/testbed"),
                name=f"qa-replay-{index}-{kind}",
            )
            assert expected == grader.variant_digest
            native_rows[kind] = {
                **row,
                "grader": grader.label,
                "grader_identity": grader.identity_json,
                "trial": str(trial),
                "recorded": 1.0,
                "outcome": "stays" if score else "flips",
                "environment": "docker",
            }
        controls.append(
            {
                "grader_identity": grader.identity_json,
                "engine": "qa-trace-counterfactual/3",
                "reference_sha256": hashlib.sha256(reference.encode()).hexdigest(),
                "nop": 0.0,
                "reference": 1.0,
                "reference_available": True,
                "valid": True,
                "rows": {kind: native_rows[kind] for kind in ("nop", "reference")},
            }
        )
        observations.append(
            {
                **native_rows["source"],
                "engine": "qa-trace-counterfactual/3",
                "execution_id": source.key[1],
                "source_digest": cf.source_digest(trial),
                "grader_source": grader.to_json(),
                "transform": grader.transform,
                "valid_grader": True,
                "controls": {"nop": 0.0, "reference": 1.0},
            }
        )
    evidence = build.Evidence(tmp_path)
    inputs = {
        "v6_manifest": evidence.capture(manifest),
        "repairs": [evidence.capture(path) for path in repair_paths],
        "controls": [
            document(tmp_path / "controls" / f"{index}.json", control)
            for index, control in enumerate(controls)
        ],
        "regrades": [document(tmp_path / "observations.json", observations)],
    }
    return SimpleNamespace(
        source=source,
        inputs=inputs,
        controls=controls,
        observations=observations,
        manifest=manifest,
        manifest_row=manifest_row,
        manifest_package=manifest_package,
        before_package=before_package,
        before_path=before_path,
        repaired_path=repaired_path,
        v6_digest=final.variant_digest,
        repaired_digest=repaired.variant_digest,
        qualification=lambda: build.Qualification(build.Evidence(tmp_path), inputs),
    )


@pytest.mark.parametrize("representative", ["manifest", "earlier_repair"])
def test_physical_aliases_share_native_controls_and_one_observation_per_identity(
    tmp_path, monkeypatch, representative
):
    case = qualified_case(tmp_path, monkeypatch)
    v6 = next(
        row for row in case.observations
        if row["grader_identity"]["variant_digest"] == case.v6_digest
    )
    aliases = v6["grader_source"]["aliases"]
    assert len(aliases) == 3
    assert {alias["transform"] for alias in aliases} == {"mimo-clean-v6", "verifier-repair@1"}
    if representative == "earlier_repair":
        alias = next(alias for alias in aliases if alias["transform"] == "verifier-repair@1")
        v6.update(grader=alias["label"], transform=alias["transform"])
        v6["grader_source"].update(
            label=alias["label"], transform=alias["transform"],
            package=alias["package"], record=alias["records"][0],
        )
        case.inputs["regrades"] = [document(tmp_path / "observations.json", case.observations)]
    # Repeated document references and selection aliases do not create measurements.
    case.inputs["regrades"] *= 2
    qualification = case.qualification()
    accepted = qualification.clean(case.source)
    assert len(accepted) == len(case.controls) == 2
    assert qualification.target(v6, case.source)[:2] == (
        {"mimo-clean-v6"}, (TASK, case.v6_digest)
    )
    repaired = next(
        row for row in accepted if row["grader_identity"]["variant_digest"] == case.repaired_digest
    )
    assert qualification.target(repaired, case.source)[:2] == (
        {"verifier-repair@1"}, (TASK, case.repaired_digest)
    )
    assert qualification._checked_controls == {
        (TASK, case.v6_digest), (TASK, case.repaired_digest)
    }
    assert all("grader" not in control for control in case.controls)
    assert all("grader_digest" not in row for row in accepted)
    assert str(case.before_path) in qualification.evidence.files
    assert str(case.repaired_path) in qualification.evidence.files
    assert any(path.endswith("agent/oracle.txt") for path in qualification.evidence.files)


@pytest.mark.parametrize("composed", [False, True])
def test_aliases_cannot_replace_a_genuine_exact_v6_child_measurement(
    tmp_path, monkeypatch, composed
):
    case = qualified_case(tmp_path, monkeypatch, composed=composed)
    v6 = next(
        row for row in case.observations
        if row["grader_identity"]["variant_digest"] == case.v6_digest
    )
    case.inputs["regrades"] = [document(tmp_path / "only-v6.json", [v6])]
    with pytest.raises(build.BuildError, match="BOTH mimo-clean-v6 and verifier-repair@1"):
        case.qualification().clean(case.source)


def test_composed_alias_retains_raw_source_custody_without_granting_its_repair_role(
    tmp_path, monkeypatch
):
    case = qualified_case(tmp_path, monkeypatch, composed=True)
    qualification = case.qualification()
    accepted = qualification.clean(case.source)
    v6 = next(
        row for row in accepted if row["grader_identity"]["variant_digest"] == case.v6_digest
    )
    composed = next(alias for alias in v6["grader_source"]["aliases"] if alias["source"])
    assert composed["source"]["variant_digest"] != case.v6_digest
    assert qualification.target(v6, case.source)[0] == {"mimo-clean-v6"}
    assert str(case.before_package / "task.toml") in qualification.evidence.files
    assert str(case.before_path) in qualification.evidence.files
    assert str(build.REPO / composed["records"][0]) in qualification.evidence.files


@pytest.mark.parametrize(
    "change",
    ["task", "digest_suffix", "short_digest", "label_only", "engine_v2", "native_task", "native_digest"],
)
def test_controls_require_exact_full_physical_identity_not_a_shared_label(
    tmp_path, monkeypatch, change
):
    case = qualified_case(tmp_path, monkeypatch)
    index = next(
        i for i, control in enumerate(case.controls)
        if control["grader_identity"]["variant_digest"] == case.repaired_digest
    )
    control = case.controls[index]
    identity = control["grader_identity"]
    if change == "task":
        identity["task_name"] = TASK.replace("000001", "000002")
    elif change == "digest_suffix":
        identity["variant_digest"] = case.repaired_digest[:19] + "0" * 52
    elif change == "short_digest":
        identity["variant_digest"] = case.repaired_digest[:19]
    elif change == "label_only":
        control["grader"] = f"verifier-repair@1:{case.repaired_digest[7:19]}"
        del control["grader_identity"]
    elif change == "engine_v2":
        control["engine"] = "qa-trace-counterfactual/2"
    else:
        identity = control["rows"]["nop"]["grader_identity"]
        if change == "native_task":
            identity["task_name"] = TASK.replace("000001", "000002")
        else:
            identity["variant_digest"] = case.v6_digest
    case.inputs["controls"][index] = document(tmp_path / "changed-controls.json", control)
    with pytest.raises(build.BuildError):
        case.qualification().clean(case.source)


@pytest.mark.parametrize(
    "change",
    [
        "identity_task", "identity_digest", "source_identity", "source_digest", "alias_digest",
        "manifest_row", "manifest_path", "manifest_record", "alias_package", "engine_v2",
        "missing_identity", "missing_aliases",
    ],
)
def test_observation_and_every_alias_must_bind_actual_package_and_native_manifest(
    tmp_path, monkeypatch, change
):
    case = qualified_case(tmp_path, monkeypatch)
    row = next(
        row for row in case.observations
        if row["grader_identity"]["variant_digest"] == case.v6_digest
    )
    gs = row["grader_source"]
    alias = next(alias for alias in gs["aliases"] if alias["manifest"])
    wrong_digest = case.v6_digest[:19] + "0" * 52
    if change == "identity_task":
        row["grader_identity"] = {**row["grader_identity"], "task_name": TASK + "-other"}
    elif change == "identity_digest":
        row["grader_identity"] = {**row["grader_identity"], "variant_digest": wrong_digest}
    elif change == "source_identity":
        gs["identity"] = {**gs["identity"], "task_name": TASK + "-other"}
    elif change == "source_digest":
        gs["variant_digest"] = wrong_digest
    elif change == "alias_digest":
        alias["variant_digest"] = wrong_digest
    elif change == "manifest_row":
        alias["manifest_row"] = {**alias["manifest_row"], "final_digest": wrong_digest}
    elif change == "manifest_path":
        other = tmp_path / "different-v6.csv"
        shutil.copyfile(case.manifest, other)
        alias["manifest"] = str(other)
    elif change == "manifest_record":
        alias["records"] = [str(case.manifest) + "#format-code-task-000002"]
    elif change == "alias_package":
        bad = tmp_path / "changed-alias-package"
        shutil.copytree(case.before_package, bad)
        (bad / "instruction.md").write_text("Different package with the same claimed digest.\n")
        next(a for a in gs["aliases"] if not a["manifest"])["package"] = str(bad)
    elif change == "engine_v2":
        row["engine"] = "qa-trace-counterfactual/2"
    elif change == "missing_identity":
        del row["grader_identity"]
    else:
        del gs["aliases"]
    case.inputs["regrades"] = [document(tmp_path / "changed-observations.json", case.observations)]
    with pytest.raises(build.BuildError):
        case.qualification().clean(case.source)


@pytest.mark.parametrize("change", ["task", "digest", "record"])
def test_repair_alias_cannot_borrow_another_native_task_digest_or_record(
    tmp_path, monkeypatch, change
):
    case = qualified_case(tmp_path, monkeypatch)
    row = next(
        row for row in case.observations
        if row["grader_identity"]["variant_digest"] == case.repaired_digest
    )
    alias = row["grader_source"]["aliases"][0]
    record = json.loads(case.repaired_path.read_text())
    if change == "task":
        record["task_name"] = TASK.replace("000001", "000002")
    elif change == "digest":
        record["variant_digest"] = case.v6_digest
    else:
        alias["records"] = [str(case.before_path)]
    if change != "record":
        ref = document(tmp_path / "substituted-repair.json", record)
        case.inputs["repairs"].append(ref)
        alias["records"] = [ref["path"]]
    case.inputs["regrades"] = [document(tmp_path / "changed-observations.json", case.observations)]
    with pytest.raises(build.BuildError, match="native task/digest/record binding"):
        case.qualification().clean(case.source)


@pytest.mark.parametrize("change", ["source_digest", "source_package", "source_record", "parent_digest"])
def test_composed_alias_refuses_wrong_original_source_and_native_ancestry(
    tmp_path, monkeypatch, change
):
    case = qualified_case(tmp_path, monkeypatch, composed=True)
    row = next(
        row for row in case.observations
        if row["grader_identity"]["variant_digest"] == case.v6_digest
    )
    alias = next(alias for alias in row["grader_source"]["aliases"] if alias["source"])
    source = alias["source"]
    if change == "source_digest":
        source["variant_digest"] = case.repaired_digest
    elif change == "source_package":
        (case.before_package / "instruction.md").write_text("Mutated original package.\n")
    elif change == "source_record":
        source["records"] = [str(case.repaired_path)]
    else:
        path = build.REPO / alias["records"][0]
        record = json.loads(path.read_text())
        record["parent"]["digest"] = case.repaired_digest
        document(path, record)
    case.inputs["regrades"] = [document(tmp_path / "changed-observations.json", case.observations)]
    with pytest.raises(build.BuildError):
        case.qualification().clean(case.source)


@pytest.mark.parametrize("borrow", ["control", "observation"])
def test_distinct_full_physical_identities_cannot_share_native_execution_receipts(
    tmp_path, monkeypatch, borrow
):
    case = qualified_case(tmp_path, monkeypatch)
    if borrow == "control":
        left, right = case.controls
        left["rows"] = copy.deepcopy(right["rows"])
        case.inputs["controls"][0] = document(tmp_path / "borrowed-control.json", left)
    else:
        left, right = case.observations
        for field in ("matrix_receipt", "job_dir", "script_sha256"):
            left[field] = right[field]
        case.inputs["regrades"] = [document(tmp_path / "borrowed-observation.json", case.observations)]
    with pytest.raises(build.BuildError, match="physical grader identity|native matrix grader digest"):
        case.qualification().clean(case.source)


def test_genuine_native_replay_uses_exact_staged_digest_and_patch_bytes(tmp_path):
    qualification, row, expected, script, _, _ = replay(tmp_path)
    qualification.native_replay(row, (TASK, expected), 1, script=script)
    with pytest.raises(build.BuildError, match="actual source/control patch"):
        qualification.native_replay(
            row, (TASK, expected), 1, script=script + "# substituted answer\n"
        )


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
    with pytest.raises(build.BuildError, match="native matrix|script hash|staged package"):
        qualification.native_replay(row, (TASK, expected), 1)


@pytest.mark.parametrize(
    "change", ["reward", "exception", "oracle_exit", "oracle_log", "staging", "timed_out"]
)
def test_matrix_success_cannot_override_actual_native_counterevidence(tmp_path, change):
    qualification, row, expected, _, _, _ = replay(tmp_path)
    job = Path(row["job_dir"])
    result_path = job / "native-trial/result.json"
    result = json.loads(result_path.read_text())
    if change == "reward":
        result["verifier_result"]["rewards"]["reward"] = 0
        document(result_path, result)
    elif change == "exception":
        result["exception_info"] = {"exception_type": "VerifierTimeoutError"}
        document(result_path, result)
    elif change == "oracle_exit":
        (job / "native-trial/agent/exit-code.txt").write_text("42")
    elif change == "oracle_log":
        (job / "native-trial/agent/oracle.txt").unlink()
    else:
        metadata_path = job / "lab-metadata.json"
        metadata = json.loads(metadata_path.read_text())
        if change == "staging":
            metadata["task_staging"]["source_package_digest"] = expected
        else:
            metadata["timed_out"] = True
        document(metadata_path, metadata)
    with pytest.raises(build.BuildError, match="native replay outcome/staging"):
        qualification.native_replay(row, (TASK, expected), 1)


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
        qualification.native_replay(row, (TASK, "sha256:" + "d" * 64), 1)


def test_missing_real_envcheck_correction_is_a_refusal_not_a_fabricated_row(tmp_path):
    with pytest.raises(build.BuildError, match="missing genuine source/corrected"):
        build.correction_row(
            build.Evidence(tmp_path),
            {"source": ["a", "b"], "corrected": ["c", "d"]},
            {},
            None,
            set(),
            None,
        )


def envcheck_controls(tmp_path: Path, change: str | None = None):
    expected = "sha256:" + "a" * 64
    task = "mimo-v2.6-rl/format-code-task-000169"
    identity = task, expected
    variant = {
        "variant_digest": expected,
        "task": task,
        "status": "repaired",
        "declared_survivors": 0,
        "v6_oracle_x3": [1, 1, 1],
        "v6_nop_x3": [0, 0, 0],
    }
    archive = tmp_path / "archive"
    for agent, score in (("oracle", 1), ("nop", 0)):
        job = archive / "v6-controls/jobs" / f"v6rep-000169-{agent}"
        name = f"v6rep-000169-{agent}__actual"
        metadata = {
            "exit_code": 0,
            "timed_out": False,
            "task_staging": {
                "source_package_digest": expected,
                "declared_task_name": task,
                "source_harbor_digest": "sha256:" + "b" * 64,
                "staged_harbor_digest": "sha256:" + "b" * 64,
            },
        }
        result = {
            "id": "native-" + agent,
            "task_name": task,
            "exception_info": None,
            "config": {"job_id": "job-" + agent, "agent": {"name": agent}},
            "verifier_result": {"rewards": {"reward": score}},
        }
        stability = {
            "schema": "evallab.verifier_stability/v1",
            "n_runs": 3,
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
        document(
            job / "result.json",
            {
                "id": "job-" + agent,
                "stats": {
                    "evals": {agent + "__adhoc": {"reward_stats": {"reward": {str(score): [name]}}}}
                },
            },
        )
        document(job / name / "result.json", result)
        document(job / name / "verifier/stability.json", stability)
        (job / name / "verifier/reward.txt").write_text(str(score))
    qualification = build.Qualification.__new__(build.Qualification)
    qualification.evidence = build.Evidence(tmp_path)
    qualification.envcheck_controls = {identity: (variant, archive)}
    return qualification, identity, {"task": task}


def test_retained_envcheck_reader_uses_real_native_control_contract(tmp_path):
    qualification, identity, package = envcheck_controls(tmp_path)
    qualification.retained_envcheck_controls(identity, package)
    evidence_paths = qualification.evidence.files
    assert any(path.endswith("verifier/stability.json") for path in evidence_paths)
    assert any(path.endswith("lab-metadata.json") for path in evidence_paths)


@pytest.mark.parametrize("change", ["digest", "reward", "repeat", "partial"])
def test_reported_envcheck_certification_does_not_override_native_controls(tmp_path, change):
    qualification, identity, package = envcheck_controls(tmp_path, change)
    with pytest.raises(build.BuildError, match="EnvCheck"):
        qualification.retained_envcheck_controls(identity, package)


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
    corrected.provenance["metadata"] = document(
        tmp_path / "correction-metadata.json",
        {
            "task_staging": {
                "source_package_digest": "sha256:" + "b" * 64
                if bad == "different_digest"
                else expected,
                "declared_task_name": corrected.task,
                "source_harbor_digest": "sha256:" + "c" * 64,
                "staged_harbor_digest": "sha256:" + "c" * 64,
            },
        },
    )
    if bad is None:
        build.correction_environment(evidence, corrected, expected)
    else:
        with pytest.raises(build.BuildError, match="separate"):
            build.correction_environment(evidence, corrected, expected)


@pytest.mark.parametrize(
    "change", ["control_reward", "observation_reward", "oracle_exit", "staged_package"]
)
def test_physical_identity_does_not_override_actual_native_custody_counterevidence(
    tmp_path, monkeypatch, change
):
    case = qualified_case(tmp_path, monkeypatch)
    observation = next(
        row for row in case.observations
        if row["grader_identity"]["variant_digest"] == case.repaired_digest
    )
    if change == "control_reward":
        control = next(
            control for control in case.controls
            if control["grader_identity"]["variant_digest"] == case.repaired_digest
        )
        row = control["rows"]["reference"]
    else:
        row = observation
    job = Path(row["job_dir"])
    if change.endswith("reward"):
        path = job / "native-trial/result.json"
        result = json.loads(path.read_text())
        result["verifier_result"]["rewards"]["reward"] = 0
        document(path, result)
    elif change == "oracle_exit":
        (job / "native-trial/agent/exit-code.txt").write_text("42")
    else:
        path = job / "lab-metadata.json"
        metadata = json.loads(path.read_text())
        # The original grader is never interchangeable with the script-staged package.
        metadata["task_staging"]["source_package_digest"] = case.repaired_digest
        document(path, metadata)
    with pytest.raises(build.BuildError, match="native replay outcome/staging"):
        case.qualification().clean(case.source)


@pytest.mark.parametrize(
    "change", ["earlier_parent_digest", "repair_parent_digest", "agent_visible", "manifest_package"]
)
def test_alias_roles_require_authentic_parent_bytes_and_test_only_v6_child_ancestry(
    tmp_path, monkeypatch, change
):
    case = qualified_case(tmp_path, monkeypatch)
    if change == "manifest_package":
        from evallab.mimo_clean import write_manifest

        bad = tmp_path / "wrong-manifest-package"
        shutil.copytree(case.manifest_package, bad)
        (bad / "instruction.md").write_text("Wrong manifest package.\n")
        case.manifest_row["package_path"] = str(bad)
        write_manifest([case.manifest_row], case.manifest)
        case.inputs["v6_manifest"] = build.Evidence(tmp_path).capture(case.manifest)
        for row in case.observations:
            for alias in row["grader_source"]["aliases"]:
                if alias["manifest"]:
                    alias["manifest_row"] = case.manifest_row
        case.inputs["regrades"] = [
            document(tmp_path / "changed-observations.json", case.observations)
        ]
    else:
        path = case.before_path if change == "earlier_parent_digest" else case.repaired_path
        record = json.loads(path.read_text())
        if change.endswith("parent_digest"):
            old = record["parent"]["digest"]
            record["parent"]["digest"] = old[:-1] + ("0" if old[-1] != "0" else "1")
        else:
            record["components_changed"] = ["verifier", "solution"]
        ref = document(path, record)
        case.inputs["repairs"] = [
            ref if item["path"] == str(path) else item for item in case.inputs["repairs"]
        ]
    with pytest.raises(build.BuildError):
        case.qualification().clean(case.source)


def test_correction_regrade_keeps_proven_repair_role_and_full_native_fail_to_pass_identity(
    tmp_path, monkeypatch
):
    from evallab.qa import counterfactual as cf

    case = qualified_case(tmp_path, monkeypatch)
    passed = next(
        row for row in case.observations
        if row["grader_identity"]["variant_digest"] == case.repaired_digest
    )
    diff = (case.source.result_path.parent / "verifier/agent.diff").read_text()
    kept, _ = cf.strip_binary_artifacts(diff)
    _, native_failed, _, _, _, _ = replay(
        tmp_path,
        0,
        package=Path(passed["grader_source"]["package"]),
        script=cf._patch_script(kept, "/testbed"),
        name="qa-replay-native-failed-correction",
    )
    failed = {
        **passed,
        **native_failed,
        "grader": passed["grader"],
        "grader_identity": passed["grader_identity"],
        "outcome": "flips",
    }
    qualification = case.qualification()
    expected = {"verifier-repair@1"}, (TASK, case.repaired_digest)
    assert qualification.regrade(failed, case.source, 0) == expected
    assert qualification.regrade(passed, case.source, 1) == expected
