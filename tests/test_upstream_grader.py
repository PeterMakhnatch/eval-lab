"""Behavioral regressions; all runtime/provider transports here are inert/local.

The shell fixture really executes its immutable solution and verifier. It is not
Docker/Modal coverage: parent-owned smoke controls exercise those boundaries.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import runpy
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from evallab import upstream_grader as grader


@pytest.fixture
def task(tmp_path: Path) -> Path:
    root = tmp_path / "upstream-original"
    for name in ("environment", "tests", "solution"):
        (root / name).mkdir(parents=True)
    (root / "task.toml").write_text(
        'schema_version = "1.0"\nartifacts = ["/app/answer.txt"]\n'
        '[task]\nname = "upstream/fixture"\n'
        "[environment]\ncpus = 2\nmemory_mb = 4096\nstorage_mb = 10240\n"
        'build_timeout_sec = 600\ngpus = 0\nworkdir = "/app"\n'
        "[agent]\ntimeout_sec = 28800\n"
        '[verifier]\nenvironment_mode = "separate"\ntimeout_sec = 300\n',
        encoding="utf-8",
    )
    (root / "instruction.md").write_text("Write good to /app/answer.txt.\n", encoding="utf-8")
    for folder in ("environment", "tests"):
        (root / folder / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    (root / "environment" / "base.txt").write_text("unchanged base\n", encoding="utf-8")
    (root / "solution" / "solve.sh").write_text(
        "set -eu\ntest ! -e /tests/test.sh\nprintf good > /app/answer.txt\n",
        encoding="utf-8",
    )
    (root / "tests" / "test.sh").write_text(
        "#!/bin/bash\nset -eu\n"
        'if [ "$(cat /app/answer.txt 2>/dev/null || true)" = good ]; then\n'
        "  reward=1; outcome=passed\nelse\n  reward=0; outcome=failed\nfi\n"
        'printf "%s\\n" "$reward" > /logs/verifier/reward.txt\n'
        'printf \'{"results":{"tests":[{"name":"test_answer","status":"%s","duration":12}]}}\\n\' '
        '"$outcome" > /logs/verifier/ctrf.json\n'
        'printf "native-verifier %s\\n" "$outcome"\n'
        'printf "native-stderr\\n" >&2\n',
        encoding="utf-8",
    )
    return root


@pytest.fixture
def local_transport(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, Any]]:
    """Exercise staging/native evidence with real local shell input, no runtimes.

    This deliberately implements only the controlled fixture's paths. Candidate
    scripts never see tests until verification, and each call starts fresh.
    """
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(grader, "docker_slot", lambda **_: contextlib.nullcontext())
    monkeypatch.setattr(
        grader,
        "_admit_docker",
        lambda plan: {
            "platform": plan["platform"] or "linux/amd64",
            "available_cpus": 8,
            "available_memory_mb": 16384,
            "emulated": False,
        },
    )

    async def execute(config: dict[str, Any], active: grader._Execution) -> dict[str, Any]:
        trial = Path(config["trials_dir"]) / config["trial_name"]
        verifier = trial / "verifier"
        agent = trial / "agent"
        verifier.mkdir(parents=True)
        agent.mkdir()
        sandbox = tmp_path / "inert-sandboxes" / str(len(calls))
        (sandbox / "app").mkdir(parents=True)
        (sandbox / "logs").mkdir()
        calls.append(
            {
                "config": config,
                "sandbox": sandbox,
                "preexisting_answer": (sandbox / "app" / "answer.txt").exists(),
            }
        )
        staged = Path(config["task"]["path"])
        solution = config["agent"]["kwargs"]["qa_solution_dir"]
        if solution is not None:
            shutil.copytree(solution, sandbox / "solution")

        def run_script(script: Path) -> subprocess.CompletedProcess[str]:
            command = script.read_text(encoding="utf-8")
            for original, replacement in (
                ("/logs/verifier", str(verifier)),
                ("/solution", str(sandbox / "solution")),
                ("/tests", str(sandbox / "tests")),
                ("/app", str(sandbox / "app")),
            ):
                command = command.replace(original, replacement)
            return subprocess.run(
                ["bash", "-c", command],
                cwd=sandbox / "app",
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )

        if solution is not None:
            candidate = run_script(sandbox / "solution" / "solve.sh")
            (agent / "oracle.txt").write_text(candidate.stdout + candidate.stderr, encoding="utf-8")
            if candidate.returncode:
                (agent / "exit-code.txt").write_text(str(candidate.returncode), encoding="utf-8")
                return {
                    "exception_info": {"exception_message": "candidate failed"},
                    "verifier_result": None,
                }
        shutil.copytree(staged / "tests", sandbox / "tests")
        verified = run_script(sandbox / "tests" / "test.sh")
        (verifier / "test-stdout.txt").write_text(
            verified.stdout + verified.stderr, encoding="utf-8"
        )
        try:
            rewards = grader._read_native_rewards(trial)
        except (ValueError, OSError):
            rewards = None
        payload = {"exception_info": None, "verifier_result": {"rewards": rewards}}
        (trial / "result.json").write_text(json.dumps(payload), encoding="utf-8")
        active.elapsed_seconds = 10.0
        if config["environment"]["import_path"].endswith("UpstreamModalEnvironment"):
            for resource in active.plan["resources"]:
                active.claim(resource["cpus"], resource["memory_mb"])
                active.environments.append(
                    SimpleNamespace(
                        _upstream_image_build_completed=True,
                        _upstream_termination_proven=True,
                        _upstream_sandbox_id=f"inert-{len(calls)}-{resource['role']}",
                        _upstream_exit_code=0,
                    )
                )
        return payload

    monkeypatch.setattr(grader, "_execute_native", execute)
    return calls


def modal_options(tmp_path: Path) -> dict[str, Any]:
    return {
        "backend": "modal",
        "timeout_seconds": 140,
        "approval": "Peter approved IN-1 controls",
        "max_usd": 1.50,
        "spend_ledger": tmp_path / "campaign-spend.json",
        "modal_app_name": "2026-10-20-open-qa-upstream",
        "billing_tag": "2026-10-20-open-qa",
    }


def test_plan_is_read_only_and_does_not_probe_or_import_runtime(
    task: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def forbidden(*_: Any, **__: Any) -> None:
        pytest.fail("plan attempted a runtime probe, allocation, staging, or spend reservation")

    for name in (
        "_execute_native",
        "_admit_docker",
        "docker_available_resources",
        "_reserve_spend",
        "_stage_inputs",
    ):
        monkeypatch.setattr(grader, name, forbidden)
    before = grader._tree_digest(task)
    modules = set(sys.modules)
    options = modal_options(tmp_path)
    planned = grader.run_upstream_grader(
        task, oracle=True, plan=True, output_dir=tmp_path / "must-not-exist", **options
    )
    assert planned["executed"] is False
    assert planned["runnable"] is True
    assert planned["reserved_usd"] == 0
    assert planned["resources"][0]["cpus"] == planned["resources"][1]["cpus"] == 2
    assert planned["resources"][1]["memory_mb"] == 4096
    assert planned["timeout_seconds"] == 140
    assert planned["modal_reservation"]["reserved_usd"] == pytest.approx(0.693794, abs=0.000002)
    assert not (tmp_path / "must-not-exist").exists()
    assert not options["spend_ledger"].exists()
    assert not options["spend_ledger"].with_suffix(".json.lock").exists()
    assert grader._tree_digest(task) == before
    assert not any(name.startswith(("harbor.", "modal.")) for name in set(sys.modules) - modules)
    assert [resource["build_context"] for resource in planned["resources"]] == [
        "environment",
        "tests",
    ]
    assert all(
        "not an exact disk quota" in resource["storage_enforcement"]
        for resource in planned["resources"]
    )


def test_fresh_process_import_and_plan_never_require_native_runtime(task: Path) -> None:
    program = (
        "import importlib.abc, json, sys\n"
        "class NoRuntime(importlib.abc.MetaPathFinder):\n"
        " def find_spec(self, fullname, path=None, target=None):\n"
        "  if fullname.split('.')[0] in {'harbor','modal'}:\n"
        "   raise ModuleNotFoundError('optional runtime is absent: '+fullname, name=fullname)\n"
        "sys.meta_path.insert(0, NoRuntime())\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "from evallab.upstream_grader import plan_upstream_grader\n"
        "result=plan_upstream_grader(sys.argv[1], blank=True, backend='docker')\n"
        "assert not any(name.split('.')[0] in {'harbor','modal'} for name in sys.modules)\n"
        "print(json.dumps(result))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program, str(task), str(Path(grader.__file__).resolve().parents[1])],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert json.loads(completed.stdout)["executed"] is False


def test_oracle_blank_oracle_are_fresh_and_preserve_upstream_bytes(
    task: Path, tmp_path: Path, local_transport: list[dict[str, Any]]
) -> None:
    before = grader._tree_digest(task)
    observed = [
        grader.run_upstream_grader(task, backend="docker", output_dir=tmp_path / str(index), **mode)
        for index, mode in enumerate(({"oracle": True}, {"blank": True}, {"oracle": True}))
    ]
    assert [item["status"] for item in observed] == ["measured"] * 3
    assert [item["reward"] for item in observed] == [1, 0, 1]
    assert [call["preexisting_answer"] for call in local_transport] == [False] * 3
    assert local_transport[1]["config"]["agent"]["kwargs"]["qa_command"] is None
    assert not (local_transport[1]["sandbox"] / "solution").exists()
    assert grader._tree_digest(task) == before
    for result in observed:
        assert result["task"]["exact_tree_digest"] == before
        assert result["per_test"]["tests"][0]["id"] == "test_answer"
        assert result["per_test"]["tests"][0]["duration_seconds"] == pytest.approx(0.012)
        assert result["raw_verifier_evidence"]
        assert result["agent_model_calls"] == 0
        assert result["task_external_api_calls"] is None
        assert (
            Path(result["trial_dir"], "verifier", "test-stdout.txt")
            .read_text()
            .endswith("native-stderr\n")
        )
        assert (
            Path(result["output_dir"], "inputs", "task", task.name, "tests", "test.sh").read_bytes()
            == (task / "tests" / "test.sh").read_bytes()
        )
        config = next(
            call["config"]
            for call in local_transport
            if str(call["config"]["trials_dir"]) == result["output_dir"]
        )
        assert Path(config["task"]["path"]).name == task.name
        assert config["verifier"] == {}  # native test timeout and command stay unchanged
        assert "override_cpus" not in config["environment"]
        assert "override_memory_mb" not in config["environment"]


def test_explicit_solution_and_snapshot_replay_real_contents(
    task: Path, tmp_path: Path, local_transport: list[dict[str, Any]]
) -> None:
    solution = tmp_path / "candidate"
    solution.mkdir()
    (solution / "solve.sh").write_text("printf bad > /app/answer.txt\n", encoding="utf-8")
    before = grader._tree_digest(solution)
    direct = grader.run_upstream_grader(
        task, solution, backend="docker", output_dir=tmp_path / "direct"
    )
    assert direct["reward"] == 0
    assert grader._tree_digest(solution) == before
    archive = tmp_path / "app.tar"
    with tarfile.open(archive, "w") as stream:
        payload = b"good"
        member = tarfile.TarInfo("answer.txt")
        member.size = len(payload)
        member.mode = 0o640
        stream.addfile(member, io.BytesIO(payload))
    snapshot = grader.ContainerSnapshot(archive, "/app", "linux/amd64")
    replay = grader.run_upstream_grader(
        task, container_snapshot=snapshot, backend="docker", output_dir=tmp_path / "snapshot"
    )
    assert replay["reward"] == 1
    assert replay["input"]["kind"] == "directory-tar-snapshot"
    assert "no process/image/volume replay" in replay["input"]["semantics"]
    assert (local_transport[-1]["sandbox"] / "app" / "answer.txt").read_bytes() == b"good"
    assert (
        Path(replay["output_dir"]) / "inputs" / "snapshot.tar"
    ).read_bytes() == archive.read_bytes()
    assert not (local_transport[-1]["sandbox"] / "app" / "solve.sh").exists()


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("/app/escape", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("hardlink", tarfile.LNKTYPE),
        ("device", tarfile.CHRTYPE),
    ],
)
def test_snapshot_rejects_unsafe_members_before_allocation(
    task: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, kind: bytes
) -> None:
    archive = tmp_path / "unsafe.tar"
    with tarfile.open(archive, "w") as stream:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.linkname = "/etc/passwd"
        stream.addfile(member)
    monkeypatch.setattr(
        grader, "_admit_docker", lambda *_: pytest.fail("unsafe snapshot reached admission")
    )
    with pytest.raises(grader.GraderRefusal, match="snapshot"):
        grader.run_upstream_grader(
            task,
            container_snapshot=grader.ContainerSnapshot(archive, "/app", "linux/amd64"),
            output_dir=tmp_path / "no-output",
        )
    assert not (tmp_path / "no-output").exists()
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize(
    "change",
    [
        "missing-memory",
        "gpu",
        "compose",
        "float-cpu",
        "separate-verifier-missing-memory",
        "separate-verifier-empty",
    ],
)
def test_resource_errors_are_refused_without_invented_limits(task: Path, change: str) -> None:
    config = task / "task.toml"
    text = config.read_text()
    if change == "missing-memory":
        text = text.replace("memory_mb = 4096\n", "")
    elif change == "gpu":
        text = text.replace("gpus = 0", "gpus = 1")
    elif change == "compose":
        (task / "environment" / "docker-compose.yaml").write_text(
            "services: {main: {}, sidecar: {}}\n"
        )
    elif change == "float-cpu":
        text = text.replace("cpus = 2", "cpus = 2.0")
    elif change == "separate-verifier-empty":
        text += "\n[verifier.environment]\n"
    else:
        text += "\n[verifier.environment]\ncpus = 1\n"
    config.write_text(text)
    with pytest.raises(grader.GraderRefusal):
        grader.plan_upstream_grader(task, blank=True)


def test_unbounded_docker_gate_is_not_bypassed_and_auto_needs_approval(
    task: Path,
    tmp_path: Path,
    local_transport: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_: Any) -> None:
        raise ValueError("running service lacks finite CPU/RAM limits")

    monkeypatch.setattr(grader, "_admit_docker", refuse)
    direct = grader.run_upstream_grader(
        task, blank=True, backend="docker", output_dir=tmp_path / "docker"
    )
    automatic = grader.run_upstream_grader(
        task, blank=True, backend="auto", output_dir=tmp_path / "auto"
    )
    assert direct["status"] == automatic["status"] == "refused"
    assert direct["reward"] is automatic["reward"] is None
    assert "finite CPU/RAM" in direct["error"]
    assert "approval" in automatic["error"]
    assert local_transport == []


def test_docker_execution_failure_does_not_fall_back_to_modal(
    task: Path,
    tmp_path: Path,
    local_transport: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def fail(config: dict[str, Any], _: Any) -> dict[str, Any]:
        calls.append(config["environment"]["import_path"])
        raise RuntimeError("task image build failed")

    monkeypatch.setattr(grader, "_execute_native", fail)
    options = modal_options(tmp_path)
    options["backend"] = "auto"
    result = grader.run_upstream_grader(
        task, oracle=True, output_dir=tmp_path / "failed", **options
    )
    assert result["status"] == "error"
    assert result["reward"] is None
    assert result["backend"] == "docker"
    assert calls == ["evallab.upstream_grader:UpstreamDockerEnvironment"]
    assert not options["spend_ledger"].exists()


def test_spend_ledger_keeps_failures_and_releases_only_proven_unused_reserve(
    task: Path,
    tmp_path: Path,
    local_transport: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = modal_options(tmp_path)
    execute = grader._execute_native

    async def preparation_failure(*_: Any) -> dict[str, Any]:
        raise RuntimeError("cold preparation failed")

    monkeypatch.setattr(grader, "_execute_native", preparation_failure)
    failed = grader.run_upstream_grader(
        task, oracle=True, output_dir=tmp_path / "failed-build", **options
    )
    assert failed["reward"] is None
    assert failed["spend"]["this_run_reserved_usd"] == pytest.approx(0.693794, abs=0.000002)
    assert failed["spend"]["reconciliation"]["status"] == "retained-full-reservation"
    monkeypatch.setattr(grader, "_execute_native", execute)
    controls = [
        grader.run_upstream_grader(
            task,
            output_dir=tmp_path / f"control-{index}",
            **({"oracle": True} if index % 2 == 0 else {"blank": True}),
            **options,
        )
        for index in range(6)
    ]
    assert [result["reward"] for result in controls] == [1, 0, 1, 0, 1, 0]
    assert all(result["lifecycle"]["modal_budget_closure_proven"] for result in controls)
    assert all(
        result["spend"]["this_run_finalized_estimate_usd"] == pytest.approx(0.01319, abs=0.000002)
        for result in controls
    )
    ledger = json.loads(options["spend_ledger"].read_bytes())
    assert len(ledger["reservations"]) == 7
    assert ledger["reserved_usd"] == pytest.approx(0.772934, abs=0.000015)
    assert "finalized_estimate_usd" not in ledger["reservations"][0]
    assert all(
        item["closure"]["all_owned_sandboxes_terminated"] for item in ledger["reservations"][1:]
    )
    assert ledger["price_basis"]["cpu_usd_per_core_second"] == 0.00003942
    assert ledger["price_basis"]["memory_usd_per_gib_second"] == 0.00000667
    assert ledger["price_basis"]["contingency_multiplier"] == 1.25
    monkeypatch.setattr(grader, "_execute_native", preparation_failure)
    another_failure = grader.run_upstream_grader(
        task, blank=True, output_dir=tmp_path / "another-failure", **options
    )
    assert another_failure["status"] == "error"
    ledger_before = options["spend_ledger"].read_bytes()
    refused = grader.run_upstream_grader(
        task, blank=True, output_dir=tmp_path / "cannot-reserve", **options
    )
    assert refused["status"] == "refused"
    assert refused["reward"] is None
    assert refused["executed"] is False
    assert "remaining" in refused["error"]
    assert len(local_transport) == 6
    assert options["spend_ledger"].read_bytes() == ledger_before
    changed_cap = grader.plan_upstream_grader(task, blank=True, **(options | {"max_usd": 10}))
    assert changed_cap["runnable"] is False
    assert "differs" in changed_cap["refusals"][0]


def test_completed_grade_with_unknown_closure_keeps_full_reserve(
    task: Path,
    tmp_path: Path,
    local_transport: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = grader._execute_native

    async def unknown(config: dict[str, Any], active: grader._Execution) -> dict[str, Any]:
        payload = await execute(config, active)
        active.environments[-1]._upstream_termination_proven = False
        active.cleanup_errors.append("public SDK wait failed; terminal state unconfirmed")
        return payload

    monkeypatch.setattr(grader, "_execute_native", unknown)
    options = modal_options(tmp_path)
    results = [
        grader.run_upstream_grader(
            task, oracle=True, output_dir=tmp_path / f"unknown-{index}", **options
        )
        for index in range(2)
    ]
    assert [result["reward"] for result in results] == [1, 1]
    assert all(result["status"] == "measured" and result["error"] is None for result in results)
    assert all(not result["lifecycle"]["modal_budget_closure_proven"] for result in results)
    assert all(
        result["spend"]["reconciliation"]["status"] == "retained-full-reservation"
        for result in results
    )
    ledger_before = options["spend_ledger"].read_bytes()
    assert all(
        "finalized_estimate_usd" not in item for item in json.loads(ledger_before)["reservations"]
    )
    refused = grader.run_upstream_grader(
        task, blank=True, output_dir=tmp_path / "unknown-third", **options
    )
    assert refused["status"] == "refused"
    assert refused["executed"] is False
    assert len(local_transport) == 2
    assert options["spend_ledger"].read_bytes() == ledger_before


def test_spend_cap_is_serialized_across_independent_processes(tmp_path: Path) -> None:
    ledger = tmp_path / "process-spend.json"
    command = """
