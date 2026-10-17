"""Patch-oracle packages (raw MiMo code tasks) through ``HarborBackend``.

No real Docker, model or network: a fake ``docker`` records every command the backend issues,
and the generated patch script runs for real against a throwaway git repository.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab.qa.task import (
    HarborBackend,
    QATaskConfig,
    Reproducer,
    run_task,
    workdir_patch_script,
)
from evallab.registry import task_directory_digest

DIGEST = "sha256:" + "a" * 64
PATCH = b"--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n+y = 3\n"


def split_exec(argv: list[str]) -> tuple[str | None, list[str]]:
    """``docker exec [-w DIR] CONTAINER COMMAND...`` -> (DIR or None, COMMAND...)."""
    rest = argv[2:]
    cwd = None
    if rest[:1] == ["-w"]:
        cwd, rest = rest[1], rest[2:]
    return cwd, rest[1:]


class FakeDocker:
    """Stands in for ``subprocess.run``; records commands and what was copied into /solution."""

    def __init__(self, *, healthcheck_rcs: list[int] | None = None, reward: str = "1\n") -> None:
        self.calls: list[list[str]] = []
        self.solutions: list[dict[str, bytes]] = []
        self.scripts: list[str] = []
        self.copy_sources: list[str] = []
        self._rcs = list(healthcheck_rcs or [0])
        self._reward = reward

    def __call__(self, args: list[Any], **_: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in args]
        self.calls.append(argv)
        rc, stdout = 0, ""
        if argv[:2] == ["docker", "cp"]:
            source, dest = argv[2], argv[3]
            if dest.endswith(":/solution"):
                self.copy_sources.append(source)
                self.solutions.append({p.name: p.read_bytes() for p in Path(source).iterdir()})
            elif dest.endswith(":/tmp/qa-shortcut.sh"):
                self.scripts.append(Path(source).read_text(encoding="utf-8"))
        elif argv[:2] == ["docker", "exec"]:
            _, command = split_exec(argv)
            if command[:2] == ["sh", "-c"]:
                rc = self._rcs.pop(0) if len(self._rcs) > 1 else self._rcs[0]
            elif command[:2] == ["cat", "/logs/verifier/reward.txt"]:
                stdout = self._reward
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="")

    def execs(self) -> list[list[str]]:
        return [split_exec(c)[1] for c in self.calls if c[:2] == ["docker", "exec"]]

    def position(self, *needle: str) -> int:
        for index, command in enumerate(self.execs()):
            if command[: len(needle)] == list(needle):
                return index
        return -1

    def cwd_of(self, *needle: str) -> str | None:
        for call in self.calls:
            if call[:2] == ["docker", "exec"]:
                cwd, command = split_exec(call)
                if command[: len(needle)] == list(needle):
                    return cwd
        raise AssertionError(f"no exec of {needle}")


def _write_package(root: Path, name: str, toml: str, *, solution: bool) -> Path:
    package = root / name
    (package / "tests").mkdir(parents=True)
    (package / "environment").mkdir()
    (package / "environment" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (package / "tests" / "test.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    (package / "task.toml").write_text(toml, encoding="utf-8")
    if solution:
        (package / "solution").mkdir()
        (package / "solution" / "solve.sh").write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
    return package


def _mimo(root: Path, *, retries: int = 0, healthcheck: bool = True) -> Path:
    toml = (
        '[task]\nname = "mimo/format-code-task-000001"\n'
        '[environment]\ndocker_image = "img@sha256:1"\nworkdir = "/testbed"\ncpus = 2\n'
        "memory_mb = 4096\n"
    )
    if healthcheck:
        toml += (
            f"[environment.healthcheck]\ncommand = 'true'\ntimeout_sec = 5\nretries = {retries}\n"
            "interval_sec = 0.01\n"
        )
    return _write_package(root, "format-code-task-000001", toml, solution=False)


def _harbor(root: Path) -> Path:
    toml = '[task]\nname = "bench/tb-task"\n[environment]\nworkdir = "/app"\n'
    return _write_package(root, "tb-task", toml, solution=True)


def _mimo_separate(root: Path) -> Path:
    """A v6 separate-verifier MiMo package: own solve.sh, setup healthcheck, workdir /testbed."""
    toml = (
        '[task]\nname = "mimo/format-code-task-000002"\n'
        '[environment]\ndocker_image = "img@sha256:2"\nworkdir = "/testbed"\n'
        "[environment.healthcheck]\ncommand = 'true'\ntimeout_sec = 5\n"
    )
    return _write_package(root, "format-code-task-000002", toml, solution=True)


@pytest.fixture
def backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HarborBackend:
    monkeypatch.setenv("EVALLAB_DOCKER_LOCK_DIR", str(tmp_path / "lock"))
    monkeypatch.setattr("evallab.qa.task.time.sleep", lambda _: None)
    return HarborBackend(repo_root=tmp_path)


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeDocker) -> FakeDocker:
    monkeypatch.setattr("evallab.qa.task.subprocess.run", fake)
    return fake


def _reference(tmp_path: Path) -> Path:
    path = tmp_path / "reference.patch"
    path.write_bytes(PATCH)
    return path


def _git(cwd: Path, *args: str) -> bytes:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )
    return done.stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "testbed"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def test_patch_script_applies_in_the_workdir_and_rejects_a_mismatch_without_changing_it(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    script = workdir_patch_script(PATCH, str(repo))

    first = subprocess.run(
        ["bash", "-c", script], capture_output=True, stdin=subprocess.DEVNULL, check=False
    )
    assert first.returncode == 0, first.stderr
    assert (repo / "a.py").read_text(encoding="utf-8") == "x = 1\ny = 3\n"

    # The context no longer matches: a failed apply must exit nonzero, never pass quietly.
    again = subprocess.run(
        ["bash", "-c", script], capture_output=True, stdin=subprocess.DEVNULL, check=False
    )
    assert again.returncode != 0
    assert (repo / "a.py").read_text(encoding="utf-8") == "x = 1\ny = 3\n"


def test_patch_oracle_runs_the_healthcheck_first_and_stages_the_bound_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: HarborBackend
) -> None:
    package = _mimo(tmp_path)
    before = task_directory_digest(package)
    backend.bind_reference_patch(package, _reference(tmp_path))
    fake = _install(monkeypatch, FakeDocker())

    obs = backend.run_container_trial(
        package, digest=DIGEST, mode="oracle", trial_dir=tmp_path / "trial"
    )

    assert obs.status == "executed"
    assert obs.facts["reward"] == 1.0
    assert obs.facts["healthcheck"]["attempts"] == 1
    assert obs.facts["oracle"]["sha256"] == hashlib.sha256(PATCH).hexdigest()
    # Setup must precede the oracle and the verifier: test.sh needs what setup writes.
    setup, solve, verify = (
        fake.position("sh", "-c"),
        fake.position("bash", "/solution/solve.sh"),
        fake.position("bash", "-c", "mkdir -p /logs/verifier && bash /tests/test.sh"),
    )
    assert 0 <= setup < solve < verify
    (staged,) = fake.solutions
    assert staged["reference.patch"] == PATCH
    assert b"cd /testbed" in staged["solve.sh"]
    # The oracle is synthesized outside the package, so the digest a report binds to is intact.
    assert task_directory_digest(package) == before


def test_patch_package_without_a_reference_patch_is_unmeasured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: HarborBackend
) -> None:
    package = _mimo(tmp_path)
    fake = _install(monkeypatch, FakeDocker())

    obs = backend.run_container_trial(
        package, digest=DIGEST, mode="oracle", trial_dir=tmp_path / "trial"
    )

    assert obs.status == "failed"
    assert obs.facts["reward"] is None
    assert "no reference patch" in (obs.reason or "")
    assert fake.position("bash", "-c", "mkdir -p /logs/verifier && bash /tests/test.sh") == -1
    assert any(c[:3] == ["docker", "rm", "-f"] for c in fake.calls)


@pytest.mark.parametrize(
    ("rcs", "retries", "status", "attempts"),
    [
        ([1], 0, "failed", None),
        ([1, 1, 0], 2, "executed", 3),
        ([1, 1, 1], 2, "failed", None),
    ],
)
def test_healthcheck_retries_are_bounded_and_failure_leaves_the_trial_unmeasured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: HarborBackend,
    rcs: list[int],
    retries: int,
    status: str,
    attempts: int | None,
) -> None:
    package = _mimo(tmp_path, retries=retries)
    backend.bind_reference_patch(package, _reference(tmp_path))
    fake = _install(monkeypatch, FakeDocker(healthcheck_rcs=rcs))

    obs = backend.run_container_trial(
        package, digest=DIGEST, mode="oracle", trial_dir=tmp_path / "trial"
    )

    assert obs.status == status
    if status == "executed":
        assert obs.facts["healthcheck"]["attempts"] == attempts
    else:
        assert obs.facts["reward"] is None
        assert (obs.reason or "").startswith("environment healthcheck failed")
        assert fake.position("bash", "/solution/solve.sh") == -1
        assert fake.position("bash", "-c", "mkdir -p /logs/verifier && bash /tests/test.sh") == -1


def test_package_with_its_own_solution_and_no_healthcheck_is_run_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: HarborBackend
) -> None:
    package = _harbor(tmp_path)
    # A stray reference patch must not displace a package's own oracle.
    backend.bind_reference_patch(package, _reference(tmp_path))
    fake = _install(monkeypatch, FakeDocker())

    obs = backend.run_container_trial(
        package, digest=DIGEST, mode="oracle", trial_dir=tmp_path / "trial"
    )

    assert obs.status == "executed"
    assert fake.copy_sources == [str(package / "solution")]
    assert fake.position("sh", "-c") == -1
    assert "healthcheck" not in obs.facts
    assert "oracle" not in obs.facts
    assert fake.cwd_of("bash", "/solution/solve.sh") is None


def test_replayed_patch_applies_in_the_declared_workdir_only_for_patch_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: HarborBackend
) -> None:
    patch = tmp_path / "candidate.patch"
    patch.write_bytes(PATCH)
    reproducer = Reproducer(kind="patch", path=str(patch), expected_reward=1.0)
    scripts = {}
    packages = (
        ("mimo", _mimo(tmp_path)),
        ("separate", _mimo_separate(tmp_path)),
        ("harbor", _harbor(tmp_path)),
    )
    for label, package in packages:
        fake = _install(monkeypatch, FakeDocker())
        obs = backend.replay_reproducer(
            package,
            reproducer,
            digest=DIGEST,
            run_dir=tmp_path / f"replay-{label}",
            timeout_seconds=60,
        )
        assert obs.status == "executed"
        (scripts[label],) = fake.scripts

    assert "cd /testbed" in scripts["mimo"]
    assert "cd /testbed" in scripts["separate"]
    assert "/testbed" not in scripts["harbor"]
    assert "/app" in scripts["harbor"]


def test_leak_probe_waits_for_setup_only_when_the_package_declares_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: HarborBackend
) -> None:
    with_setup = _mimo(tmp_path)
    fake = _install(monkeypatch, FakeDocker())
    backend._run_probe(with_setup, "img", "echo probe", timeout=5)
    assert fake.position("sh", "-c") < fake.position("bash", "-c", "echo probe")
    assert any(c[:3] == ["docker", "rm", "-f"] for c in fake.calls)

    plain = _harbor(tmp_path)
    fake = _install(monkeypatch, FakeDocker())
    backend._run_probe(plain, "img", "echo probe", timeout=5)
    assert [c[:3] for c in fake.calls] == [["docker", "run", "--rm"]]


class _NamedBackend:
    """Only what ``run_task`` needs to name the subject; every check is then blocked."""

    def __init__(self, task_id: str, task_name: str) -> None:
        self._id, self._name = task_id, task_name

    def resolve(self, package: Path) -> SimpleNamespace:
        return SimpleNamespace(
            task_id=self._id, task_name=self._name, package_digest=task_directory_digest(package)
        )

    def resource_blocker(self, package: Path) -> str:
        return "blocked for this test"


@pytest.mark.parametrize(
    ("task_id", "native", "directory", "expected"),
    [
        # A raw MiMo package keeps its own namespace, in the original and in a digest-named variant.
        ("t-1", "mimo-v2.6-rl/t-1", "t-1", "mimo-v2.6-rl/t-1"),
        ("t-1", "mimo-v2.6-rl/t-1", "92cc1d16665a", "mimo-v2.6-rl/t-1"),
        ("t-1", "terminal-bench/t-1", "t-1", "terminal-bench/t-1"),
        # An older TB2 package has no `[task].name`: the benchmark prefix applies.
        ("t-1", "t-1", "t-1", "terminal-bench/t-1"),
        # No namespace anywhere and a directory that is not the id: left bare, never invented.
        ("t-1", "t-1", "92cc1d16665a", "t-1"),
    ],
)
def test_subject_id_keeps_the_package_namespace_so_findings_bind_to_their_key(
    tmp_path: Path, task_id: str, native: str, directory: str, expected: str
) -> None:
    package = _write_package(
        tmp_path, directory, '[environment]\nworkdir = "/app"\n', solution=True
    )

    report = run_task(
        package,
        config=QATaskConfig(stages=("S0",)),
        run_dir=tmp_path / "run",
        backend=_NamedBackend(task_id, native),
    )

    assert report.subject.task_id == expected


def test_package_with_its_own_solve_and_a_workdir_runs_it_there_not_in_the_image_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: HarborBackend
) -> None:
    # A v6 MiMo image has no WORKDIR and its solve.sh relies on running in the workdir.
    package = _mimo_separate(tmp_path)
    fake = _install(monkeypatch, FakeDocker())

    obs = backend.run_container_trial(
        package, digest=DIGEST, mode="oracle", trial_dir=tmp_path / "trial"
    )

    assert obs.status == "executed"
    assert fake.cwd_of("bash", "/solution/solve.sh") == "/testbed"
    assert fake.copy_sources == [str(package / "solution")]  # its own oracle, not a synthesized one
    assert fake.position("sh", "-c") < fake.position("bash", "/solution/solve.sh")


@pytest.mark.parametrize(
    ("workdir", "solution", "healthcheck", "expected"),
    [
        (True, False, False, "/testbed"),  # raw MiMo: reference patch is the oracle
        (True, True, True, "/testbed"),  # v6 separate-verifier MiMo: own solve.sh + setup
        (True, True, False, None),  # a package with its own oracle and no setup is untouched
        (False, True, True, None),  # no declared workdir: nothing to run in
        (False, False, False, None),
    ],
)
def test_workdir_bound_rule_names_only_mimo_style_packages(
    tmp_path: Path, workdir: bool, solution: bool, healthcheck: bool, expected: str | None
) -> None:
    toml = '[task]\nname = "x/pkg"\n[environment]\n'
    toml += 'workdir = "/testbed"\n' if workdir else ""
    toml += "[environment.healthcheck]\ncommand = 'true'\n" if healthcheck else ""
    package = _write_package(tmp_path, "pkg", toml, solution=solution)

    assert HarborBackend.patch_workdir(package) == expected


def test_compose_package_is_unblocked_and_runs_phased_hooks_and_separate_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: HarborBackend
) -> None:
    toml = (
        'schema_version = "1.0"\n'
        'artifacts = ["/tmp/agent.patch", { source = "/warehouse", service = "warehouse", destination = "/tmp/warehouse", exclude = ["*.pyc"] }]\n'
        '[task]\nname = "terminal-bench/compose-pkg"\n'
        "[environment]\ncpus = 2\nmemory_mb = 4096\n"
        '[verifier]\nenvironment_mode = "separate"\n'
        '[verifier.environment.env]\nEXPECT_TAG = "ok"\n'
        '[[verifier.collect]]\ncommand = "echo main-hook"\n'
        '[[verifier.collect]]\nservice = "warehouse"\ncommand = "echo sidecar-hook"\n'
    )
    package = _write_package(tmp_path, "compose-pkg", toml, solution=True)
    (package / "environment" / "docker-compose.yaml").write_text(
        "services:\n  main:\n    depends_on: [warehouse]\n  warehouse:\n    image: alpine:3.19\n",
        encoding="utf-8",
    )
    (package / "tests" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    assert backend.resource_blocker(package) is None

    calls: list[list[str]] = []

    def fake_run(args: list[Any], **_: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in args]
        calls.append(argv)
        stdout = ""
        if argv[:2] == ["docker", "compose"] and "cp" in argv:
            target = Path(argv[-1])
            if not str(target).startswith("main:"):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("artifact\n", encoding="utf-8")
        elif argv[:2] == ["docker", "exec"] and argv[-2:] == ["cat", "/logs/verifier/reward.txt"]:
            stdout = "1.0\n"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("evallab.qa.task.subprocess.run", fake_run)
    obs = backend.run_container_trial(
        package, digest=DIGEST, mode="oracle", trial_dir=tmp_path / "compose-trial"
    )

    assert obs.status == "executed"
    assert obs.facts["reward"] == 1.0
    assert obs.facts["backend"] == "docker-compose"
    compose_subcmds = [c[10] for c in calls if c[:2] == ["docker", "compose"] and len(c) > 10]
    assert "build" in compose_subcmds
    assert "up" in compose_subcmds
    assert compose_subcmds.index("stop") < compose_subcmds.index("down")
    assert any("-e" in c and "EXPECT_TAG=ok" in c for c in calls if c[:2] == ["docker", "exec"])


def test_separate_verifier_trial_scores_reward_even_when_solve_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: HarborBackend
) -> None:
    package = _write_package(
        tmp_path,
        "sep-pkg",
        '[task]\nname = "bench/sep-pkg"\n[environment]\nworkdir = "/app"\n[verifier]\nenvironment_mode = "separate"\n',
        solution=True,
    )
    (package / "tests" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    def fake_run(args: list[Any], **_: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in args]
        rc, stdout = 0, ""
        if argv[:2] == ["docker", "exec"]:
            _, cmd = split_exec(argv)
            if cmd == ["bash", "/solution/solve.sh"]:
                rc = 1
            elif cmd == ["cat", "/logs/verifier/reward.txt"]:
                stdout = "1.0\n"
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="")

    monkeypatch.setattr("evallab.qa.task.subprocess.run", fake_run)
    obs = backend.run_container_trial(
        package, digest=DIGEST, mode="oracle", trial_dir=tmp_path / "sep-trial"
    )
    assert obs.status == "executed"
    assert obs.facts["reward"] == 1.0
    assert obs.facts["oracle_exit_code"] == 1


def test_amd64_wheel_detection_adds_platform_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: HarborBackend
) -> None:
    package = _write_package(
        tmp_path,
        "amd64-pkg",
        '[task]\nname = "bench/amd64-pkg"\n[environment]\n',
        solution=True,
    )
    wheels = package / "tests" / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    (wheels / "foo-1.0-cp312-cp312-manylinux_2_28_x86_64.whl").write_bytes(b"wheel")
    fake = _install(monkeypatch, FakeDocker())
    obs = backend.run_container_trial(
        package, digest=DIGEST, mode="oracle", trial_dir=tmp_path / "amd64-trial"
    )
    assert obs.status == "executed"
    build_calls = [c for c in fake.calls if c[:2] == ["docker", "build"]]
    run_calls = [c for c in fake.calls if c[:2] == ["docker", "run"]]
    assert all("--platform" in c and "linux/amd64" in c for c in build_calls)
    assert all("--platform" in c and "linux/amd64" in c for c in run_calls)


def test_mimo_v6_embedded_patch_extraction_and_probe_package_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: HarborBackend
) -> None:
    import base64

    toml = (
        'schema_version = "1.0"\n'
        'artifacts = ["/var/tmp/mimo-separate/patch.diff"]\n'
        '[task]\nname = "mimo-v2.6-rl/format-code-task-000079"\n'
        '[environment]\ndocker_image = "img:1"\nworkdir = "/testbed"\n'
        '[environment.healthcheck]\ncommand = "bash -c /setup/setup.sh"\n'
        '[verifier]\nenvironment_mode = "separate"\n'
    )
    pkg = _write_package(tmp_path, "format-code-task-000079", toml, solution=True)
    b64 = base64.b64encode(PATCH).decode("ascii")
    (pkg / "solution" / "solve.sh").write_text(
        f'#!/bin/bash\nset -euo pipefail\necho "{b64}" | base64 -d | git apply -\n',
        encoding="utf-8",
    )
    (pkg / "tests" / "test.patch").write_text(
        "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@ -1 +1 @@\n-assert False\n+assert True\n",
        encoding="utf-8",
    )
    (pkg / "tests" / "test_command.sh").write_text("pytest -q\n", encoding="utf-8")

    extracted = backend._extract_embedded_patch(pkg, tmp_path / "out")
    assert extracted is not None and extracted.read_bytes() == PATCH

    probe_pkg, cleanup_dir = backend._prepare_mimo_probe_package(pkg)
    try:
        staged_sh = (probe_pkg / "tests" / "test.sh").read_text(encoding="utf-8")
        assert "EOF_RESET_TEST_FILES" in staged_sh
        assert "tests/test_a.py" in staged_sh
        assert "pytest -q" in staged_sh
    finally:
        if cleanup_dir is not None:
            import shutil

            shutil.rmtree(cleanup_dir, ignore_errors=True)


def test_prebuilt_alexgshaw_image_uses_prebuilt_tag_instead_of_docker_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: HarborBackend
) -> None:
    fake = _install(monkeypatch, FakeDocker())
    package = _write_package(
        tmp_path,
        "qemu-startup",
        '[task]\nname = "qemu-startup"\n[environment]\ndocker_image = "alexgshaw/qemu-startup:20251031"\n',
        solution=True,
    )
    obs = backend.run_container_trial(
        package,
        digest=DIGEST,
        mode="oracle",
        trial_dir=tmp_path / "trial",
        timeout_seconds=30,
    )
    assert obs.status == "executed"
    assert not any(c[:2] == ["docker", "build"] for c in fake.calls)
    assert any(
        c[:2] == ["docker", "run"] and "alexgshaw/qemu-startup:20251031" in c for c in fake.calls
    )
