"""Tests for evallab.request_accounting.

Covers:
1. Success with cache usage (raw Anthropic cache fields, completeness, cost is None).
2. Error with null usage (missing fields stay null, no zero-fill).
3. Late receipt updating the same attempt (same record, late=True, outcome preserved).
4. Role split actor / image_controller with role_summary.
5. Unresolved trial reference yields trial_reference=None + unresolved_trial=True.
6. Export round-trip through Parquet and manifest (sorted keys, no timestamps, nulls survive).
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from evallab.request_accounting import (
    DEFAULT_CONTRACT_SHA256,
    RequestAttempt,
    apply_late_receipt,
    export_request_accounting,
    load_development_usage,
    load_exported_accounting,
    load_replay_attempts,
    role_summary,
)


def test_success_with_cache_usage(tmp_path: Path) -> None:
    fixture_data = {
        "evidence_level": "synthetic_http_replay",
        "scenarios": {
            "success_with_cache_usage": {
                "raw_usage": {
                    "outcome": "success",
                    "transport_state": "response_received",
                    "http_attempts": 1,
                    "http_status": 200,
                    "usage_source": "provider_http_json",
                    "uncached_input_tokens": 10,
                    "cache_creation_tokens": 3,
                    "cached_tokens": 7,
                    "prompt_tokens": 20,
                    "completion_tokens": 5,
                    "error_type": None,
                    "late": False,
                },
                "complete_by_field": {
                    "prompt_tokens": True,
                    "completion_tokens": True,
                    "cached_tokens": True,
                },
            }
        },
    }
    fixture_file = tmp_path / "replay_success.json"
    fixture_file.write_text(json.dumps(fixture_data))

    attempts = load_replay_attempts(fixture_file)
    assert len(attempts) == 1
    a = attempts[0]

    assert a.role == "actor"
    assert a.outcome == "success"
    assert a.transport_state == "response_received"
    assert a.http_status == 200
    assert a.uncached_input_tokens == 10
    assert a.cache_creation_tokens == 3
    assert a.cached_tokens == 7
    assert a.prompt_tokens == 20
    assert a.completion_tokens == 5
    assert a.error_type is None
    assert a.late is False
    assert a.cost is None  # Cost is always None
    assert a.trial_reference is None
    assert a.evidence_level == "synthetic_http_replay"
    assert a.complete_by_field["prompt_tokens"] is True


def test_error_with_null_usage_stays_null(tmp_path: Path) -> None:
    fixture_data = {
        "evidence_level": "synthetic_http_replay",
        "scenarios": {
            "null_or_missing_usage": {
                "usage_null": {
                    "raw_usage": {
                        "outcome": "error",
                        "transport_state": "response_received",
                        "http_attempts": 1,
                        "http_status": 200,
                        "usage_source": None,
                        "uncached_input_tokens": None,
                        "cache_creation_tokens": None,
                        "cached_tokens": None,
                        "prompt_tokens": None,
                        "completion_tokens": None,
                        "error_type": "APIConnectionError",
                        "late": False,
                    }
                }
            }
        },
    }
    fixture_file = tmp_path / "replay_error.json"
    fixture_file.write_text(json.dumps(fixture_data))

    attempts = load_replay_attempts(fixture_file)
    assert len(attempts) == 1
    a = attempts[0]

    assert a.outcome == "error"
    assert a.error_type == "APIConnectionError"
    # All token counts must stay null/None; never coerced to zero
    assert a.uncached_input_tokens is None
    assert a.cache_creation_tokens is None
    assert a.cached_tokens is None
    assert a.prompt_tokens is None
    assert a.completion_tokens is None
    assert a.cost is None


def test_late_receipt_updates_same_attempt() -> None:
    initial = RequestAttempt(
        source_file="simulated.json",
        record_path="calls[0]",
        role="actor",
        attempt_ordinal=1,
        outcome="local_timeout_remote_unknown",
        transport_state="sent_remote_unknown",
        http_attempts=1,
        http_status=None,
        usage_source=None,
        prompt_tokens=None,
        completion_tokens=None,
        cached_tokens=None,
        late=False,
    )
    assert initial.late is False
    assert initial.prompt_tokens is None

    updated = apply_late_receipt(
        initial,
        http_status=200,
        usage_source="provider_http_json",
        uncached_input_tokens=10,
        cache_creation_tokens=3,
        cached_tokens=7,
        prompt_tokens=20,
        completion_tokens=5,
        transport_state="response_received",
    )

    # Late receipt updates the same attempt: late=True, outcome stays remote-unknown
    assert updated.attempt_ordinal == initial.attempt_ordinal
    assert updated.outcome == "local_timeout_remote_unknown"
    assert updated.late is True
    assert updated.transport_state == "response_received"
    assert updated.http_status == 200
    assert updated.prompt_tokens == 20
    assert updated.completion_tokens == 5
    assert updated.cached_tokens == 7
    assert updated.cost is None


def test_role_split_actor_and_image_controller() -> None:
    actor_attempt = RequestAttempt(
        source_file="simulated.json",
        record_path="calls[0]",
        role="actor",
        attempt_ordinal=1,
        outcome="success",
        prompt_tokens=100,
        completion_tokens=20,
        cached_tokens=50,
    )
    controller_attempt = RequestAttempt(
        source_file="simulated.json",
        record_path="calls[1]",
        role="image_controller",
        attempt_ordinal=2,
        outcome="success",
        prompt_tokens=500,
        completion_tokens=40,
        cached_tokens=0,
    )

    summary = role_summary([actor_attempt, controller_attempt])
    assert "actor" in summary
    assert "image_controller" in summary

    assert summary["actor"]["attempts"] == 1
    assert summary["actor"]["known_prompt_tokens"] == 100
    assert summary["actor"]["known_completion_tokens"] == 20
    assert summary["actor"]["known_cached_tokens"] == 50

    assert summary["image_controller"]["attempts"] == 1
    assert summary["image_controller"]["known_prompt_tokens"] == 500
    assert summary["image_controller"]["known_completion_tokens"] == 40
    assert summary["image_controller"]["known_cached_tokens"] == 0

    # Role validation rejects unauthorized roles
    with pytest.raises(AssertionError):
        RequestAttempt(
            source_file="simulated.json",
            record_path="calls[2]",
            role="unauthorized_role",
            attempt_ordinal=3,
            outcome="success",
        )


def test_unresolved_trial_reference(tmp_path: Path) -> None:
    dev_data = {
        "evidence_level": "existing_native_model_trials_not_new_experiments",
        "rows": [
            {
                "trial_path": "/path/does/not/exist/canary/event-summary__missing",
                "trial_id": "fake-trial-id",
                "task": "event-summary",
                "reward": 1.0,
                "exception": None,
                "step_count": 9,
                "reported_usage": {
                    "n_input_tokens": 100,
                    "n_output_tokens": 20,
                    "n_cache_tokens": 80,
                    "cost_usd": 0.05,
                },
            }
        ],
    }
    dev_file = tmp_path / "dev_usage.json"
    dev_file.write_text(json.dumps(dev_data))

    attempts = load_development_usage(dev_file, repo_root=tmp_path)
    assert len(attempts) == 1
    a = attempts[0]

    # Non-existent trial must yield unresolved flag and None references; never invent identity
    assert a.unresolved_trial is True
    assert a.trial_reference is None
    assert a.trial_id is None
    assert a.job_id is None
    assert a.cost is None  # Reported cost_usd is not imported into cost
    assert a.prompt_tokens == 100
    assert a.atif_step_count == 9

    # Sealed task_000009 must never appear
    sealed_data = {
        "rows": [
            {
                "trial_path": "/some/path",
                "task": "task_000009",
            }
        ]
    }
    sealed_file = tmp_path / "sealed.json"
    sealed_file.write_text(json.dumps(sealed_data))
    with pytest.raises(AssertionError, match="task_000009"):
        load_development_usage(sealed_file, repo_root=tmp_path)


def test_export_roundtrip_parquet_and_manifest(tmp_path: Path) -> None:
    attempts = [
        RequestAttempt(
            source_file="test_source.json",
            record_path="calls[0]",
            role="actor",
            attempt_ordinal=1,
            outcome="success",
            http_status=200,
            prompt_tokens=100,
            completion_tokens=25,
            cached_tokens=75,
            uncached_input_tokens=25,
            cache_creation_tokens=None,
            error_type=None,
            late=False,
            complete_by_field={"prompt_tokens": True, "completion_tokens": True},
        ),
        RequestAttempt(
            source_file="test_source.json",
            record_path="calls[1]",
            role="actor",
            attempt_ordinal=2,
            outcome="error",
            http_status=500,
            prompt_tokens=None,
            completion_tokens=None,
            cached_tokens=None,
            error_type="InternalServerError",
            late=False,
        ),
    ]

    out_root = tmp_path / "accounting_export"
    summary = export_request_accounting(attempts, output_root=out_root)

    assert summary.parquet_path.is_file()
    assert summary.manifest_path.is_file()
    assert summary.row_count == 2

    # Manifest checks: contract sha recorded, sorted keys, no timestamps
    manifest_raw = summary.manifest_path.read_text()
    manifest_dict = json.loads(manifest_raw)

    assert manifest_dict["contract_sha256"] == DEFAULT_CONTRACT_SHA256
    assert manifest_dict["row_count"] == 2
    assert manifest_dict["per_role_attempt_counts"] == {"actor": 2, "image_controller": 0}
    assert manifest_dict["source_paths"] == ["test_source.json"]

    # Manifest keys are strictly sorted
    assert list(manifest_dict.keys()) == sorted(manifest_dict.keys())

    # Manifest must not contain timestamps
    forbidden_time_tokens = ("created_at", "timestamp", "datetime", "mtime", "generated_at")
    for key in manifest_dict:
        assert not any(tok in key.lower() for tok in forbidden_time_tokens)

    # Null field inventory counts missing fields accurately
    null_inv = manifest_dict["null_field_inventory"]
    assert null_inv["prompt_tokens"] == 1
    assert null_inv["completion_tokens"] == 1
    assert null_inv["cached_tokens"] == 1
    assert null_inv["cost"] == 2  # cost is null in all 2 rows
    assert null_inv["cache_creation_tokens"] == 2
    assert null_inv["error_type"] == 1

    # Round-trip read back from Parquet
    reloaded = load_exported_accounting(out_root)
    assert len(reloaded) == 2

    # Verify nulls survived Parquet roundtrip as null, not zero
    assert reloaded[0].prompt_tokens == 100
    assert reloaded[0].cost is None
    assert reloaded[1].prompt_tokens is None
    assert reloaded[1].completion_tokens is None
    assert reloaded[1].cached_tokens is None
    assert reloaded[1].cost is None
    assert reloaded[1].error_type == "InternalServerError"
