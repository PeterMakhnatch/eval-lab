"""Docker grading discards stale rewards without removing other verifier logs."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest

from evallab.qa.task import HarborBackend

DIGEST = "sha256:" + "a" * 64
REWARD_PATHS = {"/logs/verifier/reward.txt", "/logs/verifier/reward.json"}
OTHER_PATH = "/logs/verifier/other.txt"
CLEAR_SCRIPT = (
    "mkdir -p /logs/verifier && rm -f /logs/verifier/reward.txt /logs/verifier/reward.json"
)


class FakeDocker:
    """Keep independent files for each container or Compose service."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, str]] = {}
        self.execs: list[tuple[str, str]] = []
        self.cleared: list[tuple[int, set[str], dict[str, str]]] = []

    def __call__(self, args: list[Any], **_: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(arg) for arg in args]
        rc, stdout = 0, ""
        if "exec" in argv and "bash" in argv:
            # The command tail follows the target, regardless of exec options.
            command_index = argv.index("bash")
            target = argv[command_index - 1]
            if argv[command_index + 1 : command_index + 2] == ["-c"]:
                script = argv[command_index + 2]
                self.execs.append((target, script))
                files = self._files(target)
                if "rm -f" in script:
                    tokens = shlex.split(script)
                    removed = set(tokens[tokens.index("rm") + 2 :])
                    for path in removed & REWARD_PATHS:
                        files.pop(path, None)
                    self.cleared.append((len(self.execs) - 1, removed, dict(files)))
                if "/tests/test.sh" in script:
                    # A failing test exits before it can replace either reward file.
                    rc, stdout = 1, "test exits without writing a reward\n"
        elif "exec" in argv and argv[-2] == "cat" and argv[-1] in REWARD_PATHS:
            files = self._files(argv[-3])
            stdout = files.get(argv[-1], "")
            rc = 0 if argv[-1] in files else 1
        elif "cp" in argv and argv[-2].endswith(":/logs/verifier/."):
            Path(argv[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="")

    def _files(self, target: str) -> dict[str, str]:
        return self.files.setdefault(
            target,
            {
                "/logs/verifier/reward.txt": "1\n",
                "/logs/verifier/reward.json": '{"reward": 1}\n',
                OTHER_PATH: "keep this log\n",
            },
        )


def _write_package(root: Path, mode: str) -> Path:
    package = root / f"{mode}-pkg"
    (package / "environment").mkdir(parents=True)
    (package / "tests").mkdir()
    (package / "solution").mkdir()
    (package / "environment" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (package / "tests" / "test.sh").write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    (package / "solution" / "solve.sh").write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
    toml = f'[task]\nname = "bench/{mode}-pkg"\n[environment]\nworkdir = "/app"\n'
    if mode == "separate":
        toml += (
            '[verifier]\nenvironment_mode = "separate"\n'
            '[verifier.environment.env]\nEXPECT_TAG = "ok"\n'
        )
        (package / "tests" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    elif mode == "compose":
        toml = (
            'schema_version = "1.0"\n'
            'artifacts = ["/tmp/agent.patch", { source = "/warehouse", '
            'service = "warehouse", destination = "/tmp/warehouse", exclude = ["*.pyc"] }]\n'
            '[task]\nname = "terminal-bench/compose-pkg"\n'
            '[environment]\ncpus = 2\nmemory_mb = 4096\n'
            '[[verifier.collect]]\ncommand = "echo main-hook"\n'
            '[[verifier.collect]]\nservice = "warehouse"\ncommand = "echo sidecar-hook"\n'
        )
        # A separate verifier would bypass grading inside the Compose service.
        (package / "environment" / "docker-compose.yaml").write_text(
            "services:\n  main:\n    depends_on: [warehouse]\n"
            "  warehouse:\n    image: alpine:3.19\n",
            encoding="utf-8",
        )
    (package / "task.toml").write_text(toml, encoding="utf-8")
    return package


@pytest.fixture
def backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HarborBackend:
    monkeypatch.setenv("EVALLAB_DOCKER_LOCK_DIR", str(tmp_path / "lock"))
    monkeypatch.setattr("evallab.qa.task.time.sleep", lambda _: None)
    return HarborBackend(repo_root=tmp_path)


@pytest.mark.parametrize("mode", ["shared", "separate", "compose"])
def test_grading_clears_stale_rewards_before_test_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: HarborBackend, mode: str
) -> None:
    package = _write_package(tmp_path, mode)
    fake = FakeDocker()
    monkeypatch.setattr("evallab.qa.task.subprocess.run", fake)

    obs = backend.run_container_trial(
        package, digest=DIGEST, mode="oracle", trial_dir=tmp_path / "trial"
    )

    test_execs = [
        (i, target)
        for i, (target, script) in enumerate(fake.execs)
        if "/tests/test.sh" in script
    ]
    assert len(test_execs) == 1
    assert obs.facts["exception"] is None
    assert obs.facts["reward"] is None
    assert "test exits without writing a reward" in obs.facts["verifier_stdout"]
    # A completed test with no reward is not a usable result under the existing API.
    assert obs.status == "failed"
    assert len(fake.cleared) == 1
    clear_index, removed, after_clear = fake.cleared[0]
    test_index, test_target = test_execs[0]
    clear_target, clear_script = fake.execs[clear_index]
    assert clear_index < test_index
    assert clear_target == test_target
    assert clear_script == CLEAR_SCRIPT
    assert removed == REWARD_PATHS
    assert not (after_clear.keys() & REWARD_PATHS)
    assert after_clear[OTHER_PATH] == "keep this log\n"
    assert fake.files[test_target][OTHER_PATH] == "keep this log\n"
    if mode == "compose":
        assert test_target == "main"
        assert obs.facts["backend"] == "docker-compose"
