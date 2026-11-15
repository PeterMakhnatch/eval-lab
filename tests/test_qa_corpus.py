"""``evallab qa corpus`` pure parts: discovery, stubs, classes, resume, summary. No Modal."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from evallab.dataset_audit_contracts import AuditObservation
from evallab.qa import corpus
from evallab.regrade import SpendCapReached
from evallab.task_import import discover_task_packages

STUB = b'#!/bin/bash\necho "no solution written"\n'


def _package(root: Path, name: str, solve: bytes | None = b"#!/bin/bash\necho 42 > /app/out\n") -> Path:
    package = root / name
    (package / "tests").mkdir(parents=True)
    (package / "task.toml").write_text(f'[task]\nname = "bench/{name}"\n', encoding="utf-8")
    (package / "tests" / "test.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    if solve is not None:
        (package / "solution").mkdir()
        (package / "solution" / "solve.sh").write_bytes(solve)
    return package


def _observation(reward: float | None, status: str = "executed") -> AuditObservation:
    return AuditObservation(
        stage="oracle",
        status=status,
        facts={"reward": reward, "agent_stdout": "ran", "verifier_stdout": "x" * 9000},
        reason=None if status == "executed" else "QA script exited 2",
    )


def _ticks():
    value = 0.0

    def clock() -> float:
        nonlocal value
        value += 1.5
        return value

    return clock


def test_select_tasks_filters_by_relative_path_or_name_skips_done_and_limits(tmp_path: Path) -> None:
    for name in ("a", "group/b", "group/c", "d"):
        _package(tmp_path, name)
    packages = discover_task_packages(tmp_path)
    keys = [corpus.task_key(p, tmp_path) for p in packages]
    assert keys == ["a", "d", "group/b", "group/c"]

    chosen = corpus.select_tasks(packages, corpus_root=tmp_path, only=["group/b", "c", "a"])
    assert [corpus.task_key(p, tmp_path) for p in chosen] == ["a", "group/b", "group/c"]

    pending = corpus.select_tasks(
        packages, corpus_root=tmp_path, done=frozenset({"a", "group/b"}), limit=1
    )
    assert [corpus.task_key(p, tmp_path) for p in pending] == ["d"]


@pytest.mark.parametrize(
    "data",
    [
        STUB,
        b'echo "no solution written"\n',
        b"#!/usr/bin/env bash\n# placeholder\nprintf 'no solution written\\n'\n",
    ],
)
def test_stub_scripts_are_detected(data: bytes) -> None:
    assert corpus.is_stub(data)


@pytest.mark.parametrize(
    "data",
    [
        b"#!/bin/bash\necho 42 > /app/out\n",
        b'#!/bin/bash\necho "no solution written"\nexit 1\n',
        b'#!/bin/bash\necho "no solution written yet, but here it is" && make\n',
        b"\xff\xfe",
    ],
)
def test_real_scripts_are_not_stubs(data: bytes) -> None:
    assert not corpus.is_stub(data)


@pytest.mark.parametrize(
    ("reward", "status", "expected"),
    [
        (1.0, "executed", "clean"),
        (0.5, "executed", "reference_below_one"),
        (0.0, "executed", "reference_below_one"),
        (None, "executed", "cannot_run"),
        (1.0, "failed", "cannot_run"),
        (None, "failed", "cannot_run"),
    ],
)
def test_classify_from_reward_and_status(reward: float | None, status: str, expected: str) -> None:
    assert corpus.classify(reward=reward, status=status) == expected


def test_run_writes_every_class_continues_after_a_failure_and_resumes(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    _package(root, "clean")
    _package(root, "missing", solve=None)
    _package(root, "stub", solve=STUB)
    _package(root, "partial")
    _package(root, "broken")
    _package(root, "crash")
    results = {
        "clean": _observation(1.0),
        "partial": _observation(0.25),
        "broken": _observation(None, status="failed"),
    }
    calls: list[str] = []

    def run_trial(package: Path) -> AuditObservation:
        calls.append(package.name)
        if package.name == "crash":
            raise RuntimeError("sandbox vanished")
        return results[package.name]

    out = tmp_path / "out"
    packages = discover_task_packages(root)
    rows, stopped = corpus.run_corpus(
        packages, corpus_root=root, out=out, run_trial=run_trial,
        clock=_ticks(), today=date(2026, 11, 15),
    )
    assert not stopped
    assert sorted(calls) == ["broken", "clean", "crash", "partial"]
    by_task = {row["task"]: row for row in json.loads((out / "audit.json").read_text())}
    assert {task: row["class"] for task, row in by_task.items()} == {
        "broken": "cannot_run",
        "clean": "clean",
        "crash": "cannot_run",
        "missing": "no_reference",
        "partial": "reference_below_one",
        "stub": "no_reference",
    }
    assert by_task["crash"]["status"] == "error"
    assert "sandbox vanished" in by_task["crash"]["log_excerpt"]
    assert by_task["missing"]["reference_digest"] is None
    assert by_task["stub"]["reference_digest"].startswith("sha256:4e25c8e51ff8")
    assert by_task["partial"]["reward"] == 0.25 and by_task["partial"]["seconds"] == 1.5
    assert all(len(row["log_excerpt"]) <= corpus.EXCERPT_LIMIT for row in rows)
    assert all(row["package_digest"].startswith("sha256:") for row in rows)
    report = (out / "AUDIT.md").read_text()
    assert report.startswith("# Corpus reference audit, 2026-11-15")
    assert "| no_reference | 2 |" in report and "| partial | reference_below_one | 0.25 | 1.5 |" in report

    _package(root, "late")
    prior = corpus.load_prior(out, resume=True)
    pending = corpus.select_tasks(
        discover_task_packages(root), corpus_root=root, done=frozenset(r["task"] for r in prior)
    )
    assert [p.name for p in pending] == ["late"]
    results["late"] = _observation(1.0)
    rows, _ = corpus.run_corpus(
        pending, corpus_root=root, out=out, run_trial=run_trial, prior=prior, clock=_ticks()
    )
    assert len(rows) == 7 and rows[-1]["task"] == "late"
    with pytest.raises(ValueError, match="--resume"):
        corpus.load_prior(out, resume=False)


def test_spend_cap_stops_without_writing_a_row(tmp_path: Path) -> None:
    _package(tmp_path / "corpus", "a")

    def run_trial(package: Path) -> AuditObservation:
        raise SpendCapReached("over the approved $1.00")

    rows, stopped = corpus.run_corpus(
        discover_task_packages(tmp_path / "corpus"),
        corpus_root=tmp_path / "corpus",
        out=tmp_path / "out",
        run_trial=run_trial,
    )
    assert stopped and rows == []
    assert json.loads((tmp_path / "out" / "audit.json").read_text()) == []


def test_summary_line_counts_every_class() -> None:
    rows = [{"class": c} for c in ("clean", "clean", "cannot_run", "no_reference")]
    assert corpus.summary_line(rows) == (
        "4 tasks: 2 clean, 0 reference_below_one, 1 cannot_run, 1 no_reference"
    )
