"""Modal ref-subset behavior against an inert, filesystem-preserving native boundary."""

from __future__ import annotations

import copy
import fnmatch
import importlib
import io
import json
import logging
import posixpath
import shlex
import tarfile
import threading
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab import ref_subset
from evallab.ref_subset import Job, probe_task, sweep
from evallab.registry import task_directory_digest
from evallab.regrade import CloudSpendGate

try:
    ref_subset_modal = importlib.import_module("evallab.ref_subset_modal")
except ModuleNotFoundError as error:
    if not (
        error.name in {"harbor", "modal"} or (error.name or "").startswith(("harbor.", "modal."))
    ):
        raise
    # Keep every test collected in the base profile; only runtime execution
    # requires the optional native dependency, as in test_harbor_docker.
    ref_subset_modal = None


@pytest.fixture(autouse=True)
def _require_native_modal():
    if ref_subset_modal is None:
        pytest.skip("native Modal regressions require uv sync --extra laminar")


_PATCH = """diff --git a/a.go b/a.go
--- a/a.go
+++ b/a.go
@@ -1 +1 @@
-old
+fixed
diff --git a/b.go b/b.go
--- a/b.go
+++ b/b.go
@@ -1 +1 @@
-old
+fixed
"""


def _package(root: Path, *, separate: bool = False) -> Path:
    files = {
        "tests/test.sh": "#!/bin/bash\n# inert verifier\n",
        "environment/Dockerfile": "FROM scratch\n",
    }
    if separate:
        files.update(
            {
                "tests/Dockerfile": "FROM scratch\n",
                "solution/solve.sh": "#!/bin/bash\n# inert oracle\n",
                "task.toml": (
                    'artifacts = ["/app/out/"]\n[task]\nname = "infra/demo"\n'
                    '[environment]\nnetwork_mode = "no-network"\n'
                    '[verifier]\nenvironment_mode = "separate"\nuser = "nobody"\n'
                    '[verifier.env]\nFLAG = "literal"\n'
                ),
            }
        )
    else:
        files.update(
            {
                "environment/setup/setup.sh": "#!/bin/bash\n# inert setup\n",
                "task.toml": '[environment]\ndocker_image = "repo/img@sha256:00"\n',
            }
        )
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o755 if name.endswith(".sh") else 0o644)
    return root


class _Provider:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.allocations: list[Any] = []
        self.image_preparations = 0
        self.setup_calls = 0
        self.oracle_calls = 0
        self.grades: list[dict[str, Any]] = []
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.missing_grade: int | None = None
        self.return_code = 0
        self.fail_setup = False
        self.fail_oracle = False
        self.require_healthcheck = False
        self.fail_healthcheck = False
        self.healthcheck_calls = 0

    def initial(self, environment: Any) -> dict[str, tuple[bytes, int]]:
        if environment.environment_dir.name == "tests":
            return {
                "/app/out/poison.go": (b"verifier image debris\n", 0o644),
                "/logs/verifier/reward.txt": (b"1\n", 0o644),
            }
        root = "/app/out" if environment.separate else "/testbed"
        return {
            f"{root}/a.go": (b"old\n", 0o755),
            f"{root}/b.go": (b"old\n", 0o640),
            f"{root}/unchanged.bin": (b"\x00\xff\x01", 0o600),
            f"{root}/deleted.go": (b"old\n", 0o644),
            "/logs/verifier/reward.txt": (b"1\n", 0o644),
        }


