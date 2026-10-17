"""Deterministic rollout tests: fake executor, no Modal, no network."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from evallab.rollout import (
    TrialOutcome,
    run_rollout,
    sandbox_cost_usd,
    summarize,
    task_resources,
    validate_model_selector,
)


def _task(tmp_path: Path, name: str, *, cpus: int = 2, memory_mb: int = 8192) -> Path:
    task = tmp_path / name
    task.mkdir()
    (task / "task.toml").write_text(
        f"[environment]\ncpus = {cpus}\nmemory_mb = {memory_mb}\n", encoding="utf-8"
    )
    return task


def test_selector_validation_admits_the_three_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    assert validate_model_selector("tinker/Qwen/Qwen3.5-9B") == "tinker"
    assert validate_model_selector("openrouter-metered/xiaomi/mimo-v2.6-flash") == "openrouter"
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", "https://lora.example/modal")
    base = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
    assert validate_model_selector(base) == "selfhosted"
    for adapter in ("rds1-a", "rds1-b", "rds1-c"):
        assert validate_model_selector(f"{base}:{adapter}") == "selfhosted"


def test_selector_validation_refuses_unknown_and_missing_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", raising=False)
    with pytest.raises(ValueError, match="EVALLAB_MIMO_SELFHOSTED_UPSTREAM"):
        validate_model_selector("selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:rds1-a")
    with pytest.raises(ValueError):
        validate_model_selector("selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:rds1-z")
    with pytest.raises(ValueError, match="unroutable"):
        validate_model_selector("gpt-5")


def test_attempts_expand_to_independent_trials(tmp_path: Path) -> None:
    tasks = [_task(tmp_path, "task-a"), _task(tmp_path, "task-b")]
    summary = run_rollout(
        tasks,
        model="tinker/Qwen/Qwen3.5-9B",
        concurrency=4,
        attempts=3,
        out_dir=tmp_path / "out",
        root=tmp_path,
        execute=_fake_execute,
    )
    assert summary["trials"] == 6 and summary["attempts"] == 3
    rows = [
        json.loads(line) for line in (tmp_path / "out" / "results.jsonl").read_text().splitlines()
    ]
    assert {row["name"].rsplit("-a", 1)[-1] for row in rows} == {"1", "2", "3"}


def test_arm_slugs_are_disjoint() -> None:
    import re

    from evallab.rollout import _arm_slug

    base = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
    assert _arm_slug(base) == "mimo-v2-6-distill-qwen-9b"
    assert _arm_slug(f"{base}:rds1-b") == "mimo-v2-6-distill-qwen-9b-rds1-b"
    slugs = {
        _arm_slug(base),
        *(_arm_slug(f"{base}:{arm}") for arm in ("rds1-a", "rds1-b", "rds1-c")),
    }
    assert len(slugs) == 4
    name_re = re.compile(r"^[a-z0-9][a-z0-9-]+$")
    for slug in slugs:
        assert name_re.match(f"rollout-{slug}-000-7d0add50180f-a1")


def test_production_spec_names_are_admitted() -> None:
    from evallab.rollout import _arm_slug
    from evallab.schemas import ExperimentSpec

    base = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
    for model in (base, *(f"{base}:{a}" for a in ("rds1-a", "rds1-b", "rds1-c"))):
        spec = ExperimentSpec(
            spec_id="01M00000000000000000000000",
            name=f"rollout-{_arm_slug(model)}-000-7d0add50180f-a1",
            hypothesis="test",
            purpose="baseline",
            task="derived/task",
            task_path="derived/task",
            agent="terminus-2",
            model=model,
            environment="modal",
            jobs_dir="runs",
            submitted_by="infra-rollout",
            timeout_seconds=1800,
            est_cost_usd=0.10,
            cost_limit_usd=1.00,
            max_requests=300,
            max_input_tokens=2_000_000,
            max_output_tokens=300_000,
            max_total_tokens=2_300_000,
            concurrency=1,
            attempts=1,
        )
        assert spec.name.endswith("-a1")


def test_failed_trial_emits_error_row_not_a_pass(tmp_path: Path) -> None:
    from evallab.rollout import _outcome

    job_dir = tmp_path / "job"
    trial = job_dir / "trial__x"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps(
            {
                "verifier_result": None,
                "exception_info": {"exception_type": "RuntimeError"},
            }
        )
    )
    outcome = _outcome(tmp_path / "task", "rollout-x", job_dir, 1.0, None)
    assert outcome.reward is None
    assert outcome.passed is None
    assert outcome.error is not None and "RuntimeError" in outcome.error


def _capture_executor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list:
    import evallab.queue as queue_module

    captured: list = []

    class FakeQueue:
        def approve(self, spec_id: str, *, actor: str) -> None:
            pass

        def locate(self, spec_id: str) -> Path:
            return tmp_path / "done" / "spec.json"

    class FakeExecutor:
        queue = FakeQueue()

        @classmethod
        def from_repo(cls, root: Path, **kwargs: object) -> FakeExecutor:
            return cls()

        def submit(self, spec: object) -> None:
            captured.append(spec)

        def tick(self, **kwargs: object) -> int:
            return 0

    monkeypatch.setattr(queue_module, "Executor", FakeExecutor)
    return captured


def _lineage_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Fixture repo whose ledger + scoped record admit one variant package."""
    import shutil

    import yaml

    from evallab.registry import compute_task_digests
    from evallab.setup_fingerprint import resolve_repo_root
    from evallab.task_variants import append_status_evidence, derive_task

    real_root = resolve_repo_root(None, Path(__file__))
    root = tmp_path / "repo"
    profiles = root / "research" / "setup-profiles"
    profiles.mkdir(parents=True)
    shutil.copy(
        real_root / "research/setup-profiles/xiaomi-mimo-rl.yaml",
        profiles / "xiaomi-mimo-rl.yaml",
    )
    serve = root / "tools" / "modal-mimo-serve"
    serve.mkdir(parents=True)
    shutil.copy(real_root / "tools/modal-mimo-serve/serve.py", serve / "serve.py")
    tree = root / "tools" / "mimoagent-harbor"
    tree.mkdir(parents=True)
    shutil.copy(real_root / "tools/mimoagent-harbor/swe.yaml", tree / "swe.yaml")
    marker = root / "src" / "evallab"
    marker.mkdir(parents=True)
    (marker / "mimo_tool_calls.py").write_text("# fixture presence marker\n")
    parent = root / "tasks" / "format-code-task-900001"
    parent.mkdir(parents=True)
    (parent / "task.toml").write_text(
        '[task]\nname = "mimo-v2.6-rl/format-code-task-900001"\n\n'
        '[agent]\nnetwork_mode = "no-network"\n'
    )
    digest = compute_task_digests(parent).package
    ledger = root / "research" / "experiments" / "python-task-ledger"
    ledger.mkdir(parents=True)
    header = (
        "task_id,split,project,image_mib,status,reason,run,run_digest,"
        "run_transform,run_variant_status,census_label,census_nop_job,"
        "census_evidence,leak_channel,evidence"
    )
    (ledger / "ledger.csv").write_text(
        header
        + "\nformat-code-task-900001,train,fixture,100,usable,fixture sound,"
        + f"original,{digest},,,sound,fp-test-job,fixture evidence,none_found,"
        + "fixture-evidence\n"
    )
    record = derive_task(
        parent,
        changes={"tests/integrity/probe.py": b'"""rollout task-id probe"""\n'},
        transform="separate-verifier@10",
        rationale="rollout task-id regression",
        created_by="test",
        parent_source={"kind": "local", "path": str(parent)},
        repo_root=root,
        variants_root=tmp_path / "variants-store",
    )
    record = append_status_evidence(
        record, "validated", evidence="locked nop sound", by="test", repo_root=root
    )
    profile_path = profiles / "xiaomi-mimo-rl.yaml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["campaign_lineage"]["headline"]["package_digests"].append(record.variant_digest)
    profile_path.write_text(yaml.safe_dump(profile))
    package = tmp_path / "variants-store" / record.task_slug / record.digest12
    dest = root / "variant-tasks" / "format-code-task-900001"
    shutil.copytree(package, dest)
    return root, dest


