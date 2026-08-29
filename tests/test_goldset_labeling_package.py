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


def test_item_identity_is_the_full_context_digest(package: dict) -> None:
    """Identity is context, not step content.

    Deduping on step content alone wrongly merged 16 distinct contexts - worst
    case 6 steps across 6 DIFFERENT trials sharing one terminal message, plus
    consecutive indices 17/18 inside a single trial. Distinct trial or step
    ordinal must never merge.
    """
    contexts = [item["item_context_digest"] for item in package["items"]]
    assert len(contexts) == len(set(contexts))
    # Two items MAY share a step digest: same message, different trial/context.
    logicals = [item["logical_step_digest"] for item in package["items"]]
    assert len(set(logicals)) < len(logicals)


def test_rating_contract_digest_is_non_circular(package: dict) -> None:
    """The signed contract is fixed BEFORE intake; artifact digests are separate.

    Ratings previously had to bind package_digest, which covers readiness and
    rating summaries and therefore changes as ratings arrive - a circular
    requirement.
    """
    auth = package["readiness"]["authentication"]
    contract = auth["rating_contract_digest"]
    assert contract
    assert contract != package["package_digest"]
    assert contract != package["build_id"]
    assert "rating_contract_digest" in auth["requires"]
    assert "item_context_digest" in auth["requires"]


def test_item_context_digest_covers_the_whole_rater_view(package: dict) -> None:
    """Altering the instruction or a prior step must change the bound digest."""

    def context_digest(item: dict, context: object) -> str:
        """Independent reimplementation: verifies the FORMULA, not the module.

        Payload is {cluster_id, step_index, rater_context}: trial identity and
        step ordinal are inside the digest so distinct contexts never collide.
        """
        payload = {
            "cluster_id": item["cluster_id"],
            "step_index": item["step_index"],
            "rater_context": context,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    item = package["items"][0]
    assert context_digest(item, item["rater_context"]) == item["item_context_digest"]
    tampered_instruction = {
        **item["rater_context"],
        "instruction": {"presumed_task_statement": {"message": "A DIFFERENT TASK"}},
    }
    assert context_digest(item, tampered_instruction) != item["item_context_digest"]
    tampered_prior = {**item["rater_context"], "prior_steps": []}
    assert context_digest(item, tampered_prior) != item["item_context_digest"]


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


def test_tutor_decided_parameters_are_recorded(package: dict) -> None:
    """Tutor's 2026-08-28 decisions, with acceptance_threshold explicitly null."""
    params = package["statistical_parameters_owned_by_tutor"]
    decided = params["decided_2026_08_28"]
    assert decided["primary_statistic"] == "gwet_ac1_multirater_nominal"
    assert decided["declared_universe_q"] == 12
    assert decided["interval_method"] == "percentile_cluster_bootstrap"
    assert decided["bootstrap_resamples"] == 4000
    assert decided["target_ci_half_width_95"] == 0.05
    assert decided["prevalence_valid_core_required"] is True
    assert decided["sampling_weights_required"] is True
    # Explicitly null BY DECISION, not oversight.
    assert params["still_null"]["acceptance_threshold"] is None


def test_prevalence_core_carries_weights(package: dict) -> None:
    """Core arm is prevalence-valid; boost arm excluded from that arithmetic."""
    for item in package["items"]:
        if item["selection_arm"] == "prevalence_core":
            assert item["sampling_weight"] > 0
        else:
            assert item["sampling_weight"] == 0.0


def test_three_digests_are_distinct_and_named(package: dict) -> None:
    """File SHA, in-band package_digest and build_id are different quantities."""
    file_sha = hashlib.sha256(PACKAGE.read_bytes()).hexdigest()
    assert file_sha != package["package_digest"]
    assert package["package_digest"] != package["build_id"]
    assert file_sha != package["build_id"]


def test_build_lock_is_not_committed() -> None:
    assert not (GOLDSET / ".goldset-build.lock").exists()


def test_diagnostic_consumes_only_accepted_records(package: dict) -> None:
    """A forged or unsigned record must never reach the diagnostic.

    The 2x2 previously consumed the RAW loaded records, before signature and
    qualification validation, so an unsigned submission claiming
    INSUFFICIENT_CONTEXT would have poisoned it.
    """
    readiness = package["readiness"]
    intake = readiness["rating_intake"]
    counts = readiness["context_diagnostic_2x2"]["counts"]
    assert intake["records_accepted"] + intake["records_rejected"] == intake["records_seen"]
    # The diagnostic may only account for accepted records.
    assert sum(counts.values()) == intake["records_accepted"]
    assert "rejection_reasons" in intake


def test_context_diagnostic_2x2_is_reported(package: dict) -> None:
    """The 2x2 requires builder verdict and rater judgement to be independent."""
    diag = package["readiness"]["context_diagnostic_2x2"]
    assert set(diag["counts"]) == {
        "COMPLETE|sufficient",
        "COMPLETE|INSUFFICIENT_CONTEXT",
        "INCOMPLETE|sufficient",
        "INCOMPLETE|INSUFFICIENT_CONTEXT",
    }
    assert "builder_missed_a_defect" in diag
    assert "builder_over_strict" in diag


def test_frontmatter_blocker_count_matches_readiness(package: dict) -> None:
    """Protocol frontmatter must not advertise a stale blocker count."""
    doc = (GOLDSET / "GOLDSET-ITEM-SELECTION-AND-TAXONOMY-2026-08-28.md").read_text(
        encoding="utf-8"
    )
    n = len(package["readiness"]["blockers"])
    assert f"readiness: NOT_READY ({n} blockers)" in doc
