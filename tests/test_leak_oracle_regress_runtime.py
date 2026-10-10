"""Operational-replay regression tests: interpreter probe parsing, pre-existing
worktree dirt tolerance, extractor failure-status propagation, image build retry.
Appended for the reference-fix slice (local repro of sweep failure classes).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parents[1] / "research/experiments/leak-oracle"
BASE = "a" * 40
FIX = "b" * 40


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.syspath_prepend(str(HERE))
    spec = importlib.util.spec_from_file_location("har191_runtime_regress", HERE / "runtime.py")
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


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/usr/bin/python3\n312\n", ("/usr/bin/python3", 312)),
        ("/usr/bin/python3.9\n309\n", ("/usr/bin/python3.9", 309)),
    ],
)
def test_interpreter_probe_parses_clean_selection(runtime, text, expected):
    assert runtime._parse_interpreter_probe(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",  # all interpreters too old: exit 47 path prints nothing
        "/usr/bin/python3\n",  # truncated
        # The 152-task class: startup warnings must never reach stdout again,
        # and the parser still fails closed if they ever do.
        "Error processing line 1 of /usr/lib/python3/dist-packages/x.pth:\n\n/usr/bin/python3\n",
        "relative/python3\n312\n",
        "/usr/bin/python3\nthree-twelve\n",
        "/usr/bin/python3\n312\nextra\n",
    ],
)
def test_interpreter_probe_fails_closed(runtime, text):
    with pytest.raises(runtime.RuntimeFailure):
        runtime._parse_interpreter_probe(text)


def _stub_session(runtime, tmp_path, *, dirt_before, dirt_after, clean_exit):
    """A session whose remote is canned files; no provider objects involved."""
    session = runtime._Session(source(), object(), tmp_path, "locked")
    session.arm["base"] = BASE
    session.git_dir = "/var/lib/mimo/git-hidden"
    dirt_queue = {"image-base-dirt": [dirt_before], "setup-base-dirt": [dirt_after]}

    def artifact(phase: str, data: bytes, stream: str = "stdout") -> Path:
        path = tmp_path / (phase.replace("/", "_") + f".{stream}.log")
        path.write_bytes(data)
        return path

    async def fake_command(phase: str, command: str) -> dict:
        if phase.endswith("-dirt"):
            lines = dirt_queue[phase].pop(0)
            blob = ("\n".join(lines) + "\n").encode() if lines else b""
            return {"exit_code": 0, "stdout": artifact(phase, blob), "stderr": artifact(phase, b"", "stderr")}
        if phase in ("image-base", "setup-base"):
            blob = b"/var/lib/mimo/git-hidden\n" + BASE.encode() + b"\n"
            return {"exit_code": 0, "stdout": artifact(phase, blob), "stderr": artifact(phase, b"", "stderr")}
        if phase == "setup":
            return {"exit_code": 0, "stdout": artifact(phase, b"setup done\n"), "stderr": artifact(phase, b"", "stderr")}
        if phase == "setup-clean-base":
            return {"exit_code": clean_exit, "stdout": artifact(phase, b""), "stderr": artifact(phase, b"", "stderr")}
        raise AssertionError(f"unexpected phase {phase}")

    async def fake_download(phase: str, remote: str, *, optional: bool = False) -> Path:
        return artifact(phase, BASE.encode() + b"\n")

    session.command = fake_command  # type: ignore[method-assign]
    session.download = fake_download  # type: ignore[method-assign]
    return session


def test_setup_tolerates_preexisting_image_dirt(runtime, tmp_path):
    baseline = [" D gone.py", " M mod.py", "?? build/out.pyc"]
    session = _stub_session(
        runtime, tmp_path, dirt_before=baseline, dirt_after=list(baseline), clean_exit=1
    )
    asyncio.run(session.probe_base("image-base"))
    asyncio.run(session.setup({"healthcheck": "setup done"}))
    assert session.arm["setup_base_bound"] is True
    assert session.arm["setup_new_tracked_dirt"] == []


def test_setup_rejects_new_tracked_dirt(runtime, tmp_path):
    baseline = [" D gone.py"]
    session = _stub_session(
        runtime,
        tmp_path,
        dirt_before=baseline,
        dirt_after=baseline + [" M setup-changed.py", "?? new-untracked.py"],
        clean_exit=1,
    )
    asyncio.run(session.probe_base("image-base"))
    with pytest.raises(runtime.RuntimeFailure, match="setup-changed.py"):
        asyncio.run(session.setup({"healthcheck": "setup done"}))
    assert session.arm["setup_new_tracked_dirt"] == [" M setup-changed.py"]
    assert session.arm.get("setup_base_bound") is not True


def _stub_extract_session(runtime, tmp_path, evidence: dict):
    session = runtime._Session(source(), object(), tmp_path, "locked")
    session.arm["base"] = BASE
    session.git_dir = "/testbed/.git"

    def artifact(phase: str, data: bytes, stream: str = "stdout") -> Path:
        path = tmp_path / (phase.replace("/", "_") + f".{stream}.log")
        path.write_bytes(data)
        return path

    async def fake_upload(remote: str, data: bytes) -> None:
        return None

    async def fake_require(phase: str, command: str) -> dict:
        return {
            "exit_code": 0,
            "stdout": artifact(phase, b"/usr/bin/python3\n312\n"),
            "stderr": artifact(phase, b"", "stderr"),
        }

    async def fake_command(phase: str, command: str) -> dict:
        return {"exit_code": 0, "stdout": artifact(phase, b""), "stderr": artifact(phase, b"", "stderr")}

    async def fake_download(phase: str, remote: str, *, optional: bool = False) -> Path:
        return artifact("evidence", json.dumps(evidence).encode())

    session.upload = fake_upload  # type: ignore[method-assign]
    session.require = fake_require  # type: ignore[method-assign]
    session.command = fake_command  # type: ignore[method-assign]
    session.download = fake_download  # type: ignore[method-assign]
    return session


def test_extractor_failure_status_propagates_not_overwritten(runtime, tmp_path):
    cases = [
        {
            "task": "format-code-task-000001",
            "fix": None,
            "status": "unsupported-tree",
            "rationale": "submodules and special file modes are unsupported",
        },
        {
            "task": "format-code-task-000001",
            "fix": None,
            "status": "unsupported-tree",
            "rationale": "submodules and special file modes are unsupported",
            "base": BASE,
        },
    ]
    for index, evidence in enumerate(cases):
        case_dir = tmp_path / f"case-{index}"
        case_dir.mkdir()
        session = _stub_extract_session(runtime, case_dir, evidence)
        asyncio.run(session.extract({"files": {}}, b"extractor"))
        assert session.extraction["status"] == "unsupported-tree"
        assert session.patch is None


def test_extractor_base_mismatch_still_fails_closed(runtime, tmp_path):
    evidence = {
        "task": "format-code-task-000001",
        "fix": None,
        "status": "unsupported-tree",
        "rationale": "submodules and special file modes are unsupported",
        "base": "c" * 40,
    }
    session = _stub_extract_session(runtime, tmp_path, evidence)
    with pytest.raises(runtime.RuntimeFailure, match="differs"):
        asyncio.run(session.extract({"files": {}}, b"extractor"))


def test_extractor_task_mismatch_still_fails_closed(runtime, tmp_path):
    evidence = {
        "task": "format-code-task-000002",
        "fix": None,
        "status": "unsupported-tree",
        "rationale": "submodules and special file modes are unsupported",
    }
    session = _stub_extract_session(runtime, tmp_path, evidence)
    with pytest.raises(runtime.RuntimeFailure, match="differs"):
        asyncio.run(session.extract({"files": {}}, b"extractor"))


def _fake_modal(failures: int):
    calls = {"n": 0}

    class Builder:
        async def aio(self, app):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise RuntimeError("transient builder boom")
            return SimpleNamespace(object_id="im-1")

    image = SimpleNamespace(build=Builder())
    modal = SimpleNamespace(Image=SimpleNamespace(from_registry=lambda ref: image))
    return modal, calls


def test_build_image_retries_transient_failure(runtime, tmp_path, monkeypatch):
    modal, calls = _fake_modal(failures=1)
    monkeypatch.setitem(sys.modules, "modal", modal)

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(runtime.asyncio, "sleep", no_sleep)
    task = source()
    image, record = asyncio.run(runtime._build_image(task, object()))
    assert image.object_id == "im-1"
    assert record["status"] == "complete"
    assert record["attempts"] == 2
    assert len(record["attempt_errors"]) == 1


def test_build_image_records_final_failure(runtime, tmp_path, monkeypatch):
    modal, calls = _fake_modal(failures=99)
    monkeypatch.setitem(sys.modules, "modal", modal)

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(runtime.asyncio, "sleep", no_sleep)
    image, record = asyncio.run(runtime._build_image(source(), object()))
    assert image is None
    assert record["status"] == "infrastructure-error"
    assert record["attempts"] == runtime.BUILD_ATTEMPTS
    assert f"({runtime.BUILD_ATTEMPTS} attempts)" in record["reason"]
