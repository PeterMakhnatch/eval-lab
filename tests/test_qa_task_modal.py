"""Modal QA behavior through native Harbor with an inert, per-sandbox SDK.

No Docker, provider, model, subprocess execution or cloud credentials are needed.
The SDK transports bytes; native Trial owns setup, script agents, collection,
separate verifiers, raw results, fresh isolation and cleanup.
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab.cli import parser as build_root_parser
from evallab.qa import task as qa
from evallab.registry import task_directory_digest
from evallab.regrade import CloudSpendGate

modal_qa: Any = None


@pytest.fixture(autouse=True)
def _require_native_modal():
    global modal_qa
    modal_qa = pytest.importorskip(
        "evallab.qa.task_modal", reason="native Modal regressions require uv sync --extra laminar"
    )


def _package(root: Path, name: str = "modal-task", *, separate: bool = False) -> Path:
    package = root / name
    for directory in ("environment", "tests", "solution"):
        (package / directory).mkdir(parents=True, exist_ok=True)
    (package / "task.toml").write_text(
        'schema_version = "1.0"\nartifacts = ["/app/out.txt"]\n'
        f'[task]\nname = "bench/{name}"\n'
        '[environment]\ndocker_image = "inert:cpu"\nworkdir = "/app"\n'
        'network_mode = "no-network"\n'
        '[verifier]\nenvironment_mode = "' + ("separate" if separate else "shared") + '"\n',
        encoding="utf-8",
    )
    (package / "instruction.md").write_text("Write the answer to /app/out.txt.\n", encoding="utf-8")
    (package / "environment" / "Dockerfile").write_text(
        "FROM inert:cpu\nWORKDIR /app\n", encoding="utf-8"
    )
    (package / "tests" / "test.sh").write_text("#!/bin/bash\n# inert verifier\n", encoding="utf-8")
    (package / "solution" / "solve.sh").write_text("#!/bin/bash\n# qa-answer\n", encoding="utf-8")
    return package


def _gate(cap: float = 100.0) -> CloudSpendGate:
    return CloudSpendGate(approval="unit-test approval", cap_usd=cap, usd_per_execution=0.1)


def _forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("unexpected Docker, model or provider operation")


@pytest.fixture
def inert_sdk(monkeypatch):
    import harbor.environments.modal as sdk
    import harbor.models.job.lock as native_lock
    from harbor.environments.base import ExecResult

    state = SimpleNamespace(
        allocations=[],
        lookups=[],
        images=[],
        executions=[],
        creates=[],
        capture_bytes=b"native capture",
        leak_text="LEAK_PROBE_V1 no_repository candidates_searched=7\n",
        fail_start=False,
        fail_verifier=False,
        creations_fail=0,
        active_names=set(),
        duplicate_names=[],
        creation_barrier=None,
        provider_lock=threading.Lock(),
    )

    class Sandbox:
        def __init__(self, kwargs):
            self.object_id = f"inert-{len(state.allocations)}"
            self.files: dict[str, bytes] = {}
            self.closed = False
            self.kwargs = kwargs
            self.terminate = SimpleNamespace(aio=self._terminate)
            self.wait = SimpleNamespace(aio=self._wait)
            state.allocations.append(self)

        async def _terminate(self, **kwargs):
            with state.provider_lock:
                self.closed = True
                state.active_names.discard(self.kwargs["name"])

        async def _wait(self, **kwargs):
            return 0

    async def create(*args, **kwargs):
        with state.provider_lock:
            state.creates.append(kwargs)
            if state.creations_fail:
                state.creations_fail -= 1
                raise RuntimeError("inert creation failure")
            name = kwargs["name"]
            if name in state.active_names:
                state.duplicate_names.append(name)
                raise RuntimeError(f"duplicate active sandbox name: {name}")
            state.active_names.add(name)
            sandbox = Sandbox(kwargs)
        if state.creation_barrier is not None:
            # Hold both real native creations active together. This catches
            # name reuse even when all other inert SDK methods finish inline.
            await asyncio.to_thread(state.creation_barrier.wait, timeout=20)
        return sandbox

    async def lookup(**kwargs):
        state.lookups.append(kwargs)
        return object()

    def image(*args, **kwargs):
        state.images.append((args, kwargs))
        return object()

    async def execute(self, command, **kwargs):
        if state.fail_start and command.startswith("mkdir"):
            raise RuntimeError("inert setup failed after allocation")
        sandbox = self._sandbox
        assert sandbox is not None
        files = sandbox.files
        state.executions.append((sandbox.object_id, command, kwargs))
        output = ""
        code = 0
        script_path = (
            "/solution/solve.sh"
            if "bash /solution/solve.sh" in command
            else "/tmp/qa-shortcut.sh"
            if "bash /tmp/qa-shortcut.sh" in command
            else None
        )
        if script_path:
            content = files[script_path].decode()
            if "LEAK_PROBE_V1" in content:
                output = state.leak_text
            else:
                # These are inert SDK outcomes for submitted test scripts, not
                # execution of a shell or a mock of QA's stage/report decisions.
                files["/app/out.txt"] = b"answer" if "qa-answer" in content else b"wrong"
                if "qa-capture" in content:
                    files["/logs/artifacts/capture.txt"] = state.capture_bytes
                output = "inert script output\n"
                if "qa-fail" in content:
                    code = 7
            files["/logs/agent/oracle.txt"] = output.encode()
        elif "/tests/test.sh" in command and not command.startswith("chmod"):
            reward = 1 if files.get("/app/out.txt") == b"answer" else 0
            files["/logs/verifier/test-stdout.txt"] = b"inert verifier output\n"
            if state.fail_verifier:
                raise RuntimeError("inert verifier unavailable")
            files["/logs/verifier/reward.txt"] = str(reward).encode()
        return ExecResult(return_code=code, stdout=output, stderr="")

    async def upload_file(self, source_path, target_path):
        self._sandbox.files[str(target_path)] = Path(source_path).read_bytes()

    async def upload_dir(self, source_dir, target_dir):
        for path in Path(source_dir).rglob("*"):
            if path.is_file():
                target = str(Path(target_dir) / path.relative_to(source_dir))
                self._sandbox.files[target] = path.read_bytes()

    async def download_file(self, source_path, target_path):
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self._sandbox.files[str(source_path)])

    async def download_dir(self, source_dir, target_dir):
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        prefix = str(source_dir).rstrip("/") + "/"
        for source, content in self._sandbox.files.items():
            if source.startswith(prefix):
                path = target / source.removeprefix(prefix)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

    async def is_dir(self, path, **kwargs):
        return any(name.startswith(str(path).rstrip("/") + "/") for name in self._sandbox.files)

    async def is_file(self, path, **kwargs):
        return str(path) in self._sandbox.files

    monkeypatch.setattr(sdk, "_HAS_MODAL", True)
    monkeypatch.setattr(
        sdk, "App", SimpleNamespace(lookup=SimpleNamespace(aio=lookup)), raising=False
    )
    monkeypatch.setattr(
        sdk, "Image", SimpleNamespace(from_registry=image, from_dockerfile=image), raising=False
    )
    monkeypatch.setattr(
        sdk, "Sandbox", SimpleNamespace(create=SimpleNamespace(aio=create)), raising=False
    )
    monkeypatch.setattr(native_lock, "_get_harbor_version", lambda: "0.24.0")
    monkeypatch.setattr(native_lock, "_get_harbor_git_commit_hash", lambda: None)
    monkeypatch.setattr(native_lock, "_get_harbor_is_editable_install", lambda: False)
    monkeypatch.setattr(native_lock, "_get_git_commit_hash", lambda path: None)
    monkeypatch.setattr(
        sdk.ModalEnvironment, "_secrets_config", lambda self: [self._persistent_env]
    )
    monkeypatch.setattr(sdk.ModalEnvironment, "_sdk_exec", execute)
    monkeypatch.setattr(sdk.ModalEnvironment, "_sdk_upload_file", upload_file)
    monkeypatch.setattr(sdk.ModalEnvironment, "_sdk_upload_dir", upload_dir)
    monkeypatch.setattr(sdk.ModalEnvironment, "_sdk_download_file", download_file)
    monkeypatch.setattr(sdk.ModalEnvironment, "_sdk_download_dir", download_dir)
    monkeypatch.setattr(sdk.ModalEnvironment, "is_dir", is_dir)
    monkeypatch.setattr(sdk.ModalEnvironment, "is_file", is_file)
    monkeypatch.setattr(modal_qa, "lab_harbor_problem", lambda: None)
    monkeypatch.setattr(qa, "docker_slot", _forbidden)
    monkeypatch.setattr(qa.HarborBackend, "_ensure_images", _forbidden)
    monkeypatch.setattr(qa.CodexLane, "complete", _forbidden)
    return state


@pytest.mark.parametrize("separate", [False, True])
def test_native_trial_controls_are_fresh_and_keep_raw_package_identity(
    tmp_path, inert_sdk, separate
):
    package = _package(tmp_path, separate=separate)
    digest = task_directory_digest(package)
    gate = _gate()
    backend = modal_qa.ModalBackend(repo_root=tmp_path, spend_gate=gate)
    outcomes = []
    for index, mode in enumerate(("oracle", "nop", "oracle", "nop")):
        directory = tmp_path / "runs" / f"control-{index}"
        observation = backend.run_container_trial(
            package,
            digest=digest,
            mode=mode,
            trial_dir=directory,
        )
        assert observation.status == "executed", observation.reason
        outcomes.append(observation.facts["reward"])
        raw = json.loads((directory / "result.json").read_text())
        config = json.loads((directory / "config.json").read_text())
        assert Path(config["task"]["path"]) == package
        assert raw["exception_info"] is None
        assert (directory / "agent" / "oracle.txt").is_file()
        assert "inert verifier" in (directory / "verifier" / "test-stdout.txt").read_text()
        lock = json.loads((directory / "egress-lock.json").read_text())
        assert lock["requested"] is True
        assert lock["applied"] is True
    assert outcomes == [1.0, 0.0, 1.0, 0.0]
    assert len(inert_sdk.allocations) == 4 * (2 if separate else 1)
    assert len({sandbox.object_id for sandbox in inert_sdk.allocations}) == len(
        inert_sdk.allocations
    )
    assert all(sandbox.closed for sandbox in inert_sdk.allocations)
    assert all(sandbox.kwargs["timeout"] <= 3600 for sandbox in inert_sdk.allocations)
    assert all(sandbox.kwargs["block_network"] for sandbox in inert_sdk.allocations)
    assert gate.executions == 3 * len(inert_sdk.allocations)
    assert task_directory_digest(package) == digest


def test_budget_refusal_precedes_native_constructor_and_provider(tmp_path, inert_sdk, monkeypatch):
    package = _package(tmp_path)
    gate = _gate(cap=0.2)  # not enough for preparation + both native create attempts
    backend = modal_qa.ModalBackend(repo_root=tmp_path, spend_gate=gate)
    monkeypatch.setattr(modal_qa.Trial, "create", _forbidden)
    observation = backend.run_container_trial(
        package,
        digest=task_directory_digest(package),
        mode="oracle",
        trial_dir=tmp_path / "refused",
    )
    assert observation.status == "failed"
    assert observation.facts["reward"] is None
    assert "SpendCapReached" in observation.reason
    assert not inert_sdk.creates and not inert_sdk.lookups and not inert_sdk.images
    assert gate.to_json()["reserved_usd"] <= gate.cap_usd


def test_separate_verifier_has_independent_admission_and_cleanup(tmp_path, inert_sdk):
    package = _package(tmp_path, separate=True)
    gate = _gate(cap=0.5)  # agent fits, verifier does not
    backend = modal_qa.ModalBackend(repo_root=tmp_path, spend_gate=gate)
    observation = backend.run_container_trial(
        package,
        digest=task_directory_digest(package),
        mode="oracle",
        trial_dir=tmp_path / "partial",
    )
    assert observation.status == "failed"
    assert observation.facts["reward"] is None
    assert "over the approved" in observation.reason
    assert len(inert_sdk.allocations) == 1
    assert inert_sdk.allocations[0].closed
    assert gate.executions == 5


@pytest.mark.parametrize("failure", ["agent", "verifier", "setup"])
def test_native_failures_are_unmeasured_and_reclaim_allocations(tmp_path, inert_sdk, failure):
    package = _package(tmp_path)
    inert_sdk.fail_verifier = failure == "verifier"
    inert_sdk.fail_start = failure == "setup"
    backend = modal_qa.ModalBackend(repo_root=tmp_path, spend_gate=_gate())
    observation = backend.run_container_trial(
        package,
        digest=task_directory_digest(package),
        mode="oracle",
        solve_override="#!/bin/bash\n# qa-answer qa-fail\n" if failure == "agent" else None,
        trial_dir=tmp_path / failure,
    )
    assert observation.status == "failed"
    assert observation.facts["reward"] is None
    assert observation.facts["exception"]
    assert all(sandbox.closed for sandbox in inert_sdk.allocations)


def test_patch_oracle_override_and_capture_use_original_package(tmp_path, inert_sdk):
    package = _package(tmp_path)
    (package / "solution" / "solve.sh").unlink()
    reference = package / "solution" / "patch.diff"
    reference.write_bytes(
        b"diff --git a/out b/out\n--- a/out\n+++ b/out\n@@ -1 +1 @@\n-old\n+qa-answer\n"
    )
    digest = task_directory_digest(package)
    backend = modal_qa.ModalBackend(repo_root=tmp_path, spend_gate=_gate())
    first = backend.run_container_trial(
        package,
        digest=digest,
        mode="oracle",
        trial_dir=tmp_path / "patch-oracle",
        capture_logs=True,
    )
    assert first.status == "executed", first.reason
    assert first.facts["oracle"]["kind"] == "reference-patch"
    assert first.facts["oracle"]["workdir"] == "/app"
    assert first.facts["oracle"]["sha256"] == backend.reference_patch(package)[1]
    assert first.facts["capture_logs"] is True
    config = json.loads((tmp_path / "patch-oracle" / "config.json").read_text())
    assert Path(config["task"]["path"]) == package
    second = backend.run_container_trial(
        package,
        digest=digest,
        mode="oracle",
        solve_override="#!/bin/bash\n# qa-answer\n",
        trial_dir=tmp_path / "override",
    )
    assert second.status == "executed", second.reason
    assert second.facts["reward"] == 1.0
    assert any(kwargs.get("cwd") == "/app" for _, _, kwargs in inert_sdk.executions)
    assert task_directory_digest(package) == digest
    assert not (package / "solution" / "solve.sh").exists()


def test_shortcut_and_mutation_replay_are_fresh_and_preserve_solution_bytes(tmp_path, inert_sdk):
    package = _package(tmp_path, separate=True)
    digest = task_directory_digest(package)
    backend = modal_qa.ModalBackend(repo_root=tmp_path, spend_gate=_gate())
    shortcut = tmp_path / "shortcut.sh"
    shortcut.write_text("#!/bin/bash\n# qa-answer\n", encoding="utf-8")
    reproducer = qa.Reproducer(kind="command", path=str(shortcut), expected_reward=1.0)
    passed = backend.replay_reproducer(
        package,
        reproducer,
        digest=digest,
        run_dir=tmp_path / "confirm-1",
        timeout_seconds=30,
    )
    assert passed.status == "executed", passed.reason
    assert passed.facts["reward"] == 1.0
    shortcut.write_text("#!/bin/bash\n# qa-wrong\n", encoding="utf-8")
    failed = backend.replay_reproducer(
        package,
        reproducer,
        digest=digest,
        run_dir=tmp_path / "confirm-2",
        timeout_seconds=30,
    )
    assert failed.status == "executed", failed.reason
    assert failed.facts["reward"] == 0.0
    shortcut.write_text(qa.MUTATION_SCRIPT_MARK + " mutation\n# qa-answer\n", encoding="utf-8")
    mutation = backend.replay_reproducer(
        package,
        reproducer,
        digest=digest,
        run_dir=tmp_path / "confirm-mutation",
        timeout_seconds=30,
    )
    assert mutation.status == "executed", mutation.reason
    assert mutation.facts["reward"] == 1.0
    assert len(inert_sdk.allocations) == 6
    assert all(sandbox.closed for sandbox in inert_sdk.allocations)
    assert task_directory_digest(package) == digest


@pytest.mark.parametrize(
    ("output", "coverage", "measured"),
    [
        ("LEAK_PROBE_V1 no_repository candidates_searched=7\n", "no-repository", True),
        (
            "LEAK_PROBE_V1 missing_git git binary not on PATH\nLEAK_PROBE_V1 done repos=0\n",
            "no-repository",
            True,
        ),
        (
            "qa-repo:/app\nLEAK_PROBE_V1 missing_git git binary not on PATH\nLEAK_PROBE_V1 done repos=0\n",
            "missing-git",
            False,
        ),
        ("probe crashed\n", "missing-probe-output", False),
    ],
)
def test_native_leak_probe_is_one_fresh_bounded_environment(
    tmp_path, inert_sdk, output, coverage, measured
):
    package = _package(tmp_path)
    backend = modal_qa.ModalBackend(repo_root=tmp_path, spend_gate=_gate())
    inert_sdk.leak_text = output
    observation = backend.leak(
        backend.resolve(package), run_dir=tmp_path / "leak", timeout_seconds=30
    )
    assert observation.facts.get("coverage") == coverage
    assert isinstance(observation.facts.get("has_future_history"), bool) == measured
    assert len(inert_sdk.allocations) == 1
    assert inert_sdk.allocations[0].closed
    assert backend.spend_gate.executions == 3


def test_digest_mismatch_and_unsupported_resources_never_allocate(tmp_path, inert_sdk):
    package = _package(tmp_path)
    backend = modal_qa.ModalBackend(repo_root=tmp_path, spend_gate=_gate())
    old_digest = task_directory_digest(package)
    (package / "instruction.md").write_text("changed", encoding="utf-8")
    observation = backend.run_container_trial(
        package,
        digest=old_digest,
        mode="oracle",
        trial_dir=tmp_path / "stale",
    )
    assert observation.status == "failed"
    assert observation.facts["reward"] is None
    (package / "environment" / "docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
    observation = backend.run_container_trial(
        package,
        digest=task_directory_digest(package),
        mode="oracle",
        trial_dir=tmp_path / "compose",
    )
    assert observation.status == "failed" and observation.facts["reward"] is None
    (package / "environment" / "docker-compose.yaml").unlink()
    config = package / "task.toml"
    config.write_text(config.read_text().replace("[environment]\n", "[environment]\ngpus = 1\n"))
    observation = backend.run_container_trial(
        package,
        digest=task_directory_digest(package),
        mode="oracle",
        trial_dir=tmp_path / "gpu",
    )
    assert observation.status == "failed"
    assert backend.spend_gate.executions == 0
    assert not inert_sdk.allocations and not inert_sdk.images and not inert_sdk.lookups


def test_s0_s3_native_controls_and_candidate_confirmation(tmp_path, inert_sdk):
    package = _package(tmp_path)
    backend = modal_qa.ModalBackend(repo_root=tmp_path, spend_gate=_gate())
    task = backend.resolve(package)
    subject = qa.Subject(kind="task", task_id=task.task_id, task_digest=task.package_digest)
    report = qa.TaskReport(
        subject=subject,
        package=str(package),
        requested_stages=["S0", "S3"],
        started_at=qa.utcnow(),
        environment="modal",
    )
    config = qa.QATaskConfig(stages=("S0", "S3"), mutants=0)
    findings = qa.stage_s0(report, backend, config, run_dir=tmp_path / "stage")
    assert not findings
    command = tmp_path / "candidate.sh"
    command.write_text("#!/bin/bash\n# qa-answer\n", encoding="utf-8")
    finding = qa.Finding(
        id="candidate",
        subject=subject,
        stage="S2",
        verdict="fail",
        category="weak",
        severity="high",
        fault="eval",
        evidence_refs=[str(command)],
        reproducer=qa.Reproducer(kind="command", path=str(command), expected_reward=1),
        refutation=qa.Refutation(status="unverified", by="test", reason="candidate only"),
        reviewer=qa.Reviewer(agent="test"),
    )
    qa.stage_s3(report, [finding], backend, config, run_dir=tmp_path / "stage")
    assert finding.refutation.status == "verified"
    assert report.checks["fresh-replay"].status == "measured"
    assert len(inert_sdk.allocations) == 8  # six controls, leak, fresh replay
    inert_sdk.fail_verifier = True
    finding.refutation.status = "unverified"
    qa.stage_s3(report, [finding], backend, config, run_dir=tmp_path / "stage-failed")
    assert finding.refutation.status == "unverified"
    assert report.checks["fresh-replay"].status == "ran_without_usable_result"


def test_sixteen_cloud_tasks_use_independent_lanes_and_one_gate(tmp_path, monkeypatch, capsys):
    packages = [_package(tmp_path, f"task-{index}") for index in range(16)]
    barrier = threading.Barrier(16)
    lock = threading.Lock()
    seen = []

    def run(package, *, config, run_dir, backend, lane, **kwargs):
        assert backend.environment == "modal"
        barrier.wait(timeout=10)
        backend.spend_gate.charge()
        with lock:
            seen.append((backend, lane, lane.scratch_root, backend.spend_gate))
        return qa.TaskReport(
            subject=qa.Subject(
                kind="task", task_id=package.name, task_digest=task_directory_digest(package)
            ),
            package=str(package),
            requested_stages=list(config.stages),
            started_at=qa.utcnow(),
            environment="modal",
        )

    monkeypatch.setattr(qa, "run_task", run)
    monkeypatch.setattr(qa, "docker_slot", _forbidden)
    monkeypatch.setattr(modal_qa.Trial, "create", _forbidden)
    args = build_root_parser().parse_args(
        [
            "qa",
            "task",
            "--environment",
            "modal",
            "--concurrency",
            "16",
            "--stages",
            "S3",
            "--max-usd",
            "2",
            "--usd-per-execution",
            "0.1",
            "--approve",
            "unit-test approval",
            "--run-dir",
            str(tmp_path / "run"),
        ]
        + [value for package in packages for value in ("--task", str(package))]
    )
    assert args.func(args, tmp_path) == 0
    summary = json.loads(capsys.readouterr().out)
    assert len({id(backend) for backend, _, _, _ in seen}) == 16
    assert len({id(lane) for _, lane, _, _ in seen}) == 16
    assert len({scratch for _, _, scratch, _ in seen}) == 16
    assert len({id(gate) for _, _, _, gate in seen}) == 1
    assert summary["cloud_spend"]["executions"] == 16
    assert summary["cloud_spend"]["reserved_usd"] == 1.6
    assert json.loads((tmp_path / "run" / "cloud-spend.json").read_text()) == summary["cloud_spend"]
    assert json.loads((tmp_path / "run" / "run.json").read_text())["concurrency"] == 16


@pytest.mark.parametrize("environment,concurrency", [("docker", 3), ("modal", 17), ("modal", 0)])
def test_admission_rejects_out_of_range_concurrency(tmp_path, capsys, environment, concurrency):
    package = _package(tmp_path)
    args = build_root_parser().parse_args(
        [
            "qa",
            "task",
            "--task",
            str(package),
            "--environment",
            environment,
            "--concurrency",
            str(concurrency),
            "--plan",
        ]
    )
    assert args.func(args, tmp_path) == 1
    assert "concurrency must be" in capsys.readouterr().err


def test_environment_cannot_be_started_without_admission(inert_sdk):
    # Guard the actual native start boundary, not just the CLI. An accidental
    # direct use cannot turn a constructed adapter into an unbudgeted allocation.
    environment = object.__new__(modal_qa.QAModalEnvironment)
    with pytest.raises(ValueError, match="active cloud spend gate"):
        asyncio.run(environment.start())
    assert not inert_sdk.allocations and not inert_sdk.lookups


def test_native_creation_retry_is_reserved_before_either_attempt(tmp_path, inert_sdk, monkeypatch):
    from harbor.environments.modal import ModalEnvironment
    from tenacity import wait_none

    monkeypatch.setattr(ModalEnvironment._create_sandbox.retry, "wait", wait_none())
    package = _package(tmp_path)
    inert_sdk.creations_fail = 1
    gate = _gate(cap=0.3)
    backend = modal_qa.ModalBackend(repo_root=tmp_path, spend_gate=gate)
    observation = backend.run_container_trial(
        package,
        digest=task_directory_digest(package),
        mode="oracle",
        trial_dir=tmp_path / "retry",
    )
    assert observation.status == "executed", observation.reason
    assert len(inert_sdk.creates) == 2 and len(inert_sdk.allocations) == 1
    assert gate.executions == 3 and gate.to_json()["reserved_usd"] == 0.3
    assert inert_sdk.allocations[0].closed


def test_shortcut_with_solution_env_and_native_capture(tmp_path, inert_sdk, monkeypatch):
    package = _package(tmp_path, separate=True)
    monkeypatch.setenv("QA_TEST_SENTINEL", "injected-not-a-secret")
    config = package / "task.toml"
    config.write_text(
        config.read_text() + '\n[solution.env]\nQA_SENTINEL = "${QA_TEST_SENTINEL}"\n'
    )
    original = (package / "solution" / "solve.sh").read_bytes()
    digest = task_directory_digest(package)
    backend = modal_qa.ModalBackend(repo_root=tmp_path, spend_gate=_gate())
    directory = tmp_path / "with-solution"
    observation = backend.run_container_trial(
        package,
        digest=digest,
        mode="shortcut",
        script="#!/bin/bash\n# qa-answer qa-capture\n",
        with_solution=True,
        capture_logs=True,
        trial_dir=directory,
    )
    assert observation.status == "executed", observation.reason
    assert observation.facts["reward"] == 1.0
    assert inert_sdk.allocations[0].files["/solution/solve.sh"] == original
    assert (
        directory / "artifacts" / "logs" / "artifacts" / "capture.txt"
    ).read_bytes() == inert_sdk.capture_bytes
    assert inert_sdk.allocations[1].files["/logs/artifacts/capture.txt"] == inert_sdk.capture_bytes
    assert any(
        kwargs.get("env", {}).get("QA_SENTINEL") == "injected-not-a-secret"
        for _, command, kwargs in inert_sdk.executions
        if "bash /tmp/qa-shortcut.sh" in command
    )
    assert task_directory_digest(package) == digest


def test_mutation_matrix_uses_native_override_capture_and_unmeasured_failure(tmp_path, inert_sdk):
    package = _package(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "capture.sh").write_text("#!/bin/bash\n# qa-answer qa-capture\n")
    (scripts / "failed.sh").write_text("#!/bin/bash\n# qa-answer qa-fail\n")
    matrix = SimpleNamespace(
        task=package,
        task_package_digest=task_directory_digest(package),
        timeout_seconds=30,
        runs=[
            SimpleNamespace(name="capture", solution=Path("scripts/capture.sh"), expect_reward=1.0),
            SimpleNamespace(name="failed", solution=Path("scripts/failed.sh"), expect_reward=1.0),
        ],
    )
    backend = modal_qa.ModalBackend(repo_root=tmp_path, spend_gate=_gate())
    results = backend.matrix_runner(matrix, tmp_path)
    assert results[0]["status"] == "ok" and results[0]["rewards"] == [1.0]
    assert results[1]["status"] == "infra" and "rewards" not in results[1]
    assert (
        tmp_path / "runs/capture/trial-0/artifacts/logs/artifacts/capture.txt"
    ).read_bytes() == inert_sdk.capture_bytes
    assert len(inert_sdk.allocations) == 2 and all(s.closed for s in inert_sdk.allocations)


def test_s2_ref_subset_budget_stop_is_not_a_measured_pass(tmp_path, inert_sdk, monkeypatch):
    import evallab.ref_subset as ref_subset
    from evallab.regrade import SpendCapReached

    package = _package(tmp_path, separate=True)
    gate = _gate(cap=0.1)
    backend = modal_qa.ModalBackend(repo_root=tmp_path, spend_gate=gate)
    task = backend.resolve(package)
    report = qa.TaskReport(
        subject=qa.Subject(kind="task", task_id=task.task_id, task_digest=task.package_digest),
        package=str(package),
        requested_stages=["S2"],
        started_at=qa.utcnow(),
        environment="modal",
    )

    def probe(*args, environment="docker", spend_gate=None, **kwargs):
        if environment != "modal" or spend_gate is not gate:
            return {"status": "measured", "verdict": "pass"}
        try:
            spend_gate.charge()
            spend_gate.charge()
        except SpendCapReached:
            return {"status": "budget_exhausted", "verdict": "pass", "reason": "reservation cap"}
        raise AssertionError("expected budget refusal")

    monkeypatch.setattr(ref_subset.Package, "load", lambda package: SimpleNamespace(separate=True))
    monkeypatch.setattr(ref_subset, "probe_task", probe)
    findings = qa.stage_s2(
        report,
        backend,
        None,
        qa.QATaskConfig(stages=("S2",), mutants=0),
        run_dir=tmp_path / "s2",
    )
    assert not findings
    check = report.checks["ref-subset"]
    assert check.status == "ran_without_usable_result" and check.verdict == "unknown"
    assert check.facts["verdict"] == "unknown"
    assert gate.executions == 1 and not inert_sdk.allocations


def test_digest_change_during_native_trial_discards_reward(tmp_path, inert_sdk, monkeypatch):
    from harbor.environments.modal import ModalEnvironment

    package = _package(tmp_path)
    digest = task_directory_digest(package)
    execute = ModalEnvironment._sdk_exec

    async def changing_exec(self, command, **kwargs):
        result = await execute(self, command, **kwargs)
        if "/tests/test.sh" in command and not command.startswith("chmod"):
            (package / "instruction.md").write_text("changed during execution\n")
        return result

    monkeypatch.setattr(ModalEnvironment, "_sdk_exec", changing_exec)
    backend = modal_qa.ModalBackend(repo_root=tmp_path, spend_gate=_gate())
    observation = backend.run_container_trial(
        package,
        digest=digest,
        mode="oracle",
        trial_dir=tmp_path / "changed",
    )
    assert observation.status == "failed" and observation.facts["reward"] is None
    assert observation.facts["unusable_reward"] == 1.0
    assert "changed during" in observation.reason
    assert inert_sdk.allocations[0].closed


def test_changed_bound_reference_patch_refuses_before_native_execution(tmp_path, inert_sdk):
    package = _package(tmp_path)
    (package / "solution" / "solve.sh").unlink()
    reference = tmp_path / "reference.patch"
    reference.write_bytes(b"original reference patch")
    backend = modal_qa.ModalBackend(repo_root=tmp_path, spend_gate=_gate())
    backend.bind_reference_patch(package, reference)
    digest = task_directory_digest(package)
    reference.write_bytes(b"changed reference patch")
    observation = backend.run_container_trial(
        package,
        digest=digest,
        mode="oracle",
        trial_dir=tmp_path / "changed-reference",
    )
    assert observation.status == "failed" and observation.facts["reward"] is None
    assert "reference patch digest changed" in observation.reason
    assert backend.spend_gate.executions == 0 and not inert_sdk.allocations
    assert task_directory_digest(package) == digest


def test_overlapping_trial_leaves_isolate_native_agent_and_separate_verifier_names(
    tmp_path, inert_sdk
):
    packages = [
        _package(tmp_path / "packages", name, separate=True)
        for name in ("package-one", "package-two")
    ]
    for package in packages:
        solve = package / "solution" / "solve.sh"
        solve.write_text(f"#!/bin/bash\n# qa-answer\n# {package.name}\n", encoding="utf-8")
    directories = [
        tmp_path / "run-one" / "candidate" / "trial",
        tmp_path / "run-two" / "candidate" / "trial",
    ]
    inert_sdk.creation_barrier = threading.Barrier(2)
    gate = _gate()

    def execute(item):
        package, directory = item
        backend = modal_qa.ModalBackend(repo_root=tmp_path, spend_gate=gate)
        return backend.run_container_trial(
            package,
            digest=task_directory_digest(package),
            mode="oracle",
            trial_dir=directory,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        observations = list(pool.map(execute, zip(packages, directories, strict=True)))
    assert all(observation.status == "executed" for observation in observations), [
        observation.reason for observation in observations
    ]
    assert [observation.facts["reward"] for observation in observations] == [1.0, 1.0]
    assert not inert_sdk.duplicate_names and not inert_sdk.active_names
    assert len(inert_sdk.allocations) == 4
    allocated = {sandbox.object_id: sandbox for sandbox in inert_sdk.allocations}
    names = {sandbox.kwargs["name"] for sandbox in allocated.values()}
    assert len(names) == 4 and all(len(name) <= 64 for name in names)
    assert len({id(sandbox.files) for sandbox in allocated.values()}) == 4
    assert all(sandbox.closed for sandbox in allocated.values())
    assert gate.executions == 12
    for package, directory in zip(packages, directories, strict=True):
        config = json.loads((directory / "config.json").read_text())
        assert config["trial_name"] == "trial"
        assert Path(config["trials_dir"]) == directory.parent
        assert Path(config["task"]["path"]) == package
        record = json.loads((directory / "egress-lock.json").read_text())
        actual_ids = {application["sandbox_id"] for application in record["applications"]}
        assert len(actual_ids) == 2
        environments = [allocated[actual_id] for actual_id in actual_ids]
        agents = [sandbox for sandbox in environments if "/solution/solve.sh" in sandbox.files]
        verifiers = [
            sandbox for sandbox in environments if "/solution/solve.sh" not in sandbox.files
        ]
        assert len(agents) == len(verifiers) == 1
        assert agents[0].files["/solution/solve.sh"] == (package / "solution/solve.sh").read_bytes()
        assert verifiers[0].files["/app/out.txt"] == b"answer"
        assert record["session_id"] == allocated[record["sandbox_id"]].kwargs["name"]
        for sandbox in environments:
            assert sandbox.kwargs["tags"]["harbor.session_id"] == sandbox.kwargs["name"]
