from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from evallab.analysis_control import (
    CONTROL_VIEW_NAMES,
    materialize_analysis_control_views,
    query_control_view,
)
from evallab.cli import run_cli
from evallab.interpretation.feature_registry import (
    TRAJECTORY_FEATURE_REGISTRY,
    predictor_eligibility_summary,
)
from evallab.profiles import AgentProfile, builtin_profiles, evaluate_profile_readiness
from evallab.schemas import AgentReadinessRecord

REPO_ROOT = Path(__file__).resolve().parents[1]


def _offline_readiness(profile: AgentProfile) -> AgentReadinessRecord:
    return evaluate_profile_readiness(
        profile,
        root=REPO_ROOT,
        is_installed_fn=lambda _: False,
    )


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
        bbo = composite["bbo_noisy_continuous__y6S5nSJ"]
        assert bbo[1:5] == (
            "timed_out",
            "completed",
            "preserved",
            "original_verifier_authoritative",
        )
        assert bbo[5] == pytest.approx(0.18143598030936073)
        assert bbo[6] is True

        game = composite["game2048_policy_search__QzNuUbN"]
        assert game[1:5] == (
            "timed_out",
            "regrade_valid",
            "preserved",
            "regrade_authoritative",
        )
        assert game[5] == pytest.approx(0.37800819)
        assert game[6] is True

        game_reward = connection.execute(
            "SELECT authoritative_reward, superseded_count, "
            "superseded_synthetic_count, is_disputed "
            "FROM v_reward_authority "
            "WHERE trial_id = 'game2048_policy_search__QzNuUbN'"
        ).fetchone()
        assert game_reward == (pytest.approx(0.37800819), 2, 1, False)


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
                "scale_refusal_reason FROM v_scale_binding_status"
            ).fetchall()
        }
        assert scale["bbo_noisy_continuous__y6S5nSJ"] == (
            True,
            pytest.approx(-0.023318901936875358),
            None,
        )
        game_scale = scale["game2048_policy_search__QzNuUbN"]
        assert game_scale[0] is False
        assert game_scale[1] is None
        assert "no validated score-scale binding" in game_scale[2]

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