def _built_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root: Path, package: Path):
    from evallab import rollout

    captured = _capture_executor(tmp_path, monkeypatch)
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", "https://trainer.example/modal")
    rollout.run_rollout(
        [package],
        model="selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:rds1-b",
        agent="mimoagent",
        concurrency=1,
        attempts=1,
        out_dir=root / "out",
        root=root,
        actor="research-harbor",
        cost_limit_usd=1.00,
    )
    assert len(captured) == 1
    return captured[0]


def test_run_rollout_spec_admits_against_existing_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.execution_contracts import validate_request
    from evallab.queue import Executor

    root, package = _lineage_repo(tmp_path)
    spec = _built_spec(tmp_path, monkeypatch, root, package)
    assert spec.task_id == "format-code-task-900001"
    request = Executor.prepare_request(
        spec, repo_root=root, lease_path=root / "queue" / "lease", lease_generation=None
    )
    validate_request(request, repo_root=root)


def test_run_rollout_spec_refuses_when_declared_id_mismatches_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.execution_contracts import validate_request
    from evallab.queue import Executor

    root, package = _lineage_repo(tmp_path)
    toml = package / "task.toml"
    toml.write_text(toml.read_text().replace("format-code-task-900001", "format-code-task-900099"))
    spec = _built_spec(tmp_path, monkeypatch, root, package)
    assert spec.task_id == "format-code-task-900099"
    request = Executor.prepare_request(
        spec, repo_root=root, lease_path=root / "queue" / "lease", lease_generation=None
    )
    with pytest.raises(ValueError, match="no variant record|outside the ledger"):
        validate_request(request, repo_root=root)


