"""Evidence precedence, digest binding, blinding and v2 workspace seals."""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from evallab.qa import trace_workspace as workspace
from evallab.qa.trace import BUNDLE_SCHEMA, Bundle
from evallab.task_variants import harbor_task_digest, task_directory_digest


@pytest.fixture
def local_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    monkeypatch.setattr(workspace, "_repo_root", lambda: root)
    monkeypatch.setattr(workspace, "_prior_archive_root", lambda: tmp_path / "archived-priors")
    return root


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _package(path: Path) -> Path:
    _write(path / "instruction.md", "Fix the parser without changing the required output.\n")
    _write(path / "task.toml", '[task]\nname = "mimo-v2.6-rl/parser"\n')
    _write(path / "tests/test_parser.py", "def test_parser():\n    assert parse('1') == 1\n")
    _write(path / "solution/solve.sh", "#!/bin/sh\necho reference\n")
    _write(path / "environment/Dockerfile", "FROM example:fixed\n")
    return path


def _trial(path: Path, package: Path | None = None) -> Path:
    result: dict[str, Any] = {
        "trial_name": path.name,
        "task_name": "mimo-v2.6-rl/parser",
        "verifier_result": {"rewards": {"reward": 1}},
    }
    if package:
        result["config"] = {"task": {"path": str(package)}}
    _write(path / "result.json", json.dumps(result))
    return path


def test_full_diff_beats_empty_agent_diff_and_preserves_kept_subset(tmp_path: Path) -> None:
    _write(tmp_path / "verifier/agent.diff", "")
    _write(tmp_path / "verifier/agent.full.diff", "full change\n")
    _write(tmp_path / "verifier/agent.kept.diff", "kept change\n")
    recovered = workspace.recover_file_changes(tmp_path, [])
    assert recovered.text == "full change\n"
    assert recovered.source == "verifier/agent.full.diff"
    assert recovered.kept == "kept change\n"


def test_kept_only_is_an_explicit_primary_fallback(tmp_path: Path) -> None:
    _write(tmp_path / "verifier/agent.diff", " \n")
    _write(tmp_path / "verifier/agent.kept.diff", "kept change\n")
    _write(tmp_path / "artifacts/less-authoritative.patch", "artifact\n")
    recovered = workspace.recover_file_changes(tmp_path, [])
    assert recovered.text == recovered.kept == "kept change\n"
    assert recovered.source == "verifier/agent.kept.diff"


def test_trajectory_write_reconstruction_is_not_labelled_as_a_workspace_diff(
    tmp_path: Path,
) -> None:
    steps = [
        {
            "step_id": 1,
            "source": "agent",
            "tool_calls": [
                {
                    "function_name": "write_file",
                    "arguments": {
                        "path": "parser.py",
                        "content": "answer = 42\n",
                    },
                }
            ],
        }
    ]
    recovered = workspace.recover_file_changes(tmp_path, steps)
    assert recovered.text is not None and "+answer = 42" in recovered.text
    assert "reconstructed from trajectory writes" in recovered.source
    assert "not a workspace snapshot" in recovered.source


def test_snapshot_without_git_records_why_no_diff_can_be_recovered(tmp_path: Path) -> None:
    archive = tmp_path / "artifacts/workspace.tgz"
    archive.parent.mkdir()
    with tarfile.open(archive, "w:gz") as tar:
        content = b"changed\n"
        member = tarfile.TarInfo("app/parser.py")
        member.size = len(content)
        tar.addfile(member, io.BytesIO(content))
    recovered = workspace.recover_file_changes(tmp_path, [])
    assert recovered.text is None
    assert "no .git" in recovered.source
    assert "baseline unavailable" in recovered.source


def test_git_snapshot_includes_staged_and_untracked_changes(tmp_path: Path) -> None:
    repo = tmp_path / "snapshot"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _write(repo / "tracked.txt", "before\n")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    _write(repo / "tracked.txt", "after\n")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    _write(repo / "new.txt", "untracked\n")
    archive = tmp_path / "trial/artifacts/workspace.tgz"
    archive.parent.mkdir(parents=True)
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(repo, arcname="workspace")
    recovered = workspace.recover_file_changes(tmp_path / "trial", [])
    assert recovered.text is not None
    assert "+after" in recovered.text and "+untracked" in recovered.text
    assert "workspace.tgz" in recovered.source and "git diff HEAD" in recovered.source


