"""Focused posture boundaries for the HAR-178 trials view (trial_posture).

Isolated fixtures: the real reference profile is copied into a tmp
checkout (never the host checkout), fingerprints are recorded-shape
dicts, and job/trial trees are built per test. Nothing here runs models,
networks, or the database.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from evallab.execution_contracts import (
    MIMO_AGENT_IMPORT_PATH,
    MIMO_SELFHOSTED_NATIVE_MODEL,
)
from evallab.interpretation.trial_posture import (
    cut_short_by_our_limits,
    trial_posture,
)
from evallab.setup_fingerprint import FINGERPRINT_SCHEMA, resolve_repo_root

PROFILE_NAME = "xiaomi-mimo-rl"
NATIVE_MODEL = MIMO_SELFHOSTED_NATIVE_MODEL
ADAPTER_MODEL = f"{MIMO_SELFHOSTED_NATIVE_MODEL}:har129"
SELFHOSTED_SELECTOR = f"selfhosted/{MIMO_SELFHOSTED_NATIVE_MODEL}"
SELFHOSTED_ADAPTER_SELECTOR = f"selfhosted/{ADAPTER_MODEL}"


def _repo_root(base: Path) -> Path:
    root = base / "fixture-root"
    profiles = root / "research" / "setup-profiles"
    profiles.mkdir(parents=True)
    shutil.copy(
        resolve_repo_root(None, Path(__file__)) / "research" / "setup-profiles" / f"{PROFILE_NAME}.yaml",
        profiles / f"{PROFILE_NAME}.yaml",
    )
    return root


def _matching_fingerprint(
    *,
    reference_profile: str | None = PROFILE_NAME,
    lock_mode: str = "locked",
) -> dict:
    """Recorded-shape fingerprint that exactly matches xiaomi-mimo-rl."""
    return {
        "schema": FINGERPRINT_SCHEMA,
        "reference_profile": reference_profile,
        "harness": {"id": "mimoagent-default", "additions": {}},
        "server": {
            "tool_call_parser": "qwen3_coder",
            "reasoning_parser": "fixture-parser",
            "context_length": 262144,
        },
        "sampling": {"temperature": 1.0, "top_p": 0.95, "top_k": 20},
        "lock": {"mode": lock_mode},
        "budgets": {
            "step_limit": 500,
            "max_requests": None,
            "max_input_tokens": None,
            "max_output_tokens": None,
            "max_total_tokens": None,
            "cost_limit_usd": None,
        },
    }


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _result(
    trial_name: str,
    *,
    reward: float | None = 1.0,
    started_at: str | None = "2026-10-02T07:00:57Z",
    finished: bool = True,
    exception: str | None = None,
    agent_result: dict | None = None,
    agent_info: dict | None = None,
    landed_agent: dict | None = None,
) -> dict:
    record: dict = {
        "trial_name": trial_name,
        "task_name": "fixture-task",
        "started_at": started_at,
        "finished_at": "2026-10-02T07:02:41Z" if finished else None,
        "exception_info": (
            {"exception_type": exception, "exception_message": "boom"}
            if exception is not None
            else None
        ),
        "agent_result": agent_result if agent_result is not None else {},
        "verifier_result": {"rewards": {}} if reward is None else {"rewards": {"reward": reward}},
        "config": (
            {"job_id": "native-job-id", "agent": landed_agent}
            if landed_agent is not None
            else {"job_id": "native-job-id"}
        ),
    }
    if agent_info is not None:
        record["agent_info"] = agent_info
    return record


def _config(*, model: str | None = NATIVE_MODEL, agent: dict | None = None) -> dict:
    entry = dict(agent) if agent is not None else {"import_path": MIMO_AGENT_IMPORT_PATH}
    if model is not None:
        entry["model_name"] = model
    return {"agent": entry, "job_id": "native-job-id", "trial_name": "t"}


def _job(
    base: Path,
    name: str = "job-har168-fixture",
    *,
    spec: dict | None = None,
    metadata: dict | None = None,
    job_result: dict | None = None,
    job_fp: dict | None = None,
) -> Path:
    job = base / name
    job.mkdir(parents=True, exist_ok=True)
    _write(job / "experiment-spec.json", spec if spec is not None else {})
    _write(job / "lab-metadata.json", metadata if metadata is not None else {})
    _write(
        job / "result.json",
        job_result if job_result is not None else {"started_at": "2026-10-01T00:00:00Z"},
    )
    if job_fp is not None:
        _write(job / "setup-fingerprint.json", job_fp)
    return job


def _trial(
    job: Path,
    name: str,
    *,
    result: dict | None = None,
    config: dict | None = None,
    trial_fp: dict | None = None,
    egress: dict | None = None,
    laminar: dict | None = None,
) -> tuple[Path, dict]:
    trial = job / name
    trial.mkdir(parents=True, exist_ok=True)
    record = result if result is not None else _result(name)
    _write(trial / "config.json", config if config is not None else _config())
    if trial_fp is not None:
        _write(trial / "setup-fingerprint.json", trial_fp)
    if egress is not None:
        _write(trial / "egress-lock.json", egress)
    if laminar is not None:
        _write(trial / "laminar-trace.json", laminar)
    return trial, record


def _posture(repo_root: Path, job: Path, trial: Path, record: dict) -> dict:
    return trial_posture(
        repo_root=repo_root, job_dir=job, trial_dir=trial, result=record
    )


def test_exact_match_reports_no_diffs(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    job = _job(tmp_path, spec={"reference_profile": PROFILE_NAME})
    trial, record = _trial(
        job, "t1", trial_fp=_matching_fingerprint(), egress={"applied": True}
    )
    row = _posture(root, job, trial, record)
    assert row["reference_profile"] == PROFILE_NAME
    assert row["reference_profile_match"] is True
    assert json.loads(row["reference_profile_diffs"]) == []
    assert row["egress_lock"] is True


def test_single_difference_fails_closed(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    job = _job(tmp_path, spec={"reference_profile": PROFILE_NAME})
    fp = _matching_fingerprint()
    fp["server"]["context_length"] = 65536
    trial, record = _trial(
        job, "t1", trial_fp=fp, egress={"applied": True}
    )
    row = _posture(root, job, trial, record)
    assert row["reference_profile_match"] is False
    diffs = json.loads(row["reference_profile_diffs"])
    assert [d["field"] for d in diffs] == ["server.context_length"]
    assert diffs[0]["expected"] == 262144
    assert diffs[0]["actual"] == 65536


def test_covered_deviation_still_mismatch(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    job = _job(
        tmp_path,
        spec={
            "reference_profile": PROFILE_NAME,
            "deviations": [
                {"field": "server.context_length", "value": 65536, "reason": "64K served window"}
            ],
        },
    )
    fp = _matching_fingerprint()
    fp["server"]["context_length"] = 65536
    fp["deviations"] = [
        {"field": "server.context_length", "value": 65536, "reason": "64K served window"}
    ]
    trial, record = _trial(job, "t1", trial_fp=fp, egress={"applied": True})
    row = _posture(root, job, trial, record)
    assert row["reference_profile_match"] is False
    assert json.loads(row["reference_profile_diffs"]) != []


def test_unknown_sourced_observation_is_not_a_match(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    job = _job(tmp_path, spec={"reference_profile": PROFILE_NAME})
    fp = _matching_fingerprint()
    fp["sampling"]["temperature"] = None
    trial, record = _trial(job, "t1", trial_fp=fp, egress={"applied": True})
    assert _posture(root, job, trial, record)["reference_profile_match"] is False

    fp = _matching_fingerprint()
    del fp["budgets"]["max_requests"]
    trial, record = _trial(job, "t2", trial_fp=fp, egress={"applied": True})
    assert _posture(root, job, trial, record)["reference_profile_match"] is False


def test_explicitly_unbounded_ceilings_do_not_disqualify(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    job = _job(tmp_path, spec={"reference_profile": PROFILE_NAME})
    trial, record = _trial(
        job, "t1", trial_fp=_matching_fingerprint(), egress={"applied": True}
    )
    assert _posture(root, job, trial, record)["reference_profile_match"] is True


def test_unsourced_reference_fields_do_not_disqualify(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    job = _job(tmp_path, spec={"reference_profile": PROFILE_NAME})
    fp = _matching_fingerprint()
    fp["server"]["reasoning_parser"] = None
    trial, record = _trial(job, "t1", trial_fp=fp, egress={"applied": True})
    assert _posture(root, job, trial, record)["reference_profile_match"] is True


def test_missing_lock_is_unlocked_while_intent_is_not_evidence(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    job = _job(tmp_path, spec={"reference_profile": PROFILE_NAME})
    trial, record = _trial(job, "t1", trial_fp=_matching_fingerprint(lock_mode="open"))
    row = _posture(root, job, trial, record)
    assert row["egress_lock"] is False
    assert row["reference_profile_match"] is False

    trial, record = _trial(
        job, "t2", trial_fp=_matching_fingerprint(), egress={"requested": True}
    )
    row = _posture(root, job, trial, record)
    assert row["egress_lock"] is False

    trial, record = _trial(
        job, "t3", trial_fp=_matching_fingerprint(), egress={"requested": True, "applied": False}
    )
    assert _posture(root, job, trial, record)["egress_lock"] is False


def test_job_fingerprint_fallback_applies_observed_lock(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    intended = _matching_fingerprint(lock_mode="open")
    job = _job(tmp_path, spec={"reference_profile": PROFILE_NAME}, job_fp=intended)
    trial, record = _trial(job, "t1", egress={"applied": True})
    row = _posture(root, job, trial, record)
    assert row["reference_profile_match"] is True
    assert row["egress_lock"] is True

    trial, record = _trial(job, "t2")
    row = _posture(root, job, trial, record)
    assert row["reference_profile_match"] is False
    assert row["egress_lock"] is False


def test_lock_covered_env_fingerprint_covers_old_records(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    payload = _matching_fingerprint(lock_mode="open")
    job = _job(
        tmp_path,
        spec={"reference_profile": PROFILE_NAME},
        metadata={"command": ["harbor", "run", "--agent-env", f"EVALLAB_SETUP_FINGERPRINT={json.dumps(payload)}"]},
    )
    trial, record = _trial(job, "t1", egress={"applied": True})
    assert _posture(root, job, trial, record)["reference_profile_match"] is True

    job = _job(
        tmp_path,
        "job-bad-env",
        spec={"reference_profile": PROFILE_NAME},
        metadata={"command": ["harbor", "run", "--agent-env", "EVALLAB_SETUP_FINGERPRINT={nope"]},
    )
    trial, record = _trial(job, "t1", egress={"applied": True})
    row = _posture(root, job, trial, record)
    assert row["reference_profile_match"] is False


def test_recorded_agent_env_fingerprint_beats_command(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    payload = _matching_fingerprint(lock_mode="open")
    job = _job(tmp_path, spec={"reference_profile": PROFILE_NAME})
    record = _result(
        "t1",
        landed_agent={
            "model_name": NATIVE_MODEL,
            "env": {"EVALLAB_SETUP_FINGERPRINT": json.dumps(payload)},
        },
    )
    trial, _ = _trial(job, "t1", result=record, egress={"applied": True})
    assert _posture(root, job, trial, record)["reference_profile_match"] is True


def test_present_invalid_trial_fingerprint_never_uses_intention(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    intended = _matching_fingerprint(lock_mode="open")
    job = _job(tmp_path, spec={"reference_profile": PROFILE_NAME}, job_fp=intended)
    trial, record = _trial(job, "t1", egress={"applied": True})
    (trial / "setup-fingerprint.json").write_text("{corrupt", encoding="utf-8")
    row = _posture(root, job, trial, record)
    assert row["reference_profile"] == PROFILE_NAME
    assert row["reference_profile_match"] is False

    _write(
        trial / "setup-fingerprint.json",
        {"schema": "evallab.setup_fingerprint/v0", "reference_profile": PROFILE_NAME},
    )
    assert _posture(root, job, trial, record)["reference_profile_match"] is False


def test_profile_name_with_path_components_rejected(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    job = _job(tmp_path, spec={"reference_profile": "../evil"})
    trial, record = _trial(
        job, "t1", trial_fp=_matching_fingerprint(reference_profile="../evil"),
        egress={"applied": True},
    )
    row = _posture(root, job, trial, record)
    assert row["reference_profile"] is None
    assert row["reference_profile_match"] is False

    job = _job(tmp_path, "job-subdir", spec={"reference_profile": "sub/dir"})
    trial, record = _trial(job, "t1", egress={"applied": True})
    row = _posture(root, job, trial, record)
    assert row["reference_profile"] is None
    assert row["reference_profile_match"] is False


def test_reference_name_must_agree(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    job = _job(tmp_path, spec={"reference_profile": "other-profile"})
    trial, record = _trial(
        job, "t1", trial_fp=_matching_fingerprint(), egress={"applied": True}
    )
    row = _posture(root, job, trial, record)
    assert row["reference_profile"] is None
    assert row["reference_profile_match"] is False

    job = _job(tmp_path, "job-noname", spec={})
    trial, record = _trial(job, "t1", egress={"applied": True})
    row = _posture(root, job, trial, record)
    assert row["reference_profile"] is None
    assert row["reference_profile_match"] is None

    job = _job(tmp_path, "job-unknown", spec={"reference_profile": "no-such-profile"})
    trial, record = _trial(
        job, "t1", trial_fp=_matching_fingerprint(reference_profile="no-such-profile"),
        egress={"applied": True},
    )
    row = _posture(root, job, trial, record)
    assert row["reference_profile"] == "no-such-profile"
    assert row["reference_profile_match"] is False


def test_model_normalization(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    job = _job(tmp_path)
    trial, record = _trial(job, "t1", config=_config(model=NATIVE_MODEL))
    assert _posture(root, job, trial, record)["model"] == NATIVE_MODEL

    trial, record = _trial(job, "t2", config=_config(model=SELFHOSTED_SELECTOR))
    assert _posture(root, job, trial, record)["model"] == NATIVE_MODEL

    trial, record = _trial(job, "t3", config=_config(model=SELFHOSTED_ADAPTER_SELECTOR))
    row = _posture(root, job, trial, record)
    assert row["model"] == ADAPTER_MODEL
    assert row["model"] != NATIVE_MODEL

    trial, record = _trial(job, "t4", config=_config(model=ADAPTER_MODEL))
    assert _posture(root, job, trial, record)["model"] == ADAPTER_MODEL

    trial, record = _trial(job, "t5", config=_config(model="gpt-5.6-terra"))
    assert _posture(root, job, trial, record)["model"] == "gpt-5.6-terra"

    trial, record = _trial(job, "t6", config=_config(model="selfhosted/bogus-model"))
    assert _posture(root, job, trial, record)["model"] is None

    trial, record = _trial(job, "t7", config=_config(model=None))
    assert _posture(root, job, trial, record)["model"] is None


def test_model_prefers_observed_then_landed_then_file(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    job = _job(tmp_path)
    record = _result(
        "t1",
        agent_info={"model_info": {"name": "observed-model"}},
        landed_agent={"model_name": NATIVE_MODEL},
    )
    trial, _ = _trial(job, "t1", result=record, config=_config(model=NATIVE_MODEL))
    assert _posture(root, job, trial, record)["model"] == "observed-model"

    record = _result(
        "t2",
        agent_info={"model_info": {"model_name": "observed-model-name"}},
        landed_agent={"model_name": NATIVE_MODEL},
    )
    trial, _ = _trial(job, "t2", result=record, config=_config(model=NATIVE_MODEL))
    assert _posture(root, job, trial, record)["model"] == "observed-model-name"

    record = _result("t3", landed_agent={"model_name": "landed-model"})
    trial, _ = _trial(job, "t3", result=record, config=_config(model="file-model"))
    assert _posture(root, job, trial, record)["model"] == "landed-model"

    record = _result("t4", landed_agent={"model_name": SELFHOSTED_SELECTOR})
    trial, _ = _trial(job, "t4", result=record, config=_config(model="file-model"))
    assert _posture(root, job, trial, record)["model"] == NATIVE_MODEL


def test_harness_identity(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    job = _job(tmp_path)
    trial, record = _trial(
        job, "t1", config=_config(agent={"import_path": MIMO_AGENT_IMPORT_PATH})
    )
    assert _posture(root, job, trial, record)["harness"] == MIMO_AGENT_IMPORT_PATH

    trial, record = _trial(job, "t2", config=_config(agent={"name": "mimoagent"}))
    assert _posture(root, job, trial, record)["harness"] == MIMO_AGENT_IMPORT_PATH

    trial, record = _trial(
        job, "t3", config=_config(agent={"name": "custom-mimoagent-wrapper"})
    )
    assert _posture(root, job, trial, record)["harness"] == "custom-mimoagent-wrapper"

    trial, record = _trial(job, "t4", config=_config(agent={"name": "codex"}))
    assert _posture(root, job, trial, record)["harness"] == "codex"

    record = _result("t5")
    record["agent_info"] = {"name": "mimoagent"}
    trial, _ = _trial(job, "t5", result=record, config={"agent": {}})
    assert _posture(root, job, trial, record)["harness"] == MIMO_AGENT_IMPORT_PATH

    trial, record = _trial(job, "t6", config={"agent": {}})
    assert _posture(root, job, trial, record)["harness"] is None


def test_harness_prefers_landed_record_per_record(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    job = _job(tmp_path)
    record = _result("t1", landed_agent={"name": "mimoagent"})
    trial, _ = _trial(
        job, "t1", result=record, config=_config(agent={"import_path": "stale.wrapper:Agent"})
    )
    assert _posture(root, job, trial, record)["harness"] == MIMO_AGENT_IMPORT_PATH

    record = _result("t2", landed_agent={"import_path": "custom:Agent"})
    trial, _ = _trial(
        job, "t2", result=record, config=_config(agent={"name": "mimoagent"})
    )
    assert _posture(root, job, trial, record)["harness"] == "custom:Agent"


def test_infra_uses_counts_reason_only(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    job = _job(tmp_path)
    trial, record = _trial(job, "t1", result=_result("t1", reward=None, exception="RuntimeError"))
    row = _posture(root, job, trial, record)
    assert row["infra"] is True
    assert row["infra_exception"] == "RuntimeError"

    trial, record = _trial(job, "t2", result=_result("t2", reward=1.0))
    row = _posture(root, job, trial, record)
    assert row["infra"] is False
    assert row["infra_exception"] is None

    trial, record = _trial(job, "t3", result=_result("t3", reward=0.0))
    assert _posture(root, job, trial, record)["infra"] is False

    trial, record = _trial(
        job, "t4", result=_result("t4", reward=1.0, exception="AgentTimeoutError")
    )
    row = _posture(root, job, trial, record)
    assert row["infra"] is False
    assert row["infra_exception"] == "AgentTimeoutError"

    trial, record = _trial(job, "t5", result={})
    row = _posture(root, job, trial, record)
    assert row["infra"] is True
    assert row["infra_exception"] is None


def test_cut_short_by_our_limits() -> None:
    assert cut_short_by_our_limits(None) is None
    assert cut_short_by_our_limits("unknown") is None
    assert cut_short_by_our_limits("trial_budget_exhausted") is True
    assert cut_short_by_our_limits("loop_break") is True
    assert cut_short_by_our_limits("ceiling:input_tokens") is True
    assert cut_short_by_our_limits("ceiling:requests") is True
    assert cut_short_by_our_limits("harness_step_limit") is False
    assert cut_short_by_our_limits("agent_timeout") is False
    assert cut_short_by_our_limits("task_complete") is False
    assert cut_short_by_our_limits("prose_completion") is False
    assert cut_short_by_our_limits("error") is False


def test_campaign_attribution(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    job = _job(
        tmp_path,
        spec={"linear_card": "har168", "reference_profile": PROFILE_NAME},
        metadata={"experiment": {"linear_card": "HAR-168"}},
    )
    trial, record = _trial(job, "t1")
    assert _posture(root, job, trial, record)["campaign"] == "HAR-168"

    job = _job(
        tmp_path,
        "job-disagree",
        spec={"linear_card": "HAR-168"},
        metadata={"experiment": {"linear_card": "HAR-169"}},
    )
    trial, record = _trial(job, "t1")
    assert _posture(root, job, trial, record)["campaign"] is None

    job = _job(
        tmp_path,
        "job-malformed",
        spec={"linear_card": "not-a-card"},
        metadata={},
    )
    trial, record = _trial(job, "t1")
    assert _posture(root, job, trial, record)["campaign"] is None

    job = _job(tmp_path, "plain-job-har170-run", spec={}, metadata={})
    trial, record = _trial(job, "t1")
    assert _posture(root, job, trial, record)["campaign"] == "HAR-170"

    job = _job(
        tmp_path,
        "other-har171-run",
        spec={"linear_card": "HAR-172"},
        metadata={},
    )
    trial, record = _trial(job, "t1")
    assert _posture(root, job, trial, record)["campaign"] is None

    job = _job(tmp_path, "plain-job-no-card", spec={}, metadata={})
    trial, record = _trial(job, "t1")
    assert _posture(root, job, trial, record)["campaign"] is None


def test_recorded_dates_prefer_trial(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    job = _job(
        tmp_path, job_result={"started_at": "2026-10-01T00:00:00Z"}
    )
    trial, record = _trial(job, "t1", result=_result("t1", started_at="2026-10-02T07:00:57Z"))
    assert _posture(root, job, trial, record)["date"] == "2026-10-02T07:00:57Z"

    trial, record = _trial(job, "t2", result=_result("t2", started_at=None))
    assert _posture(root, job, trial, record)["date"] == "2026-10-01T00:00:00Z"

    trial, record = _trial(job, "t3", result=_result("t3", started_at="not-a-timestamp"))
    assert _posture(root, job, trial, record)["date"] == "2026-10-01T00:00:00Z"

    job = _job(tmp_path, "job-nodate", job_result={})
    trial, record = _trial(job, "t4", result=_result("t4", started_at=None))
    assert _posture(root, job, trial, record)["date"] is None


def test_laminar_trace_id(tmp_path: Path) -> None:
    root = _repo_root(tmp_path)
    job = _job(tmp_path)
    trace_id = str(uuid.uuid4())
    trial, record = _trial(
        job, "t1", laminar={"trial_name": "t1", "trace_id": trace_id}
    )
    assert _posture(root, job, trial, record)["laminar_trace_id"] == trace_id

    trial, record = _trial(job, "t2")
    assert _posture(root, job, trial, record)["laminar_trace_id"] is None

    trial, record = _trial(
        job, "t3", laminar={"trial_name": "other", "trace_id": trace_id}
    )
    assert _posture(root, job, trial, record)["laminar_trace_id"] is None

    trial, record = _trial(job, "t4", laminar={"trial_name": "t4", "trace_id": "nope"})
    assert _posture(root, job, trial, record)["laminar_trace_id"] is None

    trial, record = _trial(
        job, "t5", laminar={"trial_name": "t5", "trace_id": str(uuid.UUID(int=0))}
    )
    assert _posture(root, job, trial, record)["laminar_trace_id"] is None
