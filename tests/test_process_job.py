"""process-job writes per-trial and job reports for a landed job."""

from __future__ import annotations

import json
from pathlib import Path

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


def test_attach_trial_counts_forwards_identity_when_supported(monkeypatch) -> None:
    """The canonical task identity reaches counts once counts accepts it."""
    from evallab import counts as counts_module
    from evallab import process_job as process_job_module

    seen: dict = {}

    def fake_attach(record, result, *, label_root, task_package_digest=None, task_id=None):
        seen["digest"] = task_package_digest
        seen["task_id"] = task_id
        return {"schema": "evallab.counts/v1", "verdict": "counted_fail", "reasons": []}

    monkeypatch.setattr(counts_module, "attach_counts", fake_attach)
    monkeypatch.setattr(process_job_module, "_COUNTS_KWARGS_CACHE", {})
    identity = {"task_id": "task001618", "task_package_digest": "sha256:abc"}
    out = process_job_module._attach_trial_counts(
        {}, {}, label_root=None, task_identity=identity
    )
    assert out["verdict"] == "counted_fail"
    assert seen == {"digest": "sha256:abc", "task_id": "task001618"}
