"""Streaming public-corpus loaders and registry-admitted durable ingestion.

The synthetic corpus mirrors ``test_corpus_queue``'s sealed-split matrix: sealed
task names and digests follow items across datasets and aliases, so a misleading
``"split": "dev"`` label in an external export can never unlock TEST work.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from evallab.dataset_audit_checks import static_observation
from evallab.dataset_audit_contracts import AuditTask
from evallab.production.corpus_queue import (
    ContinuousWorkQueue,
    CorpusItem,
    CorpusRegistry,
    IdempotencyConflict,
    SplitLockError,
)
from evallab.production.public_corpus_loader import (
    ANNOTATION_JOB,
    AUDIT_JOB,
    PREFIX_AUDIT_JOB,
    SEEK_JOB,
    PublicCorpusJob,
    ingest_public_corpus,
    load_goodfire_rollouts,
    load_swebench_annotations,
    load_terminal_wrench_traces,
)
from evallab.production.tq_worker import TaskQualityLiveWorker
from evallab.registry import task_directory_digest

DATASET = "tb4-audit-v1"
FROZEN_TOKEN = "test-only-explicit-frozen-evaluation"


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _item(item_id: str, split: str, task: str, digest: str | None = None) -> dict:
    return {"item_id": item_id, "split": split, "subject": {"task_id": task, "task_digest": digest}}


def _write_corpus(root: Path, *, extra_mimo: list[dict] | None = None) -> Path:
    keys = root / "research/answer-keys"
    _write_jsonl(
        keys / DATASET / "items.jsonl",
        [
            _item("finding-dev", "dev", "terminal-bench/dev-task", "sha256:dev"),
            _item("finding-test", "test", "terminal-bench/sealed-task", "sha256:sealed"),
        ],
    )
    _write_jsonl(
        keys / DATASET / "tasks.jsonl",
        [
            {"split": "dev", "subject": {"task_id": "terminal-bench/dev-task", "task_digest": "sha256:dev"}},
            {"split": "test", "subject": {"task_id": "terminal-bench/sealed-task", "task_digest": "sha256:sealed"}},
        ],
    )
    _write_jsonl(
        keys / "cheatbench-actions-v1/items.jsonl",
        [
            _item("cheat-dev", "dev", "cheatbench/independent"),
            _item("renamed-sealed", "dev", "cheatbench/renamed", "sha256:sealed"),
        ],
    )
    _write_jsonl(
        keys / "hacktrace-act-v1/items.jsonl",
        [
            _item("hack-dev", "dev", "hacktrace/independent"),
            _item("name-alias", "dev", "another-benchmark/sealed-task@v2"),
        ],
    )
    tasks = [
        {
            "record_id": "mimo-dev@sha256:version1",
            "task_id": "mimo-dev",
            "package_digest": "sha256:version1",
            "materialization": {"resolved_locator": "/immutable/mimo-dev"},
        },
        {
            "record_id": "mimo-held@sha256:version1",
            "task_id": "mimo-held",
            "package_digest": "sha256:held",
        },
        *(extra_mimo or []),
    ]
    inventory_path = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_text(
        json.dumps(
            {
                "dataset_id": "mimo-defect-map-v1",
                "heldout_task_ids_excluded_from_training_all_versions": ["mimo-held"],
                "tasks": tasks,
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def corpus_root(tmp_path: Path) -> Path:
    return _write_corpus(tmp_path / "corpus")


@pytest.fixture
def registry(corpus_root: Path) -> CorpusRegistry:
    return CorpusRegistry(corpus_root, frozen_eval_token=FROZEN_TOKEN)


@pytest.fixture
def queue(tmp_path: Path, registry: CorpusRegistry) -> Iterator[ContinuousWorkQueue]:
    with ContinuousWorkQueue(tmp_path / "production.sqlite", registry=registry) as result:
        yield result


def _step(number: int, **extra: object) -> dict:
    return {
        "step_id": number,
        "source": "agent",
        "reasoning": "Inspect the workspace.",
        "tool_calls": [
            {
                "tool_call_id": f"call-{number}",
                "function_name": "bash",
                "arguments": {"command": "pwd"},
            }
        ],
        **extra,
    }


def _goodfire_row(rollout_id: str, task: str, **extra: object) -> dict:
    return {
        "rollout_id": rollout_id,
        "task": task,
        "model": "gf-model",
        "license": "CC-BY-4.0",
        "steps": [_step(1), _step(2)],
        **extra,
    }


def _tw_row(label: str, task: str, **extra: object) -> dict:
    return {"label": label, "task_id": task, "model": "tw-model", **extra}


def _swebench_row(instance: str, item_id: str, **extra: object) -> dict:
    return {
        "instance_id": instance,
        "dataset_id": "cheatbench-actions-v1",
        "item_id": item_id,
        "underspecified": "major",
        "unfair_tests": "minor",
        "task_package": "/pkgs/original",
        "repaired_task": "/pkgs/repaired",
        "failing_runs": ["/runs/fail-1"],
        **extra,
    }


# --------------------------------------------------------------------- loaders


def test_goodfire_rollouts_yield_normalized_prefix_audit_jobs(tmp_path: Path) -> None:
    chat_row = _goodfire_row(
        "gf-1",
        "dev-task",
        provenance={"dataset": "goodfire/rollouts", "revision": "abc123"},
    )
    chat_row.pop("steps")
    chat_row["messages"] = [
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": "Solve the task."},
        {
            "role": "assistant",
            "content": "Inspecting.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                }
            ],
        },
    ]
    path = _write_jsonl(
        tmp_path / "rollouts.jsonl",
        [chat_row, _goodfire_row("gf-2", "terminal-bench/dev-task")],
    )
    jobs = list(load_goodfire_rollouts(path))
    assert [job.job_type for job in jobs] == [PREFIX_AUDIT_JOB] * 2
    first, second = jobs
    assert first.source_id == "gf-1"
    # The external record keeps its genuine dataset id, never tb4-audit-v1.
    assert first.payload["dataset_id"] == "goodfire-rollouts"
    assert first.payload["task_id"] == "dev-task"
    assert first.payload["source_kind"] == "public_rollout"
    assert first.payload["license"] == "CC-BY-4.0"
    assert first.payload["provenance"]["dataset"] == "goodfire/rollouts"
    assert first.payload["provenance"]["revision"] == "abc123"
    assert first.payload["provenance"]["loader"] == "goodfire_rollouts"
    assert first.payload["provenance"]["source_path"] == str(path)
    assert first.payload["provenance"]["record_locator"] == "rollouts.jsonl:1"
    assert first.payload["model"] == "gf-model"
    steps = first.payload["steps"]
    assert [s["source"] for s in steps] == ["system", "user", "agent"]
    assert [s["step_id"] for s in steps] == [1, 2, 3]
    call = steps[2]["tool_calls"][0]
    assert call["tool_call_id"] == "call_1"
    assert call["function_name"] == "bash"
    assert call["arguments"] == {"command": "ls"}
    assert call["type"] == "function"
    assert second.payload["task_id"] == "terminal-bench/dev-task"


def test_goodfire_rollouts_require_declared_license_and_stable_id(tmp_path: Path) -> None:
    unlicensed = _goodfire_row("gf-1", "dev-task")
    unlicensed.pop("license")
    path = _write_jsonl(tmp_path / "rollouts.jsonl", [unlicensed])
    with pytest.raises(ValueError, match="license"):
        list(load_goodfire_rollouts(path))
    unidentified = _goodfire_row("gf-1", "dev-task")
    unidentified.pop("rollout_id")
    path = _write_jsonl(tmp_path / "rollouts.jsonl", [unidentified])
    with pytest.raises(ValueError, match="stable row id"):
        list(load_goodfire_rollouts(path))


def test_goodfire_json_envelope_supplies_license(tmp_path: Path) -> None:
    row = _goodfire_row("gf-1", "dev-task")
    row.pop("license")
    path = tmp_path / "rollouts.json"
    path.write_text(
        json.dumps({"license": "Apache-2.0", "dataset": "goodfire/rollouts", "rollouts": [row]}),
        encoding="utf-8",
    )
    (job,) = list(load_goodfire_rollouts(path))
    assert job.payload["license"] == "Apache-2.0"


def test_loaders_are_lazy_single_pass_iterators(tmp_path: Path) -> None:
    path = _write_jsonl(tmp_path / "rollouts.jsonl", [_goodfire_row("gf-old", "dev-task")])
    jobs = load_goodfire_rollouts(path)
    _write_jsonl(path, [_goodfire_row("gf-new", "dev-task")])
    assert [job.source_id for job in jobs] == ["gf-new"]

    broken = tmp_path / "broken.jsonl"
    broken.write_text(
        json.dumps(_goodfire_row("gf-ok", "dev-task")) + "\nnot json\n", encoding="utf-8"
    )
    partial = load_goodfire_rollouts(broken)
    assert next(partial).source_id == "gf-ok"
    with pytest.raises(json.JSONDecodeError):
        next(partial)


def test_terminal_wrench_loader_routes_task_and_hack_trajectories(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "traces.jsonl",
        [
            _tw_row(
                "hack-1",
                "hacktrace/independent",
                dataset_id="hacktrace-act-v1",
                item_id="hack-dev",
                classification="rewarded_serious_exploit",
                steps=[_step(1)],
            ),
            _tw_row(
                "clean-1",
                "hacktrace/independent",
                dataset_id="hacktrace-act-v1",
                item_id="hack-dev",
                classification="attacker_legitimate_solve",
                task_package="/pkgs/original",
                repaired_task="/pkgs/repaired",
            ),
            _tw_row(
                "clean-unusable",
                "hacktrace/independent",
                dataset_id="hacktrace-act-v1",
                item_id="hack-dev",
                classification="attacker_legitimate_solve",
            ),
            _tw_row(
                "hack-nosteps",
                "hacktrace/independent",
                dataset_id="hacktrace-act-v1",
                item_id="hack-dev",
                classification="rewarded_nonserious_hack",
            ),
            _tw_row(
                "hack-alias",
                "hacktrace/independent",
                dataset_id="hacktrace-act-v1",
                item_id="hack-dev",
                tw_key="hacks__task__model__label",
                steps=[_step(1)],
            ),
        ],
    )
    jobs = list(load_terminal_wrench_traces(path))
    assert [(job.source_id, job.job_type) for job in jobs] == [
        ("hacktrace/independent|tw-model|hack-1", PREFIX_AUDIT_JOB),
        ("hacktrace/independent|tw-model|clean-1", SEEK_JOB),
        ("hacks__task__model__label", PREFIX_AUDIT_JOB),
    ]
    hack = jobs[0].payload
    assert hack["license"] == "Apache-2.0"
    assert hack["dataset_id"] == "hacktrace-act-v1"
    assert hack["steps"][0]["step_id"] == 1
    assert hack["provenance"]["classification"] == "rewarded_serious_exploit"
    assert hack["provenance"]["dataset"] == "few-sh/terminal-wrench"
    assert hack["provenance"]["record_locator"] == "traces.jsonl:1"
    assert hack["provenance"]["source_path"] == str(path)
    seek = jobs[1].payload
    assert seek["task_package"] == "/pkgs/original"
    assert seek["repaired_task"] == "/pkgs/repaired"
    assert seek["model"] == "tw-model"


def test_terminal_wrench_source_ids_never_collide_on_shared_labels(
    tmp_path: Path,
) -> None:
    """``v5_4``-style labels are shared across tasks/models, not run ids."""
    rows = [
        _tw_row(
            "v5_4",
            "task-a",
            trajectory_id="traj-unique-1",
            task_package="/pkgs/o",
            repaired_task="/pkgs/r",
        ),
        _tw_row(
            "v5_4",
            "task-a",
            id="tw-77",
            task_package="/pkgs/o",
            repaired_task="/pkgs/r",
        ),
        _tw_row(
            "v5_4",
            "task-a",
            tw_key="clean__task-a__tw-model__v5_4",
            task_package="/pkgs/o",
            repaired_task="/pkgs/r",
        ),
        _tw_row("v5_4", "task-b", task_package="/pkgs/o", repaired_task="/pkgs/r"),
        _tw_row("v5_4", "task-a", model="other-model", task_package="/pkgs/o", repaired_task="/pkgs/r"),
    ]
    path = _write_jsonl(tmp_path / "traces.jsonl", rows)
    jobs = list(load_terminal_wrench_traces(path))
    assert [job.source_id for job in jobs] == [
        "traj-unique-1",
        "tw-77",
        "clean__task-a__tw-model__v5_4",
        "task-b|tw-model|v5_4",
        "task-a|other-model|v5_4",
    ]
    assert len({job.source_id for job in jobs}) == len(jobs)

    path = _write_jsonl(tmp_path / "unstable.jsonl", [{"label": "v5_4", "steps": [_step(1)]}])
    with pytest.raises(ValueError, match="stable row id"):
        list(load_terminal_wrench_traces(path))


def test_terminal_wrench_task_and_hack_records_normalize_both_families(
    tmp_path: Path,
) -> None:
    """A hack-classified row with runnable task fields yields both job types."""
    path = _write_jsonl(
        tmp_path / "traces.jsonl",
        [
            _tw_row(
                "hack-runnable",
                "hacktrace/independent",
                classification="rewarded_serious_exploit",
                steps=[_step(1)],
                task_package="/pkgs/original",
                repaired_task="/pkgs/repaired",
            )
        ],
    )
    jobs = list(load_terminal_wrench_traces(path))
    assert [job.job_type for job in jobs] == [PREFIX_AUDIT_JOB, SEEK_JOB]
    assert {job.source_id for job in jobs} == {
        "hacktrace/independent|tw-model|hack-runnable"
    }
    assert jobs[1].payload["task_package"] == "/pkgs/original"
    assert jobs[1].payload["provenance"]["classification"] == "rewarded_serious_exploit"


def test_terminal_wrench_declared_license_overrides_default(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "traces.jsonl",
        [
            _tw_row(
                "hack-1",
                "hacktrace/independent",
                classification="rewarded_serious_exploit",
                license="CC-BY-4.0",
                steps=[_step(1)],
            )
        ],
    )
    (job,) = list(load_terminal_wrench_traces(path))
    assert job.payload["license"] == "CC-BY-4.0"


def test_swebench_annotation_rows_all_route_annotation_jobs(tmp_path: Path) -> None:
    """Every row carrying human annotation content yields annotation_task.

    Runnable locators never flip an annotation record into a plain audit job:
    ``task_package``/``task_dir`` + ``repaired_task`` + replay inputs only mark
    the annotation job execution-ready for the worker's shared differential.
    """
    path = _write_jsonl(
        tmp_path / "annotations.jsonl",
        [
            _swebench_row("sw-1", "cheat-dev"),
            _swebench_row("sw-no-runs", "cheat-dev", failing_runs=None),
            _swebench_row("sw-no-package", "cheat-dev", repaired_task=None),
            _swebench_row(
                "sw-exploit",
                "cheat-dev",
                failing_runs=None,
                exploit_files={"solve.sh": "exit 0"},
            ),
        ],
    )
    jobs = list(load_swebench_annotations(path))
    assert [(job.source_id, job.job_type) for job in jobs] == [
        ("sw-1", ANNOTATION_JOB),
        ("sw-no-runs", ANNOTATION_JOB),
        ("sw-no-package", ANNOTATION_JOB),
        ("sw-exploit", ANNOTATION_JOB),
    ]
    first, no_runs, no_package, exploit = jobs
    assert first.payload["license"] == "MIT"
    assert first.payload["dataset_id"] == "cheatbench-actions-v1"
    assert first.payload["source_kind"] == "public_annotation"
    assert first.payload["annotation"]["underspecified"] == "major"
    assert first.payload["annotation"]["unfair_tests"] == "minor"
    assert first.payload["failing_runs"] == ["/runs/fail-1"]
    assert exploit.payload["exploit_files"] == {"solve.sh": "exit 0"}
    # Rows whose declared locators and replay inputs are complete are marked
    # execution-ready; annotation-only rows document what execution needs.
    assert first.payload["packaging_state"] == "execution_ready"
    assert exploit.payload["packaging_state"] == "execution_ready"
    for annotation_job in (no_runs, no_package):
        payload = annotation_job.payload
        assert payload["source_kind"] == "public_annotation"
        assert payload["annotation"]["underspecified"] == "major"
        assert payload["annotation"]["unfair_tests"] == "minor"
        assert payload["packaging_state"] == "annotation_only"
        assert payload["execution_prerequisites"]
        assert payload["license"] == "MIT"
    # Declared locators are preserved; missing ones are never fabricated.
    assert no_runs.payload["task_package"] == "/pkgs/original"
    assert no_runs.payload["repaired_task"] == "/pkgs/repaired"
    assert "failing_runs" not in no_runs.payload
    assert no_package.payload["task_package"] == "/pkgs/original"
    assert "repaired_task" not in no_package.payload


def test_swebench_evidence_rows_without_annotations_still_audit(
    tmp_path: Path,
) -> None:
    """Runnable evidence carrying no human content stays a plain audit job."""
    row = _swebench_row("sw-audit", "cheat-dev", underspecified=None, unfair_tests=None)
    path = _write_jsonl(tmp_path / "annotations.jsonl", [row])
    (job,) = list(load_swebench_annotations(path))
    assert job.job_type == AUDIT_JOB
    assert job.payload["source_kind"] == "public_rollout"
    assert job.payload["task_package"] == "/pkgs/original"
    assert job.payload["failing_runs"] == ["/runs/fail-1"]


def test_swebench_rows_without_annotations_or_evidence_are_skipped(
    tmp_path: Path,
) -> None:
    row = _swebench_row(
        "sw-empty",
        "cheat-dev",
        underspecified=None,
        unfair_tests=None,
        failing_runs=None,
        repaired_task=None,
        task_package=None,
    )
    path = _write_jsonl(tmp_path / "annotations.jsonl", [row])
    assert list(load_swebench_annotations(path)) == []


def test_swebench_declared_severity_annotation_block_is_preserved(tmp_path: Path) -> None:
    row = _swebench_row(
        "sw-1",
        "cheat-dev",
        annotations={"q1": "underspecified_major", "q2": "fair"},
        severity="high",
    )
    path = _write_jsonl(tmp_path / "annotations.jsonl", [row])
    (job,) = list(load_swebench_annotations(path))
    assert job.payload["annotation"]["annotations"] == {
        "q1": "underspecified_major",
        "q2": "fair",
    }
    assert job.payload["annotation"]["severity"] == "high"


# ---------------------------------------------------------------------- ingest


def test_ingest_registers_and_enqueues_novel_external_records(
    queue: ContinuousWorkQueue, registry: CorpusRegistry, tmp_path: Path
) -> None:
    path = _write_jsonl(tmp_path / "rollouts.jsonl", [_goodfire_row("gf-1", "dev-task")])
    result = ingest_public_corpus(queue, load_goodfire_rollouts(path), run_id="public-ingest")
    assert result["excluded"] == []
    (envelope,) = result["enqueued"]
    assert envelope["event_type"] == PREFIX_AUDIT_JOB
    assert envelope["run_id"] == "public-ingest"
    payload = envelope["payload"]
    assert payload["dataset_id"] == "goodfire-rollouts"
    assert payload["item_id"] == "gf-1"
    assert payload["task_id"] == "dev-task"
    assert payload["split"] == "dev"
    item = registry.get("goodfire-rollouts", "gf-1")
    assert item.split == "dev" and item.task_id == "dev-task"
    claimed = queue.claim("monitor-dispatch", job_types=(PREFIX_AUDIT_JOB,))
    assert claimed is not None and claimed["event_id"] == envelope["event_id"]


def test_ingested_jobs_carry_source_and_revision_provenance(
    queue: ContinuousWorkQueue, tmp_path: Path
) -> None:
    row = _goodfire_row(
        "gf-1",
        "dev-task",
        revision="export-2026-10-24",
        source={"repo": "goodfire/rollouts"},
    )
    path = _write_jsonl(tmp_path / "rollouts.jsonl", [row])
    (envelope,) = ingest_public_corpus(
        queue, load_goodfire_rollouts(path), run_id="provenance"
    )["enqueued"]
    provenance = envelope["payload"]["provenance"]
    assert provenance["source_path"] == str(path)
    assert provenance["record_locator"] == "rollouts.jsonl:1"
    assert provenance["source"] == {"repo": "goodfire/rollouts"}
    assert provenance["revision"] == "export-2026-10-24"


def test_novel_external_annotations_ingest_as_annotation_jobs(
    queue: ContinuousWorkQueue, tmp_path: Path
) -> None:
    path = _write_jsonl(
        tmp_path / "annotations.jsonl",
        [
            {
                "instance_id": "django__django-17084",
                "underspecified": "major",
                "unfair_tests": "minor",
            }
        ],
    )
    result = ingest_public_corpus(
        queue, load_swebench_annotations(path), run_id="ext-annotations"
    )
    assert result["excluded"] == []
    (envelope,) = result["enqueued"]
    assert envelope["event_type"] == ANNOTATION_JOB
    payload = envelope["payload"]
    assert payload["dataset_id"] == "swebench-annotations"
    assert payload["item_id"] == "django__django-17084"
    assert payload["annotation"]["underspecified"] == "major"
    assert payload["execution_prerequisites"]
    assert "task_package" not in payload


@pytest.mark.parametrize(
    "task",
    ["dev-task", "terminal-bench/dev-task", "terminal-bench-4/dev-task@452bf305c6"],
)
def test_ingest_keeps_external_dataset_id_with_declared_task_identity(
    queue: ContinuousWorkQueue, tmp_path: Path, task: str
) -> None:
    """External records register under their own dataset id, never tb4."""
    path = _write_jsonl(tmp_path / "rollouts.jsonl", [_goodfire_row("gf-1", task)])
    result = ingest_public_corpus(queue, load_goodfire_rollouts(path), run_id="alias")
    (payload,) = (event["payload"] for event in result["enqueued"])
    assert payload["dataset_id"] == "goodfire-rollouts"
    assert payload["item_id"] == "gf-1"
    assert payload["task_id"] == task


def test_ingest_explicit_item_and_digest_references(queue: ContinuousWorkQueue) -> None:
    jobs = [
        PublicCorpusJob(
            PREFIX_AUDIT_JOB,
            {
                "dataset_id": DATASET,
                "item_id": "finding-dev",
                "steps": [_step(1)],
                "license": "CC-BY-4.0",
            },
            "row-1",
        ),
        PublicCorpusJob(
            SEEK_JOB,
            {
                "dataset_id": "mimo-defect-map-v1",
                "task": "mimo-dev",
                "task_digest": "sha256:version1",
                "task_package": "/pkgs/original",
                "repaired_task": "/pkgs/repaired",
            },
            "row-2",
        ),
    ]
    result = ingest_public_corpus(queue, jobs, run_id="identity")
    assert [event["payload"]["item_id"] for event in result["enqueued"]] == [
        "finding-dev",
        "mimo-dev@sha256:version1",
    ]


def test_ingest_ambiguous_alias_fails_closed(tmp_path: Path) -> None:
    root = _write_corpus(
        tmp_path / "corpus",
        extra_mimo=[
            {
                "record_id": "mimo-dev@sha256:version2",
                "task_id": "mimo-dev",
                "package_digest": "sha256:version2",
            }
        ],
    )
    registry = CorpusRegistry(root, frozen_eval_token=FROZEN_TOKEN)
    with ContinuousWorkQueue(tmp_path / "production.sqlite", registry=registry) as queue:
        job = PublicCorpusJob(
            SEEK_JOB,
            {"dataset_id": "mimo-defect-map-v1", "task": "mimo-dev"},
            "row-1",
        )
        with pytest.raises(ValueError, match="ambiguous|resolves to 2"):
            ingest_public_corpus(queue, [job], run_id="ambiguous")
        resolved = PublicCorpusJob(
            SEEK_JOB,
            {
                "dataset_id": "mimo-defect-map-v1",
                "task": "mimo-dev",
                "task_digest": "sha256:version2",
            },
            "row-2",
        )
        result = ingest_public_corpus(queue, [resolved], run_id="ambiguous")
        assert [e["payload"]["item_id"] for e in result["enqueued"]] == [
            "mimo-dev@sha256:version2"
        ]


def test_ingest_excludes_sealed_items_even_with_dev_labels(
    queue: ContinuousWorkQueue, tmp_path: Path
) -> None:
    sealed_task_rollout = _goodfire_row("gf-sealed", "sealed-task", split="dev")
    sealed_item_rollout = _goodfire_row(
        "gf-item", "ignored", item_id="finding-test", split="dev"
    )
    path = _write_jsonl(tmp_path / "rollouts.jsonl", [sealed_task_rollout, sealed_item_rollout])
    result = ingest_public_corpus(queue, load_goodfire_rollouts(path), run_id="sealed")
    assert result["enqueued"] == []
    assert {ex["reason"] for ex in result["excluded"]} == {"sealed_test_split"}
    assert {ex["source_id"] for ex in result["excluded"]} == {"gf-sealed", "gf-item"}
    # Seals followed the identities into the external dataset id.
    assert result["excluded"][0]["dataset_id"] == "goodfire-rollouts"
    assert result["excluded"][0]["item_id"] == "gf-sealed"
    assert result["excluded"][1]["item_id"] == "finding-test"
    assert queue.read_events() == queue.pending_outbox() == []


@pytest.mark.parametrize(
    ("dataset_id", "item_id"),
    [
        ("cheatbench-actions-v1", "renamed-sealed"),
        ("hacktrace-act-v1", "name-alias"),
        ("mimo-defect-map-v1", "mimo-held@sha256:version1"),
    ],
)
def test_sealed_lock_follows_digest_and_canonical_task_names(
    queue: ContinuousWorkQueue, dataset_id: str, item_id: str
) -> None:
    """DEV-labelled external rows cannot unlock digest/name-sealed items."""
    job = PublicCorpusJob(
        AUDIT_JOB,
        {
            "dataset_id": dataset_id,
            "item_id": item_id,
            "task_package": "/pkgs/original",
            "repaired_task": "/pkgs/repaired",
            "failing_runs": ["/runs/fail-1"],
        },
        "row-1",
    )
    result = ingest_public_corpus(queue, [job], run_id="sealed")
    assert result["enqueued"] == []
    assert [ex["reason"] for ex in result["excluded"]] == ["sealed_test_split"]
    assert queue.read_events() == []


def test_public_ingestion_never_queues_test_items_even_with_token(
    queue: ContinuousWorkQueue, registry: CorpusRegistry
) -> None:
    """tb4-audit-v1 TEST items cannot pass through this path, period.

    The registry in use is configured with ``FROZEN_TOKEN``; the ingest API
    deliberately has no token parameter, so the sealed item stays excluded.
    """
    assert registry.admit(DATASET, "finding-test", frozen_eval_token=FROZEN_TOKEN).split == "test"
    job = PublicCorpusJob(
        AUDIT_JOB,
        {
            "dataset_id": DATASET,
            "item_id": "finding-test",
            "task_package": "/pkgs/original",
            "repaired_task": "/pkgs/repaired",
            "failing_runs": ["/runs/fail-1"],
        },
        "row-1",
    )
    result = ingest_public_corpus(queue, [job], run_id="frozen")
    assert result["enqueued"] == []
    assert [ex["reason"] for ex in result["excluded"]] == ["sealed_test_split"]
    with pytest.raises(TypeError):
        ingest_public_corpus(queue, [job], run_id="frozen", frozen_eval_token=FROZEN_TOKEN)  # ty: ignore[unknown-argument]
    assert queue.read_events() == []


def test_contradictory_identifiers_are_blocked(queue: ContinuousWorkQueue) -> None:
    """A DEV item_id paired with TEST task/digest identities never queues."""
    jobs = [
        # Canonical DEV item + sealed TEST task name.
        PublicCorpusJob(
            AUDIT_JOB,
            {
                "dataset_id": DATASET,
                "item_id": "finding-dev",
                "task_id": "terminal-bench/sealed-task",
            },
            "c-1",
        ),
        # Canonical DEV item + sealed TEST digest.
        PublicCorpusJob(
            AUDIT_JOB,
            {
                "dataset_id": DATASET,
                "item_id": "finding-dev",
                "task_digest": "sha256:sealed",
            },
            "c-2",
        ),
        # A record naming two different task identities at once.
        PublicCorpusJob(
            AUDIT_JOB,
            {
                "dataset_id": DATASET,
                "item_id": "finding-dev",
                "task": "terminal-bench/dev-task",
                "task_id": "terminal-bench/sealed-task",
            },
            "c-3",
        ),
    ]
    result = ingest_public_corpus(queue, jobs, run_id="contradictions")
    assert result["enqueued"] == []
    assert [ex["reason"] for ex in result["excluded"]] == ["contradictory_identity"] * 3
    assert {ex["source_id"] for ex in result["excluded"]} == {"c-1", "c-2", "c-3"}
    assert queue.read_events() == []


def test_external_ids_cannot_launder_canonical_test_identities(
    queue: ContinuousWorkQueue, registry: CorpusRegistry
) -> None:
    """Sealed task names, digests, and item ids stay sealed under new ids."""
    jobs = [
        PublicCorpusJob(
            AUDIT_JOB,
            {
                "dataset_id": "goodfire-rollouts",
                "item_id": "ext-task",
                "task_id": "another-benchmark/sealed-task@v2",
            },
            "e-1",
        ),
        PublicCorpusJob(
            AUDIT_JOB,
            {
                "dataset_id": "terminal-wrench",
                "item_id": "ext-digest",
                "task_digest": "sha256:sealed",
            },
            "e-2",
        ),
        PublicCorpusJob(
            AUDIT_JOB,
            {
                "dataset_id": "swebench-annotations",
                "item_id": "finding-test",
            },
            "e-3",
        ),
        PublicCorpusJob(
            SEEK_JOB,
            {
                "dataset_id": "goodfire-rollouts",
                "item_id": "ext-split",
                "split": "test",
            },
            "e-4",
        ),
    ]
    result = ingest_public_corpus(queue, jobs, run_id="laundering")
    assert result["enqueued"] == []
    assert {ex["reason"] for ex in result["excluded"]} == {"sealed_test_split"}
    assert {ex["source_id"] for ex in result["excluded"]} == {"e-1", "e-2", "e-3", "e-4"}
    for dataset_id, item_id in (
        ("goodfire-rollouts", "ext-task"),
        ("terminal-wrench", "ext-digest"),
        ("swebench-annotations", "finding-test"),
        ("goodfire-rollouts", "ext-split"),
    ):
        with pytest.raises(SplitLockError):
            registry.admit(dataset_id, item_id)
    assert queue.read_events() == []


def test_unregistered_items_are_excluded_not_queued(queue: ContinuousWorkQueue) -> None:
    jobs = [
        PublicCorpusJob(PREFIX_AUDIT_JOB, {"dataset_id": DATASET, "item_id": "ghost"}, "x-1"),
        PublicCorpusJob(PREFIX_AUDIT_JOB, {"task": "no-such-task"}, "x-2"),
    ]
    result = ingest_public_corpus(queue, jobs, run_id="unknown")
    assert result["enqueued"] == []
    assert [ex["reason"] for ex in result["excluded"]] == [
        "unregistered_item",
        "unregistered_item",
    ]
    assert queue.read_events() == []


def test_repeated_batches_are_idempotent_and_conflicts_rejected(
    queue: ContinuousWorkQueue, registry: CorpusRegistry, tmp_path: Path
) -> None:
    path = _write_jsonl(
        tmp_path / "rollouts.jsonl",
        [_goodfire_row("gf-1", "dev-task"), _goodfire_row("gf-2", "dev-task")],
    )
    first = ingest_public_corpus(queue, load_goodfire_rollouts(path), run_id="public-ingest")
    assert {item.item_id for item in registry.items(dataset_id="goodfire-rollouts")} == {
        "gf-1",
        "gf-2",
    }
    again = ingest_public_corpus(queue, load_goodfire_rollouts(path), run_id="public-ingest")
    assert [event["event_id"] for event in again["enqueued"]] == [
        event["event_id"] for event in first["enqueued"]
    ]
    assert len(queue.read_events()) == 2
    assert queue.queue_snapshot()["depth"] == 2

    changed = _goodfire_row("gf-1", "dev-task", steps=[_step(1)])
    path = _write_jsonl(tmp_path / "changed.jsonl", [changed])
    with pytest.raises(IdempotencyConflict):
        ingest_public_corpus(queue, load_goodfire_rollouts(path), run_id="public-ingest")
    assert len(queue.read_events()) == 2


def test_external_registration_conflicts_fail_closed(
    registry: CorpusRegistry, tmp_path: Path
) -> None:
    item = CorpusItem(
        dataset_id="goodfire-rollouts",
        item_id="gf-1",
        split="dev",
        task_id="dev-task",
        task_digest=None,
        source_path=tmp_path / "rollouts.jsonl",
        metadata={"source_kind": "public_rollout"},
        source_record_sha256="sha256:" + "0" * 64,
    )
    registered = registry.register_external(item)
    assert registered.split == "dev"
    # Same declared identity is idempotent; a different one is a conflict.
    assert registry.register_external(item).item_id == "gf-1"
    with pytest.raises(ValueError, match="already registered"):
        registry.register_external(
            CorpusItem(
                dataset_id="goodfire-rollouts",
                item_id="gf-1",
                split="dev",
                task_id="other-task",
                task_digest=None,
                source_path=tmp_path / "rollouts.jsonl",
                metadata={},
                source_record_sha256="sha256:" + "0" * 64,
            )
        )
    # Canonical datasets are never writable through the external API.
    with pytest.raises(ValueError, match="canonical dataset"):
        registry.register_external(
            CorpusItem(
                dataset_id=DATASET,
                item_id="novel",
                split="dev",
                task_id=None,
                task_digest=None,
                source_path=tmp_path / "x",
                metadata={},
                source_record_sha256="sha256:" + "0" * 64,
            )
        )


def test_ingested_prefix_audit_job_feeds_the_misalignment_consumer(
    queue: ContinuousWorkQueue, tmp_path: Path
) -> None:
    """Enqueued audit jobs carry monitor-consumable ``payload.steps``."""
    path = _write_jsonl(tmp_path / "rollouts.jsonl", [_goodfire_row("gf-1", "dev-task")])
    ingest_public_corpus(queue, load_goodfire_rollouts(path), run_id="public-ingest")
    events = queue.read_consumer("misalignment-live")
    (event,) = [e for e in events if e["event_type"] == PREFIX_AUDIT_JOB]
    assert [step["step_id"] for step in event["payload"]["steps"]] == [1, 2]
    assert event["payload"]["source_kind"] == "public_rollout"
    assert event["payload"]["provenance"]["license"] == "CC-BY-4.0"


# ----------------------------------------------------- annotation worker E2E


def _fixture_task_package(path: Path, *, repaired: bool) -> Path:
    (path / "tests").mkdir(parents=True)
    (path / "task.toml").write_text('[task]\nname = "swe-task"\n', encoding="utf-8")
    (path / "instruction.md").write_text(
        "Write the computed answer 42 to output.txt.\n", encoding="utf-8"
    )
    check = 'output.read_text().strip() == "42"' if repaired else 'output.read_text() == "42\\n"'
    (path / "tests/test_output.py").write_text(
        "from pathlib import Path\noutput = Path('output.txt')\n"
        f"assert output.is_file() and {check}\n",
        encoding="utf-8",
    )
    return path


def _fixture_trial(path: Path, script: str) -> Path:
    (path / "verifier").mkdir(parents=True)
    (path / "verifier/solve.sh").write_text(script, encoding="utf-8")
    (path / "result.json").write_text(
        json.dumps({"verifier_result": {"rewards": {"reward": 0.0}}}), encoding="utf-8"
    )
    return path


class _AnnotationBackend:
    """QA backend resolving fixture packages; static observation stays real."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def resolve(self, package: Path) -> AuditTask:
        self.calls.append("resolve")
        return AuditTask(
            dataset_id="swebench-annotations",
            task_id="swe-task",
            task_name="swe-task",
            path=package,
            package_digest=task_directory_digest(package),
            source_uri=package.as_uri(),
        )

    def static(self, task: AuditTask):
        self.calls.append("static")
        return static_observation(task)


