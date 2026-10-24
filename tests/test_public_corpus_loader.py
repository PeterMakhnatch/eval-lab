"""Streaming public-corpus loaders and registry-admitted durable ingestion.

The synthetic corpus mirrors ``test_corpus_queue``'s sealed-split matrix: sealed
task names and digests follow items across datasets and aliases, so a misleading
``"split": "dev"`` label in an external export can never unlock TEST work.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

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


def test_swebench_annotations_yield_audit_and_annotation_jobs(tmp_path: Path) -> None:
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
        ("sw-1", AUDIT_JOB),
        ("sw-no-runs", ANNOTATION_JOB),
        ("sw-no-package", ANNOTATION_JOB),
        ("sw-exploit", AUDIT_JOB),
    ]
    first, no_runs, no_package, exploit = jobs
    assert first.payload["license"] == "MIT"
    assert first.payload["dataset_id"] == "cheatbench-actions-v1"
    assert first.payload["annotation"]["underspecified"] == "major"
    assert first.payload["annotation"]["unfair_tests"] == "minor"
    assert first.payload["failing_runs"] == ["/runs/fail-1"]
    assert exploit.payload["exploit_files"] == {"solve.sh": "exit 0"}
    # Annotation-only rows keep severity and declared locators without
    # fabricated packages, and document what downstream execution needs.
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
