from evallab.interpretation.feature_registry import TRAJECTORY_FEATURE_REGISTRY


def test_unlanded_autonomous_research_family_is_not_registered() -> None:
    assert TRAJECTORY_FEATURE_REGISTRY.by_family("autonomous-research-v1") == {}
