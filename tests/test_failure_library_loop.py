"""Offline consumer-visible canary, provenance and missing-stage regressions."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab.failure_library.cli import build_failures_parser
from evallab.failure_library.loop.canary import CommandResult, _score, canary, paired_comparison
from evallab.failure_library.loop.selection import (
    digest,
    freeze,
    load_frozen,
    run_label,
    select_canary,
    verify_snapshot,
)
from evallab.failure_library.records import (
    Evidence,
    Finding,
    RunLabel,
    RunRecord,
    TaskRef,
    make_finding_id,
)
from evallab.failure_library.store import LibraryStore
from evallab.registry import task_directory_digest


@pytest.fixture
def library(tmp_path: Path) -> tuple[Path, Path, LibraryStore]:
    root, state = tmp_path / "repo", tmp_path / "state"
    key_dir = root / "research/answer-keys/tiny"
    key_dir.mkdir(parents=True)
    rows, findings = [], []
    evidence = [
        Evidence(kind="executed", receipt_ref="file:///retained/receipt.json"),
        Evidence(kind="upstream_fix", fix_revision="b" * 40),
        Evidence(kind="human_label", label_source="maintainer review"),
        Evidence(kind="static_finding", note="source check, no execution"),
    ]
    tiers = ["V3_executed", "V2_upstream_fixed", "V1_external_label", "V0_unverified"]
    for index in range(8):
        name = f"task-{index}"
        package = tmp_path / "packages" / name
        package.mkdir(parents=True)
        (package / "instruction.md").write_text("Write the requested output.\n")
        (package / "task.toml").write_text('version = "1.0"\n')
        package_digest = task_directory_digest(package)
        rows.append(
            {
                "schema": "qa.answer_key/v1",
                "key_id": "tiny",
                "item_id": name,
                "subject": {
                    "kind": "task",
                    "task_id": "tiny/" + name,
                    "task_digest": package_digest,
                },
                "expected_verdict": "fail" if index < 4 else "pass",
                "expected_category": "broken" if index < 4 else None,
                "label_source": "pinned-source@" + "a" * 40,
                "label_strength": "human_control",
                "split": "dev",
                "package": {"name": name, "pin": "a" * 40, "root_hint": str(package.parent)},
            }
        )
        if index < 4:
            task = TaskRef(
                source_id="tiny",
                task_id=name,
                upstream_repo="https://example.org/tiny",
                upstream_revision="a" * 40,
                task_version_digest=package_digest,
            )
            findings.append(
                Finding(
                    finding_id=make_finding_id("tiny", name, "a" * 40, "TS01", name),
                    collection="tiny",
                    locator=name,
                    task=task,
                    defect_class="TS01",
                    direction="wrong_pass",
                    summary="The task checks a narrower output contract.",
                    evidence=[evidence[index]],
                    tier=tiers[index],
                    finder="fixture",
                    source_url="https://example.org/issue/" + name,
                    observed_at="2026-10-27",
                    split="DEV",
                )
            )
    (key_dir / "items.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    splits = root / "research/answer-keys/splits"
    splits.mkdir()
    with (splits / "assignments.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "key_id",
                "item_id",
                "task",
                "unit",
                "split",
                "expected_verdict",
                "exposures",
            ),
        )
        writer.writeheader()
        writer.writerows(
            {
                "key_id": "tiny",
                "item_id": row["item_id"],
                "task": row["subject"]["task_id"],
                "unit": row["item_id"],
                "split": "dev",
                "expected_verdict": row["expected_verdict"],
            }
            for row in rows
        )
    store = LibraryStore.for_repo(root, state_root=state)
    store.write_findings("tiny", findings)
    runs = []
    for index, (label, kind) in enumerate(
        (
            ("reward_hacked", "executed"),
            ("clean", "human"),
            ("reward_hacked", "judge"),
            ("clean", "construction"),
        )
    ):
        runs.append(
            RunRecord(
                run_id=f"run-{index}",
                collection="tiny-runs",
                task=TaskRef(
                    source_id="tiny",
                    task_id=f"run-task-{index}",
                    upstream_repo="https://example.org/tiny",
                    upstream_revision="a" * 40,
                ),
                model="fixture",
                harness="recorded",
                reward=1,
                outcome="pass",
                labels=[RunLabel(label=label, kind=kind, source="pinned-source")],
                transcript_ref=f"file:///retained/source.jsonl#line={index + 1}&field=transcript",
                split="DEV",
            )
        )
    store.write_runs("tiny-runs", runs, source_revision="a" * 40)
    return root, state, store


def test_deterministic_stratified_labels_and_digests(
    library: tuple[Path, Path, LibraryStore],
) -> None:
    root, _, store = library
    one = select_canary(root, store, tasks=4, runs=2)
    assert one == select_canary(root, store, tasks=4, runs=2)
    assert one["counts"] == {"broken_tasks": 4, "clean_tasks": 4, "hacked_runs": 1, "clean_runs": 1}
    assert set(one["task_tiers"]) == {
        "V3_executed",
        "V2_upstream_fixed",
        "V1_external_label",
        "V0_unverified",
    }
    assert all(row["record_sha256"].startswith("sha256:") for row in one["tasks"] + one["runs"])
    assert all(row.get("matched_to") for row in one["tasks"] if row["expected"] == "clean")
    assert one["slice_sha256"] == digest({k: v for k, v in one.items() if k != "slice_sha256"})
    verify_snapshot(root, store, one)


def test_strict_cross_key_non_dev_veto(library: tuple[Path, Path, LibraryStore]) -> None:
    root, _, store = library
    path = root / "research/answer-keys/splits/assignments.csv"
    with path.open("a") as stream:
        stream.write("other,held-out-task,other/task-0,task-0,test,fail,\n")
    selected = select_canary(root, store, tasks=4, runs=2)
    assert not any(row["task"]["task_id"] == "task-0" for row in selected["tasks"])
    assert selected["counts"]["broken_tasks"] == 3


def test_frozen_slice_does_not_reroll(
    library: tuple[Path, Path, LibraryStore], tmp_path: Path
) -> None:
    root, _, store = library
    first = select_canary(root, store, tasks=2, runs=2)
    path = tmp_path / "slice.json"
    assert freeze(path, first) == first
    assert freeze(path, select_canary(root, store, tasks=4, runs=2)) == first
    tampered = json.loads(path.read_text())
    tampered["runs"][0]["expected"] = "clean"
    path.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="digest"):
        load_frozen(path)


def test_frozen_snapshot_refuses_record_and_split_drift(
    library: tuple[Path, Path, LibraryStore],
) -> None:
    root, _, store = library
    selected = select_canary(root, store, tasks=4, runs=2)
    changed = list(store.iter_findings())
    changed[0].summary = "A materially different claim."
    store.write_findings("tiny", changed)
    with pytest.raises(ValueError, match="frozen finding bytes changed"):
        verify_snapshot(root, store, selected)


def test_run_rewards_and_judge_labels_are_not_hack_ground_truth(
    library: tuple[Path, Path, LibraryStore],
) -> None:
    _, _, store = library
    runs = list(store.iter_runs())
    assert [run_label(run) for run in runs] == ["hacked", "clean", None, None]
    runs[0].labels.append(RunLabel(label="clean", kind="human", source="conflicting adjudication"))
    assert run_label(runs[0]) is None


def test_run_bytes_must_match_committed_manifest(library: tuple[Path, Path, LibraryStore]) -> None:
    root, _, store = library
    with store.runs_path("tiny-runs").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="differs from manifest"):
        select_canary(root, store)


def test_dry_run_is_offline_and_write_free(library: tuple[Path, Path, LibraryStore]) -> None:
    root, state, _ = library

    def forbidden(*_: object) -> CommandResult:
        pytest.fail("dry-run started an external command")

    before = {p.relative_to(root) for p in root.rglob("*")}
    result = canary(root, state_root=state, dry_run=True, runner=forbidden)
    assert result["dry_run"] is True
    assert result["metered_api_usd"] == result["compute_usd"] == 0
    assert result["scheduler_installed"] is False
    assert len(result["execution_items"]["tasks"]) == 2
    assert {p.relative_to(root) for p in root.rglob("*")} == before
    assert not (state / "loop").exists()


def test_parser_registers_public_command() -> None:
    parser = argparse.ArgumentParser()
    build_failures_parser(parser.add_subparsers())
    args = parser.parse_args(["failures", "loop", "canary", "--dry-run", "--max-items", "2"])
    assert args.dry_run is True
    assert args.max_items == 2
    assert callable(args.func)


def test_missing_measurements_are_blank_not_zero() -> None:
    score = _score(
        "qa-task",
        True,
        [
            {
                "expected": "broken",
                "reviewed": False,
                "flagged": None,
                "tokens": None,
                "cost_usd": None,
                "wall_seconds": 1.5,
                "reason": "quota_stop",
            }
        ],
        "fixed",
    )
    assert score["recall"] is None
    assert score["false_alarms"] is None
    assert score["cost_usd"] is None
    assert score["tokens"] is None
    assert score["status"] == "failed"
    assert score["reviewed"] == 0


def test_real_mode_records_failed_cli_stages_without_fabricating_scores(
    library: tuple[Path, Path, LibraryStore],
) -> None:
    root, state, store = library
    commands: list[list[str]] = []

    def quota_stopped(argv: list[str], _root: Path, _timeout: float) -> CommandResult:
        commands.append(argv)
        if "pack" in argv:
            directory = Path(argv[argv.index("--output-dir") + 1])
            directory.mkdir(parents=True)
            (directory / "rubric.toml").write_text(
                '[[criteria]]\nname="functional_verification"\nguidance="Check ordinary required outputs."\n'
            )
            (directory / "pack.manifest.json").write_text(
                json.dumps(
                    {
                        "status": "ready",
                        "exclusions": [
                            argv[n + 1] for n, value in enumerate(argv) if value == "--exclude-id"
                        ],
                        "excluded_tasks": [
                            argv[n + 1] for n, value in enumerate(argv) if value == "--exclude-task"
                        ],
                        "files": {
                            "rubric.toml": {
                                "sha256": hashlib.sha256(
                                    (directory / "rubric.toml").read_bytes()
                                ).hexdigest()
                            }
                        },
                    }
                )
            )
        elif "task" in argv:
            directory = Path(argv[argv.index("--run-dir") + 1]) / "tasks/pinned"
            directory.mkdir(parents=True)
            (directory / "report.json").write_text(
                json.dumps(
                    {
                        "checks": {
                            f"rubric-sample-{n}": {
                                "status": "not_run",
                                "reason": "subscription quota gate",
                            }
                            for n in (1, 2, 3)
                        },
                        "cost_usd": 0.0,
                        "model_tokens": 0,
                        "signals": [],
                    }
                )
            )
        return CommandResult(0, "{}", "", 0.25)

    result = canary(
        root, state_root=state, runner=quota_stopped, now=datetime(2026, 10, 27, tzinfo=UTC)
    )
    assert result["dry_run"] is False
    assert result["status"] == "failed"
    assert result["improvement_proven"] is False
    assert result["stages"]["qa-task-off"]["reviewed"] == 0
    assert "quota gate" in result["stages"]["qa-task-on"]["reason"]
    assert result["stages"]["seeker-off"]["status"] == "failed"
    assert result["stages"]["seeker-off"]["selected"] == 1
    assert len(result["execution_items"]["seeker_tasks"]) == 1
    assert result["spend"]["metered_api_usd"] == 0
    with (root / "research/failure-library/loop/scoreboard.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 6
    assert all(row["recall"] == "" for row in rows)
    assert all("--stages" not in cmd or cmd[cmd.index("--stages") + 1] == "S1" for cmd in commands)
    assert all("evallab.seeker" not in cmd for cmd in commands)
    packs = [cmd for cmd in commands if "pack" in cmd]
    assert packs and all("--exclude-id" in cmd for cmd in packs)
    expected_ids = {finding.finding_id for finding in store.iter_findings()} | {"run-0", "run-1"}
    expected_tasks = {f"tiny/task-{index}" for index in range(8)} | {
        "tiny/run-task-0",
        "tiny/run-task-1",
    }
    for command in packs:
        assert {
            command[n + 1] for n, value in enumerate(command) if value == "--exclude-id"
        } == expected_ids
        assert {
            command[n + 1] for n, value in enumerate(command) if value == "--exclude-task"
        } == expected_tasks
    assert (Path(result["receipt_ref"]) / "slice.json").is_file()
    repeated = canary(
        root, state_root=state, runner=quota_stopped, now=datetime(2026, 10, 27, tzinfo=UTC)
    )
    assert repeated["slice_sha256"] == result["slice_sha256"]
    with (root / "research/failure-library/loop/scoreboard.csv").open() as stream:
        history = list(csv.DictReader(stream))
    assert len(history) == 12
    assert (Path(result["receipt_ref"]) / "scores.json").is_file()


def test_public_dry_run_emits_offline_dev_plan(
    library: tuple[Path, Path, LibraryStore], capsys: pytest.CaptureFixture[str]
) -> None:
    root, state, _ = library
    parser = argparse.ArgumentParser()
    build_failures_parser(parser.add_subparsers())
    args = parser.parse_args(
        [
            "failures",
            "loop",
            "canary",
            "--dry-run",
            "--state-root",
            str(state),
        ]
    )
    assert args.func(args, root) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["manifest"]["split"] == "DEV"
    assert result["dry_run"] is True
    assert not (root / "research/failure-library/loop").exists()


def test_frozen_slice_refuses_new_cross_key_exposure_before_model_calls(
    library: tuple[Path, Path, LibraryStore],
) -> None:
    root, state, store = library
    manifest_path = root / "research/failure-library/loop/slice.json"
    selected = select_canary(root, store)
    freeze(manifest_path, selected)
    blocked_task = selected["tasks"][0]["task"]["task_id"]
    path = root / "research/answer-keys/splits/assignments.csv"
    with path.open("a") as stream:
        stream.write(f"other,held-out,{blocked_task},same-family,test,fail,\n")
    commands = []

    def capture(argv: list[str], _root: Path, _timeout: float) -> CommandResult:
        commands.append(argv)
        return CommandResult(0, "[]", "", 0.0)

    result = canary(root, state_root=state, runner=capture)
    assert result["stages"]["provenance"]["status"] == "failed"
    assert "cross-key non-DEV" in result["stages"]["provenance"]["reason"]
    assert not any("task" in argv or "review" in argv or "pack" in argv for argv in commands)


def test_knowledge_delta_requires_matched_completed_subjects() -> None:
    off = [
        {"item_id": "positive", "expected": "broken", "reviewed": True, "flagged": False},
        {"item_id": "control", "expected": "clean", "reviewed": True, "flagged": False},
    ]
    on = [{**row, "flagged": row["expected"] == "broken"} for row in off]
    comparison = paired_comparison(off, on)
    assert comparison["recall_delta_on_minus_off"] == 1.0
    assert comparison["false_alarm_rate_delta_on_minus_off"] == 0.0
    assert comparison["observed_improvement"] is True
    positive_only = paired_comparison(off[:1], on[:1], require_controls=False)
    assert positive_only["observed_improvement"] is True
    assert positive_only["false_alarm_rate_delta_on_minus_off"] is None
    assert positive_only["controls_required"] is False
    assert paired_comparison(off[:1], on[:1])["observed_improvement"] is False
    on[0]["reviewed"] = False
    comparison = paired_comparison(off, on)
    assert comparison["paired_reviewed"] == 1
    assert comparison["recall_delta_on_minus_off"] is None
    assert comparison["observed_improvement"] is False
    assert comparison["status"] == "failed"


def test_controls_deduplicate_identical_task_bytes_across_answer_keys(
    library: tuple[Path, Path, LibraryStore],
) -> None:
    root, _, store = library
    original = root / "research/answer-keys/tiny/items.jsonl"
    control = json.loads(original.read_text().splitlines()[4])
    duplicate = {**control, "key_id": "duplicate"}
    directory = root / "research/answer-keys/duplicate"
    directory.mkdir()
    (directory / "items.jsonl").write_text(json.dumps(duplicate) + "\n")
    with (root / "research/answer-keys/splits/assignments.csv").open("a") as stream:
        stream.write("duplicate,task-4,tiny/task-4,task-4,dev,pass,\n")
    manifest = select_canary(root, store, tasks=4, runs=2)
    controls = [row for row in manifest["tasks"] if row["expected"] == "clean"]
    bindings = {(row["task"]["task_id"], row["task"]["task_version_digest"]) for row in controls}
    assert len(bindings) == len(controls) == 4


def test_original_parquet_control_preserves_row_provenance_without_task_digest(
    library: tuple[Path, Path, LibraryStore],
) -> None:
    root, _, store = library
    task = TaskRef(
        source_id="impossible-swebench",
        task_id="family-x/conflicting",
        upstream_repo="https://huggingface.co/datasets/fjzzq2002/impossible_swebench",
        upstream_revision="a" * 40,
        task_path="data/conflicting-00000-of-00001.parquet#instance_id=family-x",
    )
    template = next(
        finding for finding in store.iter_findings() if finding.tier == "V1_external_label"
    )
    finding = template.model_copy(
        update={
            "finding_id": make_finding_id(
                task.source_id, task.task_id, task.upstream_revision, "TS01", "family-x"
            ),
            "collection": "impossiblebench",
            "locator": "family-x",
            "task": task,
            "evidence": [
                Evidence(kind="construction", note="controlled mutation of the original row")
            ],
        }
    )
    store.write_findings("impossiblebench", [finding])
    raw_row = {
        "instance_id": "family-x",
        "impossible_type": "original",
        "problem_statement": "Fix the bug.",
    }
    original = {
        "task": task.model_copy(
            update={
                "task_id": "family-x/original",
                "task_path": "data/original-00000-of-00001.parquet#instance_id=family-x",
            }
        ),
        "split": "DEV",
        "group": "swe-bench/family-x",
        "locator": "fixture/original/family-x",
        "label": "matched_original_control",
        "label_kind": "construction",
        "label_source": task.upstream_repo
        + "/blob/"
        + task.upstream_revision
        + "/data/original.parquet",
        "raw_row_digest": digest(raw_row),
        "artifact_sha256": "b" * 64,
    }
    manifest = select_canary(root, store, tasks=5, runs=2, originals=[original])
    control = next(
        row for row in manifest["tasks"] if row.get("control_kind") == "impossible_original"
    )
    assert control["task"]["task_version_digest"] is None
    assert control["raw_row_digest"] == digest(raw_row)
    assert control["artifact_sha256"] == "b" * 64
    assert control["label_strength"] == "construction_negative"
    assert control["match_basis"] == "same_original_problem"
    verify_snapshot(root, store, manifest, originals=[original])
    with pytest.raises(ValueError, match="frozen original control changed"):
        verify_snapshot(
            root, store, manifest, originals=[{**original, "raw_row_digest": "sha256:" + "c" * 64}]
        )
    baseline_task = task.model_copy(
        update={
            "source_id": "swe-bench",
            "task_id": "family-x",
            "upstream_repo": "https://example.org/swe-bench",
            "upstream_revision": "d" * 40,
            "task_path": None,
        }
    )
    baseline = template.model_copy(
        update={
            "finding_id": make_finding_id("swe-bench", "family-x", "d" * 40, "TS01", "baseline"),
            "collection": "baseline-defect",
            "locator": "baseline",
            "task": baseline_task,
        }
    )
    store.write_findings("baseline-defect", [baseline])
    with pytest.raises(ValueError, match="no longer an eligible negative"):
        verify_snapshot(root, store, manifest, originals=[original])
    refreshed = select_canary(root, store, tasks=6, runs=2, originals=[original])
    assert not any(row.get("control_kind") == "impossible_original" for row in refreshed["tasks"])
    with (root / "research/answer-keys/splits/assignments.csv").open("a") as stream:
        stream.write("other,family-x,source/family-x,family-x,test,fail,\n")
    with pytest.raises(ValueError, match="cross-key non-DEV"):
        verify_snapshot(root, store, manifest, originals=[original])


def test_dry_run_never_downloads_missing_original_control_cache(
    library: tuple[Path, Path, LibraryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, _ = library
    marker = root / "research/failure-library/findings/impossiblebench.jsonl"
    marker.write_text("")
    source = SimpleNamespace(
        IMPOSSIBLE_PINS={"fixture": {"revision": "a" * 40}},
        impossible_controls=lambda **_: pytest.fail("dry-run attempted raw control fetch"),
    )
    monkeypatch.setattr("evallab.failure_library.loop.selection.import_module", lambda _: source)
    result = canary(
        root,
        state_root=state,
        dry_run=True,
        runner=lambda *_: pytest.fail("dry-run launched an agent"),
    )
    assert result["dry_run"] is True
    assert not (root / "research/failure-library/loop").exists()


def test_tier_coverage_does_not_displace_available_clean_pairs(
    library: tuple[Path, Path, LibraryStore],
) -> None:
    root, _, store = library
    key = root / "research/answer-keys/tiny/items.jsonl"
    rows = [json.loads(line) for line in key.read_text().splitlines()]
    findings = list(store.iter_findings())
    paired = next(finding for finding in findings if finding.tier == "V1_external_label")
    unpaired = next(finding for finding in findings if finding.tier == "V2_upstream_fixed")
    extra = []
    for index in range(4):
        name = f"control-extra-{index}"
        rows.append(
            {
                **rows[4],
                "item_id": name,
                "subject": {**rows[4]["subject"], "task_id": "tiny/" + name},
                "package": {**rows[4]["package"], "name": name},
            }
        )
        with (root / "research/answer-keys/splits/assignments.csv").open("a") as stream:
            stream.write(f"tiny,{name},tiny/{name},{name},dev,pass,\n")
        for template, source in ((paired, "tiny"), (unpaired, "no-control-source")):
            task = template.task.model_copy(
                update={
                    "source_id": source,
                    "task_id": f"extra-{source}-{index}",
                    "task_version_digest": None,
                }
            )
            locator = task.task_id
            extra.append(
                template.model_copy(
                    update={
                        "finding_id": make_finding_id(
                            source,
                            task.task_id,
                            task.upstream_revision,
                            template.defect_class,
                            locator,
                        ),
                        "collection": "extra",
                        "task": task,
                        "locator": locator,
                    }
                )
            )
    key.write_text("".join(json.dumps(row) + "\n" for row in rows))
    store.write_findings("extra", extra)
    manifest = select_canary(root, store, tasks=8, runs=2)
    assert manifest["counts"]["broken_tasks"] == manifest["counts"]["clean_tasks"] == 8
    assert manifest["task_tiers"] == {
        "V3_executed": 1,
        "V2_upstream_fixed": 1,
        "V1_external_label": 5,
        "V0_unverified": 1,
    }


def test_knowledge_on_refuses_unproven_frozen_control_exclusions(
    library: tuple[Path, Path, LibraryStore],
) -> None:
    root, state, _ = library
    commands = []

    def incomplete_pack(argv: list[str], _root: Path, _timeout: float) -> CommandResult:
        commands.append(argv)
        if "pack" in argv:
            directory = Path(argv[argv.index("--output-dir") + 1])
            directory.mkdir(parents=True)
            manifest = {
                "status": "ready",
                "exclusions": [
                    argv[n + 1] for n, value in enumerate(argv) if value == "--exclude-id"
                ],
                "excluded_tasks": [],
                "files": {},
            }
            (directory / "pack.manifest.json").write_text(json.dumps(manifest))
        return CommandResult(0, "{}", "", 0.1)

    result = canary(
        root, state_root=state, runner=incomplete_pack, now=datetime(2026, 10, 27, tzinfo=UTC)
    )
    assert (
        result["stages"]["qa-task-on"]["reason"]
        == "pack manifest omitted frozen task/control exclusions"
    )
    assert len([command for command in commands if "task" in command]) == 2


def test_new_task_byte_defect_invalidates_a_frozen_negative(
    library: tuple[Path, Path, LibraryStore],
) -> None:
    root, _, store = library
    manifest = select_canary(root, store, tasks=4, runs=2)
    control = next(row for row in manifest["tasks"] if row["expected"] == "clean")
    task = TaskRef.model_validate(control["task"])
    template = next(store.iter_findings())
    new_claim = template.model_copy(
        update={
            "finding_id": make_finding_id(
                task.source_id,
                task.task_id,
                task.upstream_revision,
                template.defect_class,
                "new-claim",
            ),
            "collection": "new-claim",
            "locator": "new-claim",
            "task": task,
        }
    )
    store.write_findings("new-claim", [new_claim])
    with pytest.raises(ValueError, match="no longer an eligible negative"):
        verify_snapshot(root, store, manifest)
    refreshed = select_canary(root, store, tasks=5, runs=2)
    assert not any(
        row["expected"] == "clean" and row["task"]["task_id"] == task.task_id
        for row in refreshed["tasks"]
    )
