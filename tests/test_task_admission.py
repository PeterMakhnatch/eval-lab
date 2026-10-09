"""Tests for the fail-closed task admission gate (no Docker, no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab import task_admission as admission
from evallab.task_admission import (
    AdmissionError,
    CheatOutcome,
    ControlOutcome,
    ScanOutcome,
    StepDef,
)


def _task_dir(tmp_path: Path) -> Path:
    task = tmp_path / "task-pkg"
    task.mkdir()
    (task / "task.toml").write_text('[task]\nname = "demo/x"\n', encoding="utf-8")
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
    )
    assert result.record.verdict == "admitted"
    assert result.record.failing_steps == []
    assert result.record.failed_attacks == []
    assert len(result.record.steps) == 15  # scan + oracle + nop + 12 attacks
    assert result.record.skipped_steps == []
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
    assert len(result.record.skipped_steps) == 13


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
    )
    assert result.record.verdict == "admitted"
    assert result.record.steps[0].findings[0].severity == "critical"


def test_step_list_is_data_driven(tmp_path: Path) -> None:
    """A future mutation step lands as one registry entry plus one StepDef."""
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
    assert len(plan) == 15
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
        changes={"task.toml": b'[task]\nname = "demo/lineage"\n# probe\n'},
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


def test_oracle_without_solution_is_infra(tmp_path: Path) -> None:
    """MiMo packages ship no solution/solve.sh: oracle is unprovable, not failed."""
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
    assert result.record.verdict == "not_admitted"
    assert result.record.steps[0].status == "infra"
    assert "solution/solve.sh" in result.record.steps[0].detail
    assert calls == []  # fast pre-check: no Docker burned


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
        changes={"task.toml": b'[task]\nname = "demo/collide"\n# probe\n'},
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
