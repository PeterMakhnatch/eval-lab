from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from evallab.analysis_control import (
    CONTROL_VIEW_NAMES,
    materialize_analysis_control_views,
    query_control_view,
)
from evallab.autonomous_research import ResearchRunTraceV1
from evallab.cli import run_cli
from evallab.interpretation.feature_registry import (
    TRAJECTORY_FEATURE_REGISTRY,
    predictor_eligibility_summary,
)
from evallab.profiles import AgentProfile, builtin_profiles, evaluate_profile_readiness
from evallab.registry import compute_task_digests
from evallab.schemas import AgentReadinessRecord

REPO_ROOT = Path(__file__).resolve().parents[1]


def _offline_readiness(profile: AgentProfile) -> AgentReadinessRecord:
    return evaluate_profile_readiness(
        profile,
        root=REPO_ROOT,
        is_installed_fn=lambda _: False,
    )


def _digest_json(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def test_materializes_all_stable_control_views() -> None:
    with duckdb.connect(":memory:") as connection:
        result = materialize_analysis_control_views(
            connection,
            root=REPO_ROOT,
            readiness_evaluator=_offline_readiness,
        )
        assert result.readiness_profiles == len(builtin_profiles())
        assert result.calibration_runs == 2
        assert result.outcome_facts == 4
        assert result.activation_rows == 2 * len(TRAJECTORY_FEATURE_REGISTRY.all_features())
        assert result.predictor_rows == len(TRAJECTORY_FEATURE_REGISTRY.all_features())

        for view_name in CONTROL_VIEW_NAMES:
            connection.execute(f"SELECT * FROM {view_name} LIMIT 1").fetchall()


def test_bbo_and_game2048_authority_bindings() -> None:
    with duckdb.connect(":memory:") as connection:
        materialize_analysis_control_views(
            connection,
            root=REPO_ROOT,
            readiness_evaluator=_offline_readiness,
        )

        composite = {
            row[0]: row
            for row in connection.execute(
                "SELECT trial_id, agent_axis, verifier_axis, artifact_axis, "
                "authority_axis, resolved_reward, is_admissible_for_aggregation "
                "FROM v_composite_outcome_validity"
            ).fetchall()
        }
        bbo_row = composite["bbo_noisy_continuous__y6S5nSJ"]
        assert bbo_row[1:5] == ("timed_out", "completed", "preserved", "original_verifier_authoritative")
        assert bbo_row[5] == pytest.approx(0.18143598030936073)
        assert bbo_row[6] is True

        game_row = composite["game2048_policy_search__QzNuUbN"]
        assert game_row[1:5] == ("timed_out", "regrade_valid", "preserved", "regrade_authoritative")
        assert game_row[5] == pytest.approx(0.3780081907722421)
        assert game_row[6] is True

        rewards = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT trial_id, authoritative_reward, is_authoritative_summable, "
                "superseded_count, superseded_synthetic_count, is_disputed, "
                "refusal_reason FROM v_reward_authority"
            ).fetchall()
        }
        bbo_reward = rewards["bbo_noisy_continuous__y6S5nSJ"]
        assert bbo_reward[0] == pytest.approx(0.18143598030936073)
        assert bbo_reward[1] is True
        assert bbo_reward[2] == 0
        assert bbo_reward[3] == 0
        assert bbo_reward[4] is False
        assert bbo_reward[5] is None

        game_reward = rewards["game2048_policy_search__QzNuUbN"]
        assert game_reward[0] == pytest.approx(0.3780081907722421)
        assert game_reward[1] is True
        assert game_reward[2] == 2
        assert game_reward[3] == 1
        assert game_reward[4] is False
        assert game_reward[5] is None


