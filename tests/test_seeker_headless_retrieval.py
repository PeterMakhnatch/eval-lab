"""Sealed DEV-only prompt admission and retrieval-only treatment boundaries."""

from dataclasses import replace
from pathlib import Path

import pytest

from evallab.production import seeker_worker
from evallab.production.seeker_worker import (
    ResolvedSeekerTask,
    SeekerFailureMemory,
    SeekerRefusalError,
    build_headless_seeker_prompt,
)


def _task(workspace: Path) -> ResolvedSeekerTask:
    task_dir = workspace / "task"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text('[task]\nname = "synthetic-dev"\n')
    (task_dir / "instruction.md").write_text("Write a computed checksum.\n")
    return ResolvedSeekerTask(
        run_id="synthetic-run",
        job_type="reward_seeker.seek_task",
        dataset_id="synthetic-dev",
        item_id="synthetic-dev",
        task_id="synthetic-dev",
        split="dev",
        measurable=True,
        original_task=task_dir,
        repaired_task=task_dir,
        repaired_grader_kind="package",
    )


def test_sealed_dev_prompt_adds_only_bounded_retrieval_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _task(tmp_path)

    def mixed_source_guard(_text: str, **_kwargs: object) -> list[str]:
        raise AssertionError("sealed DEV prompts must not open mixed-split guard sources")

    monkeypatch.setattr(seeker_worker, "find_tb4_test_leaks", mixed_source_guard)
    options = {
        "attempt_index": 1,
        "selected_cue": None,
        "cues": [],
        "memory": SeekerFailureMemory(),
        "workspace": tmp_path,
        "dev_task_allowlist": frozenset({task.task_id}),
    }
    baseline = build_headless_seeker_prompt(task, **options)
    evidence = "## Related verified DEV evidence\n- other-task: checksum validation was missing."
    treatment = build_headless_seeker_prompt(task, retrieval_markdown=evidence, **options)
    assert treatment == baseline + "\n\n" + evidence
    with pytest.raises(SeekerRefusalError, match="4500-character"):
        build_headless_seeker_prompt(task, retrieval_markdown="x" * 4501, **options)


@pytest.mark.parametrize("split,task_id", [("test", "synthetic-dev"), ("dev", "unlisted")])
def test_sealed_dev_prompt_refuses_outside_allowlist_before_task_reads(
    tmp_path: Path, split: str, task_id: str
) -> None:
    task = replace(_task(tmp_path), split=split, task_id=task_id)
    with pytest.raises(SeekerRefusalError, match="sealed DEV-only"):
        build_headless_seeker_prompt(
            task,
            attempt_index=1,
            selected_cue=None,
            cues=[],
            memory=SeekerFailureMemory(),
            workspace=tmp_path / "missing-workspace",
            dev_task_allowlist=frozenset({"synthetic-dev"}),
        )


def test_headless_generator_refuses_unlisted_task_before_retrieval(tmp_path: Path) -> None:
    from evallab.production.seeker_worker import OmpHeadlessAttemptGenerator

    class ForbiddenRetriever:
        def retrieve(self, **_kwargs: object) -> None:
            raise AssertionError("retrieval must follow DEV task admission")

    generator = OmpHeadlessAttemptGenerator(
        models=("unused/model",),
        retriever=ForbiddenRetriever(),  # type: ignore[arg-type]
        dev_task_allowlist=frozenset({"another-dev-task"}),
    )
    with pytest.raises(SeekerRefusalError, match="sealed DEV-only"):
        generator(_task(tmp_path), 1, None, [], SeekerFailureMemory(), tmp_path)
