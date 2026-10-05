"""HAR-163: dispatch-side fixes from Baseline v1 (behavioral, real tick path)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from evallab.daytona_guard import GuardUnavailable
from evallab.dispatch_guards import (
    SelfhostedProbeOutcome,
    daytona_tick_allowance,
    smoke_trial_blocks,
)
from evallab.execution_contracts import MIMO_SELFHOSTED_MODEL_SELECTOR, DispatchCapacity
from evallab.queue import DirectoryQueue, Executor, load_events
from evallab.runner import RunRequest
from evallab.schemas import AutoRunRule, ExperimentSpec, StandingApprovalsPolicy

FINISHED_AT = "2026-10-04T00:00:10+00:00"
UPSTREAM = "http://127.0.0.1:9"
CREDENTIALS = frozenset(
    {
        "claude_oauth",
        "codex_auth",
        "mimo_selfhosted_api_environment",
        "zai_openapi_api_environment",
        "deepseek_api_environment",
    }
)


def _policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[
            AutoRunRule(name="local-controls", agents=["oracle", "nop"]),
            AutoRunRule(
                name="canary",
                tasks=["canary/*"],
                agents=["codex", "claude-code", "mimoagent"],
                max_attempts=3,
            ),
        ],
    )


def _spec(
    name: str,
    *,
    agent: str = "codex",
    model: str | None = None,
    environment: str = "docker",
) -> ExperimentSpec:
    return ExperimentSpec(
        name=name,
        hypothesis="exercise the har163 dispatch guard",
        purpose="practice",
        task="canary/event-summary",
        agent=agent,
        model=model,
        environment=environment,
        submitted_by="test-agent",
        est_cost_usd=0,
    )


def _service(root: Path, *, runner, **overrides) -> Executor:
    params: dict = {
        "repo_root": root,
        "queue": DirectoryQueue(root / "queue"),
        "policy": _policy(),
        "runner": runner,
        "ingester": lambda _path: None,
        "credential_probe": lambda: CREDENTIALS,
        "spent_today": lambda: 0.0,
        "consecutive_harness_failures": lambda: 0,
        "sleeper": lambda _seconds: None,
    }
    params.update(overrides)
    return Executor(**params)


def _approve(service: Executor, spec: ExperimentSpec) -> str:
    waiting, _ = service.submit(spec)
    spec_id = str(service.queue.load(waiting).spec_id)
    service.queue.approve(spec_id, actor="peter")
    return spec_id


def _mkdir_job(request: RunRequest) -> Path:
    job = Path(request.jobs_dir) / request.name
    job.mkdir(parents=True, exist_ok=True)
    return job


def _write_job(
    request: RunRequest,
    *,
    reward: float | None,
    exception_type: str | None = None,
) -> Path:
    job = Path(request.jobs_dir) / request.name
    job.mkdir(parents=True, exist_ok=True)
    (job / "result.json").write_text(
        json.dumps(
            {
                "id": f"job-{request.name}",
                "n_total_trials": 1,
                "stats": {},
                "finished_at": FINISHED_AT,
            }
        ),
        encoding="utf-8",
    )
    for stub in ("config.json", "lock.json"):
        (job / stub).write_text("{}", encoding="utf-8")
    trial = job / "trial-0"
    trial.mkdir(exist_ok=True)
    doc: dict = {
        "id": "trial-0",
        "trial_name": "trial-0",
        "task_name": "canary/event-summary",
        "verifier_result": {"rewards": {} if reward is None else {"reward": reward}},
    }
    if exception_type is not None:
        doc["exception_info"] = {
            "exception_type": exception_type,
            "exception_message": "mimoagent requires the runner's host proxy URL",
        }
    (trial / "result.json").write_text(json.dumps(doc), encoding="utf-8")
    for stub in ("config.json", "lock.json"):
        (trial / stub).write_text("{}", encoding="utf-8")
    return job


def _reason_codes(service: Executor) -> list[str | None]:
    return [event.reason_code for event in load_events(service.queue.events_path)]


def _probe_sleeper(monkeypatch) -> Callable[[float], None]:
    clock = [0.0]
    monkeypatch.setattr("evallab.queue.time.time", lambda: clock[0])

    def advance(seconds: float) -> None:
        clock[0] += seconds

    return advance


def test_selfhosted_probe_waits_then_dispatches(tmp_path: Path, monkeypatch) -> None:
    """A 503-then-200 upstream waits once, then both specs dispatch.

    Every launch re-probes: the second spec cannot reuse the first spec's
    passing probe, so two attempts for the first spec plus one fresh probe
    for the second.
    """
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", UPSTREAM)
    monkeypatch.setenv("MIMO_SELFHOSTED_API_KEY", "test-key")
    calls: list[str] = []

    def probe(endpoint: str, model: str, key: str, timeout: float) -> SelfhostedProbeOutcome:
        calls.append(endpoint)
        assert model == MIMO_SELFHOSTED_MODEL_SELECTOR.removeprefix("selfhosted/")
        assert key == "test-key"
        if len(calls) == 1:
            return SelfhostedProbeOutcome(ok=False, status=503, cold=True, detail="503")
        return SelfhostedProbeOutcome(ok=True, status=200, cold=False, detail="ready")

    ran: list[str] = []
    service = _service(
        tmp_path,
        runner=lambda request: (ran.append(request.name), _mkdir_job(request))[1],
        selfhosted_probe_fn=probe,
        selfhosted_warmup_seconds=60.0,
        sleeper=_probe_sleeper(monkeypatch),
        smoke_gate_enabled=False,
    )
    for name in ("probe-a", "probe-b"):
        _approve(
            service,
            _spec(name, agent="mimoagent", model=MIMO_SELFHOSTED_MODEL_SELECTOR),
        )
    assert service.tick() == 2
    assert sorted(ran) == ["probe-a", "probe-b"]
    # Two attempts for the first spec (503 then 200), plus one fresh probe
    # before the second launch: no cached-ready reuse.
    assert len(calls) == 3
    assert "selfhosted_probe_ready" in _reason_codes(service)


def test_selfhosted_probe_cold_second_endpoint_cannot_launch(tmp_path: Path, monkeypatch) -> None:
    """A freshly cold endpoint blocks the later launch even after one success."""
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", UPSTREAM)
    monkeypatch.setenv("MIMO_SELFHOSTED_API_KEY", "test-key")
    calls: list[str] = []

    def probe(endpoint: str, model: str, key: str, timeout: float) -> SelfhostedProbeOutcome:
        calls.append(endpoint)
        if len(calls) == 1:
            return SelfhostedProbeOutcome(ok=True, status=200, cold=False, detail="ready")
        return SelfhostedProbeOutcome(ok=False, status=503, cold=True, detail="503")

    ran: list[str] = []
    service = _service(
        tmp_path,
        runner=lambda request: (ran.append(request.name), _mkdir_job(request))[1],
        selfhosted_probe_fn=probe,
        selfhosted_warmup_seconds=0.05,
        sleeper=_probe_sleeper(monkeypatch),
        smoke_gate_enabled=False,
    )
    first = _approve(
        service, _spec("probe-first", agent="mimoagent", model=MIMO_SELFHOSTED_MODEL_SELECTOR)
    )
    second = _approve(
        service, _spec("probe-second", agent="mimoagent", model=MIMO_SELFHOSTED_MODEL_SELECTOR)
    )
    assert service.tick() == 1
    assert ran == ["probe-first"]
    assert service.queue.locate(first, ("done",)).is_file()
    assert service.queue.locate(second, ("approved",)).is_file()
    assert "selfhosted_endpoint_not_ready" in _reason_codes(service)


def test_selfhosted_probe_cold_blocks_dispatch(tmp_path: Path, monkeypatch) -> None:
    """A still-cold endpoint never launches: the spec stays approved."""
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", UPSTREAM)
    monkeypatch.setenv("MIMO_SELFHOSTED_API_KEY", "test-key")

    def probe(endpoint: str, model: str, key: str, timeout: float) -> SelfhostedProbeOutcome:
        return SelfhostedProbeOutcome(ok=False, status=503, cold=True, detail="503")

    ran: list[str] = []
    service = _service(
        tmp_path,
        runner=lambda request: (ran.append(request.name), _mkdir_job(request))[1],
        selfhosted_probe_fn=probe,
        selfhosted_warmup_seconds=0.05,
        sleeper=_probe_sleeper(monkeypatch),
        smoke_gate_enabled=False,
    )
    spec_id = _approve(
        service, _spec("probe-cold", agent="mimoagent", model=MIMO_SELFHOSTED_MODEL_SELECTOR)
    )
    assert service.tick() == 0
    assert ran == []
    assert service.queue.locate(spec_id, ("approved",)).is_file()
    assert "selfhosted_endpoint_not_ready" in _reason_codes(service)


def test_infra_spike_stops_dispatch(tmp_path: Path) -> None:
    """A proxy_error_spike alert fences the queue; running work is untouched."""
    progress: list[str] = []
    service = _service(
        tmp_path,
        runner=_mkdir_job,
        progress=progress.append,
        smoke_gate_enabled=False,
    )
    for name in ("spike-a", "spike-b"):
        _approve(service, _spec(name))
    watch = tmp_path / "runs" / "earlier-job" / "watch"
    watch.mkdir(parents=True)
    (watch / "alerts.jsonl").write_text(
        json.dumps({"rule": "proxy_error_spike", "severity": "high"}) + "\n",
        encoding="utf-8",
    )
    assert service.tick() == 0
    assert (tmp_path / "queue" / "STOP").is_file()
    assert any("evallab resume" in line for line in progress)
    assert "infra_spike:proxy_error_spike" in _reason_codes(service)
    assert service.last_tick_reason == "infra_spike_stop"


def test_mid_tick_spike_stops_remaining(tmp_path: Path) -> None:
    """A spike alert from the just-finished job fences the rest of the tick."""
    calls: list[str] = []

    def run(request: RunRequest) -> Path:
        calls.append(request.name)
        job = _mkdir_job(request)
        if len(calls) == 1:
            watch = job / "watch"
            watch.mkdir(parents=True, exist_ok=True)
            (watch / "alerts.jsonl").write_text(
                json.dumps({"rule": "infra_spike", "severity": "high"}) + "\n",
                encoding="utf-8",
            )
        return job

    service = _service(tmp_path, runner=run)
    for name in ("mid-a", "mid-b", "mid-c"):
        _approve(service, _spec(name))
    assert service.tick() == 1
    assert len(calls) == 1
    assert (tmp_path / "queue" / "STOP").is_file()
    assert "infra_spike:infra_spike" in _reason_codes(service)


def test_daytona_memory_clamp(tmp_path: Path) -> None:
    """The tick plan fits the memory cap up front; trimmed specs stay approved."""
    progress: list[str] = []
    ran: list[str] = []

    def observe() -> dict:
        return {
            "limits": {"memory_gib": 200.0},
            "safety_fraction": 0.8,
            "used": {"memory_gib": 140.0},
            "pending": {"memory_gib": 0.0},
            "per_sandbox_limits": {"memory_gib": 8.0},
        }

    service = _service(
        tmp_path,
        runner=lambda request: (ran.append(request.name), _mkdir_job(request))[1],
        progress=progress.append,
        daytona_observe_fn=observe,
    )
    ids = [
        _approve(service, _spec(f"daytona-{i}", agent="oracle", environment="daytona"))
        for i in range(3)
    ]
    assert service.tick() == 1
    assert len(ran) == 1
    assert any("daytona tick clamp" in line for line in progress)
    assert "daytona_memory_clamped" in _reason_codes(service)
    remaining = {path.name for path, _ in service.queue.list_specs("approved")}
    assert len(remaining) == 2
    assert (
        service.queue.locate(ids[0], ("done",)).is_file()
        or service.queue.locate(ids[1], ("done",)).is_file()
        or service.queue.locate(ids[2], ("done",)).is_file()
    )


def test_daytona_guard_unavailable_falls_back(tmp_path: Path) -> None:
    """An unreadable guard still lets one launch through and says so."""

    def observe() -> dict:
        raise GuardUnavailable("boom")

    progress: list[str] = []
    ran: list[str] = []
    service = _service(
        tmp_path,
        runner=lambda request: (ran.append(request.name), _mkdir_job(request))[1],
        progress=progress.append,
        daytona_observe_fn=observe,
    )
    for i in range(2):
        _approve(service, _spec(f"daytona-fb-{i}", agent="oracle", environment="daytona"))
    assert service.tick() == 1
    assert len(ran) == 1
    assert any("falling back conservatively" in line for line in progress)
    assert "daytona_guard_unavailable" in _reason_codes(service)


def test_smoke_gate_blocks_batch_on_wiring_error(tmp_path: Path) -> None:
    """An ungradable smoke trial (wiring error) fences the queue durably."""
    calls: list[str] = []

    def run(request: RunRequest) -> Path:
        calls.append(request.name)
        if len(calls) == 1:
            return _write_job(request, reward=None, exception_type="RuntimeError")
        return _write_job(request, reward=1.0)

    progress: list[str] = []
    service = _service(tmp_path, runner=run, progress=progress.append)
    for name in ("smoke-a", "smoke-b", "smoke-c"):
        _approve(service, _spec(name))
    assert service.tick() == 1
    assert len(calls) == 1
    assert service.last_tick_reason == "smoke_gate_blocked"
    assert any("smoke_gate_blocked" in (code or "") for code in _reason_codes(service))
    assert len(service.queue.list_specs("approved")) == 2
    assert (tmp_path / "queue" / "STOP").is_file()


def test_smoke_gate_passes_gradable_batch(tmp_path: Path) -> None:
    """A gradable smoke trial lets the rest of the batch launch."""
    service = _service(tmp_path, runner=lambda request: _write_job(request, reward=1.0))
    for name in ("smoke-p", "smoke-q", "smoke-r"):
        _approve(service, _spec(name))
    assert service.tick() == 3
    assert service.queue.list_specs("approved") == []


def test_smoke_gate_opt_out_is_recorded(tmp_path: Path) -> None:
    """--no-smoke-gate dispatches the batch and records the opt-out."""
    service = _service(
        tmp_path,
        runner=lambda request: _write_job(request, reward=1.0),
        smoke_gate_enabled=False,
    )
    for name in ("smoke-x", "smoke-y"):
        _approve(service, _spec(name))
    assert service.tick() == 2
    assert "smoke_gate_disabled" in _reason_codes(service)


def _write_garbage_job(request: RunRequest) -> Path:
    job = Path(request.jobs_dir) / request.name
    job.mkdir(parents=True, exist_ok=True)
    (job / "result.json").write_text("{not valid json", encoding="utf-8")
    return job


def _write_empty_job(request: RunRequest) -> Path:
    job = Path(request.jobs_dir) / request.name
    job.mkdir(parents=True, exist_ok=True)
    (job / "result.json").write_text(
        json.dumps(
            {
                "id": f"job-{request.name}",
                "n_total_trials": 0,
                "stats": {},
                "finished_at": FINISHED_AT,
            }
        ),
        encoding="utf-8",
    )
    return job


def _fake_trial(*, reward, exception_type=None, name="trial-0"):
    result: dict = {}
    if exception_type is not None:
        result["exception_info"] = {
            "exception_type": exception_type,
            "exception_message": "synthetic",
        }
    return SimpleNamespace(name=name, path=f"/tmp/{name}", primary_reward=reward, result=result)


def _fake_job(*trials):
    return SimpleNamespace(trials=list(trials))


def test_smoke_trial_blocks_without_evidence() -> None:
    """The classifier is fail-closed on absence: no evidence ever reads gradable."""
    assert smoke_trial_blocks(None)[0] is True
    assert smoke_trial_blocks(_fake_job())[0] is True
    assert smoke_trial_blocks(_fake_job(_fake_trial(reward=None)))[0] is True
    assert smoke_trial_blocks(_fake_job(_fake_trial(reward=float("nan"))))[0] is True
    assert smoke_trial_blocks(_fake_job(_fake_trial(reward=float("inf"))))[0] is True


def test_smoke_trial_agent_stop_needs_its_grade() -> None:
    """A canonical agent stop without a finite reward blocks; with one it releases."""
    assert (
        smoke_trial_blocks(_fake_job(_fake_trial(reward=None, exception_type="AgentTimeoutError")))[
            0
        ]
        is True
    )
    assert (
        smoke_trial_blocks(_fake_job(_fake_trial(reward=None, exception_type="LoopBreakStop")))[0]
        is True
    )
    assert (
        smoke_trial_blocks(_fake_job(_fake_trial(reward=0.0, exception_type="AgentTimeoutError")))[
            0
        ]
        is False
    )
    assert (
        smoke_trial_blocks(_fake_job(_fake_trial(reward=1.0, exception_type="LoopBreakStop")))[0]
        is False
    )
    assert smoke_trial_blocks(_fake_job(_fake_trial(reward=1.0)))[0] is False


def test_smoke_trial_unexpected_exception_blocks_despite_grade() -> None:
    """Infra/wiring evidence is blocking even when a reward is present."""
    assert (
        smoke_trial_blocks(_fake_job(_fake_trial(reward=1.0, exception_type="RuntimeError")))[0]
        is True
    )
    graded_bad_shape = SimpleNamespace(
        name="t", path="/tmp/t", primary_reward=1.0, result={"exception_info": "boom"}
    )
    assert smoke_trial_blocks(_fake_job(graded_bad_shape))[0] is True


def test_smoke_gate_blocks_batch_on_missing_evidence(tmp_path: Path) -> None:
    """A smoke run that leaves no job evidence fences the queue durably."""
    service = _service(tmp_path, runner=_mkdir_job)
    for name in ("smoke-m", "smoke-n"):
        _approve(service, _spec(name))
    assert service.tick() == 1
    assert service.last_tick_reason == "smoke_gate_blocked"
    assert (tmp_path / "queue" / "STOP").is_file()
    assert len(service.queue.list_specs("approved")) == 1


def test_smoke_gate_blocks_batch_on_unreadable_evidence(tmp_path: Path) -> None:
    """A smoke run with malformed job evidence fences the queue durably."""
    service = _service(tmp_path, runner=_write_garbage_job)
    for name in ("smoke-u", "smoke-v"):
        _approve(service, _spec(name))
    assert service.tick() == 1
    assert service.last_tick_reason == "smoke_gate_blocked"
    assert (tmp_path / "queue" / "STOP").is_file()
    assert len(service.queue.list_specs("approved")) == 1


def test_smoke_gate_blocks_batch_on_empty_evidence(tmp_path: Path) -> None:
    """A smoke job that grades zero trials fences the queue durably."""
    service = _service(tmp_path, runner=_write_empty_job)
    for name in ("smoke-e", "smoke-f"):
        _approve(service, _spec(name))
    assert service.tick() == 1
    assert service.last_tick_reason == "smoke_gate_blocked"
    assert (tmp_path / "queue" / "STOP").is_file()
    assert len(service.queue.list_specs("approved")) == 1


def test_smoke_gate_blocks_agent_stop_without_grade(tmp_path: Path) -> None:
    """An agent-stop name with no verifier grade is absence of evidence: block."""
    service = _service(
        tmp_path,
        runner=lambda request: _write_job(request, reward=None, exception_type="AgentTimeoutError"),
    )
    for name in ("smoke-s", "smoke-t"):
        _approve(service, _spec(name))
    assert service.tick() == 1
    assert service.last_tick_reason == "smoke_gate_blocked"
    assert (tmp_path / "queue" / "STOP").is_file()
    assert len(service.queue.list_specs("approved")) == 1


def test_smoke_gate_blocks_graded_infra_exception(tmp_path: Path) -> None:
    """A finite reward alongside a wiring exception still fences the queue."""
    service = _service(
        tmp_path,
        runner=lambda request: _write_job(request, reward=1.0, exception_type="RuntimeError"),
    )
    for name in ("smoke-g", "smoke-h"):
        _approve(service, _spec(name))
    assert service.tick() == 1
    assert service.last_tick_reason == "smoke_gate_blocked"
    assert (tmp_path / "queue" / "STOP").is_file()
    assert len(service.queue.list_specs("approved")) == 1


def test_smoke_gate_passes_graded_agent_stop(tmp_path: Path) -> None:
    """A finite grade on a canonical agent stop releases the rest of the batch."""
    service = _service(
        tmp_path,
        runner=lambda request: _write_job(request, reward=0.0, exception_type="AgentTimeoutError"),
    )
    for name in ("smoke-j", "smoke-k", "smoke-l"):
        _approve(service, _spec(name))
    assert service.tick() == 3
    assert service.queue.list_specs("approved") == []
    assert not (tmp_path / "queue" / "STOP").exists()


def test_smoke_gate_fresh_executor_stays_fenced(tmp_path: Path) -> None:
    """After a smoke fence, a fresh executor dispatches nothing: no bypass."""
    calls: list[str] = []

    def run(request: RunRequest) -> Path:
        calls.append(request.name)
        if len(calls) == 1:
            return _write_job(request, reward=None, exception_type="RuntimeError")
        return _write_job(request, reward=1.0)

    service = _service(tmp_path, runner=run)
    for name in ("smoke-r1", "smoke-r2"):
        _approve(service, _spec(name))
    assert service.tick() == 1
    assert (tmp_path / "queue" / "STOP").is_file()
    fresh = _service(tmp_path, runner=lambda request: _write_job(request, reward=1.0))
    assert fresh.tick() == 0
    assert len(service.queue.list_specs("approved")) == 1
    assert (tmp_path / "queue" / "STOP").is_file()


def test_daytona_tick_allowance_single_launch_boundary() -> None:
    """Headroom for exactly one sandbox allows exactly one launch."""
    allowance, _ = daytona_tick_allowance(
        {
            "limits": {"memory_gib": 100.0},
            "safety_fraction": 0.8,
            "used": {"memory_gib": 56.0},
            "pending": {"memory_gib": 8.0},
            "per_sandbox_limits": {"memory_gib": 8.0},
        }
    )
    assert allowance == 1


def test_undispatched_smoke_holds_batch_and_fences_restart(tmp_path: Path, monkeypatch) -> None:
    """A readiness-rejected smoke trial launches nothing and fences restarts."""
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", UPSTREAM)
    monkeypatch.setenv("MIMO_SELFHOSTED_API_KEY", "fixture-only-auth")

    def probe(endpoint: str, model: str, key: str, timeout: float) -> SelfhostedProbeOutcome:
        return SelfhostedProbeOutcome(ok=False, status=400, cold=False, detail="rejected")

    ran: list[str] = []
    service = _service(
        tmp_path,
        runner=lambda request: (ran.append(request.name), _write_job(request, reward=1.0))[1],
        selfhosted_probe_fn=probe,
    )
    for index in range(3):
        _approve(
            service,
            _spec(f"smoke-{index}", agent="mimoagent", model=MIMO_SELFHOSTED_MODEL_SELECTOR),
        )
    assert service.tick(parallel=3) == 0
    assert ran == []
    assert service.queue.stop_path.exists()
    assert len(service.queue.list_specs("approved")) == 3
    assert service.queue.list_specs("running") == []
    assert any("smoke_gate_blocked" in (code or "") for code in _reason_codes(service))

    fresh = _service(tmp_path, runner=lambda request: _write_job(request, reward=1.0))
    assert fresh.tick(parallel=3) == 0
    assert len(fresh.queue.list_specs("approved")) == 3


def test_capacity_clamp_preserves_smoke_gate(tmp_path: Path) -> None:
    """A multi-model batch clamped to one per tick is still smoke-gated."""
    calls: list[str] = []

    def run(request: RunRequest) -> Path:
        calls.append(request.name)
        if len(calls) == 1:
            return _write_job(request, reward=None, exception_type="RuntimeError")
        return _write_job(request, reward=1.0)

    service = _service(
        tmp_path,
        runner=run,
        capacity=DispatchCapacity(max_specs_per_tick=1),
    )
    for name in ("cap-a", "cap-b", "cap-c"):
        _approve(service, _spec(name))
    assert service.tick() == 1
    assert len(calls) == 1
    assert service.queue.stop_path.exists()
    assert len(service.queue.list_specs("approved")) == 2
    assert any("smoke_gate_blocked" in (code or "") for code in _reason_codes(service))

    fresh = _service(tmp_path, runner=run)
    assert fresh.tick() == 0
    assert len(calls) == 1


def test_blocked_smoke_holds_free_controls_until_explicit_resume(tmp_path: Path) -> None:
    """No new launch follows a blocked smoke; explicit resume releases a control."""
    calls: list[str] = []

    def run(request: RunRequest) -> Path:
        calls.append(request.name)
        if request.name == "smoke-a":
            return _write_job(request, reward=None, exception_type="RuntimeError")
        return _write_job(request, reward=1.0)

    service = _service(tmp_path, runner=run)
    _approve(service, _spec("smoke-a"))
    _approve(service, _spec("smoke-b"))
    control_path, _ = service.submit(_spec("free-control", agent="oracle"))
    control_id = str(service.queue.load(control_path).spec_id)
    assert service.tick() == 1
    assert calls == ["smoke-a"]
    assert service.queue.stop_path.exists()
    remaining = {item.name for _, item in service.queue.list_specs("approved")}
    assert remaining == {"smoke-b", "free-control"}

    fresh = _service(tmp_path, runner=run)
    assert fresh.tick() == 0
    assert calls == ["smoke-a"]
    fresh.queue.resume()
    assert fresh.tick(spec_ids=[control_id]) == 1
    assert calls == ["smoke-a", "free-control"]
    assert {item.name for _, item in fresh.queue.list_specs("approved")} == {"smoke-b"}


def test_standalone_single_model_spec_is_not_a_smoke_batch(tmp_path: Path) -> None:
    """One model-backed spec plus a control dispatches without any fence."""
    service = _service(tmp_path, runner=lambda request: _write_job(request, reward=1.0))
    _approve(service, _spec("solo-model"))
    service.submit(_spec("solo-control", agent="oracle"))
    assert service.tick() == 2
    assert not service.queue.stop_path.exists()
    assert service.queue.list_specs("approved") == []
    assert not any("smoke_gate_blocked" in (code or "") for code in _reason_codes(service))


def test_failed_smoke_cannot_release_batch_using_graded_artifact(tmp_path: Path) -> None:
    """Graded files at a failed dispatch's path do not prove a healthy smoke."""
    calls: list[str] = []

    def run(request: RunRequest) -> Path:
        calls.append(request.name)
        job = _write_job(request, reward=1.0)
        if len(calls) == 1:
            raise RuntimeError("fixture dispatch failed after graded files were written")
        return job

    service = _service(tmp_path, runner=run)
    for name in ("failed-smoke", "rest"):
        _approve(service, _spec(name))
    assert service.tick() == 1
    assert calls == ["failed-smoke"]
    assert len(service.queue.list_specs("failed")) == 1
    assert len(service.queue.list_specs("approved")) == 1
    assert service.queue.stop_path.exists()
    assert _service(tmp_path, runner=run).tick() == 0
    assert calls == ["failed-smoke"]