def test_headline_scale_and_selection_refusals() -> None:
    with duckdb.connect(":memory:") as connection:
        materialize_analysis_control_views(
            connection,
            root=REPO_ROOT,
            readiness_evaluator=_offline_readiness,
        )

        bbo_headline = connection.execute(
            "SELECT headline_visible_scalar, selected_visible_value, binding_complete "
            "FROM v_headline_binding "
            "WHERE run_id = 'bbo_noisy_continuous__y6S5nSJ'"
        ).fetchone()
        assert bbo_headline == ("selected", pytest.approx(0.2047548822462361), True)

        scale = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT run_id, arithmetic_permitted, visible_hidden_transfer_gap, "
                "scale_refusal_reason, scale_binding_status FROM v_scale_binding_status"
            ).fetchall()
        }
        # Without explicit resolved artifacts, checked-in BBO truthfully reports unresolved and refuses arithmetic
        bbo_scale = scale["bbo_noisy_continuous__y6S5nSJ"]
        assert bbo_scale[0] is False
        assert bbo_scale[1] is None
        assert "unresolved_components" in (bbo_scale[2] or "")
        assert bbo_scale[3] == "unresolved"

        game_scale = scale["game2048_policy_search__QzNuUbN"]
        assert game_scale[0] is False
        assert game_scale[1] is None
        assert "no validated score-scale binding" in (game_scale[2] or "")
        assert game_scale[3] == "not_provided"

        selections = connection.execute(
            "SELECT run_id, selection_reconstructible, selection_refusal_reason "
            "FROM v_selection_reconstructibility ORDER BY run_id"
        ).fetchall()
        assert len(selections) == 2
        assert all(row[1] is False for row in selections)
        assert all("selected_version_unlogged" in row[2] for row in selections)


def test_feature_activation_and_predictor_governance() -> None:
    with duckdb.connect(":memory:") as connection:
        materialize_analysis_control_views(
            connection,
            root=REPO_ROOT,
            readiness_evaluator=_offline_readiness,
        )
        activation_counts = dict(
            connection.execute(
                "SELECT activation_status, count(*) "
                "FROM v_feature_activation_map GROUP BY activation_status"
            ).fetchall()
        )
        assert sum(activation_counts.values()) == 2 * len(
            TRAJECTORY_FEATURE_REGISTRY.all_features()
        )
        assert activation_counts["populated"] > 0
        assert activation_counts["zero"] > 0
        assert activation_counts["dormant"] > 0
        assert (
            activation_counts.get("null_with_denominator", 0)
            + activation_counts.get("null_not_applicable", 0)
            > 0
        )

        summary = predictor_eligibility_summary()
        assert summary.total_features == 260
        assert summary.eligible_predictors == 191
        assert summary.refused_predictors == 69
        assert summary.missing_temporal_count == 0
        assert summary.missing_denominator_count == 0
        assert summary.undeclared_coupling_count == 0


def test_query_control_view_rejects_unknown_names() -> None:
    with duckdb.connect(":memory:") as connection:
        materialize_analysis_control_views(
            connection,
            root=REPO_ROOT,
            readiness_evaluator=_offline_readiness,
        )
        rows = query_control_view(connection, "v_headline_binding", limit=1)
        assert len(rows) == 1
        with pytest.raises(ValueError, match="unknown control view"):
            query_control_view(connection, "not_a_view")


