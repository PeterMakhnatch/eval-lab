"""The provable-differential count is recomputed from retained bytes, never from summaries."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

TOOL = (
    Path(__file__).resolve().parents[1]
    / "research/evidence/runs/pt-self1-verified-differentials-20261027/tools/verify_differentials.py"
)
_spec = importlib.util.spec_from_file_location("verify_differentials_under_test", TOOL)
assert _spec is not None and _spec.loader is not None
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)

HEADER = "key_id,item_id,task,unit,split,expected_verdict,exposures\n"


def _assignments(tmp_path: Path) -> Path:
    rows = [
        "key-a,good,task-good,,dev,fail,\n",
        "key-a,leaky,task-leaky,,dev,fail,\n",
        "key-b,leaky,task-leaky,,test,fail,\n",
        "key-a,controlled,task-controlled,,train,fail,\n",
    ]
    path = tmp_path / "assignments.csv"
    path.write_text(HEADER + "".join(rows), encoding="utf-8")
    return path


def _receipt(
    tmp_path: Path, name: str, reward: float, exception: str | None = None
) -> tuple[str, str]:
    path = tmp_path / "evidence" / f"{name}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"reward": reward, "exception": exception}), encoding="utf-8")
    return f"{name}.json", hashlib.sha256(path.read_bytes()).hexdigest()


def _arm(
    tmp_path: Path, name: str, *, reward: float, file_reward: float, **kwargs: Any
) -> dict[str, Any]:
    relative, digest = _receipt(tmp_path, name, file_reward, kwargs.pop("exception", None))
    arm = {
        "reward": reward,
        "backend": kwargs.pop("backend", "modal"),
        "executed": True,
        "source": {"root": "E", "path": relative},
        "sha256": digest,
        "reward_pointer": "/reward",
        "exception_pointer": "/exception",
    }
    arm.update(kwargs)
    return arm


def _entry(tmp_path: Path, task: str, **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "task_id": task,
        "level": "native_differential_single",
        "candidate_identity": {"binding": "exact_digest", "digest_or_id": "c0ffee"},
        "controls": {},
        "original": _arm(tmp_path, f"{task}-original", reward=1.0, file_reward=1.0),
        "repaired": _arm(tmp_path, f"{task}-repaired", reward=0.0, file_reward=0.0),
    }
    entry.update(overrides)
    return entry


def _run(tmp_path: Path, entries: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    ledger = {"schema": verify.LEDGER_SCHEMA, "entries": entries}
    return verify.verify_ledger(
        ledger,
        assignments_csv=_assignments(tmp_path),
        roots={"E": tmp_path / "evidence"},
        **kwargs,
    )


def _status(report: dict[str, Any], task: str) -> dict[str, Any]:
    return next(row for row in report["entries"] if row["task_id"] == task)


def test_a_task_counts_only_when_retained_bytes_prove_a_cross_key_clean_differential(
    tmp_path: Path,
) -> None:
    report = _run(tmp_path, [_entry(tmp_path, "task-good")])

    assert report["counts"]["verified"] == 1
    assert _status(report, "task-good")["status"] == "verified"


@pytest.mark.parametrize(
    ("case", "build", "expected"),
    [
        (
            "tampered_bytes",
            lambda tp: _entry(
                tp,
                "task-good",
                original={
                    **_arm(tp, "t-original", reward=1.0, file_reward=1.0),
                    "sha256": "0" * 64,
                },
            ),
            "sha256 differs",
        ),
        (
            "claimed_reward_not_in_bytes",
            lambda tp: _entry(
                tp, "task-good", original=_arm(tp, "t-original", reward=1.0, file_reward=0.0)
            ),
            "does not resolve",
        ),
        (
            "repaired_arm_did_not_fail",
            lambda tp: _entry(
                tp, "task-good", repaired=_arm(tp, "t-repaired", reward=1.0, file_reward=1.0)
            ),
            "recorded reward must be 0.0",
        ),
        (
            "host_side_grade",
            lambda tp: _entry(
                tp,
                "task-good",
                repaired=_arm(tp, "t-repaired", reward=0.0, file_reward=0.0, backend="host"),
            ),
            "host-side",
        ),
        (
            "infrastructure_exception",
            lambda tp: _entry(
                tp,
                "task-good",
                repaired=_arm(tp, "t-repaired", reward=0.0, file_reward=0.0, exception="Timeout"),
            ),
            "infrastructure exception",
        ),
        ("test_row_under_another_key", lambda tp: _entry(tp, "task-leaky"), "non-dev assignment"),
        ("unassigned_task", lambda tp: _entry(tp, "task-unassigned"), "no affirmative dev"),
        (
            "controls_claimed_without_repeat_controls",
            lambda tp: _entry(
                tp,
                "task-controlled",
                level="native_differential_with_controls",
                controls={"reference": [1], "nop": [0]},
            ),
            "reference [1,1,1]",
        ),
        (
            "unknown_candidate_binding",
            lambda tp: _entry(tp, "task-good", candidate_identity={"binding": "same_name"}),
            "binding",
        ),
    ],
)
def test_entries_the_bytes_do_not_support_are_rejected(
    tmp_path: Path, case: str, build: Any, expected: str
) -> None:
    entry = build(tmp_path)
    report = _run(tmp_path, [entry])

    row = report["entries"][0]
    assert row["status"] == "rejected", case
    assert any(expected in problem for problem in row["problems"]), row["problems"]
    assert report["counts"]["verified"] == 0


def test_a_missing_source_is_unverifiable_and_never_counted(tmp_path: Path) -> None:
    entry = _entry(tmp_path, "task-good")
    (tmp_path / "evidence" / "task-good-original.json").unlink()
    (tmp_path / "evidence" / "task-good-repaired.json").unlink()

    report = _run(tmp_path, [entry])

    assert report["entries"][0]["status"] == "unverifiable"
    assert report["counts"]["verified"] == 0
    assert report["counts"]["unverifiable"] == 1


def test_a_task_listed_twice_counts_once(tmp_path: Path) -> None:
    first = _entry(tmp_path, "task-good")
    second = _entry(tmp_path, "terminal-bench/task-good")

    report = _run(tmp_path, [first, second])

    assert [row["status"] for row in report["entries"]] == ["verified", "rejected"]
    assert report["counts"]["verified"] == 1


def test_counts_split_what_is_new_from_the_database_tier1_baseline(tmp_path: Path) -> None:
    database = tmp_path / "trace.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE task_issues (task_id TEXT, status TEXT, direction TEXT, "
            "original_reward REAL, repaired_reward REAL, trust_tier TEXT)"
        )
        connection.execute(
            "INSERT INTO task_issues VALUES ('task-good', 'confirmed', 'wrong_pass', 1.0, 0.0, "
            "'tier1_container_verified')"
        )
        connection.execute(
            "INSERT INTO task_issues VALUES ('task-controlled', 'confirmed', 'wrong_pass', 1.0, 0.0, "
            "'tier2_consensus_labeled')"
        )

    report = _run(
        tmp_path,
        [_entry(tmp_path, "task-good"), _entry(tmp_path, "task-controlled")],
        database=database,
    )

    counts = report["counts"]
    assert (counts["database_tier1_tasks"], counts["verified_in_database_tier1"]) == (1, 1)
    assert counts["verified_beyond_database_tier1"] == 1


def test_exit_status_ranks_rejection_above_an_absent_file(tmp_path: Path) -> None:
    ledger = {
        "schema": verify.LEDGER_SCHEMA,
        "roots": {"E": str(tmp_path / "evidence")},
        "entries": [_entry(tmp_path, "task-good"), _entry(tmp_path, "task-unassigned")],
    }
    (tmp_path / "evidence" / "task-good-original.json").unlink()
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    argv = [str(ledger_path), "--assignments", str(_assignments(tmp_path))]

    assert verify.main(argv) == 1

    ledger["entries"].pop()
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    assert verify.main(argv) == 2
