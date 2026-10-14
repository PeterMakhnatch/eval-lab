from pathlib import Path

import pytest

from evallab.verifier_hunt import HuntTask, rank_report, select_tasks
from evallab.verifier_mutation import Mutant, MutationReport, MutationRun


def _task(name: str, stratum: str, disposition: str = "eligible") -> HuntTask:
    return HuntTask(
        name,
        "/package",
        "sha256:" + "0" * 64,
        "a" * 64,
        "python",
        ("/app/a.py",),
        stratum,
        disposition,
    )


def test_stratification_covers_sparse_groups_and_respects_explicit_seeds() -> None:
    tasks = [_task(f"task-{i}", "single:small") for i in range(15)]
    tasks += [_task("wide", "multi:large"), _task("medium", "single:medium")]
    tasks += [_task("unsupported", "multi:large", "unsupported-coverage-language")]
    selected = select_tasks(tasks, 4, seed="fixed", first=("task-4",))
    assert selected[0].task_id == "task-4"
    assert {task.task_id for task in selected}.issuperset({"wide", "medium"})
    assert "unsupported" not in {task.task_id for task in selected}
    assert select_tasks(list(reversed(tasks)), 4, seed="fixed", first=("task-4",)) == selected


def _report() -> MutationReport:
    report = MutationReport(
        package="/package",
        package_digest="sha256:" + "0" * 64,
        task="task",
        verifier_mode="separate",
        targets=["/app/a.py"],
        run_dir="/reports/task",
        limits="test",
        executed=True,
        coverage="guided",
        verdict="survivors_found",
    )
    report.runs = [
        MutationRun(
            name="oracle",
            role="capture-oracle",
            script="oracle.sh",
            script_sha256="sha256:" + "0" * 64,
            status="ok",
            reward=1.0,
            outcome="control",
        ),
        MutationRun(
            name="base",
            role="capture-base",
            script="base.sh",
            script_sha256="sha256:" + "0" * 64,
            status="ok",
            reward=0.0,
            outcome="control",
        ),
    ]
    for id_, executed, outcome in (
        ("hit", True, "survived"),
        ("dead", False, "survived"),
        ("unknown", None, "survived"),
        ("blank", True, "uninformative"),
        ("killed", True, "killed"),
    ):
        report.mutants.append(
            Mutant(
                id=id_,
                file="/app/a.py",
                language="python",
                operator="integer-shift",
                line=1,
                original="1",
                replacement="2",
                changed_line=True,
                patch="patch",
                mutated_sha256="a" * 64,
                executed=executed,
            )
        )
        report.runs.append(
            MutationRun(
                name=id_,
                role="mutant",
                script=id_ + ".sh",
                script_sha256="sha256:" + "0" * 64,
                mutant=id_,
                status="ok",
                reward=0.0 if outcome == "killed" else 1.0,
                outcome=outcome,
            )
        )
    return report


def test_ranking_excludes_dead_unknown_and_uninformative_survivors() -> None:
    row = rank_report(_report())
    assert row["executed_survivors"] == ["hit"]
    assert row["scored_executed"] == 2
    assert row["rankable"] is True


@pytest.mark.parametrize("failure", ["coverage", "oracle", "nop", "infra", "planned"])
def test_untrusted_measurements_do_not_become_zero_survivor_rankings(failure: str) -> None:
    report = _report()
    if failure == "coverage":
        report.coverage = "unavailable"
    elif failure == "oracle":
        report.runs[0].reward = 0.0
    elif failure == "nop":
        report.runs[1].reward = 1.0
    elif failure == "infra":
        report.runs[0].status = "infra"
    else:
        report.executed = False
    row = rank_report(report)
    assert row["rankable"] is False
    assert row["survived"] is None
    assert row["scored_executed"] is None


def test_duplicate_manifest_task_is_rejected(tmp_path: Path) -> None:
    from evallab.verifier_hunt import inventory

    manifest = tmp_path / "manifest.csv"
    manifest.write_text(
        "task_id,reference_fix,package_path,status,language,final_digest\n"
        + "a,/patch,p,built,go,sha256:0\n" * 2
    )
    with pytest.raises(ValueError, match="duplicate manifest task"):
        inventory(manifest, tmp_path)


def test_no_informative_mutants_is_not_a_clean_task_result() -> None:
    report = _report()
    report.runs = [run for run in report.runs if run.role != "mutant"]
    report.verdict = "no_mutants"
    row = rank_report(report)
    assert row["rankable"] is False
    assert row["survived"] is None
