"""Tests for the fail-closed task admission gate (no Docker, no network)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from evallab import task_admission as admission
from evallab.task_admission import (
    AdmissionError,
    CheatOutcome,
    ControlOutcome,
    MutationOutcome,
    OracleReference,
    ScanOutcome,
    StepDef,
)


def _task_dir(tmp_path: Path) -> Path:
    task = tmp_path / "task-pkg"
    task.mkdir()
    (task / "task.toml").write_text(
        'artifacts = ["/testbed/out.json"]\n[task]\nname = "demo/x"\n', encoding="utf-8"
    )
    solution = task / "solution"
    solution.mkdir()
    (solution / "solve.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    return task


def _ctx_kwargs(tmp_path: Path, task: Path) -> dict:
    return {
        "jobs_dir": tmp_path / "jobs",
        "repo_root": tmp_path,
        "repo_src": tmp_path,
        "records_dir": tmp_path / "records",
        "by": "Test Operator",
    }


def _pass_scan(task: Path) -> ScanOutcome:
    return ScanOutcome(
        findings=[],
        package_digest="sha256:" + "a" * 64,
        harbor_digest="sha256:" + "b" * 64,
        command=["scan"],
    )


def _control(reward: float | None):
    def run(**kwargs) -> ControlOutcome:
        return ControlOutcome(
            job_dir=Path("/jobs/x"), returncode=0, rewards=[reward], command=["harbor", "run"]
        )

    return run


def _cheat(reward: float | None):
    def run(**kwargs) -> CheatOutcome:
        return CheatOutcome(
            job_dir=Path("/jobs/y"), returncode=0, reward=reward, verdict="v", command=["cheat"]
        )

    return run


def _mutation(
    verdict: str = "no_survivors",
    *,
    killed: int = 6,
    survived: int = 0,
    partial: int = 0,
    score: float | None = 1.0,
) -> Callable[..., MutationOutcome]:
    """A deterministic mutation seam: all killed by default."""

    def run(**kwargs) -> MutationOutcome:
        total = killed + survived + partial
        survivors = [f"m{index:02d}" for index in range(survived + partial)]
        return MutationOutcome(
            report_verdict=verdict,
            verdict_reason=f"{verdict} in test",
            mutation_score=score,
            counts={
                "total": total,
                "killed": killed,
                "survived": survived,
                "partial": partial,
                "uninformative": 0,
                "unscored": 0,
            },
            survivors=survivors,
            survivor_patches=[
                f"/jobs/mutation/inputs/{mid}/{mid}.patch" for mid in survivors
            ],
            run_dir=Path("/jobs/mutation"),
            report_path=Path("/jobs/mutation/mutation-report.json"),
            targets=["/testbed/src/fix.py"],
            task_broken=verdict == "task_broken",
            command=["evallab", "hack", "mutate"],
        )

    return run


def test_admit_all_pass_writes_digest_bound_record(tmp_path: Path) -> None:
    task = _task_dir(tmp_path)

    # oracle expects 1.0, nop expects 0.0: route per agent.
    def control(**kwargs) -> ControlOutcome:
        reward = 1.0 if kwargs["agent"] == "oracle" else 0.0
        return ControlOutcome(
            job_dir=Path("/jobs/x"), returncode=0, rewards=[reward], command=["harbor", "run"]
        )

    result = admission.admit_task(
        task,
        **_ctx_kwargs(tmp_path, task),
        scan=_pass_scan,
        control=control,
        cheat=_cheat(0.0),
        mutation=_mutation(),
    )
    assert result.record.verdict == "admitted"
    assert result.record.failing_steps == []
    assert result.record.failed_attacks == []
    assert len(result.record.steps) == 16  # scan + oracle + nop + 12 attacks + mutation
    assert result.record.steps[-1].name == "mutation"
    assert result.record.steps[-1].status == "pass"
    assert result.record_path.name == admission.record_filename(result.record.package_digest)
    assert result.record_path.is_file()
    payload = json.loads(result.record_path.read_text(encoding="utf-8"))
    assert payload["schema"] == "evallab.task_admission/v1"
    assert payload["verdict"] == "admitted"
    # Non-variant task: no lineage status update.
    assert result.variant_status_updated is False


def test_reject_stops_at_first_failure(tmp_path: Path) -> None:
    task = _task_dir(tmp_path)
    calls: list[str] = []

    def control(**kwargs) -> ControlOutcome:
        calls.append(kwargs["agent"])
        return ControlOutcome(
            job_dir=Path("/jobs/x"), returncode=0, rewards=[0.0], command=["harbor", "run"]
        )

    def cheat(**kwargs) -> CheatOutcome:
        calls.append("cheat:" + kwargs["attack"])
        return CheatOutcome(
            job_dir=Path("/jobs/y"), returncode=0, reward=0.0, verdict="clean", command=["cheat"]
        )

    result = admission.admit_task(
        task,
        **_ctx_kwargs(tmp_path, task),
        scan=_pass_scan,
        control=control,
        cheat=cheat,
    )
    assert result.record.verdict == "rejected"
    assert result.record.failing_steps == ["oracle"]
    # Early stop: nop and the ladder never ran.
    assert calls == ["oracle"]
    assert result.record.skipped_steps[0] == "nop"
    assert len(result.record.skipped_steps) == 14


def test_reject_names_cracked_attack(tmp_path: Path) -> None:
    task = _task_dir(tmp_path)

    def control(**kwargs) -> ControlOutcome:
        reward = 1.0 if kwargs["agent"] == "oracle" else 0.0
        return ControlOutcome(
            job_dir=Path("/jobs/x"), returncode=0, rewards=[reward], command=["harbor", "run"]
        )

    def cheat(**kwargs) -> CheatOutcome:
        reward = 1.0 if kwargs["attack"] == "skip_plant" else 0.0
        return CheatOutcome(
            job_dir=Path("/jobs/y"), returncode=0, reward=reward, verdict="v", command=["cheat"]
        )

    result = admission.admit_task(
        task,
        **_ctx_kwargs(tmp_path, task),
        scan=_pass_scan,
        control=control,
        cheat=cheat,
    )
    assert result.record.verdict == "rejected"
    assert result.record.failing_steps == ["cheat:skip_plant"]
    assert result.record.failed_attacks == ["skip_plant"]


def test_infra_is_not_admitted_not_rejected(tmp_path: Path) -> None:
    task = _task_dir(tmp_path)

    def control(**kwargs) -> ControlOutcome:
        raise RuntimeError("harbor daemon away")

    result = admission.admit_task(
        task,
        **_ctx_kwargs(tmp_path, task),
        scan=_pass_scan,
        control=control,
        cheat=_cheat(0.0),
    )
    assert result.record.verdict == "not_admitted"
    assert result.record.failing_steps == ["oracle"]
    assert result.record.failed_attacks == []
    # The record still lands: fail closed with evidence, not silence.
    assert result.record_path.is_file()


def test_scan_findings_recorded_without_veto(tmp_path: Path) -> None:
    task = _task_dir(tmp_path)

    def scan(task_dir: Path) -> ScanOutcome:
        return ScanOutcome(
            findings=[
                {
                    "flaw_class": "V3",
                    "severity": "critical",
                    "title": "verifier executes data",
                    "evidence": "tests/test.sh:35",
                }
            ],
            package_digest="sha256:" + "c" * 64,
            command=["scan"],
        )

    def control(**kwargs) -> ControlOutcome:
        reward = 1.0 if kwargs["agent"] == "oracle" else 0.0
        return ControlOutcome(
            job_dir=Path("/jobs/x"), returncode=0, rewards=[reward], command=["harbor", "run"]
        )

    result = admission.admit_task(
        task,
        **_ctx_kwargs(tmp_path, task),
        scan=scan,
        control=control,
        cheat=_cheat(0.0),
        mutation=_mutation(),
    )
    assert result.record.verdict == "admitted"
    assert result.record.steps[0].findings[0].severity == "critical"


def test_step_list_is_data_driven(tmp_path: Path) -> None:
    """A new step kind lands as one registry entry plus one StepDef."""
    assert "mutation" in admission.STEP_RUNNERS
    assert admission.default_steps()[-1] == StepDef(name="mutation", kind="mutation")
    task = _task_dir(tmp_path)
    seen: list[str] = []

    def probe_runner(step: StepDef, ctx) -> admission.AdmissionStepRecord:
        seen.append(step.name)
        return admission.AdmissionStepRecord(
            name=step.name,
            kind=step.kind,
            command=["probe"],
            duration_s=0.0,
            status="pass",
            detail="probe ok",
        )

    steps = [
        StepDef(name="static-scan", kind="static-scan"),
        StepDef(name="mutation:demo", kind="mutation"),
    ]
    runners = dict(admission.STEP_RUNNERS) | {"mutation": probe_runner}
    result = admission.admit_task(
        task,
        **_ctx_kwargs(tmp_path, task),
        steps=steps,
        step_runners=runners,
        scan=_pass_scan,
    )
    assert result.record.verdict == "admitted"
    assert seen == ["mutation:demo"]
    assert [row.name for row in result.record.steps] == ["static-scan", "mutation:demo"]


def test_unknown_step_kind_is_infra(tmp_path: Path) -> None:
    task = _task_dir(tmp_path)
    result = admission.admit_task(
        task,
        **_ctx_kwargs(tmp_path, task),
        steps=[StepDef(name="mystery", kind="unregistered")],
        scan=_pass_scan,
    )
    assert result.record.verdict == "not_admitted"
    assert result.record.failing_steps == ["mystery"]


def test_missing_task_toml_raises_before_record(tmp_path: Path) -> None:
    with pytest.raises(AdmissionError):
        admission.admit_task(tmp_path / "nope", **_ctx_kwargs(tmp_path, tmp_path))


def test_plan_lists_every_step_without_running(tmp_path: Path) -> None:
    plan = admission.plan_admission()
    assert [entry["name"] for entry in plan[:3]] == ["static-scan", "oracle", "nop"]
    assert plan[-1]["name"] == "mutation"
    assert len(plan) == 16
    assert all(entry["command"] for entry in plan)


def test_variant_status_updated_for_lineage_variant(tmp_path: Path) -> None:
    from evallab.task_variants import derive_task, resolve_record

    parent = tmp_path / "parent-pkg"
    parent.mkdir()
    (parent / "task.toml").write_text('[task]\nname = "demo/lineage"\n', encoding="utf-8")
    (parent / "solution").mkdir()
    (parent / "solution" / "solve.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    records = Path("records")
    store = tmp_path / "store"
    record = derive_task(
        parent,
        changes={
            "task.toml": b'artifacts = ["/testbed/out.json"]\n[task]\nname = "demo/lineage"\n# probe\n'
        },
        transform="probe@1",
        rationale="admission test",
        created_by="Test Operator",
        repo_root=tmp_path,
        records_dir=records,
        variants_root=store,
    )
    assert record.status == "candidate"

    from evallab.task_variants import materialize

    package = materialize(
        record, parent, repo_root=tmp_path, records_dir=records, variants_root=store
    )

    def control(**kwargs) -> ControlOutcome:
        reward = 1.0 if kwargs["agent"] == "oracle" else 0.0
        return ControlOutcome(
            job_dir=Path("/jobs/x"), returncode=0, rewards=[reward], command=["harbor", "run"]
        )

    result = admission.admit_task(
        package,
        jobs_dir=tmp_path / "jobs",
        repo_root=tmp_path,
        repo_src=tmp_path,
        records_dir=records,
        by="Test Operator",
        scan=_pass_scan,
        control=control,
        cheat=_cheat(0.0),
        mutation=_mutation(),
    )
    assert result.record.verdict == "admitted"
    assert result.variant_status_updated is True
    assert result.variant_record == record.record_relpath().as_posix()
    updated = resolve_record(
        tmp_path / records / record.task_slug / f"{record.digest12}.json",
        repo_root=tmp_path,
        records_dir=records,
    )
    assert updated.status == "validated"
    assert "admission admitted" in updated.evidence[-1].evidence
    persisted = json.loads(result.record_path.read_text(encoding="utf-8"))
    assert persisted["variant_record"] == record.record_relpath().as_posix()


def test_oracle_without_reference_is_unproven(tmp_path: Path) -> None:
    """No shipped solution and no sweep reference: unproven, not infra."""
    task = tmp_path / "nosolution-pkg"
    task.mkdir()
    (task / "task.toml").write_text('[task]\nname = "demo/nosolution"\n', encoding="utf-8")
    calls: list[str] = []

    def control(**kwargs) -> ControlOutcome:
        calls.append(kwargs["agent"])
        return _control(1.0)(**kwargs)

    result = admission.admit_task(
        task,
        **_ctx_kwargs(tmp_path, task),
        steps=[StepDef(name="oracle", kind="control", agent="oracle", expect=1.0)],
        scan=_pass_scan,
        control=control,
        cheat=_cheat(0.0),
    )
    assert result.record.verdict == "unproven"
    assert result.record.steps[0].status == "unproven"
    assert result.record.unproven_steps == ["oracle"]
    assert "no proven oracle reference" in result.record.steps[0].detail
    assert calls == []  # no reference, no Docker burned
    assert result.variant_status_updated is False


def test_all_none_rewards_is_infra(tmp_path: Path) -> None:
    """Trials ran but no reward was extractable: no proof either way."""
    task = _task_dir(tmp_path)

    def control(**kwargs) -> ControlOutcome:
        return ControlOutcome(
            job_dir=tmp_path / "jobs" / "x", returncode=0, rewards=[None], command=["harbor", "run"]
        )

    result = admission.admit_task(
        task,
        **_ctx_kwargs(tmp_path, task),
        steps=[StepDef(name="oracle", kind="control", agent="oracle", expect=1.0)],
        scan=_pass_scan,
        control=control,
        cheat=_cheat(0.0),
    )
    assert result.record.verdict == "not_admitted"
    assert result.record.steps[0].status == "infra"


def test_final_status_collision_skips_update(tmp_path: Path) -> None:
    """A rejected gate never reflips an already-validated lineage record."""
    from evallab.task_variants import derive_task, resolve_record

    parent = tmp_path / "parent-pkg"
    parent.mkdir()
    (parent / "task.toml").write_text('[task]\nname = "demo/collide"\n', encoding="utf-8")
    (parent / "solution").mkdir()
    (parent / "solution" / "solve.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    records = Path("records")
    store = tmp_path / "store"
    record = derive_task(
        parent,
        changes={
            "task.toml": b'artifacts = ["/testbed/out.json"]\n[task]\nname = "demo/collide"\n# probe\n'
        },
        transform="probe@1",
        rationale="admission test",
        created_by="Test Operator",
        repo_root=tmp_path,
        records_dir=records,
        variants_root=store,
    )
    from evallab.task_variants import materialize

    package = materialize(
        record, parent, repo_root=tmp_path, records_dir=records, variants_root=store
    )

    def control(**kwargs) -> ControlOutcome:
        reward = 1.0 if kwargs["agent"] == "oracle" else 0.0
        return ControlOutcome(
            job_dir=Path("/jobs/x"), returncode=0, rewards=[reward], command=["harbor", "run"]
        )

    kwargs = {
        "jobs_dir": tmp_path / "jobs",
        "repo_root": tmp_path,
        "repo_src": tmp_path,
        "records_dir": records,
        "by": "Test Operator",
        "scan": _pass_scan,
        "control": control,
        "mutation": _mutation(),
    }
    first = admission.admit_task(package, cheat=_cheat(0.0), **kwargs)
    assert first.record.verdict == "admitted"
    assert first.variant_status_updated is True

    second = admission.admit_task(package, cheat=_cheat(1.0), **kwargs)
    assert second.record.verdict == "rejected"
    assert second.variant_status_updated is False
    assert second.variant_status_note is not None
    assert "already 'validated'" in second.variant_status_note
    updated = resolve_record(
        tmp_path / records / record.task_slug / f"{record.digest12}.json",
        repo_root=tmp_path,
        records_dir=records,
    )
    assert updated.status == "validated"


def _sweep_row(task_id: str, label: str, receipt: Path) -> str:
    return (
        "task_id,label,fix_commit,patch_tip,how_chosen,evidence_path,run_digest\n"
        f"{task_id},{label},abc123,,”S1”,{receipt},sha256:{'d' * 64}\n"
    )


def _receipt(
    path: Path, patch: Path, *, task_id: str = "demo/ref", oracle: float = 1.0, nop: float = 0.0
) -> Path:
    out = path / "receipt.json"
    out.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "run_digest": "sha256:" + "d" * 64,
                "extraction": {
                    "status": "ok",
                    "solution_patch_path": str(patch),
                    "solution_patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
                    "fix": {"sha": "abc123"},
                },
                "arms": {"oracle": {"reward": oracle}, "nop": {"reward": nop}},
            }
        ),
        encoding="utf-8",
    )
    return out


def _ref_task(tmp_path: Path) -> Path:
    task = tmp_path / "ref-pkg"
    task.mkdir()
    (task / "task.toml").write_text(
        '[task]\nname = "demo/ref"\n[environment]\nworkdir = "/testbed"\n',
        encoding="utf-8",
    )
    return task


def test_resolve_reference_auto_hit(tmp_path: Path) -> None:
    patch = tmp_path / "ref.patch"
    patch.write_bytes(b"diff --git a/f.py b/f.py\n")
    receipt = _receipt(tmp_path, patch)
    sweep = tmp_path / "sweep.csv"
    sweep.write_text(_sweep_row("demo/ref", "oracle:pass+nop:fail", receipt), encoding="utf-8")
    ref = admission.resolve_oracle_reference(
        tmp_path / "ref-pkg", "demo/ref", sweep_csv=sweep, reference_arg="auto"
    )
    assert ref is not None
    assert ref.fix_commit == "abc123"
    assert ref.patch_bytes == b"diff --git a/f.py b/f.py\n"
    assert ref.recorded_oracle_reward == 1.0


def test_resolve_reference_wrong_label_is_none(tmp_path: Path) -> None:
    patch = tmp_path / "ref.patch"
    patch.write_bytes(b"diff\n")
    receipt = _receipt(tmp_path, patch)
    sweep = tmp_path / "sweep.csv"
    sweep.write_text(_sweep_row("demo/ref", "oracle:fail", receipt), encoding="utf-8")
    assert (
        admission.resolve_oracle_reference(
            tmp_path / "ref-pkg", "demo/ref", sweep_csv=sweep, reference_arg="auto"
        )
        is None
    )


def test_resolve_reference_bad_recorded_rewards_is_unproven(tmp_path: Path) -> None:
    patch = tmp_path / "ref.patch"
    patch.write_bytes(b"diff\n")
    receipt = _receipt(tmp_path, patch, oracle=0.0)
    sweep = tmp_path / "sweep.csv"
    sweep.write_text(_sweep_row("demo/ref", "oracle:pass+nop:fail", receipt), encoding="utf-8")
    with pytest.raises(admission.ReferenceUnproven):
        admission.resolve_oracle_reference(
            tmp_path / "ref-pkg", "demo/ref", sweep_csv=sweep, reference_arg="auto"
        )


def test_resolve_reference_explicit_patch(tmp_path: Path) -> None:
    patch = tmp_path / "explicit.patch"
    patch.write_bytes(b"diff --git a/g.py b/g.py\n")
    ref = admission.resolve_oracle_reference(
        tmp_path / "ref-pkg", "demo/ref", sweep_csv=None, reference_arg=str(patch)
    )
    assert ref is not None
    assert ref.label == "explicit"
    assert ref.recorded_oracle_reward is None


def test_render_reference_solve_sh_applies_patch() -> None:
    solve = admission.render_reference_solve_sh(
        "/testbed", b"diff --git a/f.py b/f.py\n", "sha256:" + "e" * 64
    )
    text = solve.decode("utf-8")
    assert text.startswith("#!/bin/bash")
    assert 'cd "$CWD"' in text and 'CWD="/testbed"' in text
    assert "git apply" in text and "diff --git a/f.py b/f.py" in text
    assert "ADMISSION_REF_eeeeeeeeeeee" in text


def test_oracle_reference_step_plants_and_grades(tmp_path: Path) -> None:
    task = _ref_task(tmp_path)
    seen: dict[str, object] = {}
    patch = (
        b"diff --git a/numpyro/distributions/batch_util.py "
        b"b/numpyro/distributions/batch_util.py\n"
        b"--- a/numpyro/distributions/batch_util.py\n"
        b"+++ b/numpyro/distributions/batch_util.py\n"
    )

    def reference(task_dir: Path, task_id: str, **_: object) -> OracleReference | None:
        assert task_id == "ref"  # bare id after the namespace split
        return OracleReference(
            task_id=task_id,
            label="oracle:pass+nop:fail",
            fix_commit="abc123",
            receipt_path="receipt.json",
            receipt_run_digest="",
            patch_sha256="sha256:" + "f" * 64,
            patch_bytes=patch,
            recorded_oracle_reward=1.0,
            recorded_nop_reward=0.0,
        )

    def control(**kwargs: object) -> ControlOutcome:
        agent = kwargs.get("agent")
        if agent == "oracle":
            staged = kwargs["task_dir"]
            assert isinstance(staged, Path)
            solve = staged / "solution" / "solve.sh"
            seen["solve"] = solve.read_text(encoding="utf-8")
            assert "batch_util" in seen["solve"]
            seen["staged"] = staged
            rewards = [1.0]
        else:
            rewards = [0.0]
        return ControlOutcome(
            job_dir=tmp_path / "jobs" / "x",
            returncode=0,
            rewards=rewards,
            command=["harbor", "run"],
        )

    def mutation(**kwargs: object) -> MutationOutcome:
        seen["mutation_package"] = kwargs["package"]
        seen["mutation_targets"] = kwargs["targets"]
        return _mutation()(**kwargs)

    result = admission.admit_task(
        task,
        **_ctx_kwargs(tmp_path, task),
        steps=[step for step in admission.default_steps() if step.kind != "cheat"],
        scan=_pass_scan,
        control=control,
        cheat=_cheat(0.0),
        mutation=mutation,
        reference=reference,
    )
    assert result.record.steps[1].status == "pass"
    assert "patch=sha256:" + "f" * 64 in result.record.steps[1].reference
    # The mutation step runs against the same staged package the oracle used,
    # targeting the source files the reference patch changes as container paths.
    assert seen["mutation_package"] == seen["staged"]
    assert seen["mutation_targets"] == ["/testbed/numpyro/distributions/batch_util.py"]
    assert result.record.steps[-1].name == "mutation"
    assert result.record.steps[-1].status == "pass"
    assert result.record.verdict == "admitted"


def test_cli_admit_dry_run_uses_every_flag(tmp_path: Path, capsys) -> None:
    """The parser and the command stay in sync (missing flags fail fast here)."""
    import argparse

    from evallab import cli as cli_module

    task = _ref_task(tmp_path)
    args = argparse.Namespace(
        task=task,
        job_prefix="admit",
        jobs_dir=None,
        records_dir=Path("library/task-variants"),
        by="Test Operator",
        repeat=3,
        timeout_seconds=600,
        max_mutants=6,
        skip_mutation=False,
        output=None,
        reference="auto",
        sweep_csv=None,
        no_variant_status=False,
        dry_run=True,
        json=True,
    )
    code = cli_module._tasks_admit_command(args, tmp_path, harbor=None)
    assert code == 0
    out = capsys.readouterr().out
    assert "static-scan" in out and "cheat:skip_plant" in out
    assert "mutation" in out


def test_safe_job_name_harbor_rules() -> None:
    assert admission._safe_job_name("admit2", "format-code-task-002402", "git_history") == (
        "admit2-format-code-task-002402-git-history"
    )
    assert admission._safe_job_name("A", "b__c", "D") == "a-b-c-d"


def _all_pass_kwargs(tmp_path: Path) -> dict:
    def control(**kwargs) -> ControlOutcome:
        reward = 1.0 if kwargs["agent"] == "oracle" else 0.0
        return ControlOutcome(
            job_dir=Path("/jobs/x"), returncode=0, rewards=[reward], command=["harbor", "run"]
        )

    return {"scan": _pass_scan, "control": control, "cheat": _cheat(0.0)}


def test_mutation_survivors_route_to_review(tmp_path: Path) -> None:
    """A surviving mutant is a review hypothesis, never an auto-reject."""
    task = _task_dir(tmp_path)
    result = admission.admit_task(
        task,
        **_ctx_kwargs(tmp_path, task),
        **_all_pass_kwargs(tmp_path),
        mutation=_mutation(survived=1, partial=1, killed=4, score=0.67),
    )
    assert result.record.verdict == "needs_review"
    assert result.record.review_steps == ["mutation"]
    assert result.record.failing_steps == []
    assert result.record.failed_attacks == []
    assert result.record.survivor_patches == [
        "/jobs/mutation/inputs/m00/m00.patch",
        "/jobs/mutation/inputs/m01/m01.patch",
    ]
    assert result.record.steps[-1].status == "review"
    assert "m00" in result.record.steps[-1].detail
    assert result.variant_status_updated is False


def test_mutation_task_broken_rejects(tmp_path: Path) -> None:
    """Capture controls mis-scoring the staged package is an objective failure."""
    task = _task_dir(tmp_path)
    result = admission.admit_task(
        task,
        **_ctx_kwargs(tmp_path, task),
        **_all_pass_kwargs(tmp_path),
        mutation=_mutation(verdict="task_broken", killed=0, score=None),
    )
    assert result.record.verdict == "rejected"
    assert result.record.failing_steps == ["mutation"]
    assert result.record.steps[-1].status == "fail"


def test_mutation_uninformative_blocks_admission(tmp_path: Path) -> None:
    """Nothing informative is fail-closed: recorded plainly, admission blocked."""
    task = _task_dir(tmp_path)
    result = admission.admit_task(
        task,
        **_ctx_kwargs(tmp_path, task),
        **_all_pass_kwargs(tmp_path),
        mutation=_mutation(verdict="no_mutants", killed=0, score=None),
    )
    assert result.record.verdict == "not_admitted"
    assert result.record.failing_steps == ["mutation"]
    assert "blocks admission" in result.record.steps[-1].detail


def test_mutation_broken_is_infra(tmp_path: Path) -> None:
    """A mutation harness failure is infra, never a silent pass."""

    def broken(**kwargs) -> MutationOutcome:
        raise RuntimeError("docker daemon away")

    task = _task_dir(tmp_path)
    result = admission.admit_task(
        task,
        **_ctx_kwargs(tmp_path, task),
        **_all_pass_kwargs(tmp_path),
        mutation=broken,
    )
    assert result.record.verdict == "not_admitted"
    assert result.record.steps[-1].status == "infra"


def test_skip_mutation_records_the_skip(tmp_path: Path) -> None:
    """--skip-mutation admits without running the probe and records the skip."""
    calls: list[str] = []

    def spy(**kwargs) -> MutationOutcome:
        calls.append("mutation")
        return _mutation()(**kwargs)

    task = _task_dir(tmp_path)
    result = admission.admit_task(
        task,
        **_ctx_kwargs(tmp_path, task),
        **_all_pass_kwargs(tmp_path),
        mutation=spy,
        skip_mutation=True,
    )
    assert result.record.verdict == "admitted"
    assert calls == []
    assert result.record.mutation_skipped is True
    assert result.record.skipped_steps == ["mutation"]
    assert [row.name for row in result.record.steps][-1] != "mutation"


def test_mutation_without_targets_blocks_admission(tmp_path: Path) -> None:
    """Shipped solve.sh plus no declared artifacts: no probe possible, no admission."""
    task = tmp_path / "bare-pkg"
    task.mkdir()
    (task / "task.toml").write_text('[task]\nname = "demo/bare"\n', encoding="utf-8")
    (task / "solution").mkdir()
    (task / "solution" / "solve.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    calls: list[str] = []

    def spy(**kwargs) -> MutationOutcome:
        calls.append("mutation")
        return _mutation()(**kwargs)

    result = admission.admit_task(
        task,
        **_ctx_kwargs(tmp_path, task),
        **_all_pass_kwargs(tmp_path),
        mutation=spy,
    )
    assert result.record.verdict == "not_admitted"
    assert result.record.steps[-1].name == "mutation"
    assert result.record.steps[-1].status == "infra"
    assert calls == []  # no targets, no Docker burned


def test_needs_review_leaves_lineage_untouched(tmp_path: Path) -> None:
    """A survivor hypothesis never flips a lineage record."""
    from evallab.task_variants import derive_task, resolve_record

    parent = tmp_path / "parent-pkg"
    parent.mkdir()
    (parent / "task.toml").write_text(
        'artifacts = ["/testbed/out.json"]\n[task]\nname = "demo/review-lineage"\n',
        encoding="utf-8",
    )
    (parent / "solution").mkdir()
    (parent / "solution" / "solve.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    records = Path("records")
    store = tmp_path / "store"
    record = derive_task(
        parent,
        changes={
            "task.toml": b'artifacts = ["/testbed/out.json"]\n[task]\nname = "demo/review-lineage"\n# probe\n'
        },
        transform="probe@1",
        rationale="admission test",
        created_by="Test Operator",
        repo_root=tmp_path,
        records_dir=records,
        variants_root=store,
    )
    assert record.status == "candidate"

    from evallab.task_variants import materialize

    package = materialize(
        record, parent, repo_root=tmp_path, records_dir=records, variants_root=store
    )
    result = admission.admit_task(
        package,
        jobs_dir=tmp_path / "jobs",
        repo_root=tmp_path,
        repo_src=tmp_path,
        records_dir=records,
        by="Test Operator",
        **_all_pass_kwargs(tmp_path),
        mutation=_mutation(survived=1, killed=5, score=0.83),
    )
    assert result.record.verdict == "needs_review"
    assert result.variant_status_updated is False
    assert result.variant_status_note is None
    assert result.record.survivor_patches == ["/jobs/mutation/inputs/m00/m00.patch"]
    updated = resolve_record(
        tmp_path / records / record.task_slug / f"{record.digest12}.json",
        repo_root=tmp_path,
        records_dir=records,
    )
    assert updated.status == "candidate"


def test_patch_container_targets() -> None:
    patch = (
        b"diff --git a/src/fix.py b/src/fix.py\n"
        b"--- a/src/fix.py\n"
        b"+++ b/src/fix.py\n"
        b"diff --git a/old.py b/gone.py\n"
        b"--- a/old.py\n"
        b"+++ /dev/null\n"
    )
    assert admission._patch_container_targets(patch, "/testbed") == ["/testbed/src/fix.py"]
    assert admission._patch_container_targets(b"no diff here\n", "/testbed") == []


def test_admit_parser_mutation_flags_match_gate_default() -> None:
    """--max-mutants defaults to the gate constant (single source of intent)."""
    from evallab import cli as cli_module

    args = cli_module.parser().parse_args(["tasks", "admit", "--task", "pkg"])
    assert args.max_mutants == admission.DEFAULT_MAX_MUTANTS
    assert args.skip_mutation is False