def _fake_execute(request):  # type: ignore[no-untyped-def]
    job_dir = request.jobs_dir / request.name
    trial = job_dir / f"{request.name}__trial"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps({"verifier_result": {"rewards": {"reward": 1.0}}}), encoding="utf-8"
    )
    (job_dir / "lab-metadata.json").write_text(
        json.dumps(
            {
                "started_at": "2026-10-16T00:00:00+00:00",
                "finished_at": "2026-10-16T00:01:00+00:00",
                "provider_usage": {
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "cost_usd": 0.05,
                },
            }
        ),
        encoding="utf-8",
    )
    return job_dir


def test_sandbox_cost_uses_declared_resources(tmp_path: Path) -> None:
    task = _task(tmp_path, "shaped")
    assert task_resources(task) == (2.0, 8192.0)
    # 60 s at 2 cores + 8 GiB at Modal list prices.
    expected = 60 * (2 * 0.0000131 + 8 * 0.00000222)
    assert abs(sandbox_cost_usd(60, cpus=2.0, memory_mb=8192.0) - expected) < 1e-12


def test_run_rollout_reports_throughput_and_cost(tmp_path: Path) -> None:
    tasks = [_task(tmp_path, "task-a"), _task(tmp_path, "task-b")]
    out = tmp_path / "out"
    summary = run_rollout(
        tasks,
        model="tinker/Qwen/Qwen3.5-9B",
        concurrency=2,
        out_dir=out,
        root=tmp_path,
        execute=_fake_execute,
    )
    assert summary["completed"] == 2 and summary["errors"] == 0
    assert summary["passed"] == 2 and summary["pass_rate"] == 1.0
    assert summary["tasks_per_hour"] > 0
    per_task = 0.05 + sandbox_cost_usd(60, cpus=2.0, memory_mb=8192.0)
    assert abs(summary["cost_per_task_usd"] - round(per_task, 4)) < 1e-9
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert {row["reward"] for row in rows} == {1.0}
    assert (out / "summary.json").is_file() and (out / "spend.jsonl").is_file()