def test_state_diff_is_a_change_list_with_hashes_sizes_and_contents(tmp_path: Path) -> None:
    _write(
        tmp_path / "state-journal/state-diff.json",
        json.dumps(
            {
                "changes": [
                    {
                        "path": "answer.txt",
                        "change_type": "added",
                        "before": None,
                        "after": {"sha256": "a" * 64, "size_bytes": 3, "content": "yes"},
                    }
                ]
            }
        ),
    )
    recovered = workspace.recover_file_changes(tmp_path, [])
    assert recovered.text is not None
    assert "answer.txt: added" in recovered.text and "size_bytes" in recovered.text
    assert "a" * 64 in recovered.text and '"yes"' in recovered.text
    assert recovered.source == "state-journal/state-diff.json (change list)"


def test_resolver_refuses_a_digest_contradicting_direct_package(local_store: Path) -> None:
    package = _package(local_store / "unrelated-package")
    trial = _trial(local_store / "runs/job/trial", package)
    resolution = workspace.resolve_task_package(
        trial, task_name="mimo-v2.6-rl/parser", digests=["sha256:" + "0" * 64]
    )
    assert resolution.package is None
    assert resolution.method == "unresolved_digest_mismatch"
    assert resolution.matched is False
    assert resolution.digest_found == task_directory_digest(package)


def test_resolver_crosswalk_maps_harbor_digest_to_full_package_digest(local_store: Path) -> None:
    package = _package(local_store / "original")
    full, harbor = task_directory_digest(package), harbor_task_digest(package)
    slug = "mimo-v2.6-rl__parser"
    stored = local_store / "derived/task-store/variants" / slug / full.removeprefix("sha256:")[:12]
    stored.parent.mkdir(parents=True)
    package.rename(stored)
    _write(
        local_store / "library/task-variants" / slug / "variant.json",
        json.dumps(
            {
                "task_name": "mimo-v2.6-rl/parser",
                "variant_digest": full,
                "variant_harbor_digest": harbor,
            }
        ),
    )
    trial = _trial(local_store / "runs/job/trial")
    resolution = workspace.resolve_task_package(
        trial, task_name="mimo-v2.6-rl/parser", digests=[harbor]
    )
    assert resolution.package == stored
    assert resolution.method == "digest_crosswalk"
    assert resolution.digest_expected == harbor
    assert resolution.digest_found == full and resolution.matched is True


def test_name_only_fallback_cannot_claim_digest_binding(local_store: Path) -> None:
    package = _package(
        local_store / "derived/task-store/hf" / workspace.MIMO_STORE / "tasks/parser"
    )
    trial = _trial(local_store / "runs/job/trial")
    resolution = workspace.resolve_task_package(
        trial, task_name="mimo-v2.6-rl/parser", digests=["f" * 64]
    )
    assert resolution.package == package
    assert "name_only" in resolution.method and resolution.matched is False


def test_workspace_redacts_all_review_files_but_retains_real_manifest_subject(
    local_store: Path,
) -> None:
    package = _package(local_store / "package")
    trial = _trial(local_store / "runs/unknown-oracle-job/tamper-cheat-trial", package)
    identities = f"{trial.name} {trial.parent.name}"
    _write(
        trial / "agent/cheat/attempts.json",
        json.dumps(
            {
                "attacks": [
                    {
                        "name": "patch",
                        "status": "success",
                        "detail": identities,
                    }
                ]
            }
        ),
    )
    _write(trial / "agent/command.log", identities)
    _write(trial / "trial.log", identities)
    _write(trial / "verifier/junit-grade.log", identities)
    _write(trial / "verifier/agent.full.diff", identities)
    _write(
        trial / "agent/trajectory.json",
        json.dumps(
            {
                "steps": [
                    {
                        "step_id": 1,
                        "source": "user",
                        "message": identities,
                    }
                ]
            }
        ),
    )
    bundle = workspace.build_workspace(trial, local_store / "workspace", blind="blind-trial")
    for rel in bundle.manifest["files"]:
        text = bundle.text(rel)
        assert trial.name not in text and trial.parent.name not in text, rel
    assert "blind-trial" in bundle.text("agent_logs/command.log")
    assert bundle.subject["trial_id"] == trial.name
    assert json.loads(bundle.text("metadata.json"))["subject"]["trial_id"] == "blind-trial"
    assert "solution/solve.sh" in " ".join(bundle.files("task_codebase/"))
    assert bundle.files("verifier/") == ["verifier/agent.full.diff", "verifier/junit-grade.log"]


