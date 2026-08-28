"""Re-exports for feature registry and validation contracts."""

from evallab.interpretation.feature_registry import (
    TRAJECTORY_FEATURE_REGISTRY,
    FeatureCategory,
    FeatureDataType,
    FeatureDefinition,
    FeatureRegistry,
    register_trajectory_feature,
    verify_feature_registry,
)

__all__ = [
    "FeatureCategory",
    "FeatureDataType",
    "FeatureDefinition",
    "FeatureRegistry",
    "TRAJECTORY_FEATURE_REGISTRY",
    "register_trajectory_feature",
    "verify_feature_registry",
]
