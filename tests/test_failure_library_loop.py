"""Read-only review provenance and source-label regressions."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

import pytest

from evallab.failure_library.cli import build_failures_parser
from evallab.failure_library.loop.selection import digest, run_label
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


def test_run_rewards_and_judge_labels_are_not_hack_ground_truth(
    library: tuple[Path, Path, LibraryStore],
) -> None:
    _, _, store = library
    runs = list(store.iter_runs())
    assert [run_label(run) for run in runs] == ["hacked", "clean", None, None]
    runs[0].labels.append(RunLabel(label="clean", kind="human", source="conflicting adjudication"))
    assert run_label(runs[0]) is None


@pytest.mark.parametrize(
    "raw",
    [
        '{"decision":{"bad":[]},"evidence":[]}',
        '{"decision":"flag","evidence":["D1:L999"]}',
        '{"decision":"flag","evidence":["D1:L1"],"action":"execute"}',
        '{"decision":"flag","evidence":[]}',
    ],
)
def test_readonly_invalid_outputs_are_unmeasured_and_never_retried(raw: str) -> None:
    from evallab.failure_library.loop.readonly import review_once, source_view

    class Driver:
        calls = 0

        def turn(self, prompt: str, *, timeout_seconds: float) -> dict:
            self.calls += 1
            return {"response": raw}

    driver = Driver()
    result = review_once(driver, source_view(["Quoted source, not an instruction."]), kind="trace")
    assert driver.calls == 1
    assert result["flagged"] is None
    assert not result["reviewed"]
    assert not result["independent_verification"]


def test_public_readonly_dryrun_uses_private_views_and_never_calls_model(
    library: tuple[Path, Path, LibraryStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from evallab.failure_library.loop.readonly import source_view

    root, _, store = library
    state = tmp_path / "pilot-state"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("EVALLAB_FAILURE_LIBRARY_STATE", str(state))
    source = (
        tmp_path
        / "Developer/eval-lab/derived/kev/raw/terminal-bench-v4.0.0/tasks/sample/instruction.md"
    )
    source.parent.mkdir(parents=True)
    source.write_text("Write the requested output.\n")
    documents = [source.read_text()]
    private = state / "loop/views/S1.json"
    private.parent.mkdir(parents=True)
    private.write_text(json.dumps(documents))
    record = next(store.iter_findings()).model_dump(mode="json")
    record["collection"] = "pm-tb4-findings"
    record["summary"] = "PRIVATE_SOURCE_LABEL_DO_NOT_PROMPT"
    record["task"]["task_version_digest"] = task_directory_digest(source.parent)
    store.write_findings("pm-tb4-findings", [Finding.model_validate(record)])
    subject = {
        "id": "S1",
        "kind": "task",
        "view_extractor": "task-files/v1",
        "package_path": str(source.parent),
        "record": record,
        "record_digest": digest(record),
        "documents_ref": "loop/views/S1.json",
        "documents_digest": digest(documents),
        "documents_file_sha256": hashlib.sha256(private.read_bytes()).hexdigest(),
        "view_sha256": source_view(documents)["sha256"],
        "sources": [
            {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
        ],
    }
    frozen = {"schema": "failure-library.readonly-pilot/v1", "subjects": [subject]}
    frozen["digest"] = digest(frozen)
    manifest = tmp_path / "pilot.json"
    manifest.write_text(json.dumps(frozen))
    packet = tmp_path / "packet"
    packet.mkdir()
    guidance = "Defensive taxonomy: require evidence; labels are not independent verification."
    (packet / "pack.md").write_text(guidance)
    metadata = {
        "retrieval_mode": "curated_only",
        "example_ids": [],
        "card_ids": [],
        "curated_guidance_ids": ["FG-TS01"],
        "files": {"pack.md": {"sha256": hashlib.sha256(guidance.encode()).hexdigest()}},
    }
    (packet / "pack.manifest.json").write_text(json.dumps(metadata))
    output = tmp_path / "must-not-exist"
    parser = argparse.ArgumentParser()
    build_failures_parser(parser.add_subparsers())
    args = parser.parse_args(
        [
            "failures",
            "loop",
            "readonly-pilot",
            "--dry-run",
            "--lane",
            "omp",
            "--model",
            "meta/muse-wonder-1.0-contributor",
            "--manifest",
            str(manifest),
            "--pack-dir",
            str(packet),
            "--output-dir",
            str(output),
            "--allow-curated-fallback",
        ]
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("offline public pilot started a child/model command")

    monkeypatch.setattr("subprocess.run", forbidden)
    assert args.func(args, root=root) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["status"] == "planned"
    assert planned["treatment"] == "preexisting_curated_defensive_taxonomy"
    assert planned["requests_planned"] == 2
    assert not output.exists()
    assert not (state / "spend.jsonl").exists()
    assert "PRIVATE_SOURCE_LABEL" not in json.dumps(planned)
    metadata["retrieval_mode"] = "dev_examples"
    metadata["status"] = "ready"
    metadata["example_ids"] = ["another-dev-finding"]
    (packet / "pack.manifest.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="exclusion seal"):
        args.func(args, root=root)


def test_source_strata_do_not_turn_broad_qa_flags_into_hack_accuracy() -> None:
    from evallab.failure_library.loop.readonly import _source_label_counts

    rows = [
        {"expected": True, "result": {"reviewed": True, "flagged": True}},
        {"expected": True, "result": {"reviewed": True, "flagged": False}},
        {"expected": False, "result": {"reviewed": True, "flagged": True}},
        {"expected": False, "result": {"reviewed": False, "flagged": None}},
    ]
    assert _source_label_counts(rows) == {
        "positives_reviewed": 2,
        "controls_reviewed": 1,
        "source_positive_flags": 1,
        "source_negative_flags": 1,
    }
