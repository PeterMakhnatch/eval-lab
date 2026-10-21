"""Real artifact/patch/script application over an inert native runtime boundary.

No daemon, provider, credentials, models, or benchmark answer keys are used.
The controlled scripts and unchanged fixture verifier execute as local child
processes in per-trial scratch directories, never in a caller's source tree.
"""

from __future__ import annotations

import contextlib
import json
import runpy
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab import candidate_runner as runner
from evallab import upstream_grader as native


@pytest.fixture
def task(tmp_path: Path) -> Path:
    root = tmp_path / "native-task"
    for folder in ("environment", "tests", "solution"):
        (root / folder).mkdir(parents=True)
    (root / "task.toml").write_text(
        'schema_version = "1.0"\nartifacts = ["/app/answer.txt"]\n'
        '[task]\nname = "candidate/fixture"\n'
        '[environment]\ncpus = 1\nmemory_mb = 1024\nstorage_mb = 1024\n'
        'build_timeout_sec = 60\ngpus = 0\nworkdir = "/app"\n'
        '[agent]\ntimeout_sec = 600\n'
        '[verifier]\nenvironment_mode = "separate"\ntimeout_sec = 30\n',
        encoding="utf-8",
    )
    (root / "instruction.md").write_text("Write good to /app/answer.txt.\n", encoding="utf-8")
    for folder in ("environment", "tests"):
        (root / folder / "Dockerfile").write_text("FROM python:3.12-slim\nWORKDIR /app\n")
    (root / "environment" / "base-answer.txt").write_text("bad\n")
    (root / "solution" / "solve.sh").write_text("printf 'good\\n' > /app/answer.txt\n")
    (root / "tests" / "test.sh").write_text(
        '#!/bin/bash\nset -eu\n'
        'if [ "$(cat /app/answer.txt 2>/dev/null || true)" = good ]; then\n'
        '  reward=1; outcome=passed\nelse\n  reward=0; outcome=failed\nfi\n'
        'printf "%s\\n" "$reward" > /logs/verifier/reward.txt\n'
        'printf \'{"results":{"tests":[{"name":"answer-content","status":"%s"}]}}\\n\' '
        '"$outcome" > /logs/verifier/ctrf.json\n'
        'printf "verified-%s\\n" "$outcome"\nprintf "verifier-stderr\\n" >&2\n'
    )
    return root


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Keep native staging, parsing, reservations/reconciliation and result files."""
    state = SimpleNamespace(
        calls=[], lock=threading.Lock(), running=0, peak=0, before=None, after=None,
        closure_proven=True, image_hasher=None,
    )
    monkeypatch.setenv("EVALLAB_DOCKER_LOCK_DIR", str(tmp_path / "lane-lock"))
    monkeypatch.setattr(native, "docker_slot", lambda **_: contextlib.nullcontext())
    monkeypatch.setattr(
        native,
        "_admit_docker",
        lambda plan: {"platform": plan["platform"] or "linux/amd64", "emulated": False},
    )

    async def execute(config: dict[str, Any], active: native._Execution) -> dict[str, Any]:
        with state.lock:
            index = len(state.calls)
            state.running += 1
            state.peak = max(state.peak, state.running)
            sandbox = tmp_path / "inert-sandboxes" / str(index)
            call = {"config": config, "sandbox": sandbox, "index": index}
            state.calls.append(call)
        try:
            trial = Path(config["trials_dir"]) / config["trial_name"]
            verifier = trial / "verifier"
            agent = trial / "agent"
            verifier.mkdir(parents=True)
            agent.mkdir()
            (sandbox / "app").mkdir(parents=True)
            staged = Path(config["task"]["path"])
            shutil.copyfile(staged / "environment" / "base-answer.txt", sandbox / "app" / "answer.txt")
            solution = config["agent"]["kwargs"]["qa_solution_dir"]
            shutil.copytree(solution, sandbox / "solution")
            if state.before is not None:
                state.before(call, active)

            def run_script(path: Path) -> subprocess.CompletedProcess[str]:
                command = path.read_text(encoding="utf-8")
                for original, replacement in (
                    ("/logs/verifier", str(verifier)),
                    ("/solution", str(sandbox / "solution")),
                    ("/tests", str(sandbox / "tests")),
                    ("/app", str(sandbox / "app")),
                ):
                    command = command.replace(original, replacement)
                return subprocess.run(
                    ["bash", "-c", command], cwd=sandbox / "app",
                    capture_output=True, text=True, check=False, timeout=5,
                )

            assert not (sandbox / "tests").exists()
            candidate = run_script(sandbox / "solution" / "solve.sh")
            (agent / "oracle.txt").write_text(candidate.stdout + candidate.stderr)
            if candidate.returncode:
                (agent / "exit-code.txt").write_text(str(candidate.returncode))
                payload: dict[str, Any] = {
                    "exception_info": {"exception_message": "candidate installation/script failed"},
                    "verifier_result": None,
                }
            else:
                shutil.copytree(staged / "tests", sandbox / "tests")
                checked = run_script(sandbox / "tests" / "test.sh")
                (verifier / "test-stdout.txt").write_text(checked.stdout + checked.stderr)
                try:
                    rewards = native._read_native_rewards(trial)
                except (OSError, ValueError):
                    rewards = None
                payload = {"exception_info": None, "verifier_result": {"rewards": rewards}}
            (trial / "result.json").write_text(json.dumps(payload))
            active.elapsed_seconds = 1.0
            modal = config["environment"]["import_path"].endswith("UpstreamModalEnvironment")
            for resource in active.plan["resources"]:
                if modal:
                    active.claim(resource["cpus"], resource["memory_mb"])
                    active.environments.append(
                        SimpleNamespace(
                            _upstream_image_build_completed=True,
                            _upstream_termination_proven=state.closure_proven,
                            _upstream_sandbox_id=f"inert-{index}-{resource['role']}",
                            _upstream_exit_code=0,
                        )
                    )
                if state.image_hasher is not None:
                    identity = state.image_hasher(staged / resource["build_context"], docker_image=resource["docker_image"])
                    active.observations.append(
                        {
                            "role": resource["role"],
                            "native_image_identity": {
                                "environment_id": identity,
                                "docker_image_reference": "hb__" + identity if not modal else None,
                                "docker_image_id": "sha256:inert-" + identity if not modal else None,
                                "modal_image_id": "im-inert-" + identity if modal else None,
                                "cache_hit": None,
                            },
                        }
                    )
            if state.after is not None:
                state.after(call, active, payload)
            return payload
        finally:
            with state.lock:
                state.running -= 1

    monkeypatch.setattr(native, "_execute_native", execute)
    return state


def modal_options(tmp_path: Path) -> dict[str, Any]:
    return {
        "backend": "modal", "timeout_seconds": 140,
        "approval": "inert candidate campaign", "max_usd": 10.0,
        "spend_ledger": tmp_path / "campaign-spend.json",
        "modal_app_name": "candidate-test-runner", "billing_tag": "candidate-test",
    }


def test_mapping_installs_real_bytes_and_returns_native_grade_logs_and_tests(
    task: Path, tmp_path: Path, transport: Any
) -> None:
    before = native._tree_digest(task)
    result = runner.run_candidate(
        task, {"answer.txt": b"good\n", "nested/data.bin": b"\x00\xff"},
        backend="docker", output_dir=tmp_path / "mapping",
    )
    assert result["status"] == "measured"
    assert result["reward"] == 1
    assert result["input"]["kind"] == "files"
    assert result["per_test"]["tests"][0]["id"] == "answer-content"
    assert result["per_test"]["tests"][0]["status"] == "passed"
    sandbox = transport.calls[0]["sandbox"]
    assert (sandbox / "app" / "nested" / "data.bin").read_bytes() == b"\x00\xff"
    assert native._tree_digest(task) == before
    assert (task / "environment" / "base-answer.txt").read_text() == "bad\n"
    assert result["native_error"] is None
    assert result["cost"]["actual_invoice_usd"] is None
    assert result["logs"]["agent"]
    assert any(Path(item["path"]).name == "test-stdout.txt" for item in result["logs"]["verifier"])
    assert json.loads((tmp_path / "mapping" / "candidate.json").read_text())["reward"] == 1
    assert not list((tmp_path / "mapping").glob("*.tmp"))


def test_directory_is_artifacts_even_with_solve_script_and_explicit_bundle_is_executable(
    task: Path, tmp_path: Path, transport: Any
) -> None:
    source = tmp_path / "caller-files"
    source.mkdir()
    (source / "empty").mkdir()
    (source / "empty").chmod(0o750)
    (source / "solve.sh").write_text("printf 'good\\n' > /app/answer.txt\nprintf executed > /app/ran\n")
    (source / "solve.sh").chmod(0o750)
    before = native._tree_digest(source)
    artifact = runner.run_candidate(task, source, backend="docker", output_dir=tmp_path / "artifacts")
    executable = runner.run_candidate(
        task, solution_bundle=source, backend="docker", output_dir=tmp_path / "bundle"
    )
    assert [artifact["reward"], executable["reward"]] == [0, 1]
    first, second = [item["sandbox"] for item in transport.calls]
    assert (first / "app" / "solve.sh").read_bytes() == (source / "solve.sh").read_bytes()
    assert (first / "app" / "solve.sh").stat().st_mode & 0o777 == 0o750
    assert (first / "app" / "empty").is_dir()
    assert (first / "app" / "empty").stat().st_mode & 0o777 == 0o750
    assert not (first / "app" / "ran").exists()
    assert (second / "app" / "ran").read_text() == "executed"
    assert native._tree_digest(source) == before
    assert executable["input"]["kind"] == "solution-bundle"


def test_real_unified_patch_is_applied_only_in_fresh_environment(
    task: Path, tmp_path: Path, transport: Any
) -> None:
    patch = tmp_path / "candidate.diff"
    patch.write_text("--- a/answer.txt\n+++ b/answer.txt\n@@ -1 +1 @@\n-bad\n+good\n")
    task_before = native._tree_digest(task)
    patch_before = patch.read_bytes()
    result = runner.run_candidate(task, patch=patch, backend="docker", output_dir=tmp_path / "patch")
    assert result["reward"] == 1
    assert result["input"]["kind"] == "patch"
    assert result["input"]["paths"] == ["answer.txt"]
    assert (transport.calls[0]["sandbox"] / "app" / "answer.txt").read_text() == "good\n"
    assert patch.read_bytes() == patch_before
    assert native._tree_digest(task) == task_before
    assert not (tmp_path / "answer.txt").exists()
    assert (tmp_path / "patch" / "candidate-input" / "candidate.patch").read_bytes() == patch_before


@pytest.mark.parametrize(
    ("patch", "name", "content"),
    [
        ("--- a/new.txt\n+++ b/new.txt\n@@ -0,0 +1 @@\n+new\n", "new.txt", b"new\n"),
        ("--- a/answer.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bad\n", "answer.txt", None),
        ("--- a/answer.txt\n+++ b/answer.txt\n@@ -1 +0,0 @@\n-bad\n", "answer.txt", b""),
        (
            "diff --git a/answer.txt b/answer.txt\nindex aaaa..bbbb 100644\n"
            "--- a/answer.txt\n+++ b/answer.txt\n@@ -1 +1 @@\n-bad\n+good\n",
            "answer.txt",
            b"good\n",
        ),
    ],
)
def test_patch_add_delete_and_git_headers_are_real_unified_operations(
    task: Path, tmp_path: Path, transport: Any, patch: str, name: str, content: bytes | None
) -> None:
    before = native._tree_digest(task)
    result = runner.run_candidate(task, patch=patch, backend="docker", output_dir=tmp_path / "operation")
    assert result["status"] == "measured"
    destination = transport.calls[0]["sandbox"] / "app" / name
    if content is None:
        assert not destination.exists()
    else:
        assert destination.read_bytes() == content
    assert result["reward"] == (1 if content == b"good\n" else 0)
    assert native._tree_digest(task) == before


def test_explicit_patch_deletion_preserves_modified_and_untouched_empty_files(
    task: Path, tmp_path: Path, transport: Any
) -> None:
    def before(call: dict[str, Any], _: Any) -> None:
        workspace = call["sandbox"] / "app"
        (workspace / "remove-me.txt").write_text("remove\n")
        (workspace / "already-empty.txt").write_bytes(b"")

    transport.before = before
    before_source = native._tree_digest(task)
    result = runner.run_candidate(
        task,
        patch=(
            "--- a/remove-me.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-remove\n"
            "--- a/answer.txt\n+++ b/answer.txt\n@@ -1 +0,0 @@\n-bad\n"
        ),
        backend="docker", output_dir=tmp_path / "mixed-deletion",
    )
    workspace = transport.calls[0]["sandbox"] / "app"
    assert result["status"] == "measured"
    assert result["reward"] == 0
    assert not (workspace / "remove-me.txt").exists()
    assert (workspace / "answer.txt").read_bytes() == b""
    assert (workspace / "already-empty.txt").read_bytes() == b""
    assert result["input"]["deletions"] == ["remove-me.txt"]
    assert native._tree_digest(task) == before_source


def test_incomplete_explicit_patch_deletion_does_not_discard_remaining_file_content(
    task: Path, tmp_path: Path, transport: Any
) -> None:
    def before(call: dict[str, Any], _: Any) -> None:
        (call["sandbox"] / "app" / "answer.txt").write_text("bad\nretain this content\n")

    transport.before = before
    result = runner.run_candidate(
        task, patch="--- a/answer.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bad\n",
        backend="docker", output_dir=tmp_path / "incomplete-deletion",
    )
    assert result["status"] == "error"
    assert result["reward"] is result["passed"] is None
    assert "retain this content\n" in (transport.calls[0]["sandbox"] / "app" / "answer.txt").read_text()


@pytest.mark.parametrize("name", ["../answer.txt", "/app/answer.txt", "a/../../answer.txt", "a//b", "a\\b"])
def test_unsafe_artifact_paths_refuse_before_native_allocation(
    task: Path, tmp_path: Path, transport: Any, name: str
) -> None:
    before = native._tree_digest(task)
    result = runner.run_candidate(task, {name: "good"}, backend="docker", output_dir=tmp_path / "unsafe")
    assert result["status"] == "refused"
    assert result["reward"] is result["passed"] is None
    assert result["executed"] is False
    assert transport.calls == []
    assert native._tree_digest(task) == before


@pytest.mark.parametrize(
    ("target", "name"),
    [("/logs", "candidate.txt"), ("//logs", "candidate.txt"), ("/", "logs/candidate.txt")],
)
def test_runtime_evidence_destinations_are_rejected_before_allocation(
    task: Path, tmp_path: Path, transport: Any, target: str, name: str
) -> None:
    result = runner.run_candidate(
        task, {name: "ordinary data"}, target_dir=target,
        backend="docker", output_dir=tmp_path / "protected-destination",
    )
    assert result["status"] == "refused"
    assert result["reward"] is None
    assert transport.calls == []


@pytest.mark.parametrize(
    "patch",
    [
        "--- a/../outside\n+++ b/../outside\n@@ -0,0 +1 @@\n+bad\n",
        "--- /host/answer\n+++ /host/answer\n@@ -1 +1 @@\n-bad\n+good\n",
        "--- a/link\n+++ b/link\n@@ -1 +1 @@\n-bad\n+good\nnew file mode 120000\n",
        "--- a/answer.txt\n+++ b/answer.txt\n@@ -2,2 +2,2 @@\n-bad\n+good\n",
    ],
)
def test_unsafe_or_malformed_patch_never_touches_host_or_allocates(
    task: Path, tmp_path: Path, transport: Any, patch: str
) -> None:
    before = native._tree_digest(task)
    result = runner.run_candidate(task, patch=patch, backend="docker", output_dir=tmp_path / "unsafe-patch")
    assert result["status"] == "refused"
    assert result["reward"] is None
    assert not transport.calls
    assert native._tree_digest(task) == before
    assert not (tmp_path / "outside").exists()


def test_directory_symlinks_and_file_directory_collisions_are_rejected(
    task: Path, tmp_path: Path, transport: Any
) -> None:
    source = tmp_path / "links"
    source.mkdir()
    (source / "link").symlink_to(task / "environment" / "base-answer.txt")
    linked = runner.run_candidate(task, source, backend="docker", output_dir=tmp_path / "linked")
    collision = runner.run_candidate(
        task, {"nested": "file", "nested/answer.txt": "good"}, backend="docker",
        output_dir=tmp_path / "collision",
    )
    assert linked["status"] == collision["status"] == "refused"
    assert "symlink" in linked["error"]
    assert "collision" in collision["error"]
    assert not transport.calls


@pytest.mark.parametrize("location", ["task-child", "task-parent", "candidate-child", "ledger-task", "ledger-evidence"])
def test_evidence_and_ledger_never_mutate_immutable_sources(
    task: Path, tmp_path: Path, transport: Any, location: str
) -> None:
    source = tmp_path / "artifacts-source"
    source.mkdir()
    (source / "answer.txt").write_text("good\n")
    output = task / "evidence" if location == "task-child" else task.parent if location == "task-parent" else source / "run" if location == "candidate-child" else tmp_path / "evidence"
    ledger = task / "spend.json" if location == "ledger-task" else output / "spend.json" if location == "ledger-evidence" else None
    task_before, source_before = native._tree_digest(task), native._tree_digest(source)
    result = runner.run_candidate(task, source, backend="docker", output_dir=output, spend_ledger=ledger)
    assert result["status"] == "refused"
    assert result["output_dir"] is None
    assert not transport.calls
    assert native._tree_digest(task) == task_before
    assert native._tree_digest(source) == source_before
    if ledger is not None:
        assert not ledger.exists()


def test_runtime_destination_symlinks_are_not_followed(
    task: Path, tmp_path: Path, transport: Any
) -> None:
    def add_symlink(call: dict[str, Any], _: Any) -> None:
        outside = call["sandbox"] / "outside"
        outside.mkdir()
        (call["sandbox"] / "app" / "nested").symlink_to(outside, target_is_directory=True)

    transport.before = add_symlink
    result = runner.run_candidate(
        task, {"nested/answer.txt": "good"}, backend="docker", output_dir=tmp_path / "symlink-target"
    )
    assert result["status"] == "error"
    assert result["reward"] is result["passed"] is None
    assert not (transport.calls[0]["sandbox"] / "outside" / "answer.txt").exists()
    assert "symlink" in Path(result["trial_dir"], "agent", "oracle.txt").read_text()


def test_patch_context_failure_is_unmeasured_not_an_invented_zero(
    task: Path, tmp_path: Path, transport: Any
) -> None:
    result = runner.run_candidate(
        task, patch="--- a/answer.txt\n+++ b/answer.txt\n@@ -1 +1 @@\n-not-the-base\n+good\n",
        backend="docker", output_dir=tmp_path / "mismatch",
    )
    assert result["status"] == "error"
    assert result["reward"] is result["rewards"] is result["passed"] is None
    assert result["native_error"]
    assert result["per_test"]["tests"] == []
    assert (transport.calls[0]["sandbox"] / "app" / "answer.txt").read_text() == "bad\n"


def test_unavailable_optional_log_metadata_does_not_erase_a_completed_grade(
    task: Path, tmp_path: Path, transport: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = native._file_digest

    def inaccessible(path: Path) -> str:
        if path.parent.name == "agent" and path.name == "oracle.txt":
            raise PermissionError("inert log metadata unavailable")
        return digest(path)

    monkeypatch.setattr(native, "_file_digest", inaccessible)
    result = runner.run_candidate(
        task, {"answer.txt": "good\n"}, backend="docker", output_dir=tmp_path / "log-metadata"
    )
    assert result["status"] == "measured"
    assert result["reward"] == 1
    assert result["logs"]["agent"][-1]["error"].startswith("agent log inventory unavailable:")


def test_input_drift_invalidates_a_native_observation_without_rewriting_source(
    task: Path, tmp_path: Path, transport: Any
) -> None:
    source = tmp_path / "mutable-source"
    source.mkdir()
    (source / "answer.txt").write_text("good\n")

    def change_source(_: Any, __: Any, ___: Any) -> None:
        (source / "answer.txt").write_text("external writer changed input\n")

    transport.after = change_source
    result = runner.run_candidate(task, source, backend="docker", output_dir=tmp_path / "drift")
    assert result["status"] == "error"
    assert result["reward"] is result["passed"] is None
    assert result["observed_native_rewards"] == {"reward": 1}
    assert "immutable source changed" in result["error"]
    assert (source / "answer.txt").read_text() == "external writer changed input\n"


def test_plan_has_no_runtime_ledger_import_or_filesystem_side_effects(
    task: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_: Any, **__: Any) -> None:
        pytest.fail("candidate plan accessed runtime, ledger, or staging")

    for name in ("_execute_native", "_admit_docker", "_reserve_spend", "_read_ledger", "_stage_inputs", "docker_available_resources"):
        monkeypatch.setattr(native, name, forbidden)
    monkeypatch.setattr(runner, "_stage_payload", forbidden)
    monkeypatch.setattr(runner, "_local_lane", forbidden)
    source_before = native._tree_digest(task)
    modules = set(sys.modules)
    options = modal_options(tmp_path)
    result = runner.run_candidate(
        task, patch="--- a/answer.txt\n+++ b/answer.txt\n@@ -1 +1 @@\n-bad\n+good\n",
        output_dir=tmp_path / "not-created", plan=True, **options,
    )
    assert result["runnable"] is True
    assert result["executed"] is False
    assert result["reserved_usd"] == 0
    assert result["backend"] == "modal"
    assert result["modal_reservation"]["reserved_usd"] > 0
    assert result["images"]["observed"] == []
    assert not (tmp_path / "not-created").exists()
    assert not options["spend_ledger"].exists()
    assert native._tree_digest(task) == source_before
    assert not any(name.startswith(("harbor.", "modal.")) for name in set(sys.modules) - modules)


def test_known_mimo_hosted_judge_is_structurally_plannable_but_unapproved(
    task: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Offline structural contract from the frozen 10b732c5079c general package:
    # s3k_2280_manufacturing_quality_en_t4_rl_003/task.toml. No task solutions.
    (task / "task.toml").write_text(
        'schema_version = "1.4"\n'
        '[task]\nname = "mimo-v2.6-rl/offline-general-contract"\n'
        '[agent]\ntimeout_sec = 3600.0\nuser = "agent"\n'
        '[verifier]\ntimeout_sec = 1200.0\nuser = "root"\n'
        '[verifier.env]\nGA_JUDGE_API = "chat"\nGA_JUDGE_KEY = "${HF_TOKEN}"\n'
        'GA_JUDGE_MODEL = "${MIMO_TEXT_JUDGE:-thinkingmachines/Inkling}"\n'
        'GA_JUDGE_URL = "${MIMO_JUDGE_URL:-https://router.huggingface.co/v1}"\n'
        'VERIFY_AGENT_JUDGE = "1"\nVERIFY_DETERMINISTIC = "1"\n'
        '[environment]\ncpus = 2\nmemory_mb = 8192\nworkdir = "/work/workspace"\n'
        'build_timeout_sec = 1800.0\nnetwork_mode = "public"\n'
        'docker_image = "docker.io/xiaomimimo/mimo-v2.6-rl-oss@sha256:'
        '7b59eff30fb4cef86cc6a4e1c278b119c06c7167807ba30983f0346bc2b5d02e"\n'
        '[environment.healthcheck]\ncommand = "true"\ntimeout_sec = 1200.0\n'
        + "".join(
            f'\n[[environment.mcp_servers]]\nname = "offline-service-{index}"\n'
            f'transport = "streamable-http"\nurl = "http://127.0.0.1:{39100 + index}/mcp"\n'
            for index in range(1, 13)
        )
    )

    def forbidden(*_: Any, **__: Any) -> None:
        pytest.fail("unapproved hosted-judge task reached native staging/allocation")

    monkeypatch.setattr(runner, "_stage_payload", forbidden)
    monkeypatch.setattr(native, "_reserve_spend", forbidden)
    monkeypatch.setattr(native, "_execute_native", forbidden)
    monkeypatch.setattr(native, "_admit_docker", forbidden)
    options = modal_options(tmp_path)
    planned = runner.run_candidate(
        task, {"answer.txt": "good\n"}, plan=True,
        output_dir=tmp_path / "mimo-plan", **options,
    )
    assert planned["runnable"] is False
    assert planned["executed"] is False
    assert planned["reward"] is planned["passed"] is None
    assert planned["resources"][0]["cpus"] == 2
    assert planned["resources"][0]["memory_mb"] == 8192
    assert planned["resources"][0]["storage_mb"] is None
    assert len(planned["resources"]) == 1
    assert planned["external_dependencies"][0]["kind"] == "configured-hosted-judge"
    assert planned["external_dependencies"][0]["approved"] is False
    assert "${HF_TOKEN}" not in json.dumps(planned)
    assert not (tmp_path / "mimo-plan").exists()
    result = runner.run_candidate(
        task, {"answer.txt": "good\n"}, output_dir=tmp_path / "mimo-refusal", **options
    )
    assert result["status"] == "refused"
    assert result["reward"] is result["passed"] is None
    assert result["spend"] is None
    assert "hosted verifier judge" in result["error"]
    assert not options["spend_ledger"].exists()


def test_mimo_without_a_hosted_judge_uses_the_ordinary_native_files_path(
    task: Path, tmp_path: Path, transport: Any
) -> None:
    # Structural shape from the existing clean code variant format-code-task-001948,
    # manifest final digest 90fcc765e6113eeadb806948f5cae2c3422d64c715d864e3e660a64b38694133.
    # The verifier/content remain this test's benign fixture, never benchmark tests.
    config = task / "task.toml"
    config.write_text(
        config.read_text().replace('schema_version = "1.0"', 'schema_version = "1.4"')
        .replace('name = "candidate/fixture"', 'name = "mimo-v2.6-rl/format-code-task-001948"')
        .replace("cpus = 1\nmemory_mb = 1024\nstorage_mb = 1024\n", "cpus = 2\nmemory_mb = 8192\n")
        .replace('workdir = "/app"', 'workdir = "/testbed"')
        .replace(
            "[agent]\n", "[agent]\nnetwork_mode = \"no-network\"\n"
        )
        .replace(
            "[environment]\n",
            '[environment]\ndocker_image = "docker.io/xiaomimimo/mimo-v2.6-rl-oss@sha256:'
            '1e3227656fac8e88daba941da3bd04703b7e4042edba55d40f06e5c4392ed353"\n',
        )
    )
    planned = runner.run_candidate(
        task, {"answer.txt": "good\n"}, plan=True, output_dir=tmp_path / "mimo-code-plan"
    )
    assert planned["runnable"] is True
    assert planned["target_dir"] == "/testbed"
    assert planned["external_dependencies"] == []
    assert not (tmp_path / "mimo-code-plan").exists()
    result = runner.run_candidate(
        task, {"answer.txt": "good\n"}, target_dir="/app",
        backend="docker", output_dir=tmp_path / "mimo-code",
    )
    assert result["reward"] == 1
    assert result["per_test"]["tests"][0]["status"] == "passed"
    assert result["external_dependencies"] == []
    assert len(result["resources"]) == 2
    assert all(item["cpus"] == 2 and item["memory_mb"] == 8192 for item in result["resources"])
    assert all(item["storage_mb"] is None for item in result["resources"])
    assert len(transport.calls) == 1


@pytest.mark.parametrize("invalid_files", [False, True])
def test_batch_global_path_refusal_preserves_one_unmeasured_outcome_per_input(
    task: Path, tmp_path: Path, transport: Any, invalid_files: bool
) -> None:
    before = native._tree_digest(task)
    results = runner.run_candidates(
        [
            {"task": task, "files": "\x00" if invalid_files else {"answer.txt": "good\n"}}
            for _ in range(2)
        ],
        backend="docker", output_dir=task / "not-evidence",
    )
    assert [item["batch_index"] for item in results] == [0, 1]
    assert [item["status"] for item in results] == ["refused", "refused"]
    assert all(item["reward"] is item["passed"] is None for item in results)
    assert all(item["output_dir"] is None for item in results)
    assert not (task / "not-evidence").exists()
    assert native._tree_digest(task) == before
    assert transport.calls == []


def test_batch_orders_actual_outcomes_and_keeps_partial_refusals(
    task: Path, tmp_path: Path, transport: Any
) -> None:
    release = threading.Event()
    completed: list[int] = []

    def before(call: dict[str, Any], _: Any) -> None:
        if call["index"] == 0:
            assert release.wait(5)

    def after(call: dict[str, Any], _: Any, __: Any) -> None:
        completed.append(call["index"])
        if call["index"] == 1:
            release.set()

    transport.before, transport.after = before, after
    requests = [
        {"task": task, "files": {"answer.txt": "good\n"}},
        {"task": task, "files": {"answer.txt": "incorrect\n"}},
        {"task": task, "files": {"../escape": "invalid"}},
        {"task": task, "files": {"answer.txt": "good\n"}},
    ]
    results = runner.run_candidates(
        requests, max_concurrency=2, output_dir=tmp_path / "batch", **modal_options(tmp_path)
    )
    assert [item["batch_index"] for item in results] == [0, 1, 2, 3]
    assert [item["reward"] for item in results] == [1, 0, None, 1]
    assert [item["status"] for item in results] == ["measured", "measured", "refused", "measured"]
    assert completed[0] == 1
    assert transport.peak == 2
    assert len({item["output_dir"] for item in results}) == 4
    checkpoint = json.loads((tmp_path / "batch" / "batch.json").read_text())
    assert checkpoint["results"] == results
    for index, result in enumerate(results):
        assert json.loads((tmp_path / "batch" / "outcomes" / f"{index:05d}.json").read_text()) == result
        assert json.loads(Path(result["output_dir"], "candidate.json").read_text()) == result


def test_local_trials_serialize_even_when_batch_allows_more_workers(
    task: Path, tmp_path: Path, transport: Any
) -> None:
    transport.before = lambda *_: time.sleep(0.03)
    results = runner.run_candidates(
        [{"task": task, "files": {"answer.txt": "good\n"}} for _ in range(4)],
        backend="docker", max_concurrency=4, output_dir=tmp_path / "local-batch",
    )
    assert [item["reward"] for item in results] == [1, 1, 1, 1]
    assert transport.peak == 1
    assert max(len(item["resources"]) for item in results) * transport.peak <= 2
    assert len({call["sandbox"] for call in transport.calls}) == 4


def test_batch_shares_one_real_native_reservation_cap_and_cannot_override_policy(
    task: Path, tmp_path: Path, transport: Any
) -> None:
    reservation = native.plan_upstream_grader(task, blank=True, timeout_seconds=140)["modal_reservation"]["reserved_usd"]
    options = modal_options(tmp_path) | {"max_usd": reservation * 1.1}
    transport.closure_proven = False
    results = runner.run_candidates(
        [
            {"task": task, "files": {"answer.txt": "good\n"}},
            {"task": task, "files": {"answer.txt": "good\n"}},
            {"task": task, "files": {"answer.txt": "good\n"}, "max_usd": 999},
        ],
        max_concurrency=3, output_dir=tmp_path / "capped-batch", **options,
    )
    assert sorted(item["status"] for item in results) == ["measured", "refused", "refused"]
    assert len(transport.calls) == 1
    measured = next(item for item in results if item["status"] == "measured")
    assert measured["reward"] == 1  # uncertain cleanup does not erase a grade
    assert measured["spend"]["reconciliation"]["status"] == "retained-full-reservation"
    assert measured["cost"]["reservation_cap_usd"] == options["max_usd"]
    assert measured["cost"]["actual_invoice_usd"] is None
    assert "batch policy" in results[2]["error"]
    ledger = json.loads(options["spend_ledger"].read_text())
    assert len(ledger["reservations"]) == 1
    assert ledger["reserved_usd"] == pytest.approx(reservation)
    assert ledger["max_usd"] == options["max_usd"]
    identity = ledger["reservations"][0]
    assert identity["modal_app_name"] == options["modal_app_name"]
    assert identity["billing_tag"] == options["billing_tag"]
    for item in results:
        if item["status"] != "measured":
            assert item["reward"] is item["passed"] is None


def test_candidate_bytes_do_not_change_actual_native_environment_identity(
    task: Path, tmp_path: Path, transport: Any
) -> None:
    definition = pytest.importorskip("harbor.environments.definition")
    transport.image_hasher = definition.environment_content_hash
    before = native._tree_digest(task)
    first = runner.run_candidate(task, {"answer.txt": "good\n"}, backend="docker", output_dir=tmp_path / "image-one")
    second = runner.run_candidate(task, {"answer.txt": "wrong\n"}, backend="docker", output_dir=tmp_path / "image-two")
    assert [first["reward"], second["reward"]] == [1, 0]
    assert first["input"]["digest"] != second["input"]["digest"]
    assert first["images"]["source_contexts"] == second["images"]["source_contexts"]
    assert first["images"]["observed"] == second["images"]["observed"]
    for item in first["images"]["observed"]:
        folder = "environment" if item["role"] == "agent" else "tests"
        expected = definition.environment_content_hash(task / folder)
        assert item["environment_id"] == expected
        assert item["docker_image_reference"] == "hb__" + expected
        assert item["cache_hit"] is None
    assert native._tree_digest(task) == before
    (task / "tests" / "extra-native-data.txt").write_text("new fixed scorer context\n")
    third = runner.run_candidate(task, {"answer.txt": "good\n"}, backend="docker", output_dir=tmp_path / "image-three")
    assert first["images"]["observed"][0] == third["images"]["observed"][0]
    assert first["images"]["observed"][1]["environment_id"] != third["images"]["observed"][1]["environment_id"]


def test_interrupted_batch_keeps_completed_receipts_and_marks_pending_work_unstarted(
    task: Path, tmp_path: Path, transport: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    second_started = threading.Event()
    release = threading.Event()
    root = tmp_path / "interrupted"

    def before(call: dict[str, Any], _: Any) -> None:
        if call["index"] == 1:
            second_started.set()
            assert release.wait(5)

    class ReleasingExecutor(ThreadPoolExecutor):
        def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
            release.set()
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    def interrupt_after_first(futures: Any) -> Any:
        first = next(iter(futures))
        first.result(timeout=5)
        assert json.loads((root / "outcomes" / "00000.json").read_text())["reward"] == 1
        assert second_started.wait(5)
        raise KeyboardInterrupt

    transport.before = before
    monkeypatch.setattr(runner, "ThreadPoolExecutor", ReleasingExecutor)
    monkeypatch.setattr(runner, "as_completed", interrupt_after_first)
    results = runner.run_candidates(
        [{"task": task, "files": {"answer.txt": "good\n"}} for _ in range(3)],
        backend="docker", max_concurrency=1, output_dir=root,
    )
    assert [item["status"] for item in results] == ["measured", "measured", "unstarted"]
    assert [item["reward"] for item in results] == [1, 1, None]
    assert len(transport.calls) == 2
    assert json.loads((root / "batch.json").read_text())["results"] == results


def test_port_interface_prepares_full_native_scorer_and_keeps_both_exact_identities(
    task: Path, tmp_path: Path, transport: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab import candidate_port

    port = tmp_path / "fixed-source"
    (port / "tests").mkdir(parents=True)
    for name in ("Dockerfile", "test.sh"):
        shutil.copyfile(task / "tests" / name, port / "tests" / name)
    script = port / "tests" / "test.sh"
    script.write_text(script.read_text().replace(" = good", " = fixed"))
    prepared_paths = []

    def describe(upstream_task: Path | str, port_task: Path | str) -> dict[str, Any]:
        original = native.plan_upstream_grader(upstream_task, blank=True)
        return {
            "task_dir": str(upstream_task), "upstream_task": str(upstream_task),
            "port_task": str(port_task), "grader_digest": native._tree_digest(port / "tests"),
            "upstream_identity": original["task"],
            "port_identity": {"path": str(port), "exact_tree_digest": native._tree_digest(port)},
            "resources": original["resources"],
        }

    def prepare(upstream_task: Path | str, port_task: Path | str, output_dir: Path | str) -> dict[str, Any]:
        destination = Path(output_dir) / "task" / Path(upstream_task).name
        shutil.copytree(upstream_task, destination)
        shutil.rmtree(destination / "tests")
        shutil.copytree(port / "tests", destination / "tests")
        prepared_paths.append(destination)
        return describe(upstream_task, port_task) | {"task_dir": str(destination)}

    monkeypatch.setattr(candidate_port, "describe_port_task", describe)
    monkeypatch.setattr(candidate_port, "prepare_port_task", prepare)
    options = {"grader": "port", "port_task": port, "backend": "docker"}
    planned = runner.run_candidate(task, {"answer.txt": "good\n"}, plan=True, output_dir=tmp_path / "port-plan", **options)
    assert prepared_paths == []
    assert planned["images"]["pending_port_preparation"] is True
    assert planned["modal_reservation"] is None
    assert not (tmp_path / "port-plan").exists()
    original = runner.run_candidate(task, {"answer.txt": "good\n"}, backend="docker", output_dir=tmp_path / "original")
    fixed = runner.run_candidate(task, {"answer.txt": "good\n"}, output_dir=tmp_path / "fixed", **options)
    assert [original["reward"], fixed["reward"]] == [1, 0]
    assert fixed["per_test"]["tests"][0]["status"] == "failed"
    provenance = fixed["grader_provenance"]
    assert provenance["upstream_identity"]["exact_tree_digest"] == native._tree_digest(task)
    assert provenance["port_identity"]["exact_tree_digest"] == native._tree_digest(port)
    assert provenance["grader_digest"] == native._tree_digest(port / "tests")
    assert prepared_paths[0].name == task.name
    assert original["images"]["source_contexts"][0] == fixed["images"]["source_contexts"][0]
    assert original["images"]["source_contexts"][1] != fixed["images"]["source_contexts"][1]


def test_module_cli_emits_one_json_value_and_installs_real_artifact_content(
    task: Path, tmp_path: Path, transport: Any, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mapping = tmp_path / "files.json"
    mapping.write_text(json.dumps({"answer.txt": "good\n"}))
    monkeypatch.setattr(
        sys, "argv",
        ["evallab.candidate_runner", str(task), "--files-json", str(mapping),
         "--backend", "docker", "--output-dir", str(tmp_path / "module-cli")],
    )
    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("evallab.candidate_runner", run_name="__main__", alter_sys=True)
    result = json.loads(capsys.readouterr().out)
    assert stopped.value.code == 0
    assert result["reward"] == 1
    assert result["per_test"]["tests"][0]["status"] == "passed"
    assert len(transport.calls) == 1


def test_cli_batch_json_uses_one_policy_and_keeps_refusal_outcomes(
    task: Path, tmp_path: Path, transport: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    batch = tmp_path / "batch-requests.json"
    batch.write_text(json.dumps([
        {"task": str(task), "files": {"answer.txt": "good\n"}},
        {"task": str(task), "files": {"../escape": "bad"}},
    ]))
    code = runner.main(["--batch", str(batch), "--backend", "docker", "--output-dir", str(tmp_path / "cli-batch")])
    results = json.loads(capsys.readouterr().out)
    assert code == 2
    assert [item["reward"] for item in results] == [1, None]
    assert [item["batch_index"] for item in results] == [0, 1]
