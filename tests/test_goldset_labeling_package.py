"""Pytest wrapper bringing the gold-set package checks into CI.

The standalone script `research/goldset/test_labeling_package.py` was outside both
pytest and ty coverage, so its "92 checks pass" was local-only evidence. This
module runs the same suite under pytest and adds focused assertions on the
invariants a reviewer would otherwise have to take on trust.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDSET = REPO_ROOT / "research" / "goldset"
SCRIPT = GOLDSET / "test_labeling_package.py"
PACKAGE = GOLDSET / "labeling_package.json"
TRUTH = GOLDSET / "machine_truth_WITHHELD.json"

pytestmark = pytest.mark.skipif(
    not PACKAGE.is_file(), reason="gold-set package artifacts not present"
)


def _serialize(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


@pytest.fixture(scope="module")
def package() -> dict:
    return json.loads(PACKAGE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def truth() -> dict:
    return json.loads(TRUTH.read_text(encoding="utf-8"))


def test_standalone_check_suite_passes() -> None:
    """Run the full standalone suite; surface its output on failure."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all checks passed" in result.stdout


def test_readiness_is_not_ready(package: dict) -> None:
    assert package["readiness"]["readiness"] == "NOT_READY"


def test_no_ratings_present(package: dict) -> None:
    assert all("ratings" not in item for item in package["items"])


def test_machine_truth_absent_from_rater_items(package: dict) -> None:
    blob = json.dumps(package["items"])
    for forbidden in ("prior_error_visible", "machine_facts", "had_error_signal"):
        assert forbidden not in blob


def test_census_cluster_keys_match_item_cluster_ids(package: dict) -> None:
    """Census must key on the LOGICAL cluster id, not the raw byte sha."""
    census_keys = set(package["census"]["agent_steps_per_cluster"])
    item_clusters = {item["cluster_id"] for item in package["items"]}
    assert census_keys == item_clusters


def test_build_id_is_recomputable_from_written_pair(package: dict, truth: dict) -> None:
    """Closed formula: excludes build_id from both, and package_digest from pkg."""
    stripped_pkg = {k: v for k, v in package.items() if k not in ("build_id", "package_digest")}
    stripped_truth = {k: v for k, v in truth.items() if k != "build_id"}
    expected = hashlib.sha256(
        (_serialize(stripped_pkg) + _serialize(stripped_truth)).encode("utf-8")
    ).hexdigest()
    assert expected == package["build_id"]
    assert package["build_id"] == truth["build_id"]


def test_package_digest_is_recomputable(package: dict) -> None:
    stripped = {k: v for k, v in package.items() if k != "package_digest"}
    expected = hashlib.sha256(_serialize(stripped).encode("utf-8")).hexdigest()
    assert expected == package["package_digest"]


def test_completeness_gate_is_enforced(package: dict) -> None:
    """Gate must fire above threshold and stay silent at or below it.

    The corpus is ~756 KB, so nothing truncates and the healthy state is ZERO
    incomplete items. Asserting `items_incomplete > 0` would require the corpus
    to be broken; the gate LOGIC is what matters, in both directions.
    """
    adequacy = package["readiness"]["context_adequacy"]
    fired = any(
        "CONTEXT_INCOMPLETE_TOO_HIGH" in blocker for blocker in package["readiness"]["blockers"]
    )
    above = adequacy["incomplete_fraction"] > adequacy["max_incomplete_fraction"]
    assert fired == above, (adequacy, package["readiness"]["blockers"])


def test_incomplete_items_are_never_deliverable(package: dict) -> None:
    """No item with a known-incomplete context may reach a rater."""
    deliverable = set(package["deliverable_item_ids"])
    for item in package["items"]:
        if item["item_id"] in deliverable:
            assert item["context_completeness"]["builder_verdict"] == "COMPLETE"


def test_items_deduped_by_logical_digest(package: dict) -> None:
    """Semantic clones merged into one item, raw lineage preserved."""
    logicals = [item["logical_step_digest"] for item in package["items"]]
    assert len(logicals) == len(set(logicals))
    assert package["census"]["clone_items_dropped"] > 0
    assert any(len(item["logical_lineage"]) > 1 for item in package["items"])


def test_item_context_digest_covers_the_whole_rater_view(package: dict) -> None:
    """Altering the instruction or a prior step must change the bound digest."""

    def context_digest(context: object) -> str:
        """Independent reimplementation: verifies the FORMULA, not the module."""
        return hashlib.sha256(
            json.dumps(context, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    item = package["items"][0]
    assert context_digest(item["rater_context"]) == item["item_context_digest"]
    tampered_instruction = {
        **item["rater_context"],
        "instruction": {"presumed_task_statement": {"message": "A DIFFERENT TASK"}},
    }
    assert context_digest(tampered_instruction) != item["item_context_digest"]
    tampered_prior = {**item["rater_context"], "prior_steps": []}
    assert context_digest(tampered_prior) != item["item_context_digest"]


def test_nothing_is_truncated_at_current_corpus_size(package: dict) -> None:
    for item in package["items"]:
        views = [
            *item["rater_context"]["prior_steps"],
            item["rater_context"]["item_step"],
        ]
        for view in views:
            assert not view["message_truncated"]
            assert not any(c["arguments_truncated"] for c in view["tool_calls"])
            assert not any(o["content_truncated"] for o in view["observation"])


def test_cluster_adequacy_gate_is_enforced(package: dict) -> None:
    adequacy = package["readiness"]["cluster_adequacy"]
    if adequacy["effective_clusters_kish"] < adequacy["min_effective_clusters"]:
        assert any(
            "EFFECTIVE_CLUSTERS_BELOW_FLOOR" in blocker
            for blocker in package["readiness"]["blockers"]
        )


def test_registry_absent_is_an_explicit_blocker(package: dict) -> None:
    assert any("REGISTRY" in blocker for blocker in package["readiness"]["blockers"])


def test_every_human_field_offers_both_escapes(package: dict) -> None:
    for values in package["taxonomy"]["allowed_values"].values():
        assert "CANNOT_JUDGE" in values
        assert "INSUFFICIENT_CONTEXT" in values


def test_tutor_parameters_remain_null(package: dict) -> None:
    assert all(value is None for value in package["unset_parameters_owned_by_tutor"].values())
