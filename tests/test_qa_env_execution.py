"""Owner-only execution policy: drift, cap/approval/ledger, port pins, lock and unknown outcomes.

Native grading is replaced by an injected runtime that records what the policy
passed down; these tests exercise the policy's decisions, not the runner.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab.qa.env_execution import ExecutionPolicy, ExecutionPolicyError, Runtime

PIN = "a" * 40
CAP = 3.0


def tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            digest.update(item.relative_to(path).as_posix().encode() + b"\0" + item.read_bytes())
    return "sha256:" + digest.hexdigest()


class Fake:
    """Injected runner outcomes; admission and evidence decisions remain real."""

    def __init__(self, pin: str = PIN) -> None:
        self.pin = pin
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None
        self.override: dict[str, Any] | None = None

    def runtime(self) -> Runtime:
        return Runtime(self._upstream, self._candidate, tree_digest, lambda: self.pin)

    def _run(self, kind: str, task: Path, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"kind": kind, "task": task, "args": args, **kwargs})
        if self.error is not None:
            raise self.error
        if self.override is not None:
            return dict(self.override)
        result: dict[str, Any] = {
            "status": "planned" if kwargs.get("plan") else "measured",
            "executed": not kwargs.get("plan", False),
            "reward": None if kwargs.get("plan") else 1.0,
            "rewards": None if kwargs.get("plan") else {"reward": 1.0},
            "task": {"path": os.path.realpath(task), "id": task.name,
                     "exact_tree_digest": tree_digest(task), "tests_digest": "sha256:tests"},
            "per_test": {"status": "parsed", "tests": [{"name": "t", "status": "passed"}]},
            "cost": {"native_spend": None}, "error": None,
        }
        if kind == "candidate":
            result.update(
                grader="port",
                grader_provenance={
                    "grader_digest": "sha256:grader", "prepared_task_digest": "sha256:prepared",
                    "port_identity": {"exact_tree_digest": tree_digest(kwargs["port_task"])},
                },
            )
        return result

    def _upstream(self, task: Path, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._run("upstream", Path(task), args, kwargs)

    def _candidate(self, task: Path, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._run("candidate", Path(task), args, kwargs)


@pytest.fixture
def fake() -> Fake:
    return Fake()


@pytest.fixture(autouse=True)
def lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "docker-lock"
    monkeypatch.setenv("EVALLAB_DOCKER_LOCK_DIR", str(directory))
    return directory


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(now=1000.0)

    def advance(seconds: float) -> None:
        state.now += seconds

    monkeypatch.setattr(
        "evallab.qa.env_execution.time",
        SimpleNamespace(monotonic=lambda: state.now, sleep=advance),
    )
    return state


@pytest.fixture
def task(tmp_path: Path) -> Path:
    path = tmp_path / "tasks" / "alpha"
    (path / "tests").mkdir(parents=True)
    (path / "solution").mkdir()
    (path / "task.toml").write_text("[task]\nname='alpha'\n")
    return path


@pytest.fixture
def port(tmp_path: Path) -> Path:
    path = tmp_path / "ports" / "alpha"
    path.mkdir(parents=True)
    (path / "scorer.py").write_text("print('score')\n")
    return path


def modal_document(tmp_path: Path, **override: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema": "evallab.qa.execution-policy/v1", "backend": "modal", "max_usd": CAP,
        "approval": "Peter approved cap3", "spend_ledger": str(tmp_path / "campaign.json"),
        "modal_app_name": "qa-env-v2", "billing_tag": "qa-env",
    }
    document.update(override)
    return {key: value for key, value in document.items() if value is not ...}


def write_policy(tmp_path: Path, document: dict[str, Any] | str, name: str = "policy.json") -> Path:
    path = tmp_path / name
    path.write_text(document if isinstance(document, str) else json.dumps(document))
    return path


def load(tmp_path: Path, fake: Fake, document: dict[str, Any] | str) -> ExecutionPolicy:
    return ExecutionPolicy.from_file(
        write_policy(tmp_path, document), repo_root=tmp_path, runtime=fake.runtime()
    )


def call(policy: ExecutionPolicy, task: Path, tmp_path: Path, name: str = "out", **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("oracle", True)
    return policy.run(task, timeout_seconds=30, output_dir=tmp_path / "evidence" / name, **kwargs)


def port_document(tmp_path: Path, port: Path, **override: Any) -> dict[str, Any]:
    return modal_document(
        tmp_path,
        port={"source_pin": PIN, "tasks": {"alpha": {"path": str(port), "tree_digest": tree_digest(port)}}},
        **override,
    )


# --- policy validation -------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"max_usd": 0},
        {"max_usd": -1},
        {"max_usd": True},
        {"max_usd": 1e999},
        {"approval": ...},
        {"approval": "  "},
        {"spend_ledger": ...},
        {"billing_tag": ...},
        {"backend": "auto"},
        {"modal_app_name": "other-app"},
        {"platform": "linux/arm64"},
        {"api_key": "sk-nope"},
        {"schema": "v0"},
    ],
)
def test_modal_policy_refuses_unmetered_or_ambiguous_ownership(tmp_path, fake, override):
    with pytest.raises(ExecutionPolicyError):
        load(tmp_path, fake, modal_document(tmp_path, **override))


def test_modal_ledger_must_live_in_an_existing_directory(tmp_path, fake):
    with pytest.raises(ExecutionPolicyError, match="existing directory"):
        load(tmp_path, fake, modal_document(tmp_path, spend_ledger=str(tmp_path / "missing" / "l.json")))


def test_docker_policy_is_cap_zero_with_no_modal_settings(tmp_path, fake):
    base = {"schema": "evallab.qa.execution-policy/v1", "backend": "docker"}
    for extra in ({"max_usd": 1}, {"max_usd": 0, "approval": "x"}, {"max_usd": 0, "spend_ledger": "l.json"}, {}):
        with pytest.raises(ExecutionPolicyError):
            load(tmp_path, fake, {**base, **extra})
    assert load(tmp_path, fake, {**base, "max_usd": 0}).public_config()["metered"] is False


@pytest.mark.parametrize(
    "text",
    ['{"schema": "evallab.qa.execution-policy/v1", "backend": "docker", "backend": "modal"}',
     '{"schema": "evallab.qa.execution-policy/v1", "backend": "docker", "max_usd": NaN}',
     "[]", "not json"],
)
def test_policy_json_is_strict(tmp_path, fake, text):
    with pytest.raises(ExecutionPolicyError):
        load(tmp_path, fake, text)


def test_symlinked_policy_file_is_refused(tmp_path, fake):
    real = write_policy(tmp_path, modal_document(tmp_path), "real.json")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(ExecutionPolicyError, match="symlink"):
        ExecutionPolicy.from_file(link, repo_root=tmp_path, runtime=fake.runtime())


def test_public_config_hides_approval_paths_and_exposes_digest(tmp_path, fake, port):
    policy = load(tmp_path, fake, port_document(tmp_path, port))
    config = policy.public_config()
    text = json.dumps(config)
    assert "Peter approved" not in text and str(tmp_path) not in text
    assert config["digest"] == policy.digest == "sha256:" + hashlib.sha256(
        (tmp_path / "policy.json").read_bytes()).hexdigest()
    assert port.name not in text


# --- owner policy is what reaches the runner -----------------------------------------






def test_actor_platform_cannot_override_the_owner_platform(tmp_path, fake, task):
    policy = load(tmp_path, fake, modal_document(tmp_path, platform="linux/amd64"))
    conflict = call(policy, task, tmp_path, "a", platform="linux/arm64")
    assert conflict["status"] == "refused" and conflict["executed"] is False and not fake.calls


def test_misuse_raises_and_never_reaches_the_runner(tmp_path, fake, task):
    policy = ExecutionPolicy.local(repo_root=tmp_path, runtime=fake.runtime())
    bad = [
        {"oracle": True, "blank": True}, {"oracle": False}, {"oracle": True, "plan": 1},
        {"oracle": True, "grader": "other"}, {"oracle": True, "timeout_seconds": float("nan")},
        {"oracle": True, "timeout_seconds": True}, {"oracle": True, "timeout_seconds": 0},
    ]
    for index, kwargs in enumerate(bad):
        kwargs.setdefault("timeout_seconds", 5)
        with pytest.raises(ValueError):
            policy.run(task, output_dir=tmp_path / f"o{index}", **kwargs)
    assert not fake.calls


# --- drift ----------------------------------------------------------------------------


def test_policy_file_edit_after_pinning_refuses_before_any_execution(tmp_path, fake, task):
    policy = load(tmp_path, fake, modal_document(tmp_path))
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(modal_document(tmp_path, max_usd=300)))
    result = call(policy, task, tmp_path)
    assert result["status"] == "refused" and "changed" in result["error"]
    assert result["executed"] is False and result["reward"] is None and not fake.calls


def test_policy_file_replaced_by_symlink_refuses(tmp_path, fake, task):
    policy = load(tmp_path, fake, modal_document(tmp_path))
    path = tmp_path / "policy.json"
    other = tmp_path / "other.json"
    other.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(other)
    assert call(policy, task, tmp_path)["status"] == "refused" and not fake.calls


def test_ledger_replaced_by_symlink_or_directory_refuses(tmp_path, fake, task):
    policy = load(tmp_path, fake, modal_document(tmp_path))
    ledger = tmp_path / "campaign.json"
    elsewhere = tmp_path / "fresh-ledger.json"
    elsewhere.write_text("{}")
    ledger.symlink_to(elsewhere)
    assert call(policy, task, tmp_path)["status"] == "refused"
    ledger.unlink()
    ledger.mkdir()
    assert call(policy, task, tmp_path, "b")["status"] == "refused" and not fake.calls


def test_evidence_directory_cannot_contain_the_ledger(tmp_path, fake, task):
    policy = load(tmp_path, fake, modal_document(tmp_path))
    result = policy.run(task, oracle=True, timeout_seconds=30, output_dir=tmp_path)
    assert result["status"] == "refused" and "overlaps" in result["error"] and not fake.calls


# --- PM port ----------------------------------------------------------------------------




@pytest.mark.parametrize("failure", ["tree", "pin", "unmapped"])
def test_port_drift_or_unpinned_task_refuses_without_spending(tmp_path, fake, task, port, failure):
    policy = load(tmp_path, fake, port_document(tmp_path, port))
    if failure == "tree":
        (port / "scorer.py").write_text("print('tampered')\n")
    elif failure == "pin":
        fake.pin = "b" * 40
    else:
        task = task.rename(task.with_name("beta"))
    result = call(policy, task, tmp_path, grader="port")
    assert result["status"] == "refused" and result["executed"] is False
    assert result["reward"] is None and not fake.calls


def test_port_requested_without_port_section_is_refused(tmp_path, fake, task):
    result = call(load(tmp_path, fake, modal_document(tmp_path)), task, tmp_path, grader="port")
    assert result["status"] == "refused" and "pins no PM port" in result["error"]


def test_port_policy_misdeclared_directory_is_refused_at_load(tmp_path, fake, port):
    document = port_document(tmp_path, port)
    document["port"]["tasks"]["alpha"]["path"] = str(port.parent)
    with pytest.raises(ExecutionPolicyError, match="named"):
        load(tmp_path, fake, document)


def test_missing_port_api_is_reported_not_stubbed(tmp_path, fake, task, port):
    def unavailable() -> str:
        raise ExecutionPolicyError("PM-port grading needs evallab.candidate_port.PM_FIXED_PIN")

    runtime = Runtime(fake._upstream, fake._candidate, tree_digest, unavailable)
    policy = ExecutionPolicy.from_file(
        write_policy(tmp_path, port_document(tmp_path, port)), repo_root=tmp_path, runtime=runtime
    )
    result = call(policy, task, tmp_path, grader="port")
    assert result["status"] == "refused" and "PM_FIXED_PIN" in result["error"]
    assert result["reward"] is None and result["execution_policy"]["selected_grader"] is None


def test_result_for_a_different_port_is_not_accepted_as_a_measurement(tmp_path, fake, task, port):
    policy = load(tmp_path, fake, port_document(tmp_path, port))
    fake.override = {
        "status": "measured", "executed": True, "reward": 1.0, "rewards": {"reward": 1.0},
        "grader": "port",
        "task": {"path": os.path.realpath(task), "exact_tree_digest": tree_digest(task)},
        "grader_provenance": {"port_identity": {"exact_tree_digest": "sha256:somewhere-else"}},
    }
    result = call(policy, task, tmp_path, grader="port")
    assert result["status"] == "error" and result["reward"] is None and result["rewards"] is None
    assert "pinned PM port" in result["error"]


def test_result_for_a_different_task_is_not_accepted_as_a_measurement(tmp_path, fake, task):
    policy = load(tmp_path, fake, modal_document(tmp_path))
    fake.override = {"status": "measured", "executed": True, "reward": 1.0,
                     "task": {"path": "/somewhere/else"}}
    result = call(policy, task, tmp_path)
    assert result["status"] == "error" and result["reward"] is None


# --- unknown / failure boundaries ---------------------------------------------------------




@pytest.mark.parametrize("native_error", [ValueError("malformed native output"), RuntimeError("provider reset")])
def test_native_exception_does_not_assert_that_no_execution_happened(tmp_path, fake, task, native_error):
    policy = load(tmp_path, fake, modal_document(tmp_path))
    fake.error = native_error
    result = call(policy, task, tmp_path)
    assert result["status"] == "error"
    assert result["executed"] is None
    assert result["reward"] is None


# --- plans and the campaign lock -------------------------------------------------------------


def hold(policy_path: Path) -> int:
    descriptor = os.open(policy_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


def test_lock_wait_counts_against_the_timeout_and_refuses_without_running(tmp_path, fake, task, clock):
    policy = load(tmp_path, fake, modal_document(tmp_path))
    held = hold(tmp_path / "campaign.json.qa-grade.lock")
    try:
        result = policy.run(task, oracle=True, timeout_seconds=0.4, output_dir=tmp_path / "e")
        assert clock.now == pytest.approx(1000.4)
    finally:
        os.close(held)
    assert result["status"] == "refused" and "lock" in result["error"]
    assert result["executed"] is False and not fake.calls


def test_plan_neither_takes_the_lock_nor_is_blocked_by_it(tmp_path, fake, task):
    policy = load(tmp_path, fake, modal_document(tmp_path))
    held = hold(tmp_path / "campaign.json.qa-grade.lock")
    try:
        result = call(policy, task, tmp_path, plan=True)
    finally:
        os.close(held)
    assert fake.calls[0]["plan"] is True and result["execution_policy"]["plan"] is True
    assert not (tmp_path / "campaign.json").exists()






def test_local_policy_serializes_on_the_host_lock_directory(tmp_path, fake, task, lock_dir):
    policy = ExecutionPolicy.local(repo_root=tmp_path, runtime=fake.runtime())
    lock_dir.mkdir()
    held = hold(lock_dir / "qa-env-grade.lock")
    try:
        result = policy.run(task, oracle=True, timeout_seconds=0.3, output_dir=tmp_path / "e")
    finally:
        os.close(held)
    assert result["status"] == "refused" and not fake.calls
