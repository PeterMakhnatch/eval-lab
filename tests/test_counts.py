"""HAR-78: recorded trials decide the counts verdict, judgments do not."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from evallab.counts import (
    attach_counts,
    classify_counts,
    find_label_root,
    task_index_for,
    usability,
)
from evallab.process_job import _process_trial

REPO = find_label_root(Path(__file__))
WAITERESS = (
    "cd /testbed && pip download waitress==2.0.0 --no-deps -d /tmp/wtr 2>&1 | tail -2; "
    "ls /tmp/wtr 2>/dev/null"
)
HAR104_COPY = Path(
    "/Users/petermakhnatch/Developer/eval-lab/.worktrees/har104-runs/runs/"
    "har104-d-000226/har104-d-000226__JCDfZFi"
)
HAR110_INFRA = Path(
    "/Users/petermakhnatch/Developer/eval-lab/.worktrees/har110-live/runs/"
    "har110-dev-002256-cfe31418/har110-dev-002256-cfe31418__uDRp4Vb"
)
HAR104_BROKEN = Path(
    "/Users/petermakhnatch/Developer/eval-lab/.worktrees/har104-runs/runs/"
    "har104-d-002259/har104-d-002259__cptLF6h"
)


def _fetch(command: str = WAITERESS) -> list[dict]:
    return [
        {
            "kind": "upstream_fetch",
            "rule": "upstream_fetch:pip-download-remote-package",
            "command": command,
            "evidence": "head#4",
        }
    ]


def test_copied_pass_is_excluded_and_a_fetch_fail_still_counts() -> None:
    copied = classify_counts(reward=1.0, scored=True, taint=_fetch())
    assert copied["verdict"] == "excluded"
    assert copied["reasons"] == ["copied_fix", "pass_tainted"]
    assert copied["raw_reward"] == 1.0
    assert copied["evidence"][0]["command"].startswith("cd /testbed && pip download waitress==2.0.0")
    assert copied["judgments"] == []

    failed = classify_counts(
        reward=0.0,
        scored=True,
        taint=_fetch(),
        first_failure={"rule_id": "R-ENV-02", "attribution": "harness"},
        flags=["identical_loop:12x:1-12"],
    )
    assert failed["verdict"] == "counted_fail"
    assert failed["reasons"] == []
    assert failed["flags"] == [
        {
            "code": "upstream_fetch",
            "decisive": False,
            "note": "fetched upstream and still failed; verdict stays counted_fail",
        }
    ]
    assert [item["label"] for item in failed["judgments"]] == ["first_failure", "loop_kind"]
    assert all(item["accuracy"] is None for item in failed["judgments"])


def test_proxy_502_without_a_reward_is_infra() -> None:
    counts = classify_counts(
        reward=None,
        scored=False,
        exception={
            "exception_type": "BadGatewayError",
            "exception_message": "litellm.BadGatewayError: BadGatewayError: OpenAIException - unsupported upstream encoding",
        },
    )
    assert counts["verdict"] == "excluded"
    assert counts["reasons"] == ["infra"]
    assert "BadGatewayError" in counts["evidence"][0]["excerpt"]
    assert counts["evidence"][0]["path"] == "result.json"


def test_budget_stop_with_a_score_is_not_infra() -> None:
    counts = classify_counts(
        reward=0.0,
        scored=True,
        exception={"exception_type": "TrialBudgetExhaustedError", "exception_message": "trial budget exhausted"},
    )
    assert counts["verdict"] == "counted_fail"
    assert counts["reasons"] == []


def test_hand_broken_task_is_not_usable_even_when_census_says_sound() -> None:
    assert REPO is not None
    index = task_index_for(REPO)
    label = usability(index, trial_name="har104-d-002259__cptLF6h", task_name="format-code-task-002259")
    assert label is not None
    assert label["detector"] == "hand_label"
    assert label["status"] == "broken"
    counts = classify_counts(reward=0.0, scored=True, usability=label)
    assert counts["verdict"] == "excluded"
    assert counts["reasons"] == ["task_not_usable"]
    assert "har109/hand/" in counts["evidence"][0]["path"]


def test_sound_census_row_does_not_exclude() -> None:
    assert REPO is not None
    index = task_index_for(REPO)
    assert usability(index, trial_name="har104-d-000226__JCDfZFi", task_name="format-code-task-000226") is None


def test_validated_repair_variant_lifts_the_census_exclusion_only_for_its_task() -> None:
    # 001618's original is broken_environment; HAR-115's pip repair is validated,
    # an earlier attempt is still a candidate.
    assert REPO is not None
    index = task_index_for(REPO)
    variants = REPO / "library/task-variants/mimo-v2.6-rl__format-code-task-001618"
    repaired = json.loads((variants / "6711d55bc4e5.json").read_text())["variant_digest"]
    candidate = json.loads((variants / "1b8e64964d4e.json").read_text())["variant_digest"]
    task = "mimo-v2.6-rl/format-code-task-001618"
    assert usability(index, trial_name=None, task_name=task, package_digest=repaired) is None
    original = usability(index, trial_name=None, task_name=task, package_digest=None)
    assert original is not None and original["status"] == "broken_environment"
    assert usability(index, trial_name=None, task_name=task, package_digest=candidate) is not None
    other_task = usability(
        index, trial_name=None, task_name="format-code-task-002307", package_digest=repaired
    )
    assert other_task is not None


@pytest.mark.skipif(not HAR104_COPY.is_dir(), reason="recorded HAR-104 trial is not on this machine")
def test_recorded_copied_pass() -> None:
    record = _process_trial(HAR104_COPY, HAR104_COPY.parent)
    counts = classify_counts(
        reward=record["reward"],
        scored=record["scored"],
        taint=record["taint"],
        usability=usability(
            task_index_for(REPO),
            trial_name=record["trial_name"],
            task_name=record["task_name"],
        ),
    )
    assert record["reward"] == 1.0
    assert counts["verdict"] == "excluded"
    assert "copied_fix" in counts["reasons"]


@pytest.mark.skipif(not HAR110_INFRA.is_dir(), reason="recorded HAR-110 trial is not on this machine")
def test_recorded_proxy_502() -> None:
    result = json.loads((HAR110_INFRA / "result.json").read_text())
    record = _process_trial(HAR110_INFRA, HAR110_INFRA.parent)
    counts = classify_counts(
        reward=record["reward"],
        scored=record["scored"],
        taint=record["taint"],
        exception=result.get("exception_info"),
    )
    assert counts["verdict"] == "excluded"
    assert counts["reasons"] == ["infra"]
    assert "BadGatewayError" in counts["evidence"][0]["excerpt"]


@pytest.mark.skipif(not HAR104_BROKEN.is_dir(), reason="recorded HAR-104 trial is not on this machine")
def test_recorded_hand_broken_task() -> None:
    record = _process_trial(HAR104_BROKEN, HAR104_BROKEN.parent)
    label = usability(
        task_index_for(REPO),
        trial_name=record["trial_name"],
        task_name=record["task_name"],
    )
    counts = classify_counts(
        reward=record["reward"],
        scored=record["scored"],
        taint=record["taint"],
        usability=label,
        exception=json.loads((HAR104_BROKEN / "result.json").read_text()).get("exception_info"),
    )
    assert record["reward"] == 0.0
    assert counts["verdict"] == "excluded"
    assert counts["reasons"] == ["task_not_usable"]


def _ledger_fixture(root: Path, status: str) -> tuple[str, str]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    task_id = "format-code-task-repair"
    digest = "sha256:" + "a" * 64
    path = root / "research/experiments/python-task-ledger/ledger.csv"
    path.parent.mkdir(parents=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["task_id", "status", "run_digest", "reason", "evidence"]
        )
        writer.writeheader()
        writer.writerow({
            "task_id": task_id,
            "status": status,
            "run_digest": digest,
            "reason": "validated repair",
            "evidence": "repairs.json#repair",
        })
    census = root / "research/experiments/har108-python-census/task_health.parquet"
    census.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist([
        {"task_id": task_id, "label": "broken_environment", "evidence": "original import error"}
    ]), census)
    variant = root / "library/task-variants/repair/validated.json"
    variant.parent.mkdir(parents=True)
    variant.write_text(json.dumps({
        "task_name": task_id, "variant_digest": digest, "status": "validated"
    }))
    return task_id, digest


@pytest.mark.parametrize("binding", ["matching", "original", "absent"])
def test_repair_status_applies_only_to_its_task_package(tmp_path: Path, binding: str) -> None:
    task_id, digest = _ledger_fixture(tmp_path, "usable")
    trial_digest = {
        "matching": digest,
        "original": "sha256:" + "b" * 64,
        "absent": None,
    }[binding]
    counts = attach_counts(
        {"task_name": f"mimo-v2.6-rl/{task_id}", "reward": 1.0, "scored": True},
        {},
        label_root=tmp_path,
        package_digest=trial_digest,
    )
    if binding == "matching":
        assert counts["verdict"] == "counted_pass"
        assert counts["task_status"]["status"] == "usable"
        assert counts["task_status"]["digest_match"] is True
    else:
        assert counts["verdict"] == "excluded"
        assert counts["reasons"] == ["task_not_usable"]
        assert counts["evidence"][0]["detector"] == "task_health"
        assert counts["task_status"]["status"] is None
        assert counts["task_status"]["digest_match"] == (False if binding == "original" else None)


@pytest.mark.parametrize("status", ["review", "discarded", "unchecked"])
def test_matching_ledger_excludes_unusable_task_despite_raw_pass(tmp_path: Path, status: str) -> None:
    task_id, digest = _ledger_fixture(tmp_path, status)
    counts = attach_counts(
        {"task_name": "variant-display-name", "reward": 1.0, "scored": True},
        {},
        label_root=tmp_path,
        task_id=task_id,
        package_digest=digest,
    )
    assert counts["raw_reward"] == 1.0
    assert counts["verdict"] == "excluded"
    assert counts["reasons"] == ["task_not_usable"]
    assert counts["evidence"][0]["detector"] == "python_task_ledger"
    assert counts["task_status"]["status"] == status


def test_missing_ledger_does_not_fabricate_usable_or_unchecked() -> None:
    counts = attach_counts(
        {"task_name": "outside-the-ledger", "reward": 0.0, "scored": True},
        {},
        label_root=None,
    )
    assert counts["verdict"] == "counted_fail"
    assert counts["task_status"]["status"] is None
    assert counts["task_status"]["ledger_status"] is None
    assert counts["task_status"]["digest_match"] is None


def test_usable_repair_does_not_override_a_hand_broken_label(tmp_path: Path) -> None:
    task_id, digest = _ledger_fixture(tmp_path, "usable")
    hand = tmp_path / "research/explorations/trace-lab/har109/hand/original.json"
    hand.parent.mkdir(parents=True)
    hand.write_text(json.dumps({"task_id": task_id, "task_verdict": "broken"}))
    counts = attach_counts(
        {"task_name": task_id, "reward": 1.0, "scored": True},
        {},
        label_root=tmp_path,
        package_digest=digest,
    )
    assert counts["verdict"] == "excluded"
    assert counts["reasons"] == ["task_not_usable"]
    assert counts["evidence"][0]["detector"] == "hand_label"
