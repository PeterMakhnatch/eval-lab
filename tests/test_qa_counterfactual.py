"""Counterfactual regrade: actual patch semantics, fail-closed evidence and T3 rulings."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab.qa import counterfactual as cf
from evallab.qa import trace

TASK = "mimo-v2.6-rl/format-code-task-000001"
PATCH = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"


def _package(path: Path, *, separate: bool = True) -> Path:
    (path / "tests").mkdir(parents=True)
    (path / "tests/test.sh").write_text("#!/bin/bash\ntrue\n")
    (path / "instruction.md").write_text("Replace old with new.\n")
    config = f'[task]\nname = "{TASK}"\n[environment]\nworkdir = "/testbed"\n'
    if separate:
        config += '[verifier]\nenvironment_mode = "separate"\n'
    (path / "task.toml").write_text(config)
    return path


def _grader(tmp: Path) -> cf.Grader:
    package = _package(tmp / "pkg")
    return cf.Grader(TASK, cf.V6, cf._digest(package), package, "manifest.csv")


def _trial(tmp: Path, *, diff: str | None = PATCH, reward: float = 1.0) -> Path:
    trial = tmp / "old-pass"
    (trial / "verifier").mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps({"verifier_result": {"rewards": {"reward": reward}}})
    )
    if diff is not None:
        (trial / "verifier/agent.diff").write_text(diff)
    return trial


@pytest.fixture(autouse=True)
def _isolated_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    import evallab.docker_lock

    monkeypatch.setattr(evallab.docker_lock, "docker_slot", lambda **_: nullcontext())


def test_patch_script_applies_exactly_the_change_and_fails_on_the_wrong_base(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work dir with spaces"
    work.mkdir()
    target = work / "x.py"
    target.write_text("old\n")
    script = cf._patch_script(PATCH, str(work))
    first = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert target.read_text() == "new\n"
    second = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert second.returncode != 0
    assert target.read_text() == "new\n"


@pytest.mark.parametrize(
    ("status", "rewards", "recorded", "outcome"),
    [
        ("ok", [0.0], 1.0, "flips"),
        ("ok", [1.0], 1.0, "stays"),
        ("ok", [1.0], 0.0, "loosened"),
        ("ok", [0.0], 0.0, "not_a_pass"),
        ("infra", [0.0], 1.0, "refused"),
        ("ok", [float("nan")], 1.0, "refused"),
        ("ok", [True], 1.0, "refused"),
        ("ok", [], 1.0, "refused"),
    ],
)
def test_patch_replay_never_counts_infra_or_absent_scores_as_rejections(
    tmp_path: Path, status: str, rewards: list[Any], recorded: float, outcome: str
) -> None:
    trial = _trial(tmp_path, reward=recorded)
    before = {str(p.relative_to(trial)): p.read_bytes() for p in trial.rglob("*") if p.is_file()}

    def matrix_runner(matrix, *, root):
        return SimpleNamespace(
            results=[
                {
                    "status": status,
                    "rewards": rewards,
                    "error": "infra" if status == "infra" else None,
                }
            ],
            receipt_path=root / "runs/matrix-receipt.json",
        )

    result = cf.replay(
        trial,
        _grader(tmp_path),
        tmp_path / "work",
        repo_root=tmp_path,
        matrix_runner=matrix_runner,
    )
    assert result["outcome"] == outcome
    assert result["input_kind"] == "native_patch_replay"
    assert before == {
        str(p.relative_to(trial)): p.read_bytes() for p in trial.rglob("*") if p.is_file()
    }
    assert not (trial / "artifacts").exists()  # no fabricated snapshot, manifest, or trajectory
    assert bool(result["refusals"]) is (outcome == "refused")


@pytest.mark.parametrize(
    ("environment", "takes_local_slot"), [("docker", True), ("daytona", False), ("modal", False)]
)
def test_replay_runs_on_the_requested_backend_and_only_docker_holds_the_host_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment: str, takes_local_slot: bool
) -> None:
    import evallab.docker_lock
    from evallab import regrade

    slots: list[str] = []

    def docker_slot(*, label: str):
        slots.append(label)
        return nullcontext()

    monkeypatch.setattr(evallab.docker_lock, "docker_slot", docker_slot)
    monkeypatch.setattr(regrade, "lab_harbor_problem", lambda: None)
    seen: list[str] = []

    def matrix_runner(matrix, *, root):
        seen.append(matrix.environment)
        return SimpleNamespace(
            results=[{"status": "ok", "rewards": [0.0], "error": None}],
            receipt_path=root / "runs/matrix-receipt.json",
        )

    gate = None if environment == "docker" else regrade.CloudSpendGate("go", 1.0, 0.01)
    result = cf.replay(
        _trial(tmp_path),
        _grader(tmp_path),
        tmp_path / "work",
        repo_root=tmp_path,
        matrix_runner=matrix_runner,
        environment=environment,
        spend_gate=gate,
    )
    assert seen == [environment]
    assert result["environment"] == environment
    assert result["outcome"] == "flips"
    assert bool(slots) is takes_local_slot


def test_paid_replays_need_a_gate_and_one_over_its_cap_is_refused_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab import regrade

    monkeypatch.setattr(regrade, "lab_harbor_problem", lambda: None)
    runs: list[str] = []

    def matrix_runner(matrix, *, root):
        runs.append(matrix.name)
        return SimpleNamespace(
            results=[{"status": "ok", "rewards": [1.0], "error": None}],
            receipt_path=root / "runs/matrix-receipt.json",
        )

    trial, grader = _trial(tmp_path), _grader(tmp_path)

    def paid(gate):
        return cf.replay(
            trial,
            grader,
            tmp_path / "work",
            repo_root=tmp_path,
            matrix_runner=matrix_runner,
            environment="modal",
            spend_gate=gate,
        )

    with pytest.raises(ValueError, match="paid"):
        paid(None)
    gate = regrade.CloudSpendGate("go", cap_usd=0.01, usd_per_execution=0.01)
    assert paid(gate)["outcome"] == "stays"
    over = paid(gate)
    assert over["outcome"] == "refused"
    assert over["refusals"] == ["spend_cap_reached"]
    assert len(runs) == gate.executions == 1


def test_replay_rejects_an_unknown_backend_before_running_anything(tmp_path: Path) -> None:
    def matrix_runner(matrix, *, root):
        raise AssertionError("must not run")

    with pytest.raises(ValueError, match="unsupported replay environment"):
        cf.replay(
            _trial(tmp_path),
            _grader(tmp_path),
            tmp_path / "work",
            repo_root=tmp_path,
            matrix_runner=matrix_runner,
            environment="kubernetes",
        )


def test_missing_change_is_unmeasurable_not_a_successful_nop(tmp_path: Path) -> None:
    result = cf.replay(
        _trial(tmp_path, diff=None), _grader(tmp_path), tmp_path / "work", repo_root=tmp_path
    )
    assert result["outcome"] == "refused"
    assert result["refusals"] == ["source_patch_unavailable"]


def test_only_compiled_artifact_stanzas_are_dropped() -> None:
    bytecode = (
        "diff --git a/pkg/__pycache__/x.pyc b/pkg/__pycache__/x.pyc\nGIT binary patch\nliteral 1\n"
    )
    kept, dropped = cf.strip_binary_artifacts(bytecode + PATCH)
    assert kept == PATCH
    assert dropped == ["pkg/__pycache__/x.pyc"]


@pytest.mark.parametrize(
    ("nop", "reference", "valid"),
    [
        (0.0, 1.0, True),
        (1.0, 1.0, False),
        (0.0, 0.0, False),
        (0.0, None, False),
        (None, 1.0, False),
    ],
)
def test_controls_require_both_nop_rejection_and_reference_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nop: float | None,
    reference: float | None,
    valid: bool,
) -> None:
    monkeypatch.setattr(
        cf,
        "replay",
        lambda *_, diff_text, **__: {"regraded": nop if diff_text == "" else reference},
    )
    monkeypatch.setattr(
        cf, "reference_patch", lambda *_: None if reference is None else "reference"
    )
    controls = cf.grader_controls(
        _grader(tmp_path), tmp_path, tmp_path / "controls", repo_root=tmp_path
    )
    assert controls["valid"] is valid
    assert controls["reference_available"] is (reference is not None)


def test_changing_the_reference_invalidates_cached_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch = ["reference-v1"]
    monkeypatch.setattr(cf, "reference_patch", lambda *_: patch[0])
    monkeypatch.setattr(
        cf,
        "replay",
        lambda *_, diff_text, **__: {"regraded": 0.0 if diff_text in ("", "reference-v2") else 1.0},
    )
    grader = _grader(tmp_path)
    first = cf.grader_controls(grader, tmp_path, tmp_path / "controls", repo_root=tmp_path)
    assert first["valid"] is True
    patch[0] = "reference-v2"
    second = cf.grader_controls(grader, tmp_path, tmp_path / "controls", repo_root=tmp_path)
    assert second["valid"] is False
    assert second["reference"] == 0.0


def test_t3_counterfactual_is_path_free_and_ignores_unvalidated_graders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graders = [_grader(tmp_path)]
    monkeypatch.setattr(cf, "newer_graders", lambda *_, **__: graders)
    monkeypatch.setattr(
        cf,
        "replay",
        lambda *_, **__: {
            "grader": graders[0].label,
            "outcome": "flips",
            "recorded": 1.0,
            "regraded": 0.0,
            "refusals": ["diagnostic: /private/runs/cheat-run/source.py"],
        },
    )
    monkeypatch.setattr(trace, "counterfactual_root", lambda: tmp_path / "cf")
    for valid in (False, True):
        monkeypatch.setattr(
            cf,
            "grader_controls",
            lambda *_, valid=valid, **__: {
                "valid": valid,
                "nop": 0.0,
                "reference": 1.0 if valid else None,
            },
        )
        result = trace.regrade_counterfactual(tmp_path, TASK)
        assert result["ran"] is valid
        assert "/private" not in json.dumps(result)
        assert "cheat-run" not in json.dumps(result)
    assert result["lowest"] == 0.0
    assert result["graders"][0]["grader_identity"] == graders[0].identity_json
    assert result["graders"][0]["aliases"] == [
        {
            "label": graders[0].label,
            "transform": cf.V6,
            "variant_digest": graders[0].variant_digest,
        }
    ]


def test_repaired_graders_use_only_the_recorded_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import evallab.task_variants as task_variants

    store = tmp_path / "variants"
    monkeypatch.setattr(task_variants, "default_variants_root", lambda *_: store)
    digests = {}

    def repair(name: str, *, separate: bool, task_name: str = TASK, tampered: bool = False):
        digest = "sha256:" + name * 64
        package = _package(store / task_name.replace("/", "__") / digest[7:19], separate=separate)
        digests[package] = "sha256:" + "f" * 64 if tampered else digest
        return (
            tmp_path / f"{name}.json",
            SimpleNamespace(
                task_name=task_name, transform="verifier-repair@1", variant_digest=digest
            ),
        )

    def separate_over(parent: Path, task_name: str, repo_root: Path, *, via: str):
        return cf.Grader(task_name, f"{via}>{cf.V10}", "sha256:" + "c" * 64, parent, "c.json")

    monkeypatch.setattr(cf, "_digest", lambda package: digests[package])
    monkeypatch.setattr(cf, "_records", lambda *_: [])
    monkeypatch.setattr(cf, "separate_over", separate_over)
    extra = [
        repair("1", separate=True),
        repair("2", separate=False),
        repair("3", separate=True, tampered=True),
        repair("4", separate=True, task_name="mimo-v2.6-rl/format-code-task-000002"),
    ]
    graders = cf.repaired_graders(TASK, tmp_path, [extra[0], extra[1], extra[3]])
    assert {g.variant_digest for g in graders} == {"sha256:" + "1" * 64, "sha256:" + "c" * 64}
    assert {g.transform for g in graders} == {
        "verifier-repair@1",
        "verifier-repair@1>separate-verifier@10",
    }
    with pytest.raises(ValueError, match="grader package digest mismatch"):
        cf.repaired_graders(TASK, tmp_path, extra)


def test_batch_cache_tracks_source_bytes_without_freezing_population_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trial, grader = _trial(tmp_path), _grader(tmp_path)
    monkeypatch.setattr(cf, "newer_graders", lambda *_, **__: [grader])
    monkeypatch.setattr(
        cf, "grader_controls", lambda *_, **__: {"valid": True, "nop": 0.0, "reference": 1.0}
    )
    monkeypatch.setattr(trace, "counterfactual_root", lambda: tmp_path / "cache")

    def observed(*_, **__):
        score = 0.0 if "+wrong" in (trial / "verifier/agent.diff").read_text() else 1.0
        return {
            "grader": grader.label,
            "recorded": 1.0,
            "regraded": score,
            "outcome": "flips" if score == 0 else "stays",
        }

    monkeypatch.setattr(cf, "replay", observed)
    subject = {"item_id": "example", "task": TASK, "trial": trial, "group": "unreviewed"}
    first = trace.regrade_passes([subject], tmp_path / "out", log=lambda _: None)
    assert first[0]["outcome"] == "stays"
    (trial / "verifier/agent.diff").write_text(PATCH.replace("+new", "+wrong"))
    second = trace.regrade_passes([subject], tmp_path / "out", log=lambda _: None)
    assert second[0]["outcome"] == "flips"
    subject["group"] = "faulty"
    third = trace.regrade_passes([subject], tmp_path / "out", log=lambda _: None)
    assert third[0]["outcome"] == "flips"
    assert third[0]["group"] == "faulty"


def test_plan_counts_exactly_the_executions_a_run_would_still_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trial, grader = _trial(tmp_path), _grader(tmp_path)
    monkeypatch.setattr(cf, "newer_graders", lambda *_, **__: [grader])
    monkeypatch.setattr(cf, "reference_patch", lambda *_: "reference")
    monkeypatch.setattr(trace, "counterfactual_root", lambda: tmp_path / "cache")
    started: list[str | None] = []

    def replay(*_, diff_text=None, **__):
        started.append(diff_text)
        return {"regraded": 0.0 if diff_text == "" else 1.0, "outcome": "stays"}

    monkeypatch.setattr(cf, "replay", replay)
    subject = {"item_id": "example", "task": TASK, "trial": trial, "group": "unreviewed"}
    missing = {"item_id": "gone", "task": TASK, "trial": None}

    before = trace.plan_regrade([subject, missing])
    assert started == []
    assert (before["control_executions"], before["replay_executions"]) == (2, 1)
    assert before["rows_without_execution"] == 1

    trace.regrade_passes([subject, missing], tmp_path / "out", log=lambda _: None)
    assert len(started) == before["executions"]
    assert trace.plan_regrade([subject])["executions"] == 0


def test_selected_manifest_refreshes_and_mutated_package_is_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.mimo_clean import MANIFEST_COLUMNS, write_manifest
    from evallab.storage import paths

    monkeypatch.setattr(paths, "shared_checkout_root", lambda _: tmp_path)
    packages = [_package(tmp_path / name) for name in ("first", "second")]
    (packages[1] / "tests/test.sh").write_text("#!/bin/bash\nfalse\n")
    manifest = tmp_path / "selected.csv"
    for package in packages:
        digest = cf._digest(package)
        row = {
            **dict.fromkeys(MANIFEST_COLUMNS, ""),
            "task_id": TASK.rsplit("/", 1)[-1],
            "package_path": str(package.relative_to(tmp_path)),
            "final_digest": digest,
            "status": "built",
        }
        write_manifest([row], manifest)
        selected = cf.v6_grader(TASK, tmp_path, manifest=manifest)
        assert selected is not None
        assert selected.package == package
        assert selected.variant_digest == digest
    (packages[1] / "tests/test.sh").write_text("#!/bin/bash\necho unexpected mutation\n")
    with pytest.raises(ValueError, match="grader package digest mismatch"):
        cf.v6_grader(TASK, tmp_path, manifest=manifest)


def _selection_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, same_verifier: bool = False
) -> SimpleNamespace:
    """Real lineage/packages and a local executor that consumes the generated matrix."""
    from evallab import regrade, runner, task_variants
    from evallab.mimo_clean import MANIFEST_COLUMNS, write_manifest
    from evallab.storage import paths

    workspace = tmp_path / "workspace"
    parent = _package(tmp_path / "parent")
    (parent / "task.toml").write_text(
        f'[task]\nname = "{TASK}"\n[environment]\n'
        f"workdir = {json.dumps(str(workspace))}\n"
        '[verifier]\nenvironment_mode = "separate"\n'
    )
    (parent / "environment").mkdir()
    (parent / "environment/Dockerfile").write_text("FROM scratch\n")
    accepts_pass = (
        b'#!/bin/bash\ncase "$(cat x.py)" in new|reference|reference-v2) exit 0;; '
        b"*) exit 1;; esac\n"
    )
    rejects_pass = (
        b'#!/bin/bash\ncase "$(cat x.py)" in reference|reference-v2) exit 0;; *) exit 1;; esac\n'
    )
    store, records_dir = tmp_path / "variants", tmp_path / "repairs"
    records = [
        task_variants.derive_task(
            parent,
            changes={
                "tests/test.sh": accepts_pass if index == 0 or same_verifier else rejects_pass,
                "instruction.md": f"Repair {index}: replace old with new.\n".encode(),
            },
            transform="verifier-repair@1",
            rationale="Exercise canonical physical grader selection.",
            created_by="pytest",
            repo_root=tmp_path,
            records_dir=records_dir,
            variants_root=store,
        )
        for index in range(2)
    ]
    packages = [store / record.task_slug / record.digest12 for record in records]
    record_paths = [
        records_dir / record.task_slug / f"{record.digest12}.json" for record in records
    ]
    manifest_package = tmp_path / "manifest-package-a"
    shutil.copytree(packages[0], manifest_package)
    other_record = tmp_path / "other-a-record.json"
    shutil.copyfile(record_paths[0], other_record)
    repairs = [record_paths[0], record_paths[1], record_paths[0], other_record]
    manifest = tmp_path / "selected.csv"
    manifest_row = {
        **dict.fromkeys(MANIFEST_COLUMNS, ""),
        "task_id": TASK.rsplit("/", 1)[-1],
        "chain": f"{cf.V6} > verifier-repair@1",
        "package_path": str(manifest_package.relative_to(tmp_path)),
        "final_digest": records[0].variant_digest,
        "status": "built",
    }
    write_manifest([manifest_row], manifest)
    monkeypatch.setattr(paths, "shared_checkout_root", lambda _: tmp_path)
    monkeypatch.setattr(task_variants, "default_variants_root", lambda _: store)
    monkeypatch.setattr(trace, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(trace, "counterfactual_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(regrade, "lab_harbor_problem", lambda: None)
    reference = [PATCH.replace("+new", "+reference")]
    monkeypatch.setattr(cf, "reference_patch", lambda *_: reference[0])
    calls: list[dict[str, Any]] = []
    active: set[tuple[str, str]] = set()
    ledger_lock, workspace_lock = threading.Lock(), threading.Lock()

    def execute_matrix(matrix, *, root):
        assert matrix.environment == "modal"
        package = Path(matrix.task)
        script = root / matrix.runs[0].solution
        digest = cf._digest(package)
        key = (digest, hashlib.sha256(script.read_bytes()).hexdigest())
        with ledger_lock:
            assert key not in active, "duplicate concurrent canonical execution"
            active.add(key)
        try:
            with workspace_lock:
                workspace.mkdir(exist_ok=True)
                (workspace / "x.py").write_text("old\n")
                applied = subprocess.run(
                    ["bash", str(script)], capture_output=True, text=True, cwd=workspace
                )
                checked = subprocess.run(
                    ["bash", str(package / "tests/test.sh")],
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                )
                result = {
                    "status": "ok" if applied.returncode == 0 else "infra",
                    "rewards": [float(checked.returncode == 0)] if applied.returncode == 0 else [],
                    "error": applied.stderr if applied.returncode else None,
                }
                calls.append(
                    {
                        "digest": digest,
                        "input": (workspace / "x.py").read_text().strip(),
                        "result": result,
                    }
                )
                receipt = root / "receipts" / f"{matrix.name}.json"
                receipt.parent.mkdir(exist_ok=True)
                receipt.write_text(json.dumps(result))
                return SimpleNamespace(results=[result], receipt_path=receipt)
        finally:
            with ledger_lock:
                active.remove(key)

    monkeypatch.setattr(runner, "run_matrix", execute_matrix)
    trial = _trial(tmp_path)
    subjects = [
        {"item_id": "first", "task": TASK, "trial": trial, "group": "clean", "expected": "stays"},
        {"item_id": "second", "task": TASK, "trial": trial, "group": "faulty", "expected": "flips"},
    ]
    gate = regrade.CloudSpendGate("inert local fixture only", cap_usd=1.0, usd_per_execution=0.01)

    def plan():
        return trace.plan_regrade(subjects, repairs=repairs, grader_manifest=manifest)

    def execute():
        return trace.regrade_passes(
            subjects,
            tmp_path / "out",
            repairs=repairs,
            grader_manifest=manifest,
            workers=4,
            environment="modal",
            spend_gate=gate,
            log=lambda _: None,
        )

    return SimpleNamespace(
        plan=plan,
        execute=execute,
        subjects=subjects,
        trial=trial,
        records=records,
        record_paths=record_paths,
        other_record=other_record,
        packages=packages,
        manifest_package=manifest_package,
        manifest=manifest,
        manifest_row=manifest_row,
        repairs=repairs,
        reference=reference,
        calls=calls,
        gate=gate,
    )


def test_canonical_aliases_measure_once_and_keep_current_provenance_and_population_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _selection_case(tmp_path, monkeypatch)
    before_source = cf.source_digest(case.trial)
    assert case.plan() == {
        "subjects": 2,
        "graders": 2,
        "rows_without_execution": 0,
        "control_executions": 4,
        "replay_executions": 2,
        "executions": 6,
    }
    assert case.calls == []
    rows = case.execute()
    assert len(rows) == 4  # Two population observations of each of two physical graders.
    assert len(case.calls) == case.gate.executions == 6
    assert sum(call["input"] in {"old", "reference"} for call in case.calls) == 4
    assert sum(call["input"] == "new" for call in case.calls) == 2
    assert cf.source_digest(case.trial) == before_source
    a_digest, b_digest = [record.variant_digest for record in case.records]
    for row in rows:
        digest = row["grader_identity"]["variant_digest"]
        assert row["grader_identity"]["task_name"] == TASK
        assert row["valid_grader"] is True
        assert row["controls"] == {"nop": 0.0, "reference": 1.0}
        assert row["outcome"] == ("stays" if digest == a_digest else "flips")
        assert row["regraded"] == (1.0 if digest == a_digest else 0.0)
        aliases = row["grader_source"]["aliases"]
        if digest == a_digest:
            assert len(aliases) == 4  # Manifest, repeated record, and another record reference.
            assert {alias["transform"] for alias in aliases} == {cf.V6, "verifier-repair@1"}
            assert {alias["label"] for alias in aliases} == {
                f"{cf.V6}:{a_digest[7:19]}",
                f"verifier-repair@1:{a_digest[7:19]}",
            }
            assert sum(str(case.record_paths[0]) in alias["records"] for alias in aliases) == 2
            assert any(str(case.other_record) in alias["records"] for alias in aliases)
            manifest_alias = next(alias for alias in aliases if alias["manifest"])
            assert manifest_alias["manifest"] == str(case.manifest)
            assert manifest_alias["manifest_row"] == case.manifest_row
            assert {alias["package"] for alias in aliases} == {
                str(case.manifest_package),
                str(case.packages[0]),
            }
        else:
            assert digest == b_digest
            assert [alias["records"] for alias in aliases] == [[str(case.record_paths[1])]]
    item_files = list((tmp_path / "out/items").glob("*/*.json"))
    assert len(item_files) == 4
    assert {
        json.loads(path.read_text())["grader_identity"]["variant_digest"] for path in item_files
    } == {
        a_digest,
        b_digest,
    }
    cache_files = list((tmp_path / "cache/replay-cache").glob("*/*.json"))
    assert len(cache_files) == 2
    assert {path.stem.rsplit("--", 1)[1] for path in cache_files} == {a_digest[7:], b_digest[7:]}
    assert case.plan()["executions"] == 0

    # Cached measurements have neither frozen cohort labels nor frozen provenance.
    case.subjects[0].update(group="reviewed", expected="flips")
    case.subjects[1].update(group="relabelled", expected="stays")
    new_reference = tmp_path / "new-a-record-reference.json"
    shutil.copyfile(case.record_paths[0], new_reference)
    case.repairs.append(new_reference)
    cached = case.execute()
    assert len(case.calls) == case.gate.executions == 6
    for row in cached:
        subject = next(subject for subject in case.subjects if subject["item_id"] == row["item_id"])
        assert (row["group"], row["expected"]) == (subject["group"], subject["expected"])
        if row["grader_identity"]["variant_digest"] == a_digest:
            assert len(row["grader_source"]["aliases"]) == 5
            assert any(
                str(new_reference) in alias["records"] for alias in row["grader_source"]["aliases"]
            )

    (case.trial / "verifier/agent.diff").write_text(PATCH.replace("+new", "+wrong"))
    assert (case.plan()["control_executions"], case.plan()["replay_executions"]) == (0, 2)
    changed_source = case.execute()
    assert len(case.calls) == case.gate.executions == 8
    assert all(row["outcome"] == "flips" for row in changed_source)
    assert all(row["source_digest"] != before_source for row in changed_source)

    # A reference change invalidates controls, not independent replay measurements.
    case.reference[0] = PATCH.replace("+new", "+reference-v2")
    assert (case.plan()["control_executions"], case.plan()["replay_executions"]) == (4, 0)
    assert all(row["valid_grader"] for row in case.execute())
    assert len(case.calls) == case.gate.executions == 12
    monkeypatch.setattr(cf, "ENGINE_VERSION", "qa-trace-counterfactual/test-next")
    assert (case.plan()["control_executions"], case.plan()["replay_executions"]) == (4, 2)
    case.execute()
    assert len(case.calls) == case.gate.executions == 18


def test_distinct_full_packages_with_the_same_transform_and_verifier_execute_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.regrade import verifier_identity

    case = _selection_case(tmp_path, monkeypatch, same_verifier=True)
    assert case.records[0].transform == case.records[1].transform
    assert case.records[0].variant_digest != case.records[1].variant_digest
    assert verifier_identity(case.packages[0]).digest == verifier_identity(case.packages[1]).digest
    assert (case.plan()["graders"], case.plan()["executions"]) == (2, 6)
    rows = case.execute()
    assert len(case.calls) == case.gate.executions == 6
    assert all(row["outcome"] == "stays" and row["valid_grader"] for row in rows)
    assert {row["grader_identity"]["variant_digest"] for row in rows} == {
        record.variant_digest for record in case.records
    }
    assert case.plan()["executions"] == 0


@pytest.mark.parametrize(
    "claim",
    [
        "wrong_manifest_path",
        "mutated_manifest_copy",
        "mutated_repair",
        "deleted_manifest_task",
        "deleted_repair_task",
        "missing_manifest_package",
        "missing_repair_package",
    ],
)
def test_invalid_package_claims_fail_before_controls_spend_or_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claim: str
) -> None:
    from evallab.mimo_clean import write_manifest

    case = _selection_case(tmp_path, monkeypatch)
    if claim == "wrong_manifest_path":
        write_manifest(
            [{**case.manifest_row, "package_path": str(case.packages[1].relative_to(tmp_path))}],
            case.manifest,
        )
    else:
        package = case.manifest_package if "manifest" in claim else case.packages[0]
        if claim.startswith("deleted"):
            (package / "task.toml").unlink()
        elif claim.startswith("missing"):
            shutil.rmtree(package)
        else:
            (package / "tests/test.sh").write_text("#!/bin/bash\nexit 0 # mutated\n")
    for consume in (case.plan, case.execute):
        with pytest.raises(ValueError, match=r"grader package (digest mismatch|missing)"):
            consume()
    assert case.calls == []
    assert case.gate.executions == 0
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "cache").exists()


def test_cached_measurements_do_not_hide_mutated_alias_package_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _selection_case(tmp_path, monkeypatch)
    case.execute()
    assert len(case.calls) == case.gate.executions == 6
    (case.packages[0] / "instruction.md").write_text("Unexpected mutation of the repair copy.\n")
    for consume in (case.plan, case.execute):
        with pytest.raises(ValueError, match="grader package digest mismatch"):
            consume()
    assert len(case.calls) == case.gate.executions == 6


@pytest.mark.parametrize("claim", ["package_path", "record_reference"])
def test_conflicting_resolved_claims_are_rejected_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claim: str
) -> None:
    case = _selection_case(tmp_path, monkeypatch)
    selected = cf.newer_graders(
        TASK, tmp_path, cf.repair_records(case.repairs), manifest=case.manifest
    )
    by_digest = {grader.variant_digest: grader for grader in selected}
    a, b = [by_digest[record.variant_digest] for record in case.records]
    if claim == "package_path":
        conflicting = replace(b, package=a.package)
    else:
        conflicting = replace(
            b,
            aliases=(replace(b.selections[0], records=(str(case.record_paths[0]),)),),
        )
    monkeypatch.setattr(cf, "newer_graders", lambda *_, **__: [a, conflicting])
    for consume in (case.plan, case.execute):
        with pytest.raises(ValueError, match="conflicting grader"):
            consume()
    assert case.calls == []
    assert case.gate.executions == 0
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "cache").exists()


def test_selection_provenance_order_does_not_depend_on_repair_input_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _selection_case(tmp_path, monkeypatch)
    first = cf.newer_graders(
        TASK, tmp_path, cf.repair_records(case.repairs), manifest=case.manifest
    )
    second = cf.newer_graders(
        TASK, tmp_path, cf.repair_records(list(reversed(case.repairs))), manifest=case.manifest
    )
    assert [grader.to_json() for grader in first] == [grader.to_json() for grader in second]
    assert len(first) == 2
    assert case.calls == []


def test_raw_repairs_are_grouped_only_after_actual_separate_package_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab import task_variants
    from evallab.mimo_clean import MANIFEST_COLUMNS, write_manifest
    from evallab.storage import paths

    store = tmp_path / "variants"
    monkeypatch.setattr(task_variants, "default_variants_root", lambda _: store)
    monkeypatch.setattr(paths, "shared_checkout_root", lambda _: tmp_path)
    parent = _package(tmp_path / "raw-parent", separate=False)
    (parent / "task.toml").write_text(
        'schema_version = "1.4"\n'
        f'[task]\nname = "{TASK}"\n'
        '[environment]\nworkdir = "/testbed"\ndocker_image = "fixture:never-executed"\n'
        "[verifier]\n"
    )
    (parent / "environment/setup").mkdir(parents=True)
    (parent / "environment/setup/setup.sh").write_text("#!/bin/bash\ntrue\n")
    (parent / "tests/test.patch").write_text(PATCH)
    (parent / "tests/test_command.sh").write_text("pytest x.py\n")
    record = task_variants.derive_task(
        parent,
        changes={"instruction.md": b"A raw verifier repair.\n"},
        transform="verifier-repair@1",
        rationale="Select a raw repair through a separate-verifier composition.",
        created_by="pytest",
        repo_root=tmp_path,
        records_dir=tmp_path / "raw-records",
        variants_root=store,
    )
    raw_package = store / record.task_slug / record.digest12
    record_path = tmp_path / "raw-records" / record.task_slug / f"{record.digest12}.json"
    final = cf.separate_over(raw_package, TASK, tmp_path, via=record.transform)
    assert final is not None
    assert final.variant_digest != record.variant_digest
    assert cf.is_separate(final.package)
    manifest = tmp_path / "selected-composed.csv"
    write_manifest(
        [
            {
                **dict.fromkeys(MANIFEST_COLUMNS, ""),
                "task_id": TASK.rsplit("/", 1)[-1],
                "package_path": str(final.package.relative_to(tmp_path)),
                "final_digest": final.variant_digest,
                "status": "built",
            }
        ],
        manifest,
    )
    graders = cf.newer_graders(
        TASK, tmp_path, cf.repair_records([record_path, record_path]), manifest=manifest
    )
    assert len(graders) == 1
    assert graders[0].identity == (TASK, final.variant_digest)
    assert graders[0].cache_key.endswith(final.variant_digest[7:])
    aliases = graders[0].to_json()["aliases"]
    assert len(aliases) == 3
    composed = [alias for alias in aliases if alias["source"]]
    assert len(composed) == 2
    for alias in composed:
        assert alias["label"] == final.label
        assert alias["transform"] == f"verifier-repair@1>{cf.V10}"
        assert alias["package"] == str(final.package)
        assert alias["records"] == [final.record]
        assert alias["source"]["label"] == f"verifier-repair@1:{record.digest12}"
        assert alias["source"]["variant_digest"] == record.variant_digest
        assert alias["source"]["package"] == str(raw_package)
        assert alias["source"]["records"] == [str(record_path)]
    (raw_package / "instruction.md").write_text("Mutated original repair bytes.\n")
    with pytest.raises(ValueError, match="grader package digest mismatch"):
        cf.canonical_graders(graders)


def test_removing_a_manifest_alias_reuses_measurements_but_refreshes_the_display_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.mimo_clean import write_manifest

    case = _selection_case(tmp_path, monkeypatch)
    case.execute()
    write_manifest([{**case.manifest_row, "status": "skipped"}], case.manifest)
    assert case.plan()["executions"] == 0
    rows = case.execute()
    assert len(case.calls) == case.gate.executions == 6
    assert len(rows) == 4
    for row in rows:
        assert row["transform"] == "verifier-repair@1"
        assert row["grader"].startswith("verifier-repair@1:")
        assert all(alias["manifest"] is None for alias in row["grader_source"]["aliases"])


def test_conflicting_manifest_rows_cannot_silently_choose_the_last_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.mimo_clean import write_manifest

    case = _selection_case(tmp_path, monkeypatch)
    write_manifest(
        [
            case.manifest_row,
            {
                **case.manifest_row,
                "package_path": str(case.packages[1].relative_to(tmp_path)),
                "final_digest": case.records[1].variant_digest,
            },
        ],
        case.manifest,
    )
    for consume in (case.plan, case.execute):
        with pytest.raises(ValueError, match="conflicting grader manifest rows"):
            consume()
    assert case.calls == []
    assert case.gate.executions == 0
