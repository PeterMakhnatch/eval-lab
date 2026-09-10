from __future__ import annotations

import re
import pyarrow as pa
import pytest

from evallab.interpretation.feature_registry import (
    TRAJECTORY_FEATURE_REGISTRY,
    FeatureDefinition,
)
from evallab.traj import TRAJ_FEATURES_PARQUET_SCHEMA

# Mapping from FeatureDefinition.data_type to Arrow type names
REGISTRY_TO_ARROW_TYPE_MAP: dict[str, set[str]] = {
    "BIGINT": {"int64"},
    "VARCHAR": {"string"},
    "DOUBLE": {"double", "float64"},
    "BOOLEAN": {"bool", "boolean"},
}

_IDENTIFIER_PATTERN = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b")

_KEYWORDS_AND_OPERATORS = {
    "when", "if", "else", "round", "regr_slope", "over", "sum", "min", "max",
    "count", "null", "set", "by", "default", "never", "true", "false", "is",
    "not", "and", "or", "in", "both", "between", "from", "to", "all", "where",
    "for", "float", "int", "str", "coalesce", "case", "then", "end",
}


def test_schema_fields_are_registered_and_conforming() -> None:
    """Every field in TRAJ_FEATURES_PARQUET_SCHEMA must be registered in TRAJECTORY_FEATURE_REGISTRY.
    
    Additionally, every registered trajectory feature that claims source_table='traj_features'
    and represents an emitted column in the traj_features Parquet file must exist in the Arrow schema.
    """
    schema_fields = set(TRAJ_FEATURES_PARQUET_SCHEMA.names)
    registered_features = TRAJECTORY_FEATURE_REGISTRY.all_features()

    # 1. Every field in the Arrow schema MUST be registered in the feature registry
    unregistered_schema_fields = schema_fields - set(registered_features.keys())
    assert not unregistered_schema_fields, (
        f"The following Arrow schema fields in TRAJ_FEATURES_PARQUET_SCHEMA are not registered "
        f"in TRAJECTORY_FEATURE_REGISTRY: {sorted(unregistered_schema_fields)}"
    )

    # 2. Check that registered features claiming to be in traj_features exist
    # Note: 8 legacy screening rates in the registry claim source_table='traj_features' because
    # they were registered before Phase 1 split into v_trace_baseline, but are only materialized
    # in v_trace_baseline/TRACE_BASELINE_PARQUET_SCHEMA.
    v_trace_baseline_only = {
        "linear_innocence_screening",
        "tool_error_rate_screening",
        "recovery_rate_screening",
        "autonomous_step_ratio_screening",
        "assisted_step_ratio_screening",
        "cache_hit_rate_screening",
        "subagent_overhead_ratio_screening",
        "total_tokens",
    }
    traj_claimants = {
        name
        for name, feat in registered_features.items()
        if feat.source_table == "traj_features" and name not in v_trace_baseline_only
    }
    unmatched_traj_features = traj_claimants - schema_fields
    assert not unmatched_traj_features, (
        f"The following features registered with source_table='traj_features' do not exist "
        f"in TRAJ_FEATURES_PARQUET_SCHEMA: {sorted(unmatched_traj_features)}"
    )


def test_registry_data_types_agree_with_arrow_schema() -> None:
    """Registry data_type must agree with Arrow field types for all shared columns."""
    schema_fields: dict[str, pa.Field] = {
        f.name: f for f in TRAJ_FEATURES_PARQUET_SCHEMA
    }
    registered_features = TRAJECTORY_FEATURE_REGISTRY.all_features()

    mismatches: list[str] = []
    for col_name, field in schema_fields.items():
        feat: FeatureDefinition | None = registered_features.get(col_name)
        if feat is None:
            continue

        reg_type = feat.data_type
        if reg_type not in REGISTRY_TO_ARROW_TYPE_MAP:
            pytest.fail(f"Unknown registry data_type {reg_type!r} for column {col_name!r}")

        arrow_type_str = str(field.type)
        valid_arrow_types = REGISTRY_TO_ARROW_TYPE_MAP[reg_type]
        if arrow_type_str not in valid_arrow_types:
            mismatches.append(
                f"{col_name}: registry data_type={reg_type!r} (expected {valid_arrow_types}) "
                f"!= Arrow type {arrow_type_str!r}"
            )

    assert not mismatches, f"Data type mismatches found:\n" + "\n".join(mismatches)


def test_registry_formulas_reference_valid_schema_columns() -> None:
    """Formula strings referencing other column names only reference columns that exist.
    
    Parses identifiers from formula_or_rule conservatively.
    Checks tokens that look like snake_case identifiers AND appear in the registry namespace,
    verifying they exist in the schema namespace.
    """
    schema_fields = set(TRAJ_FEATURES_PARQUET_SCHEMA.names)
    registered_features = TRAJECTORY_FEATURE_REGISTRY.all_features()

    # The universe of known feature names across registry and schema
    known_namespaces = set(registered_features.keys()) | schema_fields

    invalid_references: list[str] = []

    # Features that belong to traj_features (materialized on disk)
    for name, feat in registered_features.items():
        if feat.source_table != "traj_features":
            continue

        formula = feat.formula_or_rule
        # Only check formulas that look like algebraic / column expressions (contain / or + or *)
        if not any(op in formula for op in ("/", "+", "-", "*")):
            continue

        tokens = set(_IDENTIFIER_PATTERN.findall(formula))
        candidate_cols = {
            t.lower() for t in tokens
            if t.lower() not in _KEYWORDS_AND_OPERATORS
            and not t.isdigit()
            and t.lower() in known_namespaces
        }

        for ref_col in candidate_cols:
            if ref_col not in schema_fields:
                invalid_references.append(
                    f"Feature {name!r} formula {formula!r} references unknown column {ref_col!r}"
                )

    assert not invalid_references, (
        f"Registry formulas contain references to invalid/missing schema columns:\n"
        + "\n".join(invalid_references)
    )