def test_one_failed_trial_does_not_kill_the_batch(tmp_path: Path) -> None:
    def flaky(request):  # type: ignore[no-untyped-def]
        if "task-b" in request.name:
            raise RuntimeError("sandbox refused")
        return _fake_execute(request)

    tasks = [_task(tmp_path, "task-a"), _task(tmp_path, "task-b")]
    summary = run_rollout(
        tasks,
        model="tinker/Qwen/Qwen3.5-9B",
        concurrency=2,
        out_dir=tmp_path / "out",
        root=tmp_path,
        execute=flaky,
    )
    assert summary["completed"] == 1 and summary["errors"] == 1
    rows = [
        json.loads(line) for line in (tmp_path / "out" / "results.jsonl").read_text().splitlines()
    ]
    assert any("sandbox refused" in (row["error"] or "") for row in rows)


def test_summarize_handles_no_completions() -> None:
    outcome = TrialOutcome(
        task="t",
        name="rollout-000-t",
        reward=None,
        passed=None,
        wall_seconds=1.0,
        sandbox_seconds=None,
        input_tokens=None,
        output_tokens=None,
        model_cost_usd=None,
        sandbox_cost_usd=None,
        job_dir="",
        error="boom",
    )
    summary = summarize([outcome], batch_wall_seconds=10.0)
    assert summary["pass_rate"] is None and summary["cost_per_task_usd"] is None
    assert summary["tasks_per_hour"] == 0.0


def test_fast_completion_is_persisted_while_first_trial_is_blocked(tmp_path: Path) -> None:
    first_started, release_first = Event(), Event()
    out = tmp_path / "out"

    def execute(request):
        if "task-a" in request.name:
            first_started.set()
            assert release_first.wait(10), "test did not release the first trial"
        else:
            assert first_started.wait(10), "first trial did not start"
        return _fake_execute(request)

    # The filesystem snapshot, not executor return order, is the consumer surface.
    from evallab import rollout

    persisted = Event()
    original_write = rollout._atomic_write

    def write(path: Path, content: str) -> None:
        original_write(path, content)
        if path.name == "results.jsonl":
            persisted.set()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(rollout, "_atomic_write", write)
        with ThreadPoolExecutor(max_workers=1) as driver:
            future = driver.submit(
                run_rollout,
                [_task(tmp_path, "task-a"), _task(tmp_path, "task-b")],
                model="tinker/Qwen/Qwen3.5-9B",
                concurrency=2,
                out_dir=out,
                root=tmp_path,
                execute=execute,
            )
            try:
                assert persisted.wait(10), "completed trial was not persisted"
                rows = [
                    json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()
                ]
                assert len(rows) == 1 and rows[0]["task"].endswith("task-b")
                assert rows[0]["passed"] is True and rows[0]["error"] is None
                assert not future.done()
            finally:
                release_first.set()
            assert future.result(timeout=10)["completed"] == 2
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert len(rows) == 2 and len({row["name"] for row in rows}) == 2


def test_failed_snapshot_replace_keeps_previous_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.rollout import _atomic_write

    path = tmp_path / "results.jsonl"
    previous = '{"name":"completed","reward":1.0}\n'
    _atomic_write(path, previous)

    def fail_replace(source: Path, target: Path) -> Path:
        # A whole new file is staged; readers still see the old complete bytes.
        assert path.read_text() == previous
        assert json.loads(source.read_text())["name"] == "next"
        raise OSError("filesystem replacement refused")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement refused"):
        _atomic_write(path, '{"name":"next","reward":0.0}\n')
    assert path.read_text() == previous
    assert set(tmp_path.iterdir()) == {path}


@pytest.mark.parametrize("error", ["trial_wall_clock_timeout", "proxy_usage_unreconciled"])
def test_dispatcher_failure_overrides_native_reward(tmp_path: Path, error: str) -> None:
    from types import SimpleNamespace

    from evallab.rollout import _outcome

    task = _task(tmp_path, "task")
    job_dir = _fake_execute(SimpleNamespace(jobs_dir=tmp_path / "jobs", name="native-pass"))
    outcome = _outcome(task, "native-pass", job_dir, 60.0, error)
    assert outcome.error == error
    assert outcome.reward is None and outcome.passed is None
    assert outcome.task == str(task) and outcome.job_dir == str(job_dir)
