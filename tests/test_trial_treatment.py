"""Treatment keys pool like with like; capture records keep unknown distinct from zero."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evallab.trial_treatment import (
    CommitSources,
    collect_capture,
    collect_jobs,
    collect_treatment,
    pool_check,
    read_treatments,
    table_paths,
    upsert_tables,
)

PARSER = '''"""MiMo tool-call normalizer."""


def normalize(response: str) -> str:
    """Rewrite one response."""
    return response.strip()
'''

PROXY = """def shape(forwarded, template):
    template["enable_thinking"] = True
    forwarded["temperature"] = 0.6
    forwarded["top_p"] = 0.95
    forwarded["top_k"] = 20
"""

SERVE = """MODEL_REVISION = "2367e865"
SGLANG_IMAGE = "lmsysorg/sglang@sha256:00b0"
CONTEXT_LENGTH = 65536
"""

TERMINUS = "def run():\n    return 1\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    return root


def _base_sources() -> dict[str, str]:
    return {
        "src/evallab/mimo_tool_calls.py": PARSER,
        "src/evallab/harbor_terminus.py": TERMINUS,
        "containers/zai_openapi_secret_proxy.py": PROXY,
        "tools/modal-mimo-serve/serve.py": SERVE,
    }


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _job(
    root: Path,
    name: str,
    commit: str,
    *,
    dirty: bool = False,
    kwargs: dict[str, Any] | None = None,
    max_requests: int = 2000,
    model: str = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B",
    agent_name: str = "terminus-2",
    input_tokens: int = 1000,
    ledger: bool = True,
    continuations: tuple[int, ...] = (),
    summarization_count: int = 0,
    log_text: str | None = None,
    session_id: str | None = None,
) -> tuple[Path, Path]:
    job = root / name
    trial = job / f"{name}__abc"
    kwargs = (
        kwargs
        if kwargs is not None
        else {
            "llm_call_kwargs": {"max_tokens": 8192, "top_p": 0.95},
            "temperature": 0.6,
            "trajectory_config": {"raw_content": True, "linear_history": True},
        }
    )
    lab: dict[str, Any] = {
        "repository": {"commit": commit, "dirty": dirty},
        "task_staging": {"source_package_digest": "sha256:task-a"},
        "tools": {"harbor": "0.21.0"},
    }
    if ledger:
        lab["provider_usage"] = {
            "calls": [{"shaping_applied": True, "input_tokens": input_tokens}],
            "totals": {"input_tokens": input_tokens + 500, "output_tokens": 10},
            "unresolved_requests": 0,
        }
    _write(job / "lab-metadata.json", lab)
    task_dir = root / "tasks" / "t"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_text("[agent]\ntimeout_sec = 900.0\n", encoding="utf-8")
    _write(
        job / "experiment-spec.json",
        {
            "task": str(task_dir),
            "timeout_seconds": 900,
            "max_requests": max_requests,
            "max_input_tokens": 64_000_000,
            "max_output_tokens": 1_000_000,
            "max_total_tokens": 65_000_000,
            "cost_limit_usd": 1.0,
        },
    )
    (trial / "verifier").mkdir(parents=True)
    (trial / "verifier" / "test-stdout.txt").write_text("ok\n", encoding="utf-8")
    _write(
        trial / "config.json",
        {
            "agent": {
                "name": "evallab.harbor_terminus:SecretSafeTerminus2",
                "model_name": model,
                "kwargs": kwargs,
            },
            "environment": {"import_path": "evallab.harbor_daytona:BoundedDaytonaEnvironment"},
            "agent_timeout_multiplier": 1.0,
        },
    )
    _write(
        trial / "result.json",
        {
            "agent_info": {"name": agent_name},
            "agent_result": {
                "n_input_tokens": input_tokens,
                "n_output_tokens": 10,
                "rollout_details": [],
                "metadata": {"n_episodes": 1, "summarization_count": summarization_count},
            },
        },
    )
    step = {
        "step_id": 1,
        "source": "agent",
        "message": "ls",
        "metrics": {"prompt_tokens": input_tokens, "completion_tokens": 10},
    }
    payload: dict[str, Any] = {"steps": [step]}
    if session_id is not None:
        payload["session_id"] = session_id
    for index in continuations:
        _write(trial / "agent" / f"trajectory.cont-{index}.json", payload)
    if not continuations:
        _write(trial / "agent" / "trajectory.json", payload)
    if log_text is not None:
        (trial / "trial.log").write_text(log_text, encoding="utf-8")
    return job, trial


def _row(repo: Path, job: Path, trial: Path) -> dict[str, Any]:
    return collect_treatment(job, trial, CommitSources(repo))


def test_same_setup_same_key_despite_recording_differences(repo: Path, tmp_path: Path) -> None:
    commit = _commit(repo, _base_sources(), "base")
    job_a, trial_a = _job(tmp_path / "runs", "a", commit, input_tokens=100)
    job_b, trial_b = _job(tmp_path / "runs", "b", commit, input_tokens=90_000, continuations=(2,))
    a, b = _row(repo, job_a, trial_a), _row(repo, job_b, trial_b)
    assert a["complete"] and b["complete"]
    assert a["treatment_key"] == b["treatment_key"]
    assert pool_check([a, b], same_task=True).ok


def test_recording_only_source_changes_keep_the_key(repo: Path, tmp_path: Path) -> None:
    base = _commit(repo, _base_sources(), "base")
    recorded = _commit(
        repo,
        {
            "src/evallab/mimo_tool_calls.py": PARSER.replace(
                '"""Rewrite one response."""',
                '"""Rewrite one response (docs only)."""\n    # comment',
            ),
            "src/evallab/harbor_terminus.py": TERMINUS
            + "\n\ndef record_executed_calls():\n    return []\n",
        },
        "record executed calls",
    )
    first = _row(repo, *_job(tmp_path / "runs", "a", base))
    second = _row(repo, *_job(tmp_path / "runs", "b", recorded))
    assert first["repository_commit"] != second["repository_commit"]
    assert first["treatment_key"] == second["treatment_key"]


def test_parser_and_limit_changes_split_the_pool_and_name_the_fields(
    repo: Path, tmp_path: Path
) -> None:
    base = _commit(repo, _base_sources(), "base")
    changed = _commit(
        repo,
        {
            "src/evallab/mimo_tool_calls.py": PARSER.replace(
                "response.strip()", "response.strip().lower()"
            )
        },
        "parser behaviour change",
    )
    a = _row(repo, *_job(tmp_path / "runs", "a", base))
    b = _row(repo, *_job(tmp_path / "runs", "b", changed, max_requests=200))
    check = pool_check([a, b])
    assert not check.ok
    assert set(check.differing) == {"parser_digest", "max_requests"}
    assert "parser_digest" in check.render()


def test_route_shaping_overrides_requested_sampling(repo: Path, tmp_path: Path) -> None:
    commit = _commit(repo, _base_sources(), "base")
    requested = _row(
        repo,
        *_job(
            tmp_path / "runs",
            "a",
            commit,
            kwargs={
                "llm_call_kwargs": {"max_tokens": 8192, "extra_body": {"reasoning_effort": "none"}},
                "trajectory_config": {"raw_content": True, "linear_history": True},
            },
        ),
    )
    assert (requested["temperature"], requested["top_p"], requested["top_k"]) == (0.6, 0.95, 20)
    assert requested["thinking"] == "enable_thinking=true"
    explicit = _row(repo, *_job(tmp_path / "runs", "b", commit))
    assert requested["treatment_key"] == explicit["treatment_key"]


OPENROUTER_PROXY = '''OPENROUTER_ROUTES = {
    "xiaomi/mimo-v2.6-flash": {
        "endpoint": "xiaomi/fp8",
        "provider": {"order": ["xiaomi"], "allow_fallbacks": False},
        "reasoning": {"enabled": True},
        "prices": (140_000, 280_000),
        "context_input_tokens": 1_048_576,
    },
    "openai/gpt-oss-120b": {
        "endpoint": "deepinfra/bf16",
        "provider": {"order": ["deepinfra/bf16"], "allow_fallbacks": False},
        "reasoning": {"effort": "medium"},
        "prices": (37_000, 170_000),
        "context_input_tokens": 131_072,
    },
}
'''


def _openrouter_sources(proxy: str = OPENROUTER_PROXY) -> dict[str, str]:
    sources = _base_sources()
    sources["containers/zai_openapi_secret_proxy.py"] = proxy
    return sources


@pytest.mark.parametrize(
    ("model", "provider", "reasoning", "prices", "endpoint", "context", "thinking"),
    [
        (
            "openrouter-metered/xiaomi/mimo-v2.6-flash",
            {"order": ["xiaomi"], "allow_fallbacks": False},
            {"enabled": True},
            [140000, 280000],
            "xiaomi/fp8",
            1048576,
            "reasoning_enabled=true",
        ),
        (
            "openrouter-metered/openai/gpt-oss-120b",
            {"order": ["deepinfra/bf16"], "allow_fallbacks": False},
            {"effort": "medium"},
            [37000, 170000],
            "deepinfra/bf16",
            131072,
            "reasoning_effort=medium",
        ),
    ],
)
def test_openrouter_route_pins_land_in_the_treatment_key(
    repo: Path,
    tmp_path: Path,
    model: str,
    provider: dict[str, object],
    reasoning: dict[str, object],
    prices: list[int],
    endpoint: str,
    context: int,
    thinking: str,
) -> None:
    commit = _commit(repo, _openrouter_sources(), "base")
    row = _row(
        repo,
        *_job(
            tmp_path / "runs",
            "a",
            commit,
            model=model,
            kwargs={
                "llm_call_kwargs": {"max_tokens": 4096, "top_p": 0.95},
                "temperature": 0.6,
                "proactive_summarization_threshold": 16384,
            },
        ),
    )
    assert row["complete"]
    # Serving identity comes from the model's proxy route pins at the commit.
    assert json.loads(row["model_revision"]) == {
        "provider": provider,
        "reasoning": reasoning,
        "prices": prices,
    }
    assert row["serving_image"] == endpoint
    assert row["serving_context_tokens"] == context
    # Sampling passes through (no forced literals); thinking records the pin.
    assert (row["temperature"], row["top_p"]) == (0.6, 0.95)
    assert row["top_k"] == "default"
    assert row["thinking"] == thinking
    # HAR-104 keeps the stock parser on this route.
    assert row["parser_digest"] == "none"
    # The Harbor-default tree omits trajectory_config: ATIF defaults land in
    # the harness digest.
    harness = json.loads(row["harness_config"])
    assert harness["trajectory_config"] == {"raw_content": False, "linear_history": False}


def test_openrouter_pin_change_splits_the_treatment_key(repo: Path, tmp_path: Path) -> None:
    base = _commit(repo, _openrouter_sources(), "base")
    repinned = _commit(
        repo,
        _openrouter_sources(
            OPENROUTER_PROXY.replace('"xiaomi/fp8"', '"xiaomi/nightly"')
        ),
        "repin endpoint",
    )
    a = _row(
        repo,
        *_job(
            tmp_path / "runs",
            "a",
            base,
            model="openrouter-metered/xiaomi/mimo-v2.6-flash",
        ),
    )
    b = _row(
        repo,
        *_job(
            tmp_path / "runs",
            "b",
            repinned,
            model="openrouter-metered/xiaomi/mimo-v2.6-flash",
        ),
    )
    assert a["serving_image"] == "xiaomi/fp8"
    assert b["serving_image"] == "xiaomi/nightly"
    assert a["treatment_key"] != b["treatment_key"]
    check = pool_check([a, b])
    assert not check.ok and "serving_image" in check.differing


def test_dirty_or_missing_evidence_reads_unknown_and_refuses_pooling(
    repo: Path, tmp_path: Path
) -> None:
    commit = _commit(repo, _base_sources(), "base")
    dirty = _row(repo, *_job(tmp_path / "runs", "a", commit, dirty=True))
    clean = _row(repo, *_job(tmp_path / "runs", "b", commit))
    assert dirty["parser_digest"] is None and "parser_digest" in dirty["unknown_fields"]
    assert not dirty["complete"]
    check = pool_check([dirty, clean], accept_unknown=True)
    assert not check.ok and "parser_digest" in check.differing
    twin = _row(repo, *_job(tmp_path / "runs", "c", commit, dirty=True))
    assert not pool_check([dirty, twin]).ok
    assert pool_check([dirty, twin], accept_unknown=True).ok


def test_no_model_agents_read_not_applicable_not_unknown(repo: Path, tmp_path: Path) -> None:
    commit = _commit(repo, _base_sources(), "base")
    row = _row(
        repo,
        *_job(tmp_path / "runs", "a", commit, model="", agent_name="nop", kwargs={}, ledger=False),
    )
    assert row["model"] == "n/a" and row["parser_digest"] == "n/a"


def test_capture_full_copy_continuation_is_not_missing_and_ledger_null(tmp_path: Path) -> None:
    job, trial = _job(
        tmp_path / "runs",
        "a",
        "0" * 40,
        continuations=(3,),
        summarization_count=3,
        ledger=False,
        session_id="sess-1",
    )
    row = collect_capture(job, trial)
    assert row["trajectory_head"] is False
    assert row["continuation_indices"] == [3]
    assert row["continuation_full_copy"] is True
    assert row["continuation_split"] is False
    assert row["trajectory_session_ids"] == ["sess-1"]
    assert row["history_context_diverged"] is None
    assert row["trial_log_present"] is False
    assert row["proxy_ledger"] is False
    assert row["proxy_input_tokens"] is None and row["input_tokens_unattributed"] is None
    assert row["token_gap_basis"] is None
    assert row["trajectory_input_tokens"] == 1000


def test_capture_split_continuation_starts_past_step_one(tmp_path: Path) -> None:
    job, trial = _job(tmp_path / "runs", "a", "0" * 40, session_id="sess-1")
    steps = [
        {"step_id": 40, "source": "agent", "message": "m", "metrics": {"prompt_tokens": 5}},
        {"step_id": 41, "source": "agent", "message": "m", "metrics": {"prompt_tokens": 5}},
    ]
    (trial / "agent" / "trajectory.cont-2.json").write_text(
        json.dumps({"session_id": "sess-1", "steps": steps}), encoding="utf-8"
    )
    row = collect_capture(job, trial)
    assert row["continuation_full_copy"] is False
    assert row["continuation_split"] is True
    assert json.loads(row["continuation_step_ranges"])["cont"]["2"] == [40, 41]


def test_capture_new_session_counts_as_split(tmp_path: Path) -> None:
    job, trial = _job(tmp_path / "runs", "a", "0" * 40, session_id="sess-1")
    head = json.loads((trial / "agent" / "trajectory.json").read_text(encoding="utf-8"))
    (trial / "agent" / "trajectory.cont-1.json").write_text(
        json.dumps({"session_id": "sess-2", "steps": head["steps"]}),
        encoding="utf-8",
    )
    row = collect_capture(job, trial)
    assert row["continuation_full_copy"] is True
    assert row["continuation_split"] is True
    assert row["trajectory_session_ids"] == ["sess-1", "sess-2"]


def test_capture_log_cycles_and_failed_call_gap_basis(tmp_path: Path) -> None:
    log_text = "\n".join(
        [
            "Context length exceeded. Using fallback summarization.",
            "Unwound messages. Remaining messages: 364, Free tokens: approximately 11409",
            "SUMMARIZATION: Attempting full summary",
            "SUMMARIZATION: Full summary failed: ",
            "SUMMARIZATION: Attempting short summary",
            "SUMMARIZATION: Short summary succeeded",
            "Even fallback chat failed: ",
            "Context length exceeded. Using fallback summarization.",
            "Unwound messages. Remaining messages: 364, Free tokens: approximately 11409",
            "SUMMARIZATION: Attempting full summary",
            "SUMMARIZATION: Full summary failed: ",
            "SUMMARIZATION: Attempting short summary",
            "SUMMARIZATION: Short summary succeeded",
            "Proactively summarizing. Free tokens: approximately 7970",
            "Error in proactively summarizing: ",
            "Summary subagent trajectory saved to agent/trajectory.summarization-1-summary.json",
        ]
    )
    job, trial = _job(
        tmp_path / "runs",
        "a",
        "0" * 40,
        continuations=(3,),
        summarization_count=3,
        session_id="sess-1",
        log_text=log_text,
    )
    row = collect_capture(job, trial)
    assert row["trial_log_present"] is True
    assert row["overflow_reactive_cycles"] == 2
    assert row["overflow_proactive_attempts"] == 1
    assert row["overflow_proactive_errors"] == 1
    assert row["overflow_cycles"] == 3
    assert row["summary_full_failed"] == 2 and row["summary_full_succeeded"] == 0
    assert row["summary_short_succeeded"] == 2 and row["summary_short_failed"] == 0
    assert row["fallback_chat_failed"] == 1 and row["fallback_chat_succeeded"] == 0
    assert row["summary_subagent_saves"] == 1
    assert row["history_context_diverged"] is True
    assert row["summarization_count"] == row["overflow_cycles"]
    assert row["input_tokens_unattributed"] == 500
    assert "3 overflow cycles" in row["token_gap_basis"]
    assert "failed/unrecorded calls, not missing files" in row["token_gap_basis"]


def test_capture_gap_from_truncated_and_final_orphan_calls(tmp_path: Path) -> None:
    job, trial = _job(tmp_path / "runs", "a", "0" * 40, input_tokens=1000)
    lab = json.loads((job / "lab-metadata.json").read_text(encoding="utf-8"))
    call = {"shaping_applied": True, "reserved_output_tokens": 4096}
    lab["provider_usage"] = {
        "calls": [
            {**call, "call_id": 1, "input_tokens": 400, "output_tokens": 4096},
            {**call, "call_id": 2, "input_tokens": 1000, "output_tokens": 10},
            {**call, "call_id": 3, "input_tokens": 1100, "output_tokens": 7},
        ],
        "totals": {"input_tokens": 2500, "output_tokens": 4113},
        "unresolved_requests": 0,
    }
    _write(job / "lab-metadata.json", lab)
    result = json.loads((trial / "result.json").read_text(encoding="utf-8"))
    result["exception_info"] = {"exception_type": "AgentTimeoutError"}
    _write(trial / "result.json", result)

    row = collect_capture(job, trial)

    assert row["input_tokens_unattributed"] == 1500
    assert row["ledger_orphan_truncated_calls"] == 1
    assert row["ledger_orphan_final_calls"] == 1
    assert row["ledger_orphan_other_calls"] == 0
    assert row["ledger_orphan_input_tokens"] == 1500
    assert "truncated at max_tokens and retried" in row["token_gap_basis"]
    assert "final call cut off by AgentTimeoutError" in row["token_gap_basis"]
    assert "the whole gap" in row["token_gap_basis"]


def test_capture_orphan_without_agent_exception_is_not_final(tmp_path: Path) -> None:
    job, trial = _job(tmp_path / "runs", "a", "0" * 40, input_tokens=1000)
    lab = json.loads((job / "lab-metadata.json").read_text(encoding="utf-8"))
    lab["provider_usage"] = {
        "calls": [
            {"input_tokens": 1000, "output_tokens": 10, "reserved_output_tokens": 4096},
            {"input_tokens": 300, "output_tokens": 12, "reserved_output_tokens": 4096},
        ],
        "totals": {"input_tokens": 1500, "output_tokens": 22},
        "unresolved_requests": 0,
    }
    _write(job / "lab-metadata.json", lab)

    row = collect_capture(job, trial)

    assert row["ledger_orphan_final_calls"] == 0
    assert row["ledger_orphan_other_calls"] == 1
    assert "300 of the gap, 200 left" in row["token_gap_basis"]


def test_capture_quiet_log_leaves_context_intact(tmp_path: Path) -> None:
    job, trial = _job(
        tmp_path / "runs",
        "a",
        "0" * 40,
        session_id="sess-1",
        log_text="Healthcheck passed\n",
        ledger=False,
    )
    row = collect_capture(job, trial)
    assert row["overflow_cycles"] == 0
    assert row["history_context_diverged"] is False
    assert row["proxy_ledger"] is False
    assert row["proxy_input_tokens"] is None and row["input_tokens_unattributed"] is None
    assert row["trajectory_input_tokens"] == 1000


def test_tables_upsert_by_trial_and_round_trip_key_values(repo: Path, tmp_path: Path) -> None:
    commit = _commit(repo, _base_sources(), "base")
    job, _ = _job(tmp_path / "runs", "a", commit)
    catalog = tmp_path / "catalog"
    treatments, captures = collect_jobs([job], repo_root=repo, produced_at="t1")
    upsert_tables(treatments, captures, catalog)
    other, _ = _job(tmp_path / "runs", "b", commit)
    treatments_b, captures_b = collect_jobs([job, other], repo_root=repo, produced_at="t2")
    _, _, n_treatment, n_capture = upsert_tables(treatments_b, captures_b, catalog)
    assert (n_treatment, n_capture) == (2, 2)
    rows = read_treatments(catalog)
    assert {row["produced_at"] for row in rows} == {"t2"}
    assert rows[0]["max_requests"] == 2000 and rows[0]["top_p"] == 0.95
    assert pool_check(rows).ok


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("has_continuation", [False, True])
def test_metered_capture_keeps_continuity_separate_from_usage(
    repo: Path, tmp_path: Path, version: int, has_continuation: bool
) -> None:
    commit = _commit(repo, _base_sources(), "base")
    job, trial = _job(tmp_path / "runs", "metered", commit, session_id="first")
    lab = json.loads((job / "lab-metadata.json").read_text())
    lab["provider_usage"] = {
        "schema_version": version,
        "calls": [
            {
                "call_id": 1,
                "state": "reconciled",
                "input_tokens": 1000,
                "output_tokens": 10,
                "cost_micros": 0,
                "shaping_applied": True,
            },
            {
                "call_id": 2,
                "state": "unresolved",
                "reserved_input_tokens": 500,
                "reserved_output_tokens": 20,
                "reserved_cost_micros": 0,
            },
        ],
        "totals": {
            "requests": 1,
            "input_tokens": 1000 if version == 2 else 1500,
            "output_tokens": 10 if version == 2 else 30,
            "total_tokens": 1010 if version == 2 else 1530,
            "cost_micros": 0,
        },
        "attempted": {
            "requests": 1,
            "input_tokens": 500,
            "output_tokens": 20,
            "cost_micros": 0,
        },
        "unresolved_requests": 1,
    }
    _write(job / "lab-metadata.json", lab)
    if has_continuation:
        head = json.loads((trial / "agent" / "trajectory.json").read_text())
        _write(
            trial / "agent" / "trajectory.cont-1.json",
            {**head, "session_id": "second"},
        )

    treatments, captures = collect_jobs([job], repo_root=repo)
    _, capture_path, _, _ = upsert_tables(treatments, captures, tmp_path / "catalog")
    stored = pq.read_table(capture_path).to_pylist()[0]
    assert stored["continuation_split"] is has_continuation
    assert stored["proxy_input_tokens"] == 1000
    assert stored["proxy_output_tokens"] == 10
    assert stored["proxy_unresolved_requests"] == 1


def test_upsert_refuses_unpaired_trials_without_changing_snapshot(
    repo: Path, tmp_path: Path
) -> None:
    commit = _commit(repo, _base_sources(), "base")
    first, _ = _job(tmp_path / "runs", "first", commit)
    second, _ = _job(tmp_path / "runs", "second", commit)
    catalog = tmp_path / "catalog"
    original = collect_jobs([first], repo_root=repo)
    upsert_tables(*original, catalog)
    before = table_paths(catalog)
    treatments, _ = collect_jobs([second], repo_root=repo)
    with pytest.raises(ValueError, match="treatment/capture"):
        upsert_tables(treatments, [], catalog)
    assert table_paths(catalog) == before
    assert [row["job_name"] for row in read_treatments(catalog)] == ["first"]


@pytest.mark.parametrize("capture_state", ["missing", "missing_trial", "stale_collection"])
def test_partial_legacy_catalog_recovers_only_when_missing_trials_are_recollected(
    repo: Path, tmp_path: Path, capture_state: str
) -> None:
    commit = _commit(repo, _base_sources(), "base")
    first, _ = _job(tmp_path / "runs", "first", commit)
    second, _ = _job(tmp_path / "runs", "second", commit)
    treatments, captures = collect_jobs([first, second], repo_root=repo)
    treatment_path, capture_path, _, _ = upsert_tables(
        treatments, captures, tmp_path / "source"
    )
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / treatment_path.name).write_bytes(treatment_path.read_bytes())
    if capture_state == "missing_trial":
        pq.write_table(pq.read_table(capture_path).slice(0, 1), legacy / capture_path.name)
    elif capture_state == "stale_collection":
        table = pq.read_table(capture_path)
        stale = [{**row, "produced_at": "older"} for row in table.to_pylist()]
        pq.write_table(pa.Table.from_pylist(stale, schema=table.schema), legacy / capture_path.name)

    with pytest.raises(ValueError, match="rerun tasks treatment-collect"):
        read_treatments(legacy)
    partial = collect_jobs([first], repo_root=repo)
    with pytest.raises(ValueError, match="source jobs: second"):
        upsert_tables(*partial, legacy)

    # Replay the retained jobs rather than inventing absent capture evidence.
    upsert_tables(treatments, captures, legacy)
    rows = read_treatments(legacy)
    assert {row["job_name"] for row in rows} == {"first", "second"}
    published = table_paths(legacy)
    assert pq.read_table(published["trial_capture.parquet"]).num_rows == 2
    assert (legacy / treatment_path.name).read_bytes() == treatment_path.read_bytes()