class _Native(ref_subset_modal.ModalEnvironment if ref_subset_modal is not None else object):
    """Inert stand-in for native environment operations, not the adapter logic."""

    provider: _Provider

    def __init__(self, **kwargs: Any) -> None:
        self.environment_dir = kwargs["environment_dir"]
        self.environment_name = kwargs["environment_name"]
        self.session_id = kwargs["session_id"]
        self.trial_paths = kwargs["trial_paths"]
        self._sandbox_timeout = kwargs["sandbox_timeout_secs"]
        self._sandbox = None
        self._image = None
        self._app = None
        self.separate = (self.environment_dir.parent / "solution" / "solve.sh").is_file()
        self.default_workdir = kwargs["task_env_config"].workdir or "/"
        self.task_env_config = kwargs["task_env_config"]
        self._enable_environment_dir_upload = kwargs["enable_environment_dir_upload"]
        self.logger = logging.getLogger(__name__)

    async def start(self, force_build: bool) -> None:
        assert not force_build
        self.provider.image_preparations += 1
        self._image = SimpleNamespace(files=self.provider.initial(self), object_id="native-image")
        self._app = object()
        self._sandbox = await self._create_sandbox()
        await self._upload_environment_dir_after_start()

    async def _create_sandbox(self, **kwargs: Any) -> Any:
        del kwargs
        with self.provider.lock:
            assert self.environment_name not in {
                sandbox.name for sandbox in self.provider.allocations
            }
            identifier = f"sandbox-{len(self.provider.allocations) + 1}"
            sandbox = SimpleNamespace(
                object_id=identifier,
                name=self.environment_name,
                files=copy.deepcopy(self._image.files),
                terminated=False,
            )
            self.provider.allocations.append(sandbox)

        async def snapshot(**kwargs: Any) -> Any:
            assert kwargs["timeout"] <= 120 and kwargs["ttl"] == 86_400
            return SimpleNamespace(
                object_id=f"snapshot-{identifier}", files=copy.deepcopy(sandbox.files)
            )

        sandbox.snapshot_filesystem = SimpleNamespace(aio=snapshot)
        return sandbox

    async def stop(self, delete: bool) -> None:
        assert delete
        self._sandbox.terminated = True
        self._sandbox = None
        self._image = None
        self._app = None

    async def upload_dir(self, source: Path, target: str) -> None:
        for path in Path(source).rglob("*"):
            if path.is_file():
                self._sandbox.files[f"{target}/{path.relative_to(source).as_posix()}"] = (
                    path.read_bytes(),
                    path.stat().st_mode & 0o777,
                )

    async def upload_file(self, source: Path, target: str) -> None:
        self._sandbox.files[target] = (Path(source).read_bytes(), 0o644)

    async def download_file(self, source: str, target: Path) -> None:
        Path(target).write_bytes(self._sandbox.files[source][0])

    async def exec(self, command: str, **kwargs: Any) -> Any:
        files = self._sandbox.files
        cwd = kwargs.get("cwd") or self.default_workdir
        self.provider.commands.append((command, dict(kwargs)))

        def absolute(path: str) -> str:
            return posixpath.normpath(posixpath.join(cwd, path))

        if " && " in command:
            result = None
            for part in command.split(" && "):
                result = await self.exec(part, **kwargs)
                if result.return_code:
                    return result
            return result
        parts = shlex.split(command)
        result = SimpleNamespace(stdout="", stderr="", return_code=0)
        if parts[:2] == ["sh", "-c"]:
            entries = []
            for path in parts[4:]:
                key = absolute(path)
                if any(name.startswith(key.rstrip("/") + "/") for name in files):
                    entries.append(f"D{path}\0")
                elif key in files:
                    entries.append(f"F{path}\0")
            result.stdout = "".join(entries)
        elif parts[0] == "tar":
            root = parts[parts.index("-C") + 1]
            if "-cf" in parts:
                remote = parts[parts.index("-cf") + 1]
                roots = parts[parts.index("--") + 1 :]
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode="w") as archive:
                    for name, (data, mode) in sorted(files.items()):
                        relative = posixpath.relpath(name, root)
                        if not any(
                            relative == p or relative.startswith(p.rstrip("/") + "/") for p in roots
                        ):
                            continue
                        member = tarfile.TarInfo(relative)
                        member.mode, member.size = mode, len(data)
                        archive.addfile(member, io.BytesIO(data))
                files[remote] = (buffer.getvalue(), 0o644)
            else:
                remote = parts[parts.index("-xf") + 1]
                with tarfile.open(fileobj=io.BytesIO(files[remote][0])) as archive:
                    for member in archive.getmembers():
                        handle = archive.extractfile(member)
                        if handle is not None:
                            files[posixpath.join(root, member.name)] = (handle.read(), member.mode)
        elif parts[0] == "rm":
            for pattern in (p for p in parts[1:] if not p.startswith("-")):
                for name in list(files):
                    if fnmatch.fnmatch(name, pattern) or name.startswith(pattern.rstrip("/") + "/"):
                        del files[name]
        elif parts[0] in {"mkdir", "chmod"}:
            pass
        elif parts[0:1] == ["bash"] and parts[1].endswith("/setup/ready.sh"):
            self.provider.healthcheck_calls += 1
            if self.provider.fail_healthcheck or parts[1] not in files:
                result.return_code, result.stderr = 1, "healthcheck setup failed"
            else:
                files["/tmp/healthcheck-ready"] = (b"ready", 0o600)
        elif parts == ["bash", "/var/lib/mimo/setup.sh"]:
            self.provider.setup_calls += 1
            if self.provider.fail_setup:
                result.return_code, result.stderr = 1, "setup failed"
        elif parts == ["bash", "/solution/solve.sh"]:
            self.provider.oracle_calls += 1
            if self.provider.require_healthcheck:
                assert files.get("/tmp/healthcheck-ready") == (b"ready", 0o600)
            if self.provider.fail_oracle:
                result.return_code, result.stderr = 1, "oracle failed"
            else:
                for name in ("a.go", "b.go"):
                    key = f"/app/out/{name}"
                    files[key] = (b"fixed\n", files[key][1])
                del files["/app/out/deleted.go"]
        elif parts == ["bash", "/tests/test.sh"]:
            assert "/tmp/prior-grade" not in files
            root = "/app/out" if self.separate else "/testbed"
            observed = copy.deepcopy(files)
            self.provider.grades.append(
                {"files": observed, "kwargs": kwargs, "sandbox_id": self._sandbox.object_id}
            )
            files["/tmp/prior-grade"] = (b"must never reach another grade", 0o644)
            if self.provider.missing_grade != len(self.provider.grades):
                reward = b"1\n" if files.get(f"{root}/a.go", (None,))[0] == b"fixed\n" else b"0\n"
                files["/logs/verifier/reward.txt"] = (reward, 0o644)
            result.return_code = self.provider.return_code
            result.stdout = "native verifier output\n"
        elif parts == ["cat", "/logs/verifier/reward.txt"]:
            if parts[1] in files:
                result.stdout = files[parts[1]][0].decode()
            else:
                result.return_code = 1
        else:
            raise AssertionError(f"unexpected native command: {command}")
        return result


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> _Provider:
    provider = _Provider()
    identities = count(1)

    def uuid4() -> SimpleNamespace:
        with provider.lock:
            return SimpleNamespace(hex=f"{next(identities):012x}" + "0" * 20)

    monkeypatch.setattr(ref_subset_modal, "uuid", SimpleNamespace(uuid4=uuid4))

    class Agent(ref_subset_modal._ReservedCreation, _Native):
        pass

    class Verifier(ref_subset_modal._ReservedCreation, _Native):
        pass

    Agent.provider = Verifier.provider = provider
    monkeypatch.setattr(ref_subset_modal, "_AgentEnvironment", Agent)
    monkeypatch.setattr(ref_subset_modal, "_VerifierEnvironment", Verifier)
    monkeypatch.setattr(ref_subset, "lab_harbor_problem", lambda: None)

    def no_docker(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Modal must never invoke host Docker or its image cache")

    monkeypatch.setattr(ref_subset, "_docker", no_docker)
    monkeypatch.setattr(ref_subset, "ImageCache", no_docker)
    return provider


def _run(root: Path, *, separate: bool = False, cap: float = 100) -> dict[str, Any]:
    package = root / "package"
    if not package.is_dir():
        _package(package, separate=separate)
    patch = root / "reference.patch"
    patch.write_text(_PATCH)
    return probe_task(
        package,
        None if separate else patch,
        root / "out",
        environment="modal",
        spend_gate=CloudSpendGate("inert native test", cap, 1),
        log=lambda _: None,
    )


@pytest.mark.parametrize("separate", [False, True], ids=["raw-mimo", "separate-verifier"])
def test_modal_grades_fresh_native_snapshots_with_immutable_package_bytes(
    tmp_path: Path, provider: _Provider, separate: bool
) -> None:
    package = _package(tmp_path / "package", separate=separate)
    before = task_directory_digest(package)
    report = _run(tmp_path, separate=separate)
    assert task_directory_digest(package) == before == report["package_digest"]
    assert report["backend"] == "modal" and report["verdict"] == "fail"
    assert report["status"] == "measured"
    assert provider.image_preparations == (2 if separate else 1)
    assert provider.setup_calls == (0 if separate else 1)
    assert provider.oracle_calls == int(separate)
    assert len({g["sandbox_id"] for g in provider.grades}) == len(provider.grades)
    assert all(s.terminated for s in provider.allocations)
    assert report["spend"]["executions"] == (6 if separate else 3) + 2 * len(provider.grades)
    assert report["spend"]["executions"] <= report["reservation_bound"]
    root = "/app/out" if separate else "/testbed"
    for grade in provider.grades:
        files = grade["files"]
        assert files[f"{root}/a.go"][1] == 0o755
        assert files[f"{root}/b.go"][1] == 0o640
        assert files[f"{root}/unchanged.bin"] == (b"\x00\xff\x01", 0o600)
        assert "/logs/verifier/reward.txt" not in files
        if separate:
            assert "/app/out/poison.go" not in files
            assert grade["kwargs"]["user"] == "nobody"
            assert grade["kwargs"]["env"] == {"FLAG": "literal"}
    if separate:
        assert (tmp_path / "out" / "reference.patch").is_file()
        assert any("app/out/deleted.go" in block["path"] for block in report["blocks"])
    receipt = json.loads((tmp_path / "out" / "run-receipt.json").read_text())
    assert receipt["spend"] == report["spend"]
    assert report["execution"]["snapshot_image_id"].startswith("snapshot-")
    assert report["execution"]["egress_evidence"] == []  # No fake claim from policy intent.


@pytest.mark.parametrize("cap,units,image_work", [(0.5, 0, 0), (2, 2, 0), (3, 3, 1)])
def test_modal_budget_refuses_before_unreserved_allocations(
    tmp_path: Path, provider: _Provider, cap: float, units: int, image_work: int
) -> None:
    report = _run(tmp_path, cap=cap)
    assert report["status"] == "budget_exhausted" and report["verdict"] == "unknown"
    assert report["spend"]["executions"] == units
    assert report["spend"]["reserved_usd"] <= cap
    assert provider.image_preparations == image_work
    assert len(provider.allocations) == int(cap == 3)
    assert not provider.grades
    assert all(s.terminated for s in provider.allocations)


@pytest.mark.parametrize("grade_number", [1, 3])
def test_missing_reward_never_reuses_a_previous_grade_or_becomes_pass(
    tmp_path: Path, provider: _Provider, grade_number: int
) -> None:
    provider.missing_grade = grade_number
    report = _run(tmp_path)
    assert report["status"] == "unavailable" and report["verdict"] == "unknown"
    assert report["grades"][-1]["reward"] is None
    assert report["grades"][-1]["status"] == "unscored"
    assert all(s.terminated for s in provider.allocations)


def test_terminated_verifier_is_unmeasured_even_with_a_reward(
    tmp_path: Path, provider: _Provider
) -> None:
    provider.return_code = 137
    report = _run(tmp_path)
    assert report["verdict"] == "unknown" and report["status"] == "unavailable"
    assert report["grades"][0]["reward"] is None
    assert report["grades"][0]["status"] == "error"
    assert all(s.terminated for s in provider.allocations)


@pytest.mark.parametrize("separate", [False, True])
def test_native_preparation_errors_release_all_cloud_sandboxes(
    tmp_path: Path, provider: _Provider, separate: bool
) -> None:
    provider.fail_oracle = separate
    provider.fail_setup = not separate
    report = _run(tmp_path, separate=separate)
    assert report["status"] == "unavailable" and report["verdict"] == "unknown"
    assert not provider.grades
    assert provider.allocations and all(s.terminated for s in provider.allocations)


def test_modal_sweep_accepts_sixteen_lanes_with_one_shared_gate_and_no_docker(
    tmp_path: Path, provider: _Provider
) -> None:
    patch = tmp_path / "reference.patch"
    patch.write_text(_PATCH)
    jobs = [
        Job(f"task-{i}", _package(tmp_path / f"task-{i}"), patch, "unused-image") for i in range(16)
    ]
    gate = CloudSpendGate("inert shared gate", 3, 1)
    reports = sweep(
        jobs,
        tmp_path / "out",
        lanes=16,
        ahead=100,
        max_grades=24,
        memory="4g",
        environment="modal",
        spend_gate=gate,
        log=lambda _: None,
    )
    assert len(reports) == 16
    assert all(r["status"] == "budget_exhausted" and r["verdict"] == "unknown" for r in reports)
    assert gate.executions == 3 and len(provider.allocations) <= 1
    assert all(s.terminated for s in provider.allocations)
    receipt = json.loads((tmp_path / "out" / "run-receipt.json").read_text())
    assert receipt["tasks"] == [job.task_id for job in jobs]
    assert receipt["spend"] == gate.to_json() and receipt["lanes"] == 16


def test_previous_run_lock_evidence_is_not_reused_after_budget_refusal(
    tmp_path: Path, provider: _Provider
) -> None:
    _run(tmp_path)
    old_preparation = next((tmp_path / "out" / "native").rglob("001-prepare"))
    stale_lock = old_preparation / "egress-lock.json"
    stale_lock.write_text(json.dumps({"sandbox_id": "a-previous-run", "applied": True}))
    report = _run(tmp_path, cap=0.5)
    assert report["status"] == "budget_exhausted" and report["verdict"] == "unknown"
    assert report["execution"]["egress_evidence"] == []
    assert stale_lock.is_file()  # Raw earlier evidence remains untouched.
    assert all(s.terminated for s in provider.allocations)


def test_small_modal_probe_honors_cpu_memory_and_sixty_second_setup_grade_bounds(
    tmp_path: Path, provider: _Provider
) -> None:
    package = _package(tmp_path / "package")
    config = package / "task.toml"
    config.write_text(config.read_text() + "cpus = 1\n")
    patch = tmp_path / "reference.patch"
    patch.write_text(_PATCH)
    gate = CloudSpendGate("inert small smoke", 1, 0.05)
    report = probe_task(
        package,
        patch,
        tmp_path / "out",
        environment="modal",
        spend_gate=gate,
        memory="512m",
        max_grades=4,
        oracle_timeout=60,
        grade_timeout=60,
        log=lambda _: None,
    )
    assert [grade["files"]["/testbed/a.go"][0] for grade in provider.grades[:2]] == [
        b"fixed\n",
        b"old\n",
    ]
    commands = [
        kwargs
        for command, kwargs in provider.commands
        if command in {"bash /var/lib/mimo/setup.sh", "bash /tests/test.sh"}
    ]
    assert commands and all(kwargs["timeout_sec"] == 60 for kwargs in commands)
    bounds = [
        event for event in report["execution"]["events"] if event["kind"] == "native_environment"
    ]
    assert bounds and all(
        event["cpus_limit"] == 1 and event["memory_limit_mb"] == 512 for event in bounds
    )
    assert all(event["lifetime_limit_s"] <= 660 for event in bounds)
    assert report["reservation_bound"] == 11 and gate.executions <= 11
    assert all(sandbox.terminated for sandbox in provider.allocations)


@pytest.mark.parametrize("separate", [False, True], ids=["raw-mimo", "separate-verifier"])
@pytest.mark.parametrize("healthy", [False, True], ids=["unready", "ready"])
def test_native_healthcheck_prepares_reference_before_reading_or_snapshotting(
    tmp_path: Path, provider: _Provider, separate: bool, healthy: bool
) -> None:
    package = _package(tmp_path / "package", separate=separate)
    workdir = "/app" if separate else "/testbed"
    config = package / "task.toml"
    original = config.read_text()
    original = original.replace("[environment]\n", f'[environment]\nworkdir = "{workdir}"\n')
    if separate:
        original = original.replace(
            "[environment]\n", '[environment]\ndocker_image = "repo/img@sha256:00"\n'
        )
    config.write_text(
        original
        + "\n[environment.healthcheck]\n"
        + f'command = "bash {workdir}/setup/ready.sh"\n'
        + "retries = 1\ninterval_sec = 0.001\ntimeout_sec = 1\n"
    )
    (package / "environment" / "Dockerfile").unlink()
    setup = package / "environment" / "setup" / "ready.sh"
    setup.parent.mkdir(exist_ok=True)
    setup.write_text("#!/bin/bash\n# initialize the reference repository\n")
    provider.require_healthcheck = True
    provider.fail_healthcheck = not healthy
    report = _run(tmp_path, separate=separate)
    assert provider.healthcheck_calls == 1
    assert provider.setup_calls == 0  # A declared setup is not run twice.
    if healthy:
        assert report["status"] == "measured" and report["verdict"] == "fail"
        assert provider.grades
        assert provider.oracle_calls == int(separate)
        if not separate:
            assert all(
                grade["files"]["/tmp/healthcheck-ready"] == (b"ready", 0o600)
                for grade in provider.grades
            )
    else:
        assert report["status"] == "unavailable" and report["verdict"] == "unknown"
        assert not provider.grades and provider.oracle_calls == 0
        assert "healthcheck" in report["reason"].lower()
    checks = [event for event in report["execution"]["events"] if event["kind"] == "healthcheck"]
    assert [event["status"] for event in checks] == ["ready" if healthy else "failed"]
    assert provider.allocations and all(sandbox.terminated for sandbox in provider.allocations)
