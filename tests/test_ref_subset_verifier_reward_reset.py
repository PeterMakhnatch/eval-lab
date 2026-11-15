"""Docker verification must not reuse rewards left in the container (HAR-449)."""

from __future__ import annotations

import shlex
import subprocess
import time

import pytest

from evallab import ref_subset


class FakeDocker:
    def __init__(self) -> None:
        self.files = {
            "c": {
                "/logs/verifier/reward.txt": b"1\n",
                "/logs/verifier/reward.json": b'{"reward": 1}\n',
                "/logs/verifier/other.txt": b"keep me\n",
            }
        }
        self.calls: list[tuple[str, ...]] = []
        self.timeouts: list[float | None] = []
        self.clear_indexes: list[int] = []
        self.test_indexes: list[int] = []

    def __call__(
        self, *args: str, input: bytes | None = None, timeout: float | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        index = len(self.calls)
        self.calls.append(args)
        self.timeouts.append(timeout)
        if args[0] == "exec" and "bash" in args:
            bash_index = args.index("bash")
            files = self.files[args[bash_index - 1]]
            command = args[bash_index:]
            if command[:2] == ("bash", "-c") and "rm -f" in command[2]:
                self.clear_indexes.append(index)
                tokens = shlex.split(command[2])
                for path in tokens[tokens.index("rm") + 2 :]:
                    files.pop(path, None)
            elif command == ("bash", "/tests/test.sh"):
                self.test_indexes.append(index)
        elif args[0] == "exec" and args[-2:] == ("cat", "/logs/verifier/reward.txt"):
            reward = self.files[args[-3]].get(args[-1])
            return subprocess.CompletedProcess(args, int(reward is None), reward or b"", b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    fake = FakeDocker()
    monkeypatch.setattr(ref_subset, "_docker", fake)
    return fake


def test_verify_does_not_reuse_stale_reward(docker: FakeDocker) -> None:
    assert docker.files["c"]["/logs/verifier/reward.txt"] == b"1\n"

    grade = ref_subset._verify("c", "full", [], 30.0, time.monotonic())

    assert grade.reward is None
    assert grade.status == "unscored"


@pytest.mark.parametrize("exec_options", [(), ("-u", "1000:1000", "-w", "/app")])
def test_verify_clears_reward_before_unchanged_test_exec(
    docker: FakeDocker, exec_options: tuple[str, ...]
) -> None:
    ref_subset._verify("c", "full", [], 30.0, time.monotonic(), exec_options)

    assert len(docker.clear_indexes) == len(docker.test_indexes) == 1
    clear_index = docker.clear_indexes[0]
    test_index = docker.test_indexes[0]
    assert clear_index < test_index
    prefix = ("exec", *ref_subset._PATH_PIN, *exec_options, "c")
    assert docker.calls[clear_index][:-3] == prefix
    assert docker.calls[clear_index][-3:-1] == ("bash", "-c")
    assert docker.calls[test_index] == (*prefix, "bash", "/tests/test.sh")
    assert docker.timeouts[clear_index] is None
    assert docker.timeouts[test_index] == 30.0


def test_verify_clears_only_reward_files(docker: FakeDocker) -> None:
    ref_subset._verify("c", "full", [], 30.0, time.monotonic())

    assert len(docker.clear_indexes) == 1
    script = docker.calls[docker.clear_indexes[0]][-1]
    assert script == (
        "mkdir -p /logs/verifier && rm -f /logs/verifier/reward.txt /logs/verifier/reward.json"
    )
    tokens = shlex.split(script)
    assert tokens[tokens.index("rm") + 2 :] == [
        "/logs/verifier/reward.txt",
        "/logs/verifier/reward.json",
    ]
    assert docker.files["c"] == {"/logs/verifier/other.txt": b"keep me\n"}