class _LocalReplay:
    """Re-execute the retained submission against the package checker locally.

    This is a real (subprocess) execution of the retained ``solve.sh`` against
    the fixture grader — deterministic, free, and never invented.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Path, float]] = []
        self.refusals: list[str] = []

    def __call__(self, trial_dir, grader, work, *, repo_root, environment):
        work.mkdir(parents=True, exist_ok=True)
        if self.refusals:
            return {
                "regraded": None,
                "refusals": list(self.refusals),
                "execution_error": None,
                "outcome": "refused",
                "job_dir": str(work / "job"),
            }
        script = (Path(trial_dir) / "verifier/solve.sh").read_text(encoding="utf-8")
        (work / "solve.sh").write_text(script, encoding="utf-8")
        subprocess.run(
            ["/bin/bash", "solve.sh"], cwd=work, check=True, capture_output=True, timeout=10
        )
        checked = subprocess.run(
            [sys.executable, str(grader.package / "tests/test_output.py")],
            cwd=work,
            capture_output=True,
            timeout=10,
        )
        reward = float(checked.returncode == 0)
        (work / "job").mkdir()
        self.calls.append((grader.package, reward))
        return {
            "regraded": reward,
            "refusals": [],
            "execution_error": None,
            "outcome": "flips",
            "job_dir": str(work / "job"),
        }


def _worker(
    queue: ContinuousWorkQueue,
    tmp_path: Path,
    replay: _LocalReplay,
    backend: _AnnotationBackend | None = None,
):
    return TaskQualityLiveWorker(
        queue,
        repo_root=tmp_path,
        backend=backend if backend is not None else _AnnotationBackend(),
        replay_runner=replay,
        clock=lambda: "2026-10-24T15:00:00+00:00",
    )


def test_annotation_only_rows_flow_ingest_queue_worker_to_provisional(
    queue: ContinuousWorkQueue, tmp_path: Path
) -> None:
    path = _write_jsonl(
        tmp_path / "annotations.jsonl",
        [
            {
                "instance_id": "django__django-17084",
                "underspecified": "major",
                "unfair_tests": "minor",
                "comment": "Instruction never states the required trailing newline.",
            }
        ],
    )
    result = ingest_public_corpus(
        queue, load_swebench_annotations(path), run_id="wave4-annotations"
    )
    (envelope,) = result["enqueued"]
    assert envelope["event_type"] == ANNOTATION_JOB
    outcome = _worker(queue, tmp_path, _LocalReplay()).run_once()
    assert outcome is not None and outcome["complete"] is True
    events = queue.read_events(limit=1000)
    findings = [event for event in events if event["event_type"] == "qa.finding/v1"]
    assert [event["status"] for event in findings] == ["provisional"]
    payload = findings[0]["payload"]
    assert payload["execution_available"] is False
    assert payload["annotation"]["underspecified"] == "major"
    assert payload["annotation"]["unfair_tests"] == "minor"
    assert payload["finding"]["severity"] == "high"
    assert payload["finding"]["verdict"] == "unknown"
    assert payload["finding"]["measurement"] == "not_run"
    assert payload["provenance"]["source_id"] == "django__django-17084"
    assert payload["dataset_id"] == "swebench-annotations"
    assert payload["item_id"] == "django__django-17084"
    assert events[-1]["event_type"] == "task_quality.completed"
    assert not any(event["event_type"] == "asset.verified" for event in events)
    assert queue.queue_snapshot()["jobs"] == {"queued": 0, "leased": 0, "completed": 1}


def test_runnable_annotation_task_dir_executes_and_confirms(
    queue: ContinuousWorkQueue, tmp_path: Path
) -> None:
    """A loader-preserved ``task_dir`` feeds the standard differential path."""
    original = _fixture_task_package(tmp_path / "original", repaired=False)
    repaired = _fixture_task_package(tmp_path / "repaired", repaired=True)
    trial = _fixture_trial(tmp_path / "fail-1", "printf '42' > output.txt\n")
    row = {
        "instance_id": "swe-runnable-1",
        "task_dir": str(original),
        "repaired_task": str(repaired),
        "failing_runs": [str(trial)],
        "underspecified": "major",
        "unfair_tests": "minor",
        "comment": "Instruction never states the required trailing newline.",
    }
    path = _write_jsonl(tmp_path / "annotations.jsonl", [row])
    (job,) = list(load_swebench_annotations(path))
    assert job.job_type == ANNOTATION_JOB
    assert job.payload["task_dir"] == str(original)
    ingest_public_corpus(queue, [job], run_id="wave4-runnable")
    replay = _LocalReplay()
    outcome = _worker(queue, tmp_path, replay).run_once()
    assert outcome is not None and outcome["complete"] is True
    events = queue.read_events(limit=1000)
    findings = [event for event in events if event["event_type"] == "qa.finding/v1"]
    assert [event["status"] for event in findings] == ["provisional", "confirmed"]
    executed = findings[-1]["payload"]
    assert executed["execution_available"] is True
    assert executed["annotation"]["underspecified"] == "major"
    assert executed["finding"]["refutation"]["status"] == "verified"
    assert executed["finding"]["severity"] == "high"
    assert "trailing newline" in executed["finding"]["summary"]
    assert [reward for _, reward in replay.calls] == [0.0, 1.0]
    assert not any(event["event_type"] == "asset.verified" for event in events)


def test_runnable_annotation_refutes_when_differential_disagrees(
    queue: ContinuousWorkQueue, tmp_path: Path
) -> None:
    original = _fixture_task_package(tmp_path / "original", repaired=False)
    repaired = _fixture_task_package(tmp_path / "repaired", repaired=True)
    trial = _fixture_trial(tmp_path / "pass-1", "printf '42\\n' > output.txt\n")
    row = {
        "instance_id": "swe-runnable-2",
        "task_dir": str(original),
        "repaired_task": str(repaired),
        "failing_runs": [str(trial)],
        "underspecified": "major",
    }
    path = _write_jsonl(tmp_path / "annotations.jsonl", [row])
    ingest_public_corpus(queue, load_swebench_annotations(path), run_id="wave4-refute")
    replay = _LocalReplay()
    outcome = _worker(queue, tmp_path, replay).run_once()
    assert outcome is not None and outcome["complete"] is True
    findings = [
        event
        for event in queue.read_events(limit=1000)
        if event["event_type"] == "qa.finding/v1"
    ]
    assert [event["status"] for event in findings] == ["provisional", "refuted"]
    assert [reward for _, reward in replay.calls] == [1.0, 1.0]


def test_blocked_annotation_job_is_redelivered_and_ingest_stays_idempotent(
    queue: ContinuousWorkQueue, tmp_path: Path
) -> None:
    original = _fixture_task_package(tmp_path / "original", repaired=False)
    repaired = _fixture_task_package(tmp_path / "repaired", repaired=True)
    trial = _fixture_trial(tmp_path / "fail-1", "printf '42' > output.txt\n")
    row = {
        "instance_id": "swe-redeliver",
        "task_dir": str(original),
        "repaired_task": str(repaired),
        "failing_runs": [str(trial)],
        "underspecified": "major",
    }
    path = _write_jsonl(tmp_path / "annotations.jsonl", [row])
    first = ingest_public_corpus(queue, load_swebench_annotations(path), run_id="wave4-redelivery")
    replay = _LocalReplay()
    replay.refusals = ["source_patch_unavailable"]
    worker = _worker(queue, tmp_path, replay)
    outcome = worker.run_once()
    assert outcome is not None and outcome["complete"] is False
    assert queue.queue_snapshot()["jobs"]["queued"] == 1
    replay.refusals = []
    outcome = worker.run_once()
    assert outcome is not None and outcome["complete"] is True
    findings = [
        event
        for event in queue.read_events(limit=1000)
        if event["event_type"] == "qa.finding/v1"
    ]
    assert findings[-1]["status"] == "confirmed"
    # The released job redelivered the same finding identities deterministically.
    assert findings[2]["payload"]["finding_id"] == findings[0]["payload"]["finding_id"]
    assert queue.queue_snapshot()["jobs"] == {"queued": 0, "leased": 0, "completed": 1}
    again = ingest_public_corpus(queue, load_swebench_annotations(path), run_id="wave4-redelivery")
    assert [event["event_id"] for event in again["enqueued"]] == [
        event["event_id"] for event in first["enqueued"]
    ]


def test_sealed_identities_keep_annotation_rows_out_of_the_queue(
    queue: ContinuousWorkQueue, tmp_path: Path
) -> None:
    path = _write_jsonl(
        tmp_path / "annotations.jsonl",
        [
            {"instance_id": "sw-sealed-item", "item_id": "finding-test", "underspecified": "major"},
            {
                "instance_id": "sw-sealed-task",
                "task_id": "terminal-bench/sealed-task",
                "underspecified": "major",
            },
        ],
    )
    result = ingest_public_corpus(queue, load_swebench_annotations(path), run_id="sealed")
    assert result["enqueued"] == []
    assert {ex["reason"] for ex in result["excluded"]} == {"sealed_test_split"}
    assert {ex["source_id"] for ex in result["excluded"]} == {"sw-sealed-item", "sw-sealed-task"}
    assert queue.read_events() == queue.pending_outbox() == []


def test_loader_propagates_task_dir_without_fabricating_locators(tmp_path: Path) -> None:
    row = _swebench_row(
        "sw-dir",
        "cheat-dev",
        task_package=None,
        repaired_task=None,
        failing_runs=None,
        task_dir="/pkgs/materialized",
    )
    path = _write_jsonl(tmp_path / "annotations.jsonl", [row])
    (job,) = list(load_swebench_annotations(path))
    assert job.job_type == ANNOTATION_JOB
    assert job.payload["task_dir"] == "/pkgs/materialized"
    assert "task_package" not in job.payload
    assert "repaired_task" not in job.payload
    assert job.payload["annotation"]["underspecified"] == "major"


def test_annotation_rationale_and_annotator_metadata_survive_end_to_end(
    queue: ContinuousWorkQueue, tmp_path: Path
) -> None:
    """Human rationale and annotator fields reach the finding verbatim."""
    row = {
        "instance_id": "django__django-19999",
        "underspecified": "major",
        "rationale": "The grader rejects answers the instruction never excludes.",
        "annotator": "human-7",
        "annotator_id": "ann-7",
    }
    path = _write_jsonl(tmp_path / "annotations.jsonl", [row])
    (job,) = list(load_swebench_annotations(path))
    assert job.job_type == ANNOTATION_JOB
    assert job.payload["annotation"]["rationale"] == (
        "The grader rejects answers the instruction never excludes."
    )
    assert job.payload["annotation"]["annotator"] == "human-7"
    assert job.payload["annotation"]["annotator_id"] == "ann-7"
    ingest_public_corpus(queue, [job], run_id="wave4-rationale")
    backend = _AnnotationBackend()
    outcome = _worker(queue, tmp_path, _LocalReplay(), backend=backend).run_once()
    assert outcome is not None and outcome["complete"] is True
    events = queue.read_events(limit=1000)
    findings = [event for event in events if event["event_type"] == "qa.finding/v1"]
    assert [event["status"] for event in findings] == ["provisional"]
    payload = findings[0]["payload"]
    assert payload["execution_available"] is False
    assert payload["annotation"]["rationale"] == (
        "The grader rejects answers the instruction never excludes."
    )
    assert payload["finding"]["summary"] == (
        "The grader rejects answers the instruction never excludes."
    )
    assert payload["finding"]["severity"] == "high"
    # The default no-package path never resolves, scans, or reads a package.
    assert backend.calls == []
    assert not any(event["event_type"] == "asset.verified" for event in events)


def test_runnable_annotation_task_package_executes_and_confirms(
    queue: ContinuousWorkQueue, tmp_path: Path
) -> None:
    """A ``task_package`` (not ``task_dir``) row stays annotation-routed."""
    original = _fixture_task_package(tmp_path / "original", repaired=False)
    repaired = _fixture_task_package(tmp_path / "repaired", repaired=True)
    trial = _fixture_trial(tmp_path / "fail-1", "printf '42' > output.txt\n")
    row = {
        "instance_id": "swe-pkg-1",
        "task_package": str(original),
        "repaired_task": str(repaired),
        "failing_runs": [str(trial)],
        "underspecified": "major",
        "rationale": "Verifier demands an unstated trailing newline.",
    }
    path = _write_jsonl(tmp_path / "annotations.jsonl", [row])
    (job,) = list(load_swebench_annotations(path))
    assert job.job_type == ANNOTATION_JOB
    assert job.payload["packaging_state"] == "execution_ready"
    ingest_public_corpus(queue, [job], run_id="wave4-package")
    replay = _LocalReplay()
    outcome = _worker(queue, tmp_path, replay).run_once()
    assert outcome is not None and outcome["complete"] is True
    events = queue.read_events(limit=1000)
    findings = [event for event in events if event["event_type"] == "qa.finding/v1"]
    assert [event["status"] for event in findings] == ["provisional", "confirmed"]
    executed = findings[-1]["payload"]
    assert executed["execution_available"] is True
    assert executed["annotation"]["rationale"] == (
        "Verifier demands an unstated trailing newline."
    )
    assert executed["finding"]["severity"] == "high"
    assert executed["finding"]["summary"] == (
        "Verifier demands an unstated trailing newline."
    )
    assert executed["finding"]["refutation"]["status"] == "verified"
    assert [reward for _, reward in replay.calls] == [0.0, 1.0]
    assert not any(event["event_type"] == "asset.verified" for event in events)


def test_all_negative_annotation_records_no_defect_end_to_end(
    queue: ContinuousWorkQueue, tmp_path: Path
) -> None:
    """Explicit negative axes never invent a defect, severity, or root cause."""
    row = {
        "instance_id": "swe-negative-1",
        "underspecified": "none",
        "unfair_tests": "none",
        "severity": "none",
    }
    path = _write_jsonl(tmp_path / "annotations.jsonl", [row])
    (job,) = list(load_swebench_annotations(path))
    assert job.job_type == ANNOTATION_JOB
    assert job.payload["annotation"]["severity"] == "none"
    ingest_public_corpus(queue, [job], run_id="wave4-negative")
    backend = _AnnotationBackend()
    outcome = _worker(queue, tmp_path, _LocalReplay(), backend=backend).run_once()
    assert outcome is not None and outcome["complete"] is True
    events = queue.read_events(limit=1000)
    findings = [event for event in events if event["event_type"] == "qa.finding/v1"]
    assert [event["status"] for event in findings] == ["provisional"]
    finding = findings[0]["payload"]["finding"]
    assert finding["severity"] == "low"
    assert finding["root_cause"] == []
    assert finding["summary"] == "Human annotation asserts no task defect"
    assert "unfair failure" not in (finding["summary"] or "")
    assert backend.calls == []
    assert not any(event["event_type"] == "asset.verified" for event in events)
