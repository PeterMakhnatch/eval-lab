"""HAR-104: ledger used-vs-attempted semantics and finalize-time cost.

- v1 ledgers with reserved/unresolved calls derive used (settled actuals)
  vs attempted (open reservations) from the calls list.
- v2 ledgers round-trip from TrialBudget with an unresolved call: totals
  exclude the open reservation, ``attempted`` holds it.
- Tampered v2 totals fail validation.
- Finalize cost for a priced route equals the reconciled-call sum at the
  ledger's pinned rates (per-call ceiling rounding preserved).
- The self-hosted (0, 0) route never reports $0 without a reason.
- The catalog row prefers the ledger over Harbor/litellm's estimate, and
  the gate's daily spend sums used + attempted.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from evallab.database import trial_cost_columns
from evallab.execution_contracts import ProxyTrialLimits
from evallab.ledger import build_cost_block, split_usage
from evallab.results import JobRecord, TrialRecord

REPO_ROOT = Path(__file__).resolve().parents[1]
FLASH_RATES = (150_000, 500_000)  # glm-5.3-flash micros per 1M tokens
FLASH_PRICING = {
    "input_cost_micros_per_million": 150_000,
    "output_cost_micros_per_million": 500_000,
}
SELFHOSTED_PRICING = {
    "input_cost_micros_per_million": 0,
    "output_cost_micros_per_million": 0,
}


def _proxy_module():
    source = REPO_ROOT / "containers" / "zai_openapi_secret_proxy.py"
    spec = importlib.util.spec_from_file_location("har104_cost_proxy", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _budget_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, usage_name: str = "usage.json"
) -> tuple[str, str, Path]:
    capability = "har104-cost-capability"
    attempt_id = "har104-cost-attempt"
    usage_path = tmp_path / usage_name
    monkeypatch.delenv("EVALLAB_PROXY_PROVIDER", raising=False)
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_PROXY_CAPABILITY", capability)
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_ATTEMPT_ID", attempt_id)
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_USAGE_FILE", str(usage_path))
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_MAX_REQUESTS", "50")
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_MAX_INPUT_TOKENS", "100000")
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_MAX_OUTPUT_TOKENS", "100000")
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_MAX_TOTAL_TOKENS", "200000")
    monkeypatch.setenv("EVALLAB_ZAI_OPENAPI_MAX_COST_MICROS", "10000000")
    return capability, attempt_id, usage_path


def _limits() -> ProxyTrialLimits:
    return ProxyTrialLimits(
        max_requests=50,
        max_input_tokens=100000,
        max_output_tokens=100000,
        max_total_tokens=200000,
        max_cost_micros=10000000,
    )


def _ceil_micros(input_tokens: int, output_tokens: int) -> int:
    numerator = input_tokens * FLASH_RATES[0] + output_tokens * FLASH_RATES[1]
    return (numerator + 999_999) // 1_000_000


def _v1_ledger() -> dict:
    """One reconciled call plus one unresolved reservation, legacy shape."""
    return {
        "schema_version": 1,
        "capability_id": "sha256:" + "a" * 64,
        "attempt_id": "har104-cost-attempt",
        "sequence": 4,
        "limits": {
            "max_requests": 50,
            "max_input_tokens": 100000,
            "max_output_tokens": 100000,
            "max_total_tokens": 200000,
            "max_cost_micros": 10000000,
        },
        "pricing": dict(FLASH_PRICING),
        # Legacy inflation: totals include the unresolved reservation.
        "totals": {
            "requests": 2,
            "input_tokens": 1000 + 4000,
            "output_tokens": 500 + 4096,
            "total_tokens": 1000 + 4000 + 500 + 4096,
            "cost_micros": _ceil_micros(1000, 500) + _ceil_micros(4000, 4096),
        },
        "unresolved_requests": 1,
        "calls": [
            {
                "call_id": 1,
                "state": "reconciled",
                "status": 200,
                "reserved_input_tokens": 4000,
                "reserved_output_tokens": 4096,
                "reserved_cost_micros": _ceil_micros(4000, 4096),
                "requested_model": "zai/glm-5.3-flash",
                "input_tokens": 1000,
                "output_tokens": 500,
                "cost_micros": _ceil_micros(1000, 500),
                "returned_model": "glm-5.3-flash",
                "returned_model_reason": None,
            },
            {
                "call_id": 2,
                "state": "unresolved",
                "reason": "upstream_transport_error",
                "reserved_input_tokens": 4000,
                "reserved_output_tokens": 4096,
                "reserved_cost_micros": _ceil_micros(4000, 4096),
                "requested_model": "zai/glm-5.3-flash",
                "returned_model": None,
                "returned_model_reason": "response_not_observed",
            },
        ],
    }


def test_v1_ledger_derives_used_excluding_open_reservations() -> None:
    split = split_usage(_v1_ledger())
    assert split["used"] == {
        "requests": 1,
        "input_tokens": 1000,
        "output_tokens": 500,
        "cost_micros": _ceil_micros(1000, 500),
        "total_tokens": 1500,
    }
    assert split["attempted"] == {
        "requests": 1,
        "input_tokens": 4000,
        "output_tokens": 4096,
        "cost_micros": _ceil_micros(4000, 4096),
    }
    assert split["unresolved_requests"] == 1


def test_v1_ledger_still_validates_under_legacy_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab import runner as runner_module

    capability, attempt_id, usage_path = _budget_env(tmp_path, monkeypatch)
    usage_path.write_text(json.dumps(_v1_ledger()))
    usage_path.chmod(0o600)
    payload = runner_module._read_proxy_usage(
        usage_path,
        capability_id="sha256:" + "a" * 64,
        attempt_id=attempt_id,
        limits=_limits(),
        provider_label="Z.ai OpenAPI",
        expected_pricing=dict(FLASH_PRICING),
    )
    # Historic file, returned as written — never rewritten to v2.
    assert payload["schema_version"] == 1
    assert payload["totals"]["input_tokens"] == 5000


def test_v2_round_trip_excludes_open_reservation_from_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _proxy_module()
    capability, attempt_id, usage_path = _budget_env(tmp_path, monkeypatch)
    budget = module.TrialBudget()
    settled = budget.reserve(
        input_tokens=4000,
        output_tokens=4096,
        cost_micros=_ceil_micros(4000, 4096),
        requested_model="zai/glm-5.3-flash",
        rates=FLASH_RATES,
    )
    assert settled is not None
    budget.reconcile(
        call_id=settled,
        used_input=1000,
        used_output=500,
        used_cost=_ceil_micros(1000, 500),
        status=200,
        returned_model="glm-5.3-flash",
    )
    open_call = budget.reserve(
        input_tokens=4000,
        output_tokens=4096,
        cost_micros=_ceil_micros(4000, 4096),
        requested_model="zai/glm-5.3-flash",
        rates=FLASH_RATES,
    )
    assert open_call is not None
    budget.mark_unresolved(call_id=open_call, reason="upstream_transport_error")

    ledger = json.loads(usage_path.read_text())
    assert ledger["schema_version"] == 2
    assert ledger["totals"] == {
        "requests": 1,
        "input_tokens": 1000,
        "output_tokens": 500,
        "total_tokens": 1500,
        "cost_micros": _ceil_micros(1000, 500),
    }
    assert ledger["attempted"] == {
        "requests": 1,
        "input_tokens": 4000,
        "output_tokens": 4096,
        "cost_micros": _ceil_micros(4000, 4096),
    }
    assert ledger["unresolved_requests"] == 1

    from evallab import runner as runner_module

    validated = runner_module._read_proxy_usage(
        usage_path,
        capability_id="sha256:" + hashlib.sha256(capability.encode()).hexdigest(),
        attempt_id=attempt_id,
        limits=_limits(),
        provider_label="Z.ai OpenAPI",
        expected_pricing=dict(FLASH_PRICING),
    )
    assert validated["schema_version"] == 2


def test_v2_tampered_totals_fail_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _proxy_module()
    capability, attempt_id, usage_path = _budget_env(tmp_path, monkeypatch)
    call_id = budget_call(module)
    assert call_id is not None
    ledger = json.loads(usage_path.read_text())
    ledger["totals"]["input_tokens"] += 1
    usage_path.write_text(json.dumps(ledger))

    from evallab import runner as runner_module
    from evallab.execution_contracts import ExecutionFailure

    with pytest.raises(ExecutionFailure, match="do not reconcile"):
        runner_module._read_proxy_usage(
            usage_path,
            capability_id="sha256:" + hashlib.sha256(capability.encode()).hexdigest(),
            attempt_id=attempt_id,
            limits=_limits(),
            provider_label="Z.ai OpenAPI",
            expected_pricing=dict(FLASH_PRICING),
        )


def budget_call(module) -> int | None:
    budget = module.TrialBudget()
    return budget.reserve(
        input_tokens=100,
        output_tokens=100,
        cost_micros=_ceil_micros(100, 100),
        requested_model="zai/glm-5.3-flash",
        rates=FLASH_RATES,
    )


def test_finalize_cost_equals_reconciled_sum_at_pinned_rates() -> None:
    ledger = _v1_ledger()
    block = build_cost_block(ledger)
    assert block["source"] == "proxy_ledger_x_pinned_price"
    assert block["pricing"] == FLASH_PRICING
    assert block["reason"] is None
    # Per-call ceiling rounding is preserved: the sum of rounded calls.
    assert block["cost_usd"] == _ceil_micros(1000, 500) / 1_000_000
    assert block["attempted_cost_usd"] == _ceil_micros(4000, 4096) / 1_000_000


def test_selfhosted_route_never_reports_zero_without_reason() -> None:
    ledger = _v1_ledger()
    ledger["schema_version"] = 2
    ledger["pricing"] = dict(SELFHOSTED_PRICING)
    ledger["totals"] = {
        "requests": 1,
        "input_tokens": 1000,
        "output_tokens": 500,
        "total_tokens": 1500,
        "cost_micros": 0,
    }
    ledger["attempted"] = {
        "requests": 1,
        "input_tokens": 4000,
        "output_tokens": 4096,
        "cost_micros": 0,
    }
    ledger["calls"][0]["cost_micros"] = 0
    ledger["calls"][0]["reserved_cost_micros"] = 0
    ledger["calls"][1]["reserved_cost_micros"] = 0
    block = build_cost_block(ledger)
    assert block["cost_usd"] is None
    assert block["reason"]
    assert block["pricing"] == SELFHOSTED_PRICING


def test_missing_ledger_is_none_with_reason_never_zero() -> None:
    block = build_cost_block(None)
    assert block["cost_usd"] is None
    assert block["attempted_cost_usd"] is None
    assert block["reason"]


def _job(
    tmp_path: Path,
    provider_usage: dict | None,
    agent_cost: float | None,
    trial_count: int = 1,
) -> JobRecord:
    trials = tuple(
        TrialRecord(
            path=tmp_path / f"trial-{index}",
            result={
                "id": f"00000000-0000-0000-0000-{index:012d}",
                "trial_name": f"trial-{index}",
                "task_name": "task",
                "agent_result": {"cost_usd": agent_cost},
                "started_at": "2026-09-29T00:00:00Z",
                "finished_at": "2026-09-29T00:01:00Z",
            },
            config={},
            lock={},
            rewards={},
            artifacts=(),
        )
        for index in range(trial_count)
    )
    metadata: dict = {}
    if provider_usage is not None:
        metadata["provider_usage"] = provider_usage
    return JobRecord(
        path=tmp_path / "job",
        result={"id": "11111111-1111-1111-1111-111111111111"},
        config={},
        lock={},
        metadata=metadata,
        trials=trials,
        files=(),
    )


def test_catalog_row_prefers_ledger_over_harbor_estimate(tmp_path: Path) -> None:
    job = _job(tmp_path, _v1_ledger(), agent_cost=0.99)
    spend = trial_cost_columns(job, job.trials[0])
    assert spend["cost_usd"] == pytest.approx(_ceil_micros(1000, 500) / 1_000_000)
    assert spend["attempted_cost_usd"] == pytest.approx(
        _ceil_micros(4000, 4096) / 1_000_000
    )


def test_catalog_row_without_ledger_keeps_harbor_figure(tmp_path: Path) -> None:
    job = _job(tmp_path, None, agent_cost=0.25)
    spend = trial_cost_columns(job, job.trials[0])
    assert spend == {"cost_usd": 0.25, "attempted_cost_usd": None}


def test_catalog_row_selfhosted_is_none_with_ledger_present(
    tmp_path: Path,
) -> None:
    ledger = _v1_ledger()
    ledger["pricing"] = dict(SELFHOSTED_PRICING)
    job = _job(tmp_path, ledger, agent_cost=None)
    spend = trial_cost_columns(job, job.trials[0])
    assert spend["cost_usd"] is None
    assert spend["attempted_cost_usd"] == pytest.approx(
        _ceil_micros(4000, 4096) / 1_000_000
    )


def test_catalog_row_splits_shared_ledger_across_trials(tmp_path: Path) -> None:
    job = _job(tmp_path, _v1_ledger(), agent_cost=None, trial_count=2)
    first = trial_cost_columns(job, job.trials[0])
    second = trial_cost_columns(job, job.trials[1])
    assert first["cost_usd"] == pytest.approx(second["cost_usd"])
    assert first["cost_usd"] == pytest.approx(_ceil_micros(1000, 500) / 1_000_000 / 2)
    assert (first["cost_usd"] or 0) * 2 == pytest.approx(
        _ceil_micros(1000, 500) / 1_000_000
    )


def test_gate_spend_sums_used_plus_attempted(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date

    from evallab import database

    observed: list[str] = []

    class Result:
        def fetchone(self):
            return (0.42,)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query, parameters=None):
            observed.append(query)
            return Result()

    monkeypatch.setattr(
        database.psycopg, "connect", lambda *_args, **_kwargs: Connection()
    )
    assert database.daily_cost_usd("postgresql://catalog", date(2026, 9, 29)) == 0.42
    # Gate rule: one conservative ceiling over settled usage plus the
    # unresolved-reservation column — never settled usage alone.
    assert "sum(cost_usd)" in observed[0]
    assert "sum(attempted_cost_usd)" in observed[0]


def test_finalize_writes_authoritative_cost_block(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from evallab import runner as runner_module
    from evallab.execution_contracts import HarborProcessResult, RunRequest

    jobs_dir = tmp_path / "runs"
    job_dir = jobs_dir / "cost-job"
    job_dir.mkdir(parents=True)
    log_path = jobs_dir / ".executor" / "cost-job.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch()
    request = RunRequest(
        task=tmp_path / "task",
        agent="terminus-2",
        model="zai/glm-5.3-flash",
        name="cost-job",
        jobs_dir=jobs_dir,
    )
    process = HarborProcessResult(
        returncode=0,
        timed_out=False,
        log_path=log_path,
        proxy_usage=_v1_ledger(),
    )
    started = datetime(2026, 9, 29, tzinfo=UTC)
    runner_module._write_run_metadata(
        request,
        repo_root=tmp_path,
        command=["harbor", "run"],
        started=started,
        finished=started,
        process=process,
    )
    metadata = json.loads((job_dir / "lab-metadata.json").read_text())
    assert metadata["cost"]["source"] == "proxy_ledger_x_pinned_price"
    assert metadata["cost"]["cost_usd"] == pytest.approx(
        _ceil_micros(1000, 500) / 1_000_000
    )
    assert metadata["cost"]["attempted_cost_usd"] == pytest.approx(
        _ceil_micros(4000, 4096) / 1_000_000
    )
    assert metadata["cost"]["reason"] is None


def test_report_prefers_ledger_over_harbor_estimate() -> None:
    from evallab.interpretation.run_report import _tokens_and_cost

    result = {
        "agent_result": {
            "n_input_tokens": 999999,
            "n_output_tokens": 999,
            "cost_usd": 0.5,
        }
    }
    lab_metadata = {
        "cost": {
            "cost_usd": 0.0004,
            "attempted_cost_usd": 0.002,
            "source": "proxy_ledger_x_pinned_price",
            "pricing": dict(FLASH_PRICING),
            "reason": None,
        }
    }
    _, cost, _ = _tokens_and_cost(result, {}, [], lab_metadata)
    assert cost["total_usd"] == pytest.approx(0.0004)
    assert cost["source"] == "proxy_ledger"
    assert cost["ledger"]["attempted_cost_usd"] == pytest.approx(0.002)
    _, fallback, _ = _tokens_and_cost(result, {}, [], None)
    assert (fallback["total_usd"], fallback["source"]) == (0.5, "result_json")
