"""Sealed DEV-only prompt admission and retrieval-only treatment boundaries."""

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from evallab.production import seeker_worker
from evallab.production.cloud_trace_retriever import RetrievedTraceHit, TraceRetrievalResult
from evallab.production.seeker_error_first import RunnerError, render_feedback_block
from evallab.production.seeker_worker import (
    FailedShortcutRecord,
    HackbookTaskCue,
    OmpHeadlessAttemptGenerator,
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


def _cue() -> HackbookTaskCue:
    return HackbookTaskCue(
        entry_id="HB-SYNTHETIC",
        slug="checksum",
        title="Synthetic cue marker",
        taxonomy_class_id="TC-1",
        taxonomy_class="weak_check",
        score=1.0,
        mechanism="Checksum verification",
        goal="Check the checksum",
    )


def _memory() -> SeekerFailureMemory:
    return SeekerFailureMemory(
        [
            FailedShortcutRecord(
                attempt_index=1,
                shortcut_signature="synthetic-failed-signature",
                candidate_digest="synthetic-failed-digest",
                hackbook_entry_id="HB-OLD",
                taxonomy_class="weak_check",
                mechanism="Earlier checksum hypothesis",
                target_check="checksum",
                files_written=("/app/checksum.txt",),
                failure_kind="repaired_grader_also_passed",
                failure_reason="REPAIRED_RESULT_MARKER reward=0.375",
                failed_tests=("REPAIRED_RESULT_MARKER",),
                excerpts=(("REPAIRED_RESULT_MARKER", "REPAIRED_RESULT_MARKER"),),
                original_reward=1.0,
                repaired_reward=0.375,
            )
        ]
    )


def _runner_feedback(attempt_index: int = 1) -> str:
    return render_feedback_block(
        RunnerError(
            status="measured",
            reward=0.0,
            error="RUNNER_HARNESS_MARKER tests/test_private.py",
            sandbox_exit_codes=(7, 9),
            failed_tests=("tests/test_private.py::test_checksum",),
            excerpts=(),
            verifier_output="RUNNER_OUTPUT_MARKER\nFAILED tests/test_private.py::test_checksum",
            verifier_files=("test-stdout.txt",),
        ),
        attempt_index=attempt_index,
    )


@pytest.mark.parametrize("attempt_index", [2, 3])
@pytest.mark.parametrize("hide_tests", [False, True])
@pytest.mark.parametrize("prompt_mode", ["qa_review", "bare_task"])
def test_retry_prompt_starts_with_real_runner_then_signatures_before_cues_and_retrieval(
    tmp_path: Path, attempt_index: int, hide_tests: bool, prompt_mode: str
) -> None:
    task, cue, memory = _task(tmp_path), _cue(), _memory()
    (tmp_path / "feedback.md").write_text(
        _runner_feedback(attempt_index - 1) + "\n\n" + memory.render_markdown()
    )
    prompt = build_headless_seeker_prompt(
        task,
        attempt_index=attempt_index,
        selected_cue=cue,
        cues=(cue,),
        memory=memory,
        workspace=tmp_path,
        retrieval_markdown="## Retrieved DEV traces\nRETRIEVAL_MARKER",
        dev_task_allowlist=frozenset({task.task_id}),
        hide_tests=hide_tests,
        prompt_mode=prompt_mode,  # type: ignore[arg-type]
    )
    first_section = next(line for line in prompt.splitlines() if line.startswith("#"))
    assert first_section.startswith(f"# Runner error from your turn-{attempt_index - 1} submission")
    runner_at = prompt.index("RUNNER_OUTPUT_MARKER")
    memory_at = prompt.index("# Failure Memory")
    retrieval_at = prompt.index("## Retrieved DEV traces")
    assert runner_at < memory_at < prompt.index("synthetic-failed-signature") < retrieval_at
    if prompt_mode == "qa_review":
        assert memory_at < prompt.index("## Primary Hackbook hypothesis") < retrieval_at
        assert memory_at < prompt.index("# Hackbook toolkit") < retrieval_at
    else:
        assert "Synthetic cue marker" not in prompt
    assert "- sandbox exit codes: 7, 9" in prompt
    assert "RUNNER_HARNESS_MARKER" in prompt and "- test_checksum" in prompt
    assert "tests/test_private.py" not in prompt
    assert "REPAIRED_RESULT_MARKER" not in prompt and "0.375" not in prompt
    assert "repaired_grader_also_passed" not in prompt and "repaired=" not in prompt


@pytest.mark.parametrize("prompt_mode", ["qa_review", "bare_task"])
def test_blind_first_prompt_omits_all_retry_sections_even_with_existing_memory(
    tmp_path: Path, prompt_mode: str
) -> None:
    task, cue, memory = _task(tmp_path), _cue(), _memory()
    (tmp_path / "feedback.md").write_text(_runner_feedback() + memory.render_markdown())
    (tmp_path / "hackbook.md").write_text("WORKSPACE_CUE_MARKER")
    prompt = build_headless_seeker_prompt(
        task,
        attempt_index=1,
        selected_cue=cue,
        cues=(cue,),
        memory=memory,
        workspace=tmp_path,
        retrieval_markdown="RETRIEVAL_MARKER",
        dev_task_allowlist=frozenset({task.task_id}),
        hide_tests=True,
        prompt_mode=prompt_mode,  # type: ignore[arg-type]
    )
    for marker in (
        "# Runner error",
        "No runner output",
        "# Failure Memory",
        "synthetic-failed-signature",
        "Synthetic cue marker",
        "WORKSPACE_CUE_MARKER",
        "RETRIEVAL_MARKER",
        "REPAIRED_RESULT_MARKER",
    ):
        assert marker not in prompt


@pytest.mark.parametrize("prompt_mode", ["qa_review", "bare_task"])
def test_retry_without_original_execution_reports_no_runner_output_not_an_inferred_error(
    tmp_path: Path, prompt_mode: str
) -> None:
    task, memory = _task(tmp_path), _memory()
    prompt = build_headless_seeker_prompt(
        task,
        attempt_index=2,
        selected_cue=None,
        cues=(),
        memory=memory,
        workspace=tmp_path,
        dev_task_allowlist=frozenset({task.task_id}),
        prompt_mode=prompt_mode,  # type: ignore[arg-type]
    )
    assert prompt.index("No runner output:") < prompt.index("# Failure Memory")
    assert "not evaluated" in prompt and "no measured ORIGINAL-grader execution" in prompt
    assert "RUNNER_OUTPUT_MARKER" not in prompt and "REPAIRED_RESULT_MARKER" not in prompt
    assert "- reward:" not in prompt and "- harness error:" not in prompt


@pytest.mark.parametrize("enriched_hit", [False, True])
def test_headless_generator_preserves_failure_token_enrichment_and_error_first_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enriched_hit: bool
) -> None:
    task, cue, memory = _task(tmp_path), _cue(), _memory()
    (tmp_path / "feedback.md").write_text(_runner_feedback() + "\n\n" + memory.render_markdown())
    queries: list[str] = []

    class Retriever:
        def retrieve(self, *, task_id: str, query: str) -> TraceRetrievalResult:
            assert task_id == task.task_id
            queries.append(query)
            hits = (
                (RetrievedTraceHit("other-dev", "weak_check", "RetrievalMarker"),)
                if enriched_hit or query == "weak_check"
                else ()
            )
            return TraceRetrievalResult(hits=hits, receipt={})

    prompts: list[str] = []

    def fake_omp(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "omp"
        prompts.append(argv[-1])
        response = json.dumps({"files": {"solve.sh": "#!/bin/bash\nexit 0\n"}})
        stdout = json.dumps(
            {
                "type": "agent_end",
                "messages": [
                    {
                        "role": "assistant",
                        "provider": "test",
                        "model": "synthetic",
                        "content": [{"type": "text", "text": response}],
                    }
                ],
            }
        )
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(seeker_worker.subprocess, "run", fake_omp)
    generator = OmpHeadlessAttemptGenerator(
        models=("test/synthetic",),
        retriever=Retriever(),  # type: ignore[arg-type]
        dev_task_allowlist=frozenset({task.task_id}),
        hide_tests=True,
    )
    generator(task, 1, cue, (cue,), memory, tmp_path)
    assert not queries and "RUNNER_OUTPUT_MARKER" not in prompts[0]
    generator(task, 2, cue, (cue,), memory, tmp_path)
    assert queries == (["weak_check checksum"] if enriched_hit else ["weak_check checksum", "weak_check"])
    prompt = prompts[1]
    assert (
        prompt.index("RUNNER_OUTPUT_MARKER")
        < prompt.index("# Failure Memory")
        < prompt.index("## Primary Hackbook hypothesis")
        < prompt.index("RetrievalMarker")
    )
    assert "REPAIRED_RESULT_MARKER" not in prompt
    assert (tmp_path / "headless_prompt.md").read_text() == prompt