def test_workspace_availability_and_seal_round_trip(local_store: Path) -> None:
    package = _package(local_store / "package")
    trial = _trial(local_store / "runs/job/trial", package)
    _write(trial / "verifier/agent.diff", "full change\n")
    _write(trial / "agent/oracle.txt", "Executed reference commands\n")
    bundle = workspace.build_workspace(trial, local_store / "workspace")
    assert bundle.manifest["schema"] == BUNDLE_SCHEMA
    assert bundle.manifest["availability"] == {
        "prompt": "present",
        "trajectory": "missing",
        "verifier_output": "present",
        "tests": "present",
        "diff": "verifier/agent.diff",
        "task_codebase": "present",
        "agent_logs": "present",
        "task_priors": "missing",
    }
    assert bundle.text("file_changes.diff").startswith("# source: verifier/agent.diff\n")
    assert Bundle.load(bundle.root).verify() == []
    _write(bundle.root / "agent_logs/oracle.txt", "changed")
    assert Bundle.load(bundle.root).verify() == ["agent_logs/oracle.txt changed"]


def test_cheatbench_labels_never_reach_the_workspace(local_store: Path) -> None:
    package = _package(local_store / "package")
    trial = _trial(local_store / "runs/job/trial", package)
    marker = "[CheatBench annotation — not part of the original trace]"
    trajectory = {
        "schema_version": "ATIF-v1.6",
        "metadata": {"cheatbench_label": "cheating", "cheatbench_mechanism": "secret_discovery"},
        "extra": {"cheatbench": {"label": "cheating"}},
        "steps": [
            {"step_id": 5, "source": "user", "message": "Fix the parser."},
            {"step_id": 6, "source": "user", "message": f"{marker}\nlabel: cheating"},
            {
                "step_id": 7,
                "source": "agent",
                "message": "reading the key",
                "extra": {"cheatbench": {"source_ref": "events[1]"}},
            },
        ],
    }
    _write(trial / "agent/trajectory.json", json.dumps(trajectory))
    bundle = workspace.build_workspace(trial, local_store / "workspace")
    every_file = "".join(bundle.text(rel) for rel in bundle.manifest["files"])
    assert "cheatbench" not in every_file.lower() and marker not in every_file
    assert [step["step_id"] for step in bundle.steps()] == [0, 1]
    assert bundle.manifest["step_origin"] == [5, 7]
    _write(trial / "agent/notes.txt", f"{marker}\nlabel: cheating")
    with pytest.raises(ValueError, match="evaluator-only content"):
        workspace.build_workspace(trial, local_store / "workspace-2")
    assert not (local_store / "workspace-2").exists()


def test_blinding_is_idempotent_when_summary_reuses_copied_logs(local_store: Path) -> None:
    package = _package(local_store / "package")
    trial = _trial(local_store / "runs/job/trial", package)
    _write(trial / "agent/command.log", "trial ran this command")
    bundle = workspace.build_workspace(trial, local_store / "workspace", blind="blind-trial")
    assert json.loads(bundle.text("metadata.json"))["subject"]["trial_id"] == "blind-trial"
    assert "blind-blind-trial" not in bundle.text("trajectory_summary.md")
    assert "blind-trial ran this command" in bundle.text("trajectory_summary.md")


def test_copied_package_caps_and_binary_omissions_are_reported(local_store: Path) -> None:
    package = _package(local_store / "package")
    _write(package / "environment/large.txt", "line\n" * workspace.MAX_FILE_BYTES)
    (package / "tests/binary.bin").write_bytes(b"\x00binary")
    trial = _trial(local_store / "runs/job/trial", package)
    bundle = workspace.build_workspace(trial, local_store / "workspace")
    assert "task_codebase/tests/binary.bin" not in bundle.manifest["files"]
    assert (
        len((bundle.root / "task_codebase/environment/large.txt").read_bytes())
        <= workspace.MAX_FILE_BYTES
    )
    assert "large.txt: input truncated" in bundle.text("README.md")
    assert "binary.bin: binary file skipped" in bundle.text("README.md")