def test_analysis_control_cli_queries_a_stable_view(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            ["analyze", "control", "v_reward_authority", "--limit", "1"],
            workspace=REPO_ROOT,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["view"] == "v_reward_authority"
    assert payload["row_count"] == 1
    assert payload["materialization"] == {
        "activation_rows": 520,
        "calibration_runs": 2,
        "outcome_facts": 4,
        "predictor_rows": 260,
        "readiness_profiles": len(builtin_profiles()),
    }


def test_analysis_control_cli_queries_v_scale_binding_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            ["analyze", "control", "v_scale_binding_status", "--limit", "2"],
            workspace=REPO_ROOT,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["view"] == "v_scale_binding_status"
    assert payload["row_count"] == 2
    rows = {r["run_id"]: r for r in payload["rows"]}
    assert "bbo_noisy_continuous__y6S5nSJ" in rows
    assert "game2048_policy_search__QzNuUbN" in rows
    assert rows["bbo_noisy_continuous__y6S5nSJ"]["arithmetic_permitted"] is False
    assert rows["bbo_noisy_continuous__y6S5nSJ"]["scale_binding_status"] == "unresolved"


def test_materialize_views_with_exact_synthetic_fixtures_permits_arithmetic(
    tmp_path: Path,
) -> None:
    """Providing a complete exact artifact resolver authorizes transfer arithmetic in materialized control views."""
    task_dir = tmp_path / "syn_task"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("name = 'syn_task'\n", encoding="utf-8")
    (task_dir / "instruction.md").write_text("# Syn Task\n", encoding="utf-8")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_syn.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    task_digests = compute_task_digests(task_dir)
    metric_cfg = {"metric": "accuracy", "k": 1}
    vis_outcome = {"score": 0.8}
    hid_outcome = {"score": 0.9}

    binding = {
        "authority_kind": "benchmark_contract",
        "direction": "higher",
        "metric_name": "accuracy",
        "schema_version": "score-scale-binding/v1",
        "task_digest": task_digests.package,
        "verifier_digest": task_digests.verifier,
        "metric_config_digest": _digest_json(metric_cfg),
        "visible_split_id": "val",
        "hidden_split_id": "test",
        "visible_outcome_binding_digest": _digest_json(vis_outcome),
        "hidden_outcome_binding_digest": _digest_json(hid_outcome),
    }
    binding_body = {
        "authority_kind": "benchmark_contract",
        "direction": "higher",
        "hidden_outcome_binding_digest": _digest_json(hid_outcome),
        "hidden_split_id": "test",
        "metric_config_digest": _digest_json(metric_cfg),
        "metric_name": "accuracy",
        "task_digest": task_digests.package,
        "verifier_digest": task_digests.verifier,
        "visible_outcome_binding_digest": _digest_json(vis_outcome),
        "visible_split_id": "val",
    }
    binding["binding_digest"] = _digest_json(binding_body)

    evidence_data = {
        "schema_version": "evallab-rsi-calibration-evidence/v1",
        "autonomous_research_trace": {
            "schema_version": "research-run-trace/v1",
            "run_id": "syn-run-1",
            "benchmark_family": "synthetic/eval",
            "source_kind": "harbor",
            "source_version": "v1",
            "source_digest": "sha256:" + "1" * 64,
            "task_digest": task_digests.package,
            "verifier_digest": task_digests.verifier,
            "metric_config_digest": _digest_json(metric_cfg),
            "visible_outcome_binding_digest": _digest_json(vis_outcome),
            "hidden_outcome_binding_digest": _digest_json(hid_outcome),
            "baseline_visible_score": 0.5,
            "score_direction": "higher",
            "score_scale_binding": binding,
            "hidden_score": 0.9,
            "selected_iteration_id": "v1",
            "iterations": [
                {
                    "schema_version": "research-iteration/v1",
                    "iteration_id": "v1",
                    "visible_score": 0.8,
                    "disposition": "kept",
                }
            ],
        },
        "scores": {
            "visible": {"selected": 0.8},
            "sealed": {"reward": 0.9},
        },
    }

    evidence_file = tmp_path / "syn_evidence.json"
    evidence_file.write_text(json.dumps(evidence_data), encoding="utf-8")

    policy_data = {
        "schema_version": "analysis-bindings/v1",
        "bindings": [
            {
                "run_id": "syn-run-1",
                "evidence_path": str(evidence_file),
                "headline_visible_scalar": "selected",
                "visible_alternatives": ["selected"],
            }
        ],
    }
    policy_file = tmp_path / "policy/analysis-bindings.yaml"
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    import yaml

    policy_file.write_text(yaml.dump(policy_data), encoding="utf-8")

    # Copy sql/views.sql so tmp_path is a valid self-contained root
    (tmp_path / "sql").mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(REPO_ROOT / "sql/views.sql", tmp_path / "sql/views.sql")

    # Supply exact complete resolver
    def exact_resolver(trace: ResearchRunTraceV1) -> dict[str, Any]:
        return {
            "task_dir": task_dir,
            "metric_config": metric_cfg,
            "visible_outcome": vis_outcome,
            "hidden_outcome": hid_outcome,
        }

    with duckdb.connect(":memory:") as connection:
        materialize_analysis_control_views(
            connection,
            root=tmp_path,
            readiness_evaluator=_offline_readiness,
            artifact_resolver=exact_resolver,
        )
        row = connection.execute(
            "SELECT run_id, score_scale_compatible, arithmetic_permitted, "
            "visible_hidden_transfer_gap, scale_refusal_reason, scale_binding_status, "
            "scale_binding_task_status, scale_binding_verifier_status, "
            "scale_binding_metric_config_status, scale_binding_visible_outcome_status, "
            "scale_binding_hidden_outcome_status "
            "FROM v_scale_binding_status WHERE run_id = 'syn-run-1'"
        ).fetchone()

        assert row is not None
        assert row[0] == "syn-run-1"
        assert row[1] is True  # score_scale_compatible
        assert row[2] is True  # arithmetic_permitted
        assert row[3] == pytest.approx(0.1)  # 0.9 - 0.8
        assert row[4] is None  # scale_refusal_reason is None on success
        assert row[5] == "verified"  # scale_binding_status
        assert row[6] == "verified"  # task_status
        assert row[7] == "verified"  # verifier_status
        assert row[8] == "verified"  # metric_config_status
        assert row[9] == "verified"  # visible_outcome_status
        assert row[10] == "verified"  # hidden_outcome_status


def test_materialize_views_with_malformed_resolver_records_typed_reason(
    tmp_path: Path,
) -> None:
    """A malformed resolver return fails closed with typed reason instead of untyped AttributeError."""

    def bad_resolver(trace: ResearchRunTraceV1) -> Any:
        return [1, 2, 3]  # list instead of Mapping

    with duckdb.connect(":memory:") as connection:
        materialize_analysis_control_views(
            connection,
            root=REPO_ROOT,
            readiness_evaluator=_offline_readiness,
            artifact_resolver=bad_resolver,
        )
        row = connection.execute(
            "SELECT run_id, score_scale_compatible, arithmetic_permitted, "
            "visible_hidden_transfer_gap, scale_refusal_reason, scale_binding_status "
            "FROM v_scale_binding_status WHERE run_id = 'bbo_noisy_continuous__y6S5nSJ'"
        ).fetchone()
        assert row is not None
        assert row[1] is False
        assert row[2] is False
        assert row[3] is None
        assert "artifact_resolver_invalid_return" in (row[4] or "")
        assert row[5] == "unresolved"


def test_materialize_views_with_mismatched_resolver_records_typed_mismatch(
    tmp_path: Path,
) -> None:
    """A resolver providing mismatched metric configuration records typed mismatch."""

    def mismatch_resolver(trace: ResearchRunTraceV1) -> dict[str, Any]:
        return {
            "metric_config": {"metric": "wrong_metric_name"},
        }

    with duckdb.connect(":memory:") as connection:
        materialize_analysis_control_views(
            connection,
            root=REPO_ROOT,
            readiness_evaluator=_offline_readiness,
            artifact_resolver=mismatch_resolver,
        )
        row = connection.execute(
            "SELECT run_id, score_scale_compatible, arithmetic_permitted, "
            "visible_hidden_transfer_gap, scale_refusal_reason, scale_binding_status "
            "FROM v_scale_binding_status WHERE run_id = 'bbo_noisy_continuous__y6S5nSJ'"
        ).fetchone()
        assert row is not None
        assert row[1] is False
        assert row[2] is False
        assert row[3] is None
        assert "mismatched_components" in (row[4] or "")
        assert row[5] == "mismatch"