import sys
from pathlib import Path
from evallab.upstream_grader import _reserve_spend, SpendCapReached
try:
    _reserve_spend(Path(sys.argv[1]), approval="one campaign", cap=1.5,
                   reservation={"reserved_usd": 0.75}, identity={})
    print("accepted")
except SpendCapReached:
    print("refused")
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", command, str(ledger)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    results = [process.communicate(timeout=30) for process in processes]
    assert all(process.returncode == 0 for process in processes), results
    assert sorted(stdout.strip() for stdout, _ in results) == ["accepted", "accepted", "refused"]
    persisted = json.loads(ledger.read_text())
    assert persisted["reserved_usd"] == 1.5
    assert len(persisted["reservations"]) == 2
    assert len({item["reservation_id"] for item in persisted["reservations"]}) == 2


def test_ledger_cannot_write_into_frozen_task(task: Path, tmp_path: Path) -> None:
    options = modal_options(tmp_path) | {"spend_ledger": task / "new-ledger.json"}
    before = grader._tree_digest(task)
    planned = grader.plan_upstream_grader(task, blank=True, **options)
    assert planned["runnable"] is False
    assert "immutable" in planned["refusals"][0]
    assert grader._tree_digest(task) == before


@pytest.mark.parametrize(
    "failure",
    ["missing", "nan", "mismatch", "solution-exit", "task-drift", "staged-drift", "timeout"],
)
def test_errors_are_unmeasured_not_zero_or_observed_pass(
    task: Path,
    tmp_path: Path,
    local_transport: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    execute = grader._execute_native

    async def corrupt(config: dict[str, Any], active: grader._Execution) -> dict[str, Any]:
        payload = await execute(config, active)
        trial = Path(config["trials_dir"]) / config["trial_name"]
        if failure == "missing":
            (trial / "verifier" / "reward.txt").unlink()
        elif failure == "nan":
            (trial / "verifier" / "reward.txt").write_text("nan")
        elif failure == "mismatch":
            payload["verifier_result"]["rewards"] = {"reward": 0}
        elif failure == "solution-exit":
            (trial / "agent" / "exit-code.txt").write_text("17")
        elif failure == "task-drift":
            (task / "tests" / "test.sh").write_text("changed")
        elif failure == "staged-drift":
            (Path(config["task"]["path"]) / "tests" / "test.sh").write_text("changed")
        else:
            raise TimeoutError
        return payload

    monkeypatch.setattr(grader, "_execute_native", corrupt)
    result = grader.run_upstream_grader(
        task, oracle=True, backend="docker", output_dir=tmp_path / "error"
    )
    assert result["status"] == ("timeout" if failure == "timeout" else "error")
    assert result["reward"] is None
    assert result["rewards"] is None
    assert result["error"]
    if failure not in {"missing", "nan"}:
        assert result["observed_native_rewards"] == {"reward": 1}
    assert (
        result["per_test"]["tests"][0]["status"] == "passed"
    )  # evidence survives invalid measurement


def test_nonzero_verifier_exit_does_not_invent_reward(
    task: Path, tmp_path: Path, local_transport: list[dict[str, Any]]
) -> None:
    script = task / "tests" / "test.sh"
    script.write_text(script.read_text() + "exit 17\n")
    result = grader.run_upstream_grader(
        task, blank=True, backend="docker", output_dir=tmp_path / "nonzero"
    )
    assert result["status"] == "measured"
    assert result["reward"] == 0
    assert result["per_test"]["tests"][0]["status"] == "failed"


def test_native_trace_checks_override_always_passed_transport_wrapper(tmp_path: Path) -> None:
    (tmp_path / "ctrf.json").write_text(
        json.dumps({"results": {"tests": [{"name": "wrapper", "status": "passed"}]}})
    )
    checks = [
        {"name": "dispatch-correct", "passed": False, "weight": 2, "detail": "wrong slot"},
        {"name": "audit", "passed": True, "weight": 1, "detail": "present"},
    ]
    (tmp_path / "trace_results.json").write_text(json.dumps({"checks": checks}))
    parsed = grader.parse_per_test_results(tmp_path)
    assert parsed["format"] == "trace-checks"
    assert [(test["id"], test["status"]) for test in parsed["tests"]] == [
        ("dispatch-correct", "failed"),
        ("audit", "passed"),
    ]
    assert parsed["tests"][0]["raw"] == checks[0]


def test_native_cases_and_continuous_axes_are_not_fabricated_tests(tmp_path: Path) -> None:
    (tmp_path / "trace_results.json").write_text(
        json.dumps(
            {
                "cases": {
                    "UB-001": {"passed": False, "errors": ["missing"]},
                    "UB-002": {"passed": True},
                }
            }
        )
    )
    details = {"score": 0.57, "geometry_similarity": 0.61, "threshold": 0.8, "base": {"score": 0.4}}
    (tmp_path / "reward_details.json").write_text(json.dumps(details))
    parsed = grader.parse_per_test_results(tmp_path)
    assert parsed["format"] == "trace-cases"
    assert [test["id"] for test in parsed["tests"]] == ["UB-001", "UB-002"]
    assert grader._score_details(tmp_path)["data"] == details
    (tmp_path / "trace_results.json").unlink()
    assert grader.parse_per_test_results(tmp_path)["tests"] == []


def test_canonical_ctrf_deduplicates_retry_reports_and_preserves_raw_files(tmp_path: Path) -> None:
    for name, outcome in (
        ("ctrf_0.json", "failed"),
        ("ctrf_1.json", "passed"),
        ("ctrf.json", "passed"),
    ):
        (tmp_path / name).write_text(
            json.dumps({"results": {"tests": [{"name": "same-gate", "status": outcome}]}})
        )
    parsed = grader.parse_per_test_results(tmp_path)
    assert len(parsed["tests"]) == 1
    assert parsed["tests"][0]["status"] == "passed"
    assert len(parsed["non_selected_reports"]) == 2
    assert (tmp_path / "ctrf_0.json").exists()
    (tmp_path / "ctrf.json").unlink()
    repeated = grader.parse_per_test_results(tmp_path)
    assert repeated["tests"][0]["status"] == "multiple-observations"
    assert [item["raw_status"] for item in repeated["tests"][0]["observations"]] == [
        "failed",
        "passed",
    ]


@pytest.mark.parametrize(
    "report,content,kind,identities",
    [
        (
            "pytest.json",
            '{"tests":[{"nodeid":"test_x.py::test_a[p]","outcome":"failed","call":{"duration":0.2}}]}',
            "pytest-json",
            ["test_x.py::test_a[p]"],
        ),
        (
            "junit.xml",
            '<testsuite><testcase classname="group" name="one" time="0.1"/><testcase classname="group" name="two"><failure>detail</failure></testcase></testsuite>',
            "junit",
            ["group::one", "group::two"],
        ),
        (
            "test-stdout.txt",
            "test_x.py::test_a[p] PASSED [50%]\nFAILED test_x.py::test_b - AssertionError\n2 tests collected\n",
            "pytest-output",
            ["test_x.py::test_a[p]", "test_x.py::test_b"],
        ),
    ],
)
def test_native_supported_report_formats_preserve_identities(
    tmp_path: Path, report: str, content: str, kind: str, identities: list[str]
) -> None:
    (tmp_path / report).write_text(content)
    parsed = grader.parse_per_test_results(tmp_path)
    assert parsed["format"] == kind
    assert [test["id"] for test in parsed["tests"]] == identities


def test_visual_checks_override_wrapper_and_playwright_preserves_real_retry_outcomes(
    tmp_path: Path,
) -> None:
    (tmp_path / "ctrf.json").write_text(
        json.dumps({"results": {"tests": [{"name": "wrapper", "status": "passed"}]}})
    )
    checks = [
        {
            "name": "desktop header aligned",
            "passed": False,
            "message": "offset",
            "actual": 12,
            "expected": 0,
        }
    ]
    visual = tmp_path / "visual-results.json"
    visual.write_text(
        json.dumps([{"page": "landing", "viewport": "desktop", "checks": checks, "passed": False}])
    )
    parsed = grader.parse_per_test_results(tmp_path)
    assert parsed["format"] == "visual-checks"
    assert parsed["tests"][0]["id"] == "landing @ desktop :: desktop header aligned"
    assert parsed["tests"][0]["status"] == "failed"
    assert parsed["tests"][0]["raw"]["check"] == checks[0]
    visual.unlink()
    (tmp_path / "ctrf.json").unlink()
    report = {
        "suites": [
            {
                "title": "page.spec.ts",
                "specs": [
                    {
                        "id": "native-spec-id",
                        "title": "layout stays stable",
                        "tests": [
                            {
                                "projectName": "chromium",
                                "expectedStatus": "passed",
                                "results": [
                                    {"status": "failed", "duration": 20},
                                    {"status": "passed", "duration": 10},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    (tmp_path / "playwright-report.json").write_text(json.dumps(report))
    playwright = grader.parse_per_test_results(tmp_path)
    assert playwright["format"] == "playwright"
    assert len(playwright["tests"]) == 1
    assert playwright["tests"][0]["id"] == "native-spec-id :: chromium"
    assert playwright["tests"][0]["status"] == "multiple-observations"
    assert [item["duration_seconds"] for item in playwright["tests"][0]["observations"]] == [
        0.02,
        0.01,
    ]
    report["suites"][0]["specs"][0]["tests"][0]["results"] = []
    (tmp_path / "playwright-report.json").write_text(json.dumps(report))
    assert grader.parse_per_test_results(tmp_path)["tests"] == []


def test_aggregate_only_and_invalid_reports_never_synthesize_passes(tmp_path: Path) -> None:
    (tmp_path / "test-stdout.txt").write_text("3 passed in 1.2s\n")
    (tmp_path / "broken.json").write_text('{"tests": [')
    (tmp_path / "nan.json").write_text(
        '{"tests":[{"nodeid":"test_x.py::test_one","outcome":"passed","call":{"duration":NaN}}]}'
    )
    parsed = grader.parse_per_test_results(tmp_path)
    assert parsed["status"] == "unavailable"
    assert parsed["tests"] == []
    assert len(parsed["parse_errors"]) == 2
    assert (tmp_path / "nan.json").read_text().endswith("}}]}")


def test_docker_architecture_is_explicit_and_insufficient_capacity_refuses(
    task: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planned = grader.plan_upstream_grader(task, blank=True, platform="linux/amd64")
    monkeypatch.setattr(grader, "docker_available_resources", lambda: (4, 8192))
    monkeypatch.setattr(grader, "_docker_query", lambda *_: {"Os": "linux", "Arch": "arm64"})
    admitted = grader._admit_docker(planned)
    assert admitted["platform"] == "linux/amd64"
    assert admitted["emulated"] is True
    monkeypatch.setattr(grader, "docker_available_resources", lambda: (1, 8192))
    with pytest.raises(grader.GraderRefusal, match="task needs"):
        grader._admit_docker(planned)


def _module(monkeypatch: pytest.MonkeyPatch, name: str, **values: Any) -> None:
    module = ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)


def test_native_watchdog_always_cleans_tracked_environments_and_records_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = []
    active = grader._Execution({"timeout_seconds": 0.01})

    class Environment:
        _upstream_cleaned = False

        async def stop(self, delete: bool) -> None:
            stopped.append(delete)
            raise RuntimeError("termination denied")

    class Trial:
        @staticmethod
        async def create(_: Any) -> Any:
            active.environments.append(Environment())
            return Trial()

        async def run(self) -> None:
            await asyncio.sleep(60)

    _module(
        monkeypatch,
        "harbor.models.trial.config",
        TrialConfig=SimpleNamespace(model_validate=lambda value: value),
    )
    _module(monkeypatch, "harbor.trial.trial", Trial=Trial)
    with pytest.raises(TimeoutError):
        asyncio.run(grader._execute_native({}, active))
    assert stopped == [True]
    assert "termination denied" in active.cleanup_errors[0]
    assert grader._ACTIVE_EXECUTION.get() is None


def test_modal_exact_limits_are_checked_before_preparation_and_failed_start_is_cleaned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = []
    stopped = []

    class Base:
        def __init__(self, *, session_id: str, **_: Any) -> None:
            self.session_id = session_id
            self._effective_cpus = 2
            self._effective_memory_mb = 4096
            self.cpu = (2, 2)
            self.memory = (4096, 4096)

        def _cpu_config(self) -> Any:
            return self.cpu

        def _memory_config(self) -> Any:
            return self.memory

        async def start(self, force_build: bool) -> None:
            started.append(force_build)
            raise RuntimeError("preparation failed")

        async def stop(self, delete: bool) -> None:
            stopped.append(delete)

    _module(monkeypatch, "evallab.harbor_modal", LockedModalEnvironment=Base)
    grader._runtime_type.cache_clear()
    active = grader._Execution({"resources": [{"role": "agent", "cpus": 2, "memory_mb": 4096}]})
    token = grader._ACTIVE_EXECUTION.set(active)
    try:
        runtime = grader._runtime_type("UpstreamModalEnvironment")
        first = runtime(session_id="trial", trial_paths=SimpleNamespace(trial_dir=tmp_path / "a"))
        first.cpu = (0.125, 2)
        with pytest.raises(grader.GraderRefusal, match="exact"):
            asyncio.run(first.start())
        assert started == []
        active.claimed.clear()
        second = runtime(session_id="trial", trial_paths=SimpleNamespace(trial_dir=tmp_path / "b"))
        assert first.session_id != second.session_id
        with pytest.raises(RuntimeError, match="preparation failed"):
            asyncio.run(second.start())
        assert started == [False]
        assert stopped == [True]
        assert second._upstream_cleaned is True
    finally:
        grader._ACTIVE_EXECUTION.reset(token)
        grader._runtime_type.cache_clear()


def test_cli_json_request_plan_and_refusal_are_single_json_values(
    task: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"task": str(task), "blank": True, "backend": "docker", "plan": True})
    )
    assert grader.main(["--request", str(request)]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["executed"] is False
    assert response["input"]["kind"] == "blank"
    request.write_text(
        json.dumps({"task": str(task), "blank": "false", "backend": "docker", "plan": True})
    )
    assert grader.main(["--request", str(request)]) == 2
    invalid = json.loads(capsys.readouterr().out)
    assert invalid["reward"] is None
    assert invalid["status"] == "refused"
    assert "boolean" in invalid["error"]


def test_existing_output_and_input_overlap_are_never_overwritten(
    task: Path, tmp_path: Path
) -> None:
    old = tmp_path / "old"
    old.mkdir()
    receipt = old / "receipt.txt"
    receipt.write_text("owned evidence")
    with pytest.raises(grader.GraderRefusal, match="already exists"):
        grader.run_upstream_grader(task, blank=True, output_dir=old)
    assert receipt.read_text() == "owned evidence"
    with pytest.raises(grader.GraderRefusal, match="overlap"):
        grader.run_upstream_grader(task, blank=True, output_dir=task / "run")


@pytest.mark.parametrize(
    "tests_image,explicit_verifier_image", [(True, False), (False, False), (True, True)]
)
def test_native_resource_inheritance_and_image_precedence_match_read_only_plan(
    task: Path, tests_image: bool, explicit_verifier_image: bool
) -> None:
    native_config = pytest.importorskip("harbor.models.task.config")
    native_paths = pytest.importorskip("harbor.models.task.paths")
    native_mode = pytest.importorskip("harbor.models.task.verifier_mode")
    config_path = task / "task.toml"
    text = config_path.read_text().replace(
        "build_timeout_sec = 600",
        'docker_image = "registry.example/agent:fixed"\nbuild_timeout_sec = 600',
    )
    if not tests_image:
        (task / "tests" / "Dockerfile").unlink()
    if explicit_verifier_image:
        text += '\n[verifier.environment]\ncpus = 2\nmemory_mb = 4096\ndocker_image = "registry.example/verifier:fixed"\n'
    config_path.write_text(text)
    declared = tomllib.loads(text)
    native = native_config.TaskConfig.model_validate(
        {"environment": declared["environment"], "verifier": declared["verifier"]}
    )
    definition = native_mode.resolve_verifier_environment_definition(
        native, native_paths.TaskPaths(task)
    )
    assert definition is not None
    planned = grader.plan_upstream_grader(task, blank=True)
    verifier = planned["resources"][1]
    assert (definition.config.cpus, definition.config.memory_mb) == (
        verifier["cpus"],
        verifier["memory_mb"],
    )
    assert definition.directory == task / verifier["build_context"]
    assert definition.config.docker_image == verifier["docker_image"]
    if not explicit_verifier_image:
        assert (verifier["cpus"], verifier["memory_mb"]) == (2, 4096)


@pytest.mark.parametrize(
    "output,available,valid",
    [
        (
            "Filesystem 1024-blocks Used Available Capacity Mounted on\nrootfs 40000000 1000 20000000 1% /\n",
            20000000,
            True,
        ),
        (
            "Filesystem 1024-blocks Used Available Capacity Mounted on\nrootfs 40000000 1000 1000000 1% /\n",
            1000000,
            False,
        ),
        ("not a capacity report", None, False),
    ],
)
def test_storage_is_a_pre_execution_minimum_not_a_claimed_disk_quota(
    output: str, available: int | None, valid: bool
) -> None:
    commands = []
    active = grader._Execution(
        {"resources": [{"role": "verifier", "cpus": 2, "memory_mb": 4096, "storage_mb": 10240}]}
    )

    async def execute(command: str, **_: Any) -> Any:
        commands.append(command)
        return SimpleNamespace(stdout=output, return_code=0)

    token = grader._ACTIVE_EXECUTION.set(active)
    try:
        environment = SimpleNamespace(_upstream_role="verifier", exec=execute)
        if valid:
            asyncio.run(grader._verify_storage_capacity(environment))
        else:
            with pytest.raises(grader.GraderRefusal, match="storage|filesystem"):
                asyncio.run(grader._verify_storage_capacity(environment))
    finally:
        grader._ACTIVE_EXECUTION.reset(token)
    assert commands == ["df -Pk /"]
    observation = active.storage_observations[0]
    assert observation["hard_disk_quota_enforced"] is False
    assert observation["minimum_verified"] is valid
    assert observation.get("available_kib") == available


def test_extra_native_reports_are_captured_only_from_verifier_before_teardown(
    tmp_path: Path,
) -> None:
    active = grader._Execution(
        {
            "task": {"id": "upstream/cumulative-layout-shift"},
            "resources": [{"role": "agent"}, {"role": "verifier"}],
        }
    )
    calls = []

    async def is_file(remote: str) -> bool:
        calls.append(("is_file", remote))
        return True

    async def download(remote: str, local: Path) -> None:
        calls.append(("download", remote))
        if remote.endswith("cls-results.json"):
            raise RuntimeError("extra report transport failed")
        local.write_text(json.dumps({"native_path": remote}))

    token = grader._ACTIVE_EXECUTION.set(active)
    try:
        environment = SimpleNamespace(
            _upstream_role="agent",
            _upstream_trial_dir=tmp_path,
            is_file=is_file,
            download_file=download,
        )
        asyncio.run(grader._capture_extra_reports(environment))
        assert calls == []
        environment._upstream_role = "verifier"
        asyncio.run(grader._capture_extra_reports(environment))
    finally:
        grader._ACTIVE_EXECUTION.reset(token)
    assert len([item for item in calls if item[0] == "download"]) == 3
    saved = tmp_path / "verifier" / "upstream-extra" / "eval-result.json"
    assert json.loads(saved.read_text())["native_path"] == "/tests/eval/results/eval-result.json"
    assert active.evidence_errors == [
        "native extra report /tests/eval/results/cls-results.json: extra report transport failed"
    ]
    assert active.cleanup_errors == []


@pytest.mark.parametrize("failure", ["cpu", "memory"])
def test_native_docker_platform_overlay_preserves_resources_and_rejects_unenforced_limits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    events = []

    class Base:
        def __init__(self, *, session_id: str, **_: Any) -> None:
            self.session_id = session_id
            self._effective_cpus = 2
            self._effective_memory_mb = 4096
            self._container_id = "inert-owned-container"

        async def _write_resources_compose_file(self) -> Path:
            path = tmp_path / "native-resources.json"
            path.write_text(
                json.dumps(
                    {
                        "services": {
                            "main": {
                                "deploy": {
                                    "resources": {"limits": {"cpus": "2", "memory": "4096M"}}
                                }
                            }
                        }
                    }
                )
            )
            return path

        async def start(self, force_build: bool) -> None:
            events.append(("start", force_build))

        async def stop(self, delete: bool) -> None:
            events.append(("stop", delete))

        async def _daemon_json(self, *_: str) -> Any:
            return [{"HostConfig": host}]

    _module(monkeypatch, "evallab.harbor_host_isolation", HostIsolatedDockerEnvironment=Base)
    monkeypatch.setattr(grader, "docker_available_resources", lambda: (8, 16384))
    host = {"NanoCpus": 2_000_000_000, "Memory": 4096 * 1024 * 1024}
    grader._runtime_type.cache_clear()
    active = grader._Execution({"resources": [{"role": "agent", "cpus": 2, "memory_mb": 4096}]})
    token = grader._ACTIVE_EXECUTION.set(active)
    try:
        runtime = grader._runtime_type("UpstreamDockerEnvironment")
        first = runtime(
            upstream_platform="linux/amd64",
            session_id="same-name",
            trial_paths=SimpleNamespace(trial_dir=tmp_path / "one"),
        )
        overlay = json.loads(asyncio.run(first._write_resources_compose_file()).read_text())[
            "services"
        ]["main"]
        assert overlay["platform"] == "linux/amd64"
        assert overlay["deploy"]["resources"]["limits"] == {"cpus": "2", "memory": "4096M"}
        asyncio.run(first.start())
        assert active.observations[0]["limits_verified"] is True
        asyncio.run(first.stop())
        active.claimed.clear()
        second = runtime(
            upstream_platform="linux/amd64",
            session_id="same-name",
            trial_paths=SimpleNamespace(trial_dir=tmp_path / "two"),
        )
        assert second.session_id != first.session_id
        host["NanoCpus" if failure == "cpu" else "Memory"] = 0
        with pytest.raises(RuntimeError, match="exact declared CPU/RAM"):
            asyncio.run(second.start())
        asyncio.run(second.stop())
        assert second._upstream_cleaned is True
    finally:
        grader._ACTIVE_EXECUTION.reset(token)
        grader._runtime_type.cache_clear()
    assert events == [("start", False), ("stop", True), ("start", False), ("stop", True)]


@pytest.mark.parametrize("mode", ["clean", "sdk-terminal", "unknown", "swallowed", "partial"])
def test_modal_closure_requires_completed_hydration_and_terminal_public_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    waits = []

    async def wait(*, raise_on_termination: bool) -> None:
        waits.append(raise_on_termination)
        sandbox.returncode = None if mode in {"unknown", "partial"} and len(waits) == 1 else 0

    sandbox = SimpleNamespace(
        object_id="sb-" + "a" * 22, wait=SimpleNamespace(aio=wait), returncode=None
    )
    if mode == "sdk-terminal":
        modal = pytest.importorskip("modal")
        api_pb2 = pytest.importorskip("modal_proto.api_pb2")

        async def sandbox_wait(request: Any) -> Any:
            return api_pb2.SandboxWaitResponse(
                result=api_pb2.GenericResult(
                    status=api_pb2.GenericResult.GENERIC_STATUS_TERMINATED
                ),
                metadata=api_pb2.SandboxHandleMetadata(),
            )

        client = SimpleNamespace(_stub=SimpleNamespace(SandboxWait=sandbox_wait))
        sandbox = asyncio.run(modal.Sandbox.from_id.aio("sb-" + "a" * 22, client=client))

    class Base:
        def __init__(self, *, session_id: str, **_: Any) -> None:
            self.session_id = session_id
            self._effective_cpus = 2
            self._effective_memory_mb = 4096
            self._sandbox_timeout = 230
            self._sandbox = None

        def _cpu_config(self) -> tuple[int, int]:
            return (2, 2)

        def _memory_config(self) -> tuple[int, int]:
            return (4096, 4096)

        async def start(self, force_build: bool) -> None:
            self._sandbox = sandbox
            if mode == "partial":
                raise RuntimeError("hydration/start interrupted")

        async def _terminate_sandbox(self) -> None:
            if mode == "swallowed":
                raise RuntimeError("native swallowed termination error")

        async def stop(self, delete: bool) -> None:
            # Mirrors Harbor's native cleanup boundary, not a provider.
            with contextlib.suppress(RuntimeError):
                await self._terminate_sandbox()
            self._sandbox = None

    _module(monkeypatch, "evallab.harbor_modal", LockedModalEnvironment=Base)
    grader._runtime_type.cache_clear()
    active = grader._Execution({"resources": [{"role": "agent", "cpus": 2, "memory_mb": 4096}]})
    token = grader._ACTIVE_EXECUTION.set(active)
    try:
        environment = grader._runtime_type("UpstreamModalEnvironment")(
            session_id="trial", trial_paths=SimpleNamespace(trial_dir=tmp_path)
        )
        if mode == "partial":
            with pytest.raises(RuntimeError, match="hydration/start interrupted"):
                asyncio.run(environment.start())
            assert environment._sandbox is None
            assert environment._upstream_sandbox is sandbox
            asyncio.run(environment.stop())
        else:
            asyncio.run(environment.start())
            asyncio.run(environment.stop())
            if mode == "unknown":
                assert environment._upstream_cleaned is False
        active.elapsed_seconds = 12.25
        receipt = active.closure_receipt()
        if mode in {"clean", "sdk-terminal"}:
            assert receipt is not None
            assert receipt["all_owned_sandboxes_terminated"] is True
            assert receipt["all_image_preparations_completed"] is True
            assert receipt["sandboxes"][0]["sandbox_id"] == "sb-" + "a" * 22
            assert receipt["sandboxes"][0]["exit_code"] == (137 if mode == "sdk-terminal" else 0)
            assert active.cleanup_errors == []
        else:
            assert receipt is None
            assert active.cleanup_errors
        assert all(flag is False for flag in waits)
    finally:
        grader._ACTIVE_EXECUTION.reset(token)
        grader._runtime_type.cache_clear()


@pytest.mark.parametrize("kind", ["original", "frozen-copy", "ledger"])
def test_cli_json_output_never_mutates_original_frozen_inputs_or_budget(
    task: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    output = tmp_path / "fresh-run"
    ledger = tmp_path / "spend.json"
    destination = (
        task / "response.json"
        if kind == "original"
        else output / "inputs" / "task" / "response.json"
        if kind == "frozen-copy"
        else ledger
    )
    before = grader._tree_digest(task)
    args = [
        str(task),
        "--blank",
        "--plan",
        "--output-dir",
        str(output),
        "--spend-ledger",
        str(ledger),
        "--output",
        str(destination),
    ]
    assert grader.main(args) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "refused"
    assert not destination.exists()
    assert grader._tree_digest(task) == before
    assert not ledger.exists()


def test_removed_frozen_task_is_unmeasured_even_with_native_pass(
    task: Path,
    tmp_path: Path,
    local_transport: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = grader._execute_native

    async def remove(config: dict[str, Any], active: grader._Execution) -> dict[str, Any]:
        payload = await execute(config, active)
        shutil.rmtree(config["task"]["path"])
        return payload

    monkeypatch.setattr(grader, "_execute_native", remove)
    result = grader.run_upstream_grader(
        task, oracle=True, backend="docker", output_dir=tmp_path / "removed"
    )
    assert result["status"] == "error"
    assert result["reward"] is None
    assert result["observed_native_rewards"] == {"reward": 1}
    assert "disappeared" in result["error"]


def test_module_cli_grades_real_fixture_through_the_canonical_runtime(
    task: Path,
    tmp_path: Path,
    local_transport: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_run = subprocess.run

    def guarded_run(command: Any, *args: Any, **kwargs: Any) -> Any:
        if command[0] == "docker":
            raise AssertionError("the inert transport must not invoke a real Docker daemon")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evallab.upstream_grader",
            str(task),
            "--oracle",
            "--backend",
            "docker",
            "--output-dir",
            str(tmp_path / "module-cli"),
        ],
    )
    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("evallab.upstream_grader", run_name="__main__", alter_sys=True)
    result = json.loads(capsys.readouterr().out)
    assert stopped.value.code == 0
    assert result["status"] == "measured"
    assert result["reward"] == 1
    assert result["per_test"]["tests"][0]["status"] == "passed"