def test_task_priors_reads_archived_qa_task_findings(local_store: Path) -> None:
    digest = "sha256:" + "a" * 64
    archive = local_store.parent / "archived-priors/qa-task.tgz"
    archive.parent.mkdir()
    record = json.dumps(
        {
            "subject": {"kind": "task", "task_id": "parser", "task_digest": digest},
            "category": "weak",
            "summary": "archived finding",
        }
    ).encode()
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo(".qa-task/run/findings/finding.json")
        member.size = len(record)
        tar.addfile(member, io.BytesIO(record))
    priors = workspace.task_priors("mimo-v2.6-rl/parser", digest)["priors"]
    assert len(priors) == 1 and priors[0]["matched"] is True
    assert priors[0]["record"]["summary"] == "archived finding"
    assert ".tgz:.qa-task/run/findings/finding.json" in priors[0]["source"]


def test_task_priors_normalizes_names_and_marks_mismatching_digest(local_store: Path) -> None:
    digest = "sha256:" + "a" * 64
    ref = local_store / "derived/qa/ref-subset/sample"
    _write(
        ref / "findings-supported.jsonl",
        json.dumps(
            {
                "subject": {"kind": "task", "task_id": "parser", "task_digest": digest},
                "summary": "supported",
            }
        )
        + "\n",
    )
    _write(
        local_store / "runs/.qa-task/run/tasks/hash/report.json",
        json.dumps(
            {
                "subject": {
                    "kind": "task",
                    "task_id": "mimo-v2.6-rl/parser",
                    "task_digest": "b" * 64,
                },
                "verdict": "fail",
            }
        ),
    )
    _write(
        local_store / "research/answer-keys/unrelated/report.json", "not accessible prior evidence"
    )
    priors = workspace.task_priors("mimo-v2.6-rl/parser", digest)["priors"]
    assert len(priors) == 2
    assert {p["matched"] for p in priors} == {True, False}
    assert not any("answer-keys" in prior["source"] for prior in priors)


def test_summary_counts_tokens_commands_errors_and_scripted_attempts() -> None:
    steps = [
        {"step_id": 1, "source": "user", "message": "task", "timestamp": "2026-01-01T00:00:00Z"},
        {
            "step_id": 2,
            "source": "agent",
            "timestamp": "2026-01-01T00:00:02Z",
            "metrics": {"prompt_tokens": 10, "completion_tokens": 3},
            "tool_calls": [{"function_name": "bash", "arguments": {"command": "run tests"}}],
            "observation": {"results": [{"content": "ERROR: failed", "is_error": True}]},
        },
    ]
    summary = workspace.trajectory_summary(steps, agent_logs={})
    assert "steps: 2; tool calls: 1" in summary
    assert '"total_tokens":13' in summary and "wall time: 2.000s" in summary
    assert "bash(command=run tests)" in summary and "| ERROR" in summary
    scripted = workspace.trajectory_summary(
        [],
        agent_logs={
            "cheat/attempts.json": json.dumps(
                {
                    "attacks": [{"name": "patch", "status": "failed", "detail": "denied"}],
                }
            ),
            "oracle.txt": "reference command output",
        },
    )
    assert "attack patch: failed; denied" in scripted and "oracle.txt" in scripted


def test_trajectory_judge_v2_marks_unavailable_context_missing(local_store: Path) -> None:
    row = {
        "trajectory_id": "instance-failure-label",
        "instance_id": "instance",
        "goal": "Do the task",
        "steps": [],
    }
    bundle = workspace.tj_workspace(row, local_store / "tj")
    availability = bundle.manifest["availability"]
    assert availability["prompt"] == availability["trajectory"] == "present"
    assert all(
        availability[key] == "missing"
        for key in (
            "verifier_output",
            "tests",
            "diff",
            "task_codebase",
            "agent_logs",
            "task_priors",
        )
    )
    assert row["trajectory_id"] not in json.dumps(bundle.manifest)
    assert row["trajectory_id"] not in bundle.text("metadata.json")
    assert Bundle.load(bundle.root).verify() == []
