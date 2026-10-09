"""Offline tests for evallab.vcheck_store (no Docker, no network, no models)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.vcheck_store import (
    append_event,
    count_rows,
    export_finding,
    latest_hypotheses,
    open_store,
    raise_hypothesis,
    record_campaign,
    record_case,
    record_claim,
    record_finding,
    record_task,
    record_trial,
    transition_hypothesis,
    verify_chain,
)

AT = "2026-10-09T00:00:00+00:00"


@pytest.fixture()
def conn(tmp_path: Path):
    handle = open_store(tmp_path / "audit.duckdb")
    yield handle
    handle.close()


def test_tables_exist_append_only(conn) -> None:
    for table in (
        "campaigns",
        "tasks",
        "trials",
        "hypotheses",
        "findings",
        "claims",
        "cases",
    ):
        assert count_rows(conn, table) == 0
    with pytest.raises(ValueError, match="unknown vcheck store table"):
        count_rows(conn, "campaigns; DROP TABLE campaigns")


def test_record_round_trip(conn) -> None:
    record_campaign(conn, "camp1", "terminal-bench", "v4.0.0@abc123", at=AT)
    record_task(conn, "t1", "camp1", "task-a", "v4.0.0@abc123", at=AT)
    record_trial(
        conn,
        "tr1",
        "camp1",
        "task-a",
        {"submission": "x"},
        1.0,
        "job-dir-1",
        condition="target_baseline",
        at=AT,
    )
    record_finding(
        conn,
        "fnd1",
        {"family": "checker-logic", "class": "answer-matching-logic-error"},
        "high",
        slug="demo-gap",
        title="Demo gap",
        kinds=["verifier-gap"],
        at=AT,
    )
    record_claim(conn, "fnd1", "grader_defect", "The grader is wrong.", ["E1"], at=AT)
    record_case(
        conn,
        "fnd1",
        "max-watermark",
        "inputs/max-watermark/max-watermark.patch",
        "ab" * 32,
        {"status": "graded", "reward": 1.0},
        {"verdict": "fail", "basis": "Should fail."},
        "incorrect submission, accepted",
        task="task-a",
        at=AT,
    )
    assert count_rows(conn, "campaigns") == 1
    assert count_rows(conn, "tasks") == 1
    assert count_rows(conn, "trials") == 1
    assert count_rows(conn, "findings") == 1
    assert count_rows(conn, "claims") == 1
    assert count_rows(conn, "cases") == 1
    with pytest.raises(ValueError, match="unknown trial condition"):
        record_trial(conn, "tr2", "camp1", "task-a", {}, 0.0, "j", condition="nope")
    with pytest.raises(ValueError, match="unknown claim assertion"):
        record_claim(conn, "fnd1", "vibes", "text", [])


def test_hypothesis_lifecycle_grows_rows(conn) -> None:
    raise_hypothesis(conn, "hyp1", "task-a", "The verifier skips slow sources.", at=AT)
    assert count_rows(conn, "hypotheses") == 1
    with pytest.raises(ValueError, match="already exists"):
        raise_hypothesis(conn, "hyp1", "task-a", "Duplicate.", at=AT)
    transition_hypothesis(conn, "hyp1", "promoted", note="confirmed", at=AT)
    assert count_rows(conn, "hypotheses") == 2
    latest = latest_hypotheses(conn)
    assert len(latest) == 1
    assert latest[0]["status"] == "promoted"
    assert [entry["to"] for entry in latest[0]["history"]] == [
        "candidate",
        "promoted",
    ]
    with pytest.raises(ValueError, match="terminal"):
        transition_hypothesis(conn, "hyp1", "merged", at=AT)
    with pytest.raises(ValueError, match="unknown hypothesis"):
        transition_hypothesis(conn, "missing", "rejected", at=AT)
    with pytest.raises(ValueError, match="unknown hypothesis status"):
        transition_hypothesis(conn, "hyp1", "bogus", at=AT)


def test_chain_verify_true_and_tamper_false(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    first = append_event(run_dir, "request", {"prompt": "read the grader"})
    assert first["sequence"] == 1
    assert first["previous"] == "genesis"
    second = append_event(run_dir, "response", {"notes": "found gap"})
    assert second["sequence"] == 2
    assert second["previous"] == first["sha256"]
    assert verify_chain(run_dir) is True
    assert verify_chain(run_dir / "trajectory.jsonl") is True
    assert verify_chain(tmp_path / "missing") is False

    log_path = run_dir / "trajectory.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["data"] = {"notes": "forged"}
    lines[1] = json.dumps(tampered, sort_keys=True)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify_chain(run_dir) is False

    with pytest.raises(ValueError, match="unknown trajectory kind"):
        append_event(tmp_path / "other", "telepathy", {})


def test_chain_digest_matches_client(tmp_path: Path) -> None:
    from evallab.vcheck_client import chain_append

    records: list[dict[str, object]] = []
    expected = chain_append(records, "note", {"text": "hello"})
    actual = append_event(tmp_path / "run", "note", {"text": "hello"})
    assert actual["sha256"] == expected["sha256"]


def _finding() -> dict[str, object]:
    return {
        "slug": "demo-gap",
        "id": "fnd_demo",
        "title": "Demo verifier gap",
        "status": "open",
        "confidence": "high",
        "benchmark": "terminal-bench",
        "pin": "v4.0.0@abc123",
        "grader": "test_suite",
        "grader_command": "python3 verify.py --task <task> <input>",
        "model": "glm-5.3",
        "kinds": ["verifier-gap"],
        "defect": {
            "family": "insufficient-checking",
            "class": "missing-behavioral-assertion",
        },
        "claims": [
            {
                "assertion": "grader_defect",
                "text": "The verifier never checks slow sources.",
                "confidence": "high",
                "core": True,
                "effects": ["E1"],
            }
        ],
        "effects": [
            {
                "kind": "reward_without_intent",
                "trigger": "Slow sources are ignored yet score full reward.",
            }
        ],
        "description": "A minimal demonstration finding.",
    }


def _cases() -> list[dict[str, object]]:
    return [
        {
            "input": "slow-source",
            "task": "task-a",
            "filename": "slow-source.patch",
            "content": b"diff --git a/x b/x\n",
            "shows": "incorrect submission, accepted",
            "effect": "E1",
            "grader_verdict": {"status": "graded", "reward": 1.0},
            "intended_verdict": {"verdict": "fail", "basis": "Should fail."},
        }
    ]


def test_export_folder_has_all_artifacts(tmp_path: Path) -> None:
    export_dir = export_finding(tmp_path / "run", _finding(), _cases(), at=AT)
    assert (export_dir / "inputs" / "slow-source" / "slow-source.patch").exists()
    for name in ("expected.json", "REPRODUCE.md", "README.md", "findings.jsonl"):
        assert (export_dir / name).is_file(), name

    expected = json.loads((export_dir / "expected.json").read_text(encoding="utf-8"))
    assert expected["inputs"][0]["expected"] == {
        "kind": "exact",
        "verdict": {"status": "graded", "reward": 1.0},
        "from": "expected",
    }
    reproduce = (export_dir / "REPRODUCE.md").read_text(encoding="utf-8")
    assert "v4.0.0@abc123" in reproduce
    assert "python3 verify.py --task task-a inputs/slow-source/slow-source.patch" in (reproduce)
    readme = (export_dir / "README.md").read_text(encoding="utf-8")
    assert "C1" in readme and "E1" in readme

    lines = (export_dir / "findings.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["format"] == "envcheck-finding/1"
    assert record["slug"] == "demo-gap"
    assert record["defect"]["family"] == "insufficient-checking"
    assert record["defect"]["class"] == "missing-behavioral-assertion"
    assert record["defect"]["kinds"] == ["verifier-gap"]
    assert record["discovered_by"] == {
        "method": "harness",
        "model": "glm-5.3",
        "who": "eval-lab",
        "date": AT,
    }
    assert {claim["label"] for claim in record["claims"]} == {"C1"}
    case = record["cases"][0]
    for key in (
        "input",
        "path",
        "sha256",
        "grader_verdict",
        "intended_verdict",
        "shows",
    ):
        assert key in case, key
    assert case["path"] == "inputs/slow-source/slow-source.patch"
    on_disk = (export_dir / case["path"]).read_bytes()
    import hashlib

    assert case["sha256"] == hashlib.sha256(on_disk).hexdigest()
