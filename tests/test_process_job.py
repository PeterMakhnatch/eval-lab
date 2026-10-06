"""process-job writes per-trial and job reports for a landed job."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.process_job import process_job

MESSAGE = json.dumps({"analysis": "", "plan": "", "commands": [{"keystrokes": "echo stuck"}]})


def _step(step_id: int, message: str = MESSAGE) -> dict:
    return {
        "step_id": step_id,
        "source": "agent",
        "message": message,
        "observation": "output\n",
        "metrics": {"prompt_tokens": 1000 + step_id, "completion_tokens": 10},
    }


def _write_trial(root: Path, name: str, *, n_loop: int, reward: float, ceiling: bool) -> Path:
    trial = root / name
    agent = trial / "agent"
    agent.mkdir(parents=True)
    steps = [_step(i) for i in range(1, n_loop + 1)]
    (agent / "trajectory.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    result: dict = {
        "task_name": "task",
        "trial_name": name,
        "verifier_result": {"rewards": {"reward": reward}},
    }
    if ceiling:
        prompt = sum(step["metrics"]["prompt_tokens"] for step in steps)
        result["exception_info"] = {"exception_type": "TrialBudgetExhaustedError"}
        result["agent_result"] = {"n_input_tokens": prompt, "n_output_tokens": 120}
    (trial / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return trial


def _job(root: Path) -> Path:
    job = root / "job"
    job.mkdir()
    (job / "lab-metadata.json").write_text(
        json.dumps({"trial_budget": {"max_input_tokens": 12078, "max_output_tokens": 200000}}),
        encoding="utf-8",
    )
    _write_trial(job, "trial-loop", n_loop=12, reward=0.0, ceiling=True)
    passing = job / "trial-pass"
    passing.mkdir()
    agent = passing / "agent"
    agent.mkdir()
    steps = [_step(i, json.dumps({"commands": [{"keystrokes": f"echo {i}"}]})) for i in range(1, 4)]
    (agent / "trajectory.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    (passing / "result.json").write_text(
        json.dumps(
            {
                "task_name": "task",
                "trial_name": "trial-pass",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        ),
        encoding="utf-8",
    )
    return job


def test_process_job_writes_reports_and_flags_loops(tmp_path: Path) -> None:
    job = _job(tmp_path)
    out = tmp_path / "out"
    report = process_job(job, output_dir=out, ingest=False, publish=False)

    assert report["summary"]["n_trials"] == 2
    assert report["summary"]["n_pass"] == 1
    assert report["summary"]["n_fail"] == 1
    rows = {row["trial_name"]: row for row in report["trials"]}
    loop_flags = rows["trial-loop"]["flags"]
    assert rows["trial-loop"]["reward"] == 0.0
    assert rows["trial-loop"]["stop_reason"] == "ceiling:input_tokens"
    assert rows["trial-loop"]["tokens_used"] == 12 * 10 + sum(1000 + i for i in range(1, 13))
    assert rows["trial-loop"]["cost"] is None
    assert any(flag.startswith("token_ceiling:") for flag in loop_flags)
    assert any(flag.startswith("identical_loop:12x") for flag in loop_flags)
    assert rows["trial-pass"]["reward"] == 1.0
    # A missing ledger is None with a reason, never 0.
    assert report["summary"]["cost_usd"] is None
    assert report["summary"]["cost_source"] == "proxy_ledger_x_pinned_price"
    assert "ledger" in (report["summary"]["cost_reason"] or "")
    for name in ("trial-trial-loop", "trial-trial-pass", "job"):
        assert (out / f"{name}.json").is_file()
        assert (out / f"{name}.md").is_file()
    assert report["summary"]["ingest"] == "ingest disabled by caller"


def test_process_job_maps_diagnosis_modes(tmp_path: Path, monkeypatch) -> None:
    from evallab import trial_diagnosis
    from evallab.process_job import _process_trial

    job = _job(tmp_path)
    real = trial_diagnosis.diagnose_trial

    def fake(trial_dir):
        diagnosis = real(trial_dir)
        mode = trial_diagnosis.FailureMode(
            mode="silent_tool_output",
            step_ids=(1, 2),
            excerpt="no output",
            suggestion="look",
        )
        object.__setattr__(diagnosis, "modes", (mode,))
        return diagnosis

    monkeypatch.setattr(trial_diagnosis, "diagnose_trial", fake)
    record = _process_trial(job / "trial-pass", job)
    assert record["diagnosis"]["modes"] == [
        {"mode": "silent_tool_output", "step_ids": ["1", "2"], "excerpt": "no output"}
    ]
    assert "diagnosis:silent_tool_output" in record["flags"]


def test_shared_detector_catches_answer_leak_shapes() -> None:
    from evallab.upstream_fetch import detect_upstream_fetch

    # Ground truth shapes from the HAR-104 batch, via the canonical guard.
    findings = detect_upstream_fetch(
        [
            (4, "cd /testbed && pip download waitress==2.0.0 --no-deps -d /tmp/wtr"),
            (10, "timeout 10 curl -sL https://raw.x/y | tail -1"),
        ]
    )
    assert [(finding.step_index, finding.kind) for finding in findings] == [
        (4, "pip-download-remote-package"),
        (10, "curl-remote-url"),
    ]
    assert detect_upstream_fetch([(2, "pip show soupsieve"), (3, "ls /tmp")]) == []


def test_process_job_decision_renders_counts_single_path(tmp_path: Path) -> None:
    """The page is built once, after counts, with the real counts field."""
    from evallab.process_job import _job_task_identity, _process_trial

    job = _job(tmp_path)
    assert _job_task_identity(job) == {
        "task_id": None,
        "task_package_digest": None,
    }
    out = tmp_path / "out"
    report = process_job(job, output_dir=out, ingest=False, publish=False)

    assert report["summary"]["n_trials"] == 2
    for trial_path in (job / "trial-loop", job / "trial-pass"):
        name = trial_path.name
        saved = json.loads((out / f"trial-{name}.json").read_text(encoding="utf-8"))
        counts, decision = saved["counts"], saved["decision"]
        # One path: the stored page carries the attached counts verdict,
        # never a provisional page built without it.
        assert decision["schema"] == "trial_decision/v3"
        assert decision["counts"]["verdict"] == counts["verdict"]
        assert decision["counts"]["reasons"] == counts["reasons"]
        assert saved["task_package_digest"] is None
    loop = json.loads((out / "trial-trial-loop.json").read_text(encoding="utf-8"))
    assert loop["counts"]["verdict"] == "counted_fail"
    assert loop["decision"]["counts"]["verdict"] == "counted_fail"
    assert loop["decision"]["counts"]["task_ledger"]["status"] == "unknown"
    passing = json.loads((out / "trial-trial-pass.json").read_text(encoding="utf-8"))
    assert passing["counts"]["verdict"] == "counted_pass"
    assert passing["decision"]["counts"]["verdict"] == "counted_pass"

    # Direct _process_trial use leaves the page for the post-counts attach.
    record = _process_trial(job / "trial-pass", job)
    assert record["decision"] is None


def _har116_usage_fixture() -> dict:
    path = Path(__file__).parent / "fixtures/process_job/har116-001181-token-ledger.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_har116_proxy_tokens_include_usage_missing_from_native_totals(tmp_path: Path) -> None:
    fixture = _har116_usage_fixture()
    job = tmp_path / fixture["source"]["job"]
    job.mkdir()
    trial = _write_trial(job, fixture["source"]["trial"], n_loop=1, reward=0.0, ceiling=True)
    result_path = trial / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["agent_result"] = fixture["agent_result"]
    result_path.write_text(json.dumps(result), encoding="utf-8")
    (job / "lab-metadata.json").write_text(
        json.dumps({"provider_usage": fixture["provider_usage"]}), encoding="utf-8"
    )

    out = tmp_path / "out"
    report = process_job(job, output_dir=out, ingest=False, publish=False)
    saved = json.loads((out / f"trial-{trial.name}.json").read_text(encoding="utf-8"))
    assert saved["tokens_native"]["input_tokens"] == 2_410_295
    assert saved["tokens_native"]["output_tokens"] == 7_444
    assert saved["tokens_proxy"]["input_tokens"] == 2_423_707
    assert saved["tokens_proxy"]["output_tokens"] == 11_540
    assert saved["tokens_proxy"]["total_tokens"] == 2_435_247
    assert saved["tokens_proxy"]["source"] == "proxy_settled_ledger"
    assert saved["tokens_proxy"]["scope"] == "job"
    assert saved["tokens_proxy"]["attribution"] == "single_trial"
    assert saved["decision"]["facts"]["tokens"]["input_tokens"] == 2_423_707
    assert saved["decision"]["facts"]["tokens"]["output_tokens"] == 11_540
    assert saved["tokens_attempted_proxy"] == 2_435_247
    assert report["ledger"]["totals"]["used"]["requests"] == 89


def test_job_ledger_does_not_invent_per_trial_token_allocations(tmp_path: Path) -> None:
    job = _job(tmp_path)
    (job / "lab-metadata.json").write_text(
        json.dumps({"provider_usage": _har116_usage_fixture()["provider_usage"]}),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    report = process_job(job, output_dir=out, ingest=False, publish=False)
    assert report["ledger"]["totals"]["used"]["input_tokens"] == 2_423_707
    assert report["ledger"]["totals"]["used"]["output_tokens"] == 11_540
    for name in ("trial-loop", "trial-pass"):
        saved = json.loads((out / f"trial-{name}.json").read_text(encoding="utf-8"))
        assert saved["tokens_proxy"]["input_tokens"] is None
        assert saved["tokens_proxy"]["output_tokens"] is None
        assert saved["tokens_proxy"]["attribution"] == "unavailable"
        assert saved["tokens_attempted_proxy"] is None
        assert saved["decision"]["facts"]["tokens"]["input_tokens"] is None


@pytest.mark.parametrize("provider_usage", [None, {}])
def test_unavailable_ledger_never_falls_back_to_native_tokens(
    tmp_path: Path, provider_usage: dict | None
) -> None:
    job = tmp_path / "job"
    job.mkdir()
    trial = _write_trial(job, "trial-native-only", n_loop=1, reward=0.0, ceiling=True)
    (job / "lab-metadata.json").write_text(
        json.dumps({"provider_usage": provider_usage}), encoding="utf-8"
    )
    out = tmp_path / "out"
    process_job(job, output_dir=out, ingest=False, publish=False)
    saved = json.loads((out / f"trial-{trial.name}.json").read_text(encoding="utf-8"))
    assert saved["tokens_native"]["input_tokens"] == 1001
    assert saved["tokens_proxy"]["input_tokens"] is None
    assert saved["tokens_proxy"]["output_tokens"] is None
    assert saved["tokens_attempted_proxy"] is None
    assert saved["decision"]["facts"]["tokens"]["input_tokens"] is None


def _write_stop_trial(
    job: Path,
    name: str,
    *,
    stop: str | None = None,
    exception: str | None = None,
    completion: bool = False,
    reward: float = 0.0,
) -> Path:
    trial = _write_trial(job, name, n_loop=1, reward=reward, ceiling=False)
    result = json.loads((trial / "result.json").read_text(encoding="utf-8"))
    result["agent_result"] = {"metadata": {"stop_reason": stop} if stop else {}}
    if exception:
        result["exception_info"] = {"exception_type": exception}
    (trial / "result.json").write_text(json.dumps(result), encoding="utf-8")
    if completion:
        step = _step(1, json.dumps({"commands": [], "task_complete": True}))
        step["extra"] = {
            "step_layers": {
                "schema": "evallab.step_layers/v1",
                "provenance": "recorded",
                "accepted": {"kind": "calls", "calls": [], "task_complete": True},
            }
        }
        (trial / "agent" / "trajectory.json").write_text(
            json.dumps({"steps": [step]}), encoding="utf-8"
        )
    return trial


@pytest.mark.parametrize(
    ("stop", "exception", "completion", "reward", "expected_reason", "category"),
    [
        ("ceiling:requests", None, False, 0.0, "ceiling:requests", "our_limit"),
        ("loop_break", None, False, 0.0, "loop_break", "our_limit"),
        ("harness_step_limit", None, False, 0.0, "harness_step_limit", "harness_step_limit"),
        (None, "AgentTimeoutError", False, 1.0, "agent_timeout", "task_timeout"),
        (None, None, True, 0.0, "task_complete_confirmed", "model_end"),
        (None, None, False, 0.0, "unknown", "unknown"),
    ],
)
def test_process_job_separates_stop_category_from_counts(
    tmp_path: Path,
    stop: str | None,
    exception: str | None,
    completion: bool,
    reward: float,
    expected_reason: str,
    category: str,
) -> None:
    job = tmp_path / "job"
    _write_stop_trial(
        job, "trial", stop=stop, exception=exception, completion=completion, reward=reward
    )
    out = tmp_path / "processed"

    report = process_job(job, output_dir=out, ingest=False, publish=False)
    saved = json.loads((out / "trial-trial.json").read_text(encoding="utf-8"))
    markdown = (out / "trial-trial.md").read_text(encoding="utf-8")

    assert saved["stop_reason"] == expected_reason
    assert saved["stop_category"] == category
    assert saved["decision"]["facts"]["stop_category"] == category
    assert saved["counts"]["verdict"] == ("counted_pass" if reward == 1.0 else "counted_fail")
    assert report["summary"]["n_pass"] == int(reward == 1.0)
    assert report["trials"][0]["stop_category"] == category
    assert f"stop category: `{category}`" in markdown
    assert "job limit-hit share:" in markdown


@pytest.mark.parametrize(("trials", "flagged"), [(20, False), (19, True)])
def test_process_job_limit_share_counts_all_trials_at_strict_boundary(
    tmp_path: Path, trials: int, flagged: bool
) -> None:
    job = tmp_path / "job"
    for index in range(trials):
        _write_stop_trial(
            job,
            f"trial-{index:02}",
            stop="loop_break" if index == 0 else "harness_step_limit",
        )
    out = tmp_path / "processed"

    report = process_job(job, output_dir=out, ingest=False, publish=False)
    limits = report["summary"]["limit_hit_summary"]
    saved = json.loads((out / "trial-trial-00.json").read_text(encoding="utf-8"))
    run_markdown = (out / "trial-trial-00.md").read_text(encoding="utf-8")
    job_markdown = (out / "job.md").read_text(encoding="utf-8")

    assert limits == {
        "trials": trials,
        "limit_hit_trials": 1,
        "limit_hit_share": 1 / trials,
        "setup_limited": flagged,
    }
    assert saved["job_limit_hit_summary"] == limits
    assert f"setup-limited: `{str(flagged).lower()}`" in run_markdown
    assert f"setup-limited: `{str(flagged).lower()}`" in job_markdown
    assert ("flagged: over 5%" in run_markdown) is flagged
    assert ("flagged: over 5%" in job_markdown) is flagged


@pytest.mark.parametrize(
    ("status", "detail", "completion", "reason", "category"),
    [
        ("LimitsExceeded", None, True, "harness_step_limit", "harness_step_limit"),
        ("LimitsExceeded", "Empty assistant response", True, "error", "error"),
        ("Idle", None, False, "task_complete", "model_end"),
    ],
)
def test_process_job_native_exit_takes_precedence_over_an_earlier_completion(
    tmp_path: Path,
    status: str,
    detail: str | None,
    completion: bool,
    reason: str,
    category: str,
) -> None:
    job = tmp_path / "job"
    trial = _write_stop_trial(job, "trial", completion=completion)
    result = json.loads((trial / "result.json").read_text(encoding="utf-8"))
    result["agent_result"]["metadata"] = {
        "native_exit_status": status,
        "native_exit_result": detail,
    }
    (trial / "result.json").write_text(json.dumps(result), encoding="utf-8")
    out = tmp_path / "processed"

    process_job(job, output_dir=out, ingest=False, publish=False)
    saved = json.loads((out / "trial-trial.json").read_text(encoding="utf-8"))

    assert saved["stop_reason"] == reason
    assert saved["stop_category"] == category
    assert saved["decision"]["facts"]["stop_category"] == category
    assert saved["counts"]["verdict"] == "counted_fail"


def test_local_projection_retains_native_identity_and_reward_dimensions(tmp_path: Path) -> None:
    import pyarrow.parquet as pq

    from evallab.step_layers import stop_category

    job = _job(tmp_path)
    (job / "result.json").write_text(json.dumps({
        "id": "viewer-aggregate",
        "stats": {},
        "n_total_trials": 2,
        "finished_at": "2026-10-06T12:00:00Z",
    }))
    for name in ("trial-loop", "trial-pass"):
        result_path = job / name / "result.json"
        result = json.loads(result_path.read_text())
        result.update({
            "id": name,
            "config": {"job_id": f"native-{name}"},
            "agent_info": {"name": "mimoagent"},
        })
        result["verifier_result"]["rewards"].update({"integrity": 0, "reward_gated": 0.0})
        if name == "trial-pass":
            result["agent_result"] = {"metadata": {"native_exit_status": "Idle"}}
        result_path.write_text(json.dumps(result))
    parquet_root = tmp_path / "derived/parquet"

    process_job(
        job, root=tmp_path, output_dir=tmp_path / "reports",
        ingest=False, publish=False, parquet_root=parquet_root,
    )

    for name, reward, category in (
        ("trial-loop", 0.0, "our_limit"),
        ("trial-pass", 1.0, "model_end"),
    ):
        partition = parquet_root / f"job_id=native-{name}" / f"trial_id={name}"
        fact = pq.ParquetFile(partition / "trial_facts.parquet").read().to_pylist()[0]
        rewards = pq.ParquetFile(partition / "reward_facts.parquet").read().to_pylist()
        feature = pq.ParquetFile(partition / "features.parquet").read().to_pylist()[0]
        assert (fact["job_id"], fact["trial_id"], fact["primary_reward"]) == (
            f"native-{name}", name, reward,
        )
        assert {row["reward_name"]: row["reward_value"] for row in rewards} == {
            "reward": reward, "integrity": 0.0, "reward_gated": 0.0,
        }
        assert stop_category(feature["stop_reason"]) == category
    assert not (parquet_root / "job_id=viewer-aggregate").exists()
