"""Operational/scientific boundaries for direct replay, without task/cloud execution."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parents[1] / "research/experiments/leak-oracle"
BASE = "a" * 40
FIX = "b" * 40
PATCH = b"diff --git a/pkg/core.py b/pkg/core.py\n--- a/pkg/core.py\n+++ b/pkg/core.py\n@@ -1 +1 @@\n-old\n+new\n"


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.syspath_prepend(str(HERE))
    spec = importlib.util.spec_from_file_location("har191_runtime_test", HERE / "runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source() -> dict:
    return {
        "task_id": "format-code-task-000001",
        "task_path": "/unused-selected-package",
        "run_digest": "sha256:" + "1" * 64,
        "original_run_digest": "sha256:" + "9" * 64,
        "selected_run": "repair",
        "image": "registry.invalid/source@sha256:" + "2" * 64,
        "test_patch_sha256": "3" * 64,
        "workdir": "/testbed",
    }


def test_inner_failure_is_not_outer_shell_success(runtime):
    result = runtime._verifier_result(
        0, b"real log\ntest command exited 1\n", b"0\n", complete=True
    )
    assert result["status"] == "complete"
    assert result["verifier_exit_code"] == 1
    assert result["reward"] == 0
    assert result["test_patch_applied"] is True


@pytest.mark.parametrize(
    "outer,tail,reward,complete,expected",
    [
        (0, b"test command exited 0\n", b"1\n", True, "complete"),
        (0, b"test command exited 1\n", None, True, "infrastructure-error"),
        (1, b"hidden tests could not be applied\n", None, True, "infrastructure-error"),
        (0, b"test command exited 1\n", b"1\n", True, "infrastructure-error"),
        (0, b"test command exited 0\n", b"unavailable\n", True, "infrastructure-error"),
        (0, b"test command exited 124\n", b"0\n", True, "timeout"),
        (0, b"test command exited 137\n", b"0\n", True, "infrastructure-error"),
        (-1, b"test command exited 0\n", b"1\n", True, "infrastructure-error"),
        (0, b"test command exited 0\n", b"1\n", False, "infrastructure-error"),
        (0, b"test command exited 127\n", b"0\n", True, "infrastructure-error"),
    ],
)
def test_unscored_and_abnormal_results_are_not_oracle_failures(
    runtime, outer, tail, reward, complete, expected
):
    assert runtime._verifier_result(outer, tail, reward, complete=complete)["status"] == expected


class Sandbox:
    def __init__(self, identity, cleanup_error=None):
        self.object_id = identity
        self.cleanup_error = cleanup_error
        self.terminated = False
        self.terminate = SimpleNamespace(aio=self.finish)

    async def finish(self, *, wait):
        assert wait is True
        if self.cleanup_error:
            raise self.cleanup_error
        self.terminated = True
        return 137


@pytest.fixture
def harness(runtime, monkeypatch, tmp_path):
    """Scripted remote observations; no benchmark code executes on the host."""
    observed = {"sandboxes": [], "failures": {}, "healthchecks": [], "uploads": []}
    inputs = {
        "files": {
            "tests/test.patch": b"test patch",
            "tests/test.sh": b"selected grader",
            "tests/test_command.sh": b"selected command",
        },
        "healthcheck": "selected-repair-healthcheck",
        "config": {},
    }
    monkeypatch.setattr(runtime, "_inputs", lambda task: inputs)

    async def build_image(task, app):
        return SimpleNamespace(object_id="im-prebuilt"), {
            "status": "complete",
            "image_id": "im-prebuilt",
            "elapsed_seconds": 0,
        }

    monkeypatch.setattr(runtime, "_build_image", build_image)

    class Session(runtime._Session):
        async def create(self):
            self.started = 100.0
            self.deadline = 280.0
            sb = Sandbox("sb-" + self.directory.name)
            observed["sandboxes"].append(sb)
            self.sandbox = sb
            self.arm["sandbox_id"] = sb.object_id
            self.report.update(
                {
                    "sandbox_id": sb.object_id,
                    "creation_attempted": True,
                    "terminal_confirmed": False,
                    "lifetime_upper_seconds": 180,
                    "creation_started_at": datetime.now(UTC).isoformat(),
                }
            )

        async def upload(self, remote, data):
            observed["uploads"].append((self.directory.name, remote, data))

        async def extract(self, values, extractor):
            path = self.directory / "source.patch"
            path.write_bytes(PATCH)
            self.patch = PATCH
            self.extraction = {
                "status": "ok",
                "fix": {"sha": FIX},
                "strategy": "synthetic source selection",
                "base": BASE,
                "apply_check_on_base": True,
                "solution_patch_path": str(path),
                "solution_patch_sha256": hashlib.sha256(PATCH).hexdigest(),
            }
            self.arm["solution_patch_sha256"] = self.extraction["solution_patch_sha256"]

        async def command(self, phase, command):
            if phase == "setup":
                observed["healthchecks"].append(command)
            failure = observed["failures"].get((self.directory.name, phase))
            if isinstance(failure, BaseException):
                raise failure
            code = failure if failure is not None else 0
            if phase in ("image-base", "setup-base"):
                data = b"/var/lib/mimo/git-hidden\n" + BASE.encode() + b"\n"
            elif phase == "setup-recorded-base":
                data = BASE.encode() + b"\n"
            elif phase == "verifier":
                data = b"console excerpt\ntest command exited 1\n"
            elif phase == "verifier-reward":
                data = b"0\n"
            elif phase == "verifier-full-output":
                data = b"prefix omitted by outer shell\x00\xff\n" + b"full output\n" * 1500
            elif phase == "verifier-test-apply":
                data = b"Applied patch cleanly.\n"
            else:
                data = command.encode() + b"\n"
            path = self.directory / (phase + ".stdout.log")
            err = self.directory / (phase + ".stderr.log")
            path.write_bytes(data)
            err.write_bytes(b"")
            for name, file in (("stdout", path), ("stderr", err)):
                self.arm["artifacts"][phase + "." + name] = runtime._file_record(file)
                self.append(phase + " " + name, file)
            return {"exit_code": code, "stdout": path, "stderr": err}

    monkeypatch.setattr(runtime, "_Session", Session)
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 101.2)
    extractor = tmp_path / "extract.py"
    extractor.write_bytes(b"portable extractor bytes")
    return observed, extractor


def test_pair_preserves_nop_when_source_patch_application_fails(runtime, harness, tmp_path):
    observed, extractor = harness
    observed["failures"][("oracle", "solution-apply")] = 1
    receipt = asyncio.run(runtime.run_pair(source(), object(), tmp_path / "pair", extractor))
    assert receipt["arms"]["oracle"]["status"] == "infrastructure-error"
    assert receipt["arms"]["oracle"]["solution_patch_applied"] is False
    assert receipt["arms"]["nop"]["status"] == "complete"
    assert receipt["arms"]["nop"]["verifier_exit_code"] == 1
    assert receipt["arms"]["nop"]["solution_patch_sha256"] is None
    assert len(observed["sandboxes"]) == 2
    assert all(sb.terminated for sb in observed["sandboxes"])
    assert receipt["arms"]["nop"]["sandbox_id"] != receipt["arms"]["oracle"]["sandbox_id"]
    assert observed["healthchecks"] == ["selected-repair-healthcheck"] * 2
    evidence = Path(receipt["arms"]["nop"]["evidence_path"]).read_bytes()
    assert b"prefix omitted by outer shell\x00\xff\n" in evidence
    assert hashlib.sha256(evidence).hexdigest() == receipt["arms"]["nop"]["evidence_sha256"]


def test_failed_selected_setup_is_unscored_and_nop_independent(runtime, harness, tmp_path):
    observed, extractor = harness
    observed["failures"][("oracle", "setup")] = 1
    receipt = asyncio.run(runtime.run_pair(source(), object(), tmp_path / "pair", extractor))
    assert receipt["arms"]["oracle"]["verifier_exit_code"] is None
    assert receipt["arms"]["oracle"]["status"] == "infrastructure-error"
    assert receipt["arms"]["nop"]["status"] == "complete"
    assert not any(
        arm == "oracle" and remote.startswith("/tests/") for arm, remote, _ in observed["uploads"]
    )


def test_missing_reward_is_retained_without_scientific_failure(runtime, harness, tmp_path):
    observed, extractor = harness
    observed["failures"][("oracle", "verifier-reward")] = 44
    receipt = asyncio.run(runtime.run_pair(source(), object(), tmp_path / "pair", extractor))
    arm = receipt["arms"]["oracle"]
    assert arm["status"] == "infrastructure-error"
    assert arm["verifier_exit_code"] == 1
    assert "no reward" in arm["reason"]
    assert receipt["arms"]["nop"]["status"] == "complete"


def test_cancellation_returns_cleanup_accounting_and_unrun_other_arm(runtime, harness, tmp_path):
    observed, extractor = harness
    observed["failures"][("oracle", "setup")] = asyncio.CancelledError()
    receipt = asyncio.run(runtime.run_pair(source(), object(), tmp_path / "pair", extractor))
    assert receipt["cancelled"] is True
    assert receipt["arms"]["oracle"]["status"] == "infrastructure-error"
    assert receipt["arms"]["nop"]["status"] == "not-run"
    assert len(receipt["sandboxes"]) == 2
    assert all(report["terminal_confirmed"] for report in receipt["sandboxes"])
    assert len(observed["sandboxes"]) == 1
    assert receipt["sandboxes"][1]["creation_attempted"] is False
    assert receipt["sandboxes"][1]["lifetime_upper_seconds"] == 0
    assert receipt["sandboxes"][1]["sandbox_id"] is None
    assert all(sb.terminated for sb in observed["sandboxes"])


def test_deadline_is_censoring_not_an_applied_patch_failure(runtime, harness, tmp_path):
    observed, extractor = harness
    observed["failures"][("oracle", "verifier")] = runtime.Deadline("sandbox expired")
    receipt = asyncio.run(runtime.run_pair(source(), object(), tmp_path / "pair", extractor))
    assert receipt["arms"]["oracle"]["solution_patch_applied"] is True
    assert receipt["arms"]["oracle"]["status"] == "timeout"
    assert receipt["arms"]["oracle"]["verifier_exit_code"] is None
    assert receipt["arms"]["nop"]["status"] == "complete"


def test_unknown_cleanup_keeps_full_lifetime_and_actual_identity(runtime, monkeypatch, tmp_path):
    session = runtime._Session(source(), object(), tmp_path, "locked")
    session.started = 100.0
    session.deadline = 280.0
    session.sandbox = Sandbox("sb-real-id", OSError("provider unavailable"))
    session.report.update(
        {
            "creation_attempted": True,
            "terminal_confirmed": False,
            "sandbox_id": "sb-real-id",
            "lifetime_upper_seconds": 180,
        }
    )
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 102.0)
    asyncio.run(session.cleanup())
    assert session.report["terminal_confirmed"] is False
    assert session.report["lifetime_upper_seconds"] == 180
    assert session.report["sandbox_id"] == "sb-real-id"
    assert session.report["terminated_at"] is None
    assert "provider unavailable" in session.report["error"]


@pytest.mark.parametrize("cached_exit", [124, None])
def test_expired_sandbox_requires_actual_provider_terminal_evidence(
    runtime, monkeypatch, tmp_path, cached_exit
):
    session = runtime._Session(source(), object(), tmp_path, "locked")
    session.started = 100.0
    session.deadline = 280.0
    session.sandbox = Sandbox("sb-expired", RuntimeError("provider wait reported timeout"))
    session.sandbox.returncode = cached_exit
    session.report.update(
        creation_attempted=True,
        terminal_confirmed=False,
        sandbox_id="sb-expired",
        lifetime_upper_seconds=180,
    )
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 300.0)
    asyncio.run(session.cleanup())
    assert session.report["terminal_confirmed"] is (cached_exit is not None)
    assert session.report["lifetime_upper_seconds"] == 180
    if cached_exit is not None:
        assert session.report["sandbox_exit_code"] == cached_exit


def test_cleanup_is_awaited_when_caller_is_cancelled(runtime, monkeypatch, tmp_path):
    session = runtime._Session(source(), object(), tmp_path, "locked")
    session.started = 100.0
    session.deadline = 280.0
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 102.0)

    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()

        async def terminate(*, wait):
            assert wait is True
            started.set()
            await finish.wait()
            return 137

        session.sandbox = SimpleNamespace(terminate=SimpleNamespace(aio=terminate))
        job = asyncio.create_task(session.cleanup())
        await started.wait()
        job.cancel()
        finish.set()
        await job

    asyncio.run(scenario())
    assert session.report["terminal_confirmed"] is True
    assert session.report["lifetime_upper_seconds"] == 2
    assert session.cancelled is True


def test_expired_deadline_does_not_start_another_process(runtime, monkeypatch, tmp_path):
    session = runtime._Session(source(), object(), tmp_path, "locked")
    session.deadline = 100.0
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 101.0)
    started = []

    async def operation():
        started.append(True)

    with pytest.raises(runtime.Deadline):
        asyncio.run(session.bounded(operation()))
    assert started == []


def test_open_uses_retained_patch_without_mutating_locked_receipt(runtime, harness, tmp_path):
    observed, extractor = harness
    locked = asyncio.run(runtime.run_pair(source(), object(), tmp_path / "pair", extractor))
    before = copy.deepcopy(locked)
    opened = asyncio.run(runtime.run_open(source(), locked, object(), tmp_path / "open"))
    assert locked == before
    assert opened["arm"]["status"] == "complete"
    assert opened["arm"]["network_mode"] == "open"
    assert opened["arm"]["solution_patch_sha256"] == locked["extraction"]["solution_patch_sha256"]
    assert ("open", "/opt/har191/solution.patch", PATCH) in observed["uploads"]
    assert opened["sandbox"]["terminal_confirmed"] is True


def test_changed_open_patch_refuses_allocation_and_preserves_locked_failure(
    runtime, harness, tmp_path
):
    observed, extractor = harness
    locked = asyncio.run(runtime.run_pair(source(), object(), tmp_path / "pair", extractor))
    before = copy.deepcopy(locked)
    Path(locked["extraction"]["solution_patch_path"]).write_bytes(PATCH + b"altered")
    opened = asyncio.run(runtime.run_open(source(), locked, object(), tmp_path / "open"))
    assert locked == before
    assert opened["arm"]["status"] == "infrastructure-error"
    assert opened["sandbox"]["creation_attempted"] is False
    assert opened["sandbox"]["lifetime_upper_seconds"] == 0
    assert len(observed["sandboxes"]) == 2
    assert locked["arms"]["oracle"]["status"] == "complete"


def test_unknown_allocation_is_not_a_free_or_terminal_resource(runtime, monkeypatch, tmp_path):
    async def allocate(*args, **kwargs):
        raise OSError("allocation RPC lost response")

    modal = SimpleNamespace(
        Image=SimpleNamespace(from_registry=lambda image: object()),
        Sandbox=SimpleNamespace(create=SimpleNamespace(aio=allocate)),
    )
    monkeypatch.setitem(sys.modules, "modal", modal)
    session = runtime._Session(source(), object(), tmp_path, "locked")
    session.image = SimpleNamespace(object_id="im-prebuilt")
    arm, report = asyncio.run(session.run({"files": {}, "healthcheck": "never run"}))
    assert arm["status"] == "infrastructure-error"
    assert report["creation_attempted"] is True
    assert report["sandbox_id"] is None
    assert report["terminal_confirmed"] is False
    assert report["lifetime_upper_seconds"] == 180
    assert "allocation unknown" in report["error"]
    assert json.loads((tmp_path / "sandbox.json").read_text()) == report


def test_image_build_failure_is_not_unknown_sandbox_allocation(runtime, monkeypatch, tmp_path):
    async def build(app):
        raise OSError("registry image unavailable")

    attempted = []

    async def allocate(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("must not allocate after failed image hydration")

    image = SimpleNamespace(build=SimpleNamespace(aio=build))
    modal = SimpleNamespace(
        Image=SimpleNamespace(from_registry=lambda ref: image),
        Sandbox=SimpleNamespace(create=SimpleNamespace(aio=allocate)),
    )
    monkeypatch.setitem(sys.modules, "modal", modal)
    monkeypatch.setattr(runtime, "_inputs", lambda task: {"files": {}, "healthcheck": "selected"})
    extractor = tmp_path / "extract.py"
    extractor.write_bytes(b"portable extractor")
    receipt = asyncio.run(runtime.run_pair(source(), object(), tmp_path / "pair", extractor))
    assert attempted == []
    assert receipt["image_build"]["status"] == "infrastructure-error"
    assert "registry image unavailable" in receipt["image_build"]["reason"]
    assert receipt["execution_status"] == "infrastructure-error"
    assert receipt["base"] is None
    assert all(
        not item["creation_attempted"]
        and item["terminal_confirmed"]
        and item["lifetime_upper_seconds"] == 0
        and item["sandbox_id"] is None
        for item in receipt["sandboxes"]
    )


def test_cold_image_build_is_separate_from_sandbox_lifetime(runtime, monkeypatch, tmp_path):
    clock = [0.0]

    async def build(app):
        clock[0] = 600.0
        return image

    async def terminate(*, wait):
        clock[0] = 602.0
        return 137

    async def allocate(*args, **kwargs):
        clock[0] = 601.0
        return SimpleNamespace(object_id="sb-observed", terminate=SimpleNamespace(aio=terminate))

    image = SimpleNamespace(object_id="im-hydrated", build=SimpleNamespace(aio=build))
    modal = SimpleNamespace(
        Image=SimpleNamespace(from_registry=lambda ref: image),
        Sandbox=SimpleNamespace(create=SimpleNamespace(aio=allocate)),
    )
    monkeypatch.setitem(sys.modules, "modal", modal)
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runtime, "_utc", lambda: datetime.fromtimestamp(clock[0], UTC).isoformat())

    async def scenario():
        hydrated, build_record = await runtime._build_image(source(), object())
        session = runtime._Session(source(), object(), tmp_path, "locked")
        session.image = hydrated
        await session.create()
        await session.cleanup()
        return build_record, session.arm, session.report

    build_record, arm, report = asyncio.run(scenario())
    assert build_record["elapsed_seconds"] == 600.0
    assert build_record["image_id"] == arm["image_id"] == "im-hydrated"
    assert report["lifetime_upper_seconds"] == 2
    assert report["creation_started_at"] == datetime.fromtimestamp(600, UTC).isoformat()
    assert report["terminal_confirmed"] is True


def test_open_refuses_locked_arm_source_mismatch(runtime, harness, tmp_path):
    observed, extractor = harness
    locked = asyncio.run(runtime.run_pair(source(), object(), tmp_path / "pair", extractor))
    locked["arms"]["oracle"]["base"] = "c" * 40
    opened = asyncio.run(runtime.run_open(source(), locked, object(), tmp_path / "open"))
    assert opened["arm"]["status"] == "infrastructure-error"
    assert "locked oracle source mismatch" in opened["arm"]["reason"]
    assert opened["sandbox"]["creation_attempted"] is False
    assert len(observed["sandboxes"]) == 2


@pytest.mark.parametrize(
    "selection,arm_status",
    [
        ("no-identifiable-fix", "not-run"),
        ("test-only-fix", "not-run"),
        ("git-error", "infrastructure-error"),
    ],
)
def test_no_fix_is_distinct_from_operational_extraction_failure(
    runtime, harness, monkeypatch, tmp_path, selection, arm_status
):
    observed, extractor = harness

    async def extract(self, inputs, extractor_bytes):
        self.extraction = {
            "status": selection,
            "fix": None,
            "base": BASE,
            "rationale": "synthetic selection boundary",
        }
        self.patch = None

    monkeypatch.setattr(runtime._Session, "extract", extract)
    receipt = asyncio.run(runtime.run_pair(source(), object(), tmp_path / "pair", extractor))
    assert receipt["extraction"]["status"] == selection
    assert receipt["extraction"]["fix"] is None
    assert receipt["arms"]["oracle"]["status"] == arm_status
    assert receipt["arms"]["oracle"]["verifier_exit_code"] is None
    assert receipt["arms"]["nop"]["status"] == "complete"
    assert observed["healthchecks"] == ["selected-repair-healthcheck"] * 2


def test_missing_original_git_never_uses_a_setup_generated_base(runtime, harness, tmp_path):
    observed, extractor = harness
    observed["failures"][("oracle", "image-base")] = 45
    observed["failures"][("nop", "image-base")] = 45
    receipt = asyncio.run(runtime.run_pair(source(), object(), tmp_path / "pair", extractor))
    assert receipt["base"] is None
    assert receipt["execution_status"] == "infrastructure-error"
    assert receipt["extraction"]["status"] == "unsupported-tree"
    assert receipt["extraction"]["fix"] is None
    assert observed["healthchecks"] == []
    assert all(sb.terminated for sb in observed["sandboxes"])


def test_local_retention_failure_still_returns_allocated_resource_accounting(
    runtime, harness, monkeypatch, tmp_path
):
    observed, extractor = harness
    original = runtime._save_json

    def save(path, value):
        if path.name == "arm.json":
            raise OSError("local evidence volume is full")
        original(path, value)

    monkeypatch.setattr(runtime, "_save_json", save)
    receipt = asyncio.run(runtime.run_pair(source(), object(), tmp_path / "pair", extractor))
    assert all(arm["status"] == "infrastructure-error" for arm in receipt["arms"].values())
    assert all("volume is full" in arm["retention_error"] for arm in receipt["arms"].values())
    assert all(
        report["creation_attempted"] and report["terminal_confirmed"] and report["sandbox_id"]
        for report in receipt["sandboxes"]
    )
    assert all(sb.terminated for sb in observed["sandboxes"])
