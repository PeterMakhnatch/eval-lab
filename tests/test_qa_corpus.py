"""``evallab qa corpus`` pure parts: discovery, stubs, classes, controls, resume, summary. No Modal."""

from __future__ import annotations

import argparse
import json
import shlex
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


def _observation(reward: float | None, status: str = "executed", agent: str = "oracle") -> AuditObservation:
    return AuditObservation(
        stage=agent,
        status=status,
        facts={"reward": reward, "agent_stdout": f"ran {agent}", "verifier_stdout": "x" * 9000},
        reason=None if status == "executed" else f"{agent} QA script exited 2",
    )


def _ticks():
    value = 0.0

    def clock() -> float:
        nonlocal value
        value += 1.5
        return value

    return clock


def _command(tasks) -> str:
    return "uv run evallab qa corpus ROOT --out OUT " + " ".join(tasks)


def _cell(reward: float | None, status: str = "executed") -> dict:
    return {"reward": reward, "status": status, "seconds": 1.0}


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
    ("oracle", "nop", "expected"),
    [
        (_cell(1.0), _cell(0.0), "clean"),
        (_cell(1.0), _cell(1.0), "control_paid"),
        (_cell(0.5), _cell(0.25), "control_paid"),  # control_paid wins over reference_below_one
        (_cell(None, "failed"), _cell(1.0), "control_paid"),
        (_cell(0.5), _cell(0.0), "reference_below_one"),
        (_cell(0.0), _cell(0.0), "reference_below_one"),
        (_cell(None, "failed"), _cell(0.0), "reference_unmeasured"),
        (_cell(None, "error"), _cell(0.0), "reference_unmeasured"),
        (_cell(None, "executed"), _cell(0.0), "reference_unmeasured"),
        (_cell(None, "failed"), _cell(None, "failed"), "cannot_run"),
        (_cell(None, "error"), _cell(None, "error"), "cannot_run"),
        (_cell(1.0), _cell(None, "failed"), "cannot_run"),  # clean needs a measured control
        (_cell(0.5), _cell(None, "failed"), "reference_below_one"),
        (_cell(1.0), _cell(None, "skipped"), "clean"),
        (_cell(0.5), _cell(None, "skipped"), "reference_below_one"),
        (_cell(None, "failed"), _cell(None, "skipped"), "cannot_run"),
    ],
)
def test_classify_matrix(oracle: dict, nop: dict, expected: str) -> None:
    assert corpus.classify(oracle=oracle, nop=nop) == expected


def test_run_writes_every_class_continues_after_a_failure_and_resumes(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    for name in ("clean", "partial", "broken", "crash", "paid", "unmeasured", "noctl", "late"):
        _package(root, name)
    _package(root, "missing", solve=None)
    _package(root, "stub", solve=STUB)
    results = {
        ("clean", "oracle"): _observation(1.0),
        ("clean", "nop"): _observation(0.0, agent="nop"),
        ("partial", "oracle"): _observation(0.25),
        ("partial", "nop"): _observation(0.0, agent="nop"),
        ("broken", "oracle"): _observation(None, status="failed"),
        ("broken", "nop"): _observation(None, status="failed", agent="nop"),
        ("paid", "oracle"): _observation(0.5),
        ("paid", "nop"): _observation(1.0, agent="nop"),
        ("unmeasured", "oracle"): _observation(None, status="failed"),
        ("unmeasured", "nop"): _observation(0.0, agent="nop"),
        ("noctl", "oracle"): _observation(1.0),
        ("late", "oracle"): _observation(1.0),
        ("late", "nop"): _observation(0.0, agent="nop"),
    }
    calls: list[tuple[str, str]] = []

    def run_trial(package: Path, agent: str) -> AuditObservation:
        calls.append((package.name, agent))
        if package.name == "crash" or (package.name, agent) == ("noctl", "nop"):
            raise RuntimeError(f"sandbox vanished in {agent}")
        return results[(package.name, agent)]

    out = tmp_path / "out"
    packages = [p for p in discover_task_packages(root) if p.name != "late"]
    rows, stopped = corpus.run_corpus(
        packages, corpus_root=root, out=out, run_trial=run_trial, command=_command,
        clock=_ticks(), today=date(2026, 11, 15),
    )
    assert not stopped
    ran = {name for name, _ in calls}
    assert ran == {"broken", "clean", "crash", "noctl", "paid", "partial", "unmeasured"}
    assert all([(n, "oracle"), (n, "nop")] == [c for c in calls if c[0] == n] for n in ran)
    by_task = {row["task"]: row for row in json.loads((out / "audit.json").read_text())}
    assert {task: row["class"] for task, row in by_task.items()} == {
        "broken": "cannot_run",
        "clean": "clean",
        "crash": "cannot_run",
        "missing": "no_reference",
        "noctl": "cannot_run",
        "paid": "control_paid",
        "partial": "reference_below_one",
        "stub": "no_reference",
        "unmeasured": "reference_unmeasured",
    }
    paid = by_task["paid"]
    assert list(paid) == [
        "task", "package_digest", "class", "seconds", "log_excerpt", "oracle", "nop",
        "reference_digest",
    ]
    assert paid["oracle"] == {"reward": 0.5, "status": "executed", "seconds": 1.5}
    assert paid["nop"] == {"reward": 1.0, "status": "executed", "seconds": 1.5}
    assert paid["seconds"] == 7.5
    # The excerpt is the deciding cell's.
    assert "ran nop" in paid["log_excerpt"]
    assert "ran oracle" in by_task["partial"]["log_excerpt"]
    assert "oracle QA script exited 2" in by_task["unmeasured"]["log_excerpt"]
    assert "sandbox vanished in nop" in by_task["noctl"]["log_excerpt"]
    assert by_task["noctl"]["nop"]["status"] == "error"
    assert by_task["crash"]["oracle"]["status"] == "error"
    assert "sandbox vanished in oracle" in by_task["crash"]["log_excerpt"]
    assert by_task["missing"]["reference_digest"] is None
    assert by_task["missing"]["oracle"]["status"] == by_task["missing"]["nop"]["status"] == "not_run"
    assert by_task["stub"]["reference_digest"].startswith("sha256:4e25c8e51ff8")
    assert all(len(row["log_excerpt"]) <= corpus.EXCERPT_LIMIT for row in rows)
    assert all(row["package_digest"].startswith("sha256:") for row in rows)

    report = (out / "AUDIT.md").read_text()
    assert report.startswith("# Corpus reference audit, 2026-11-15")
    assert "| control_paid | 1 |" in report and "| no_reference | 2 |" in report
    assert "uv run evallab qa corpus ROOT --out OUT broken clean crash" in report
    table = [line for line in report.splitlines() if line.count("|") == 6][2:]
    assert [line.split(" | ")[0].strip("| ") for line in table] == [
        "paid", "partial", "unmeasured", "broken", "crash", "noctl", "missing", "stub", "clean",
    ]
    assert "| paid | 0.5 | 1 | control_paid | 7.5 |" in table
    assert "| missing |  |  | no_reference | 1.5 |" in table

    prior = corpus.load_prior(out, resume=True)
    pending = corpus.select_tasks(
        discover_task_packages(root), corpus_root=root, done=frozenset(r["task"] for r in prior)
    )
    assert [p.name for p in pending] == ["late"]
    rows, _ = corpus.run_corpus(
        pending, corpus_root=root, out=out, run_trial=run_trial, command=_command,
        prior=prior, clock=_ticks(),
    )
    assert len(rows) == 10 and rows[-1]["task"] == "late" and rows[-1]["class"] == "clean"
    with pytest.raises(ValueError, match="--resume"):
        corpus.load_prior(out, resume=False)


def test_resume_reruns_rows_without_a_nop_cell(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    for name in ("old", "new", "skipped"):
        _package(root, name)
    legacy = {"task": "old", "class": "clean", "reward": 1.0, "status": "executed", "seconds": 3.0}
    current = {"task": "new", "class": "clean", "oracle": _cell(1.0), "nop": _cell(0.0)}
    oracle_only = {"task": "skipped", "class": "clean", "oracle": _cell(1.0), "nop": _cell(None, "skipped")}
    out = tmp_path / "out"
    out.mkdir()
    (out / "audit.json").write_text(json.dumps([legacy, current, oracle_only]))

    assert corpus.load_prior(out, resume=True, controls=False) == [current, oracle_only]
    prior = corpus.load_prior(out, resume=True)
    assert prior == [current]
    superseded = json.loads((out / "audit.superseded.json").read_text())
    assert superseded == [legacy, legacy, oracle_only]  # appended, never overwritten

    pending = corpus.select_tasks(
        discover_task_packages(root), corpus_root=root, done=frozenset(r["task"] for r in prior)
    )
    assert sorted(p.name for p in pending) == ["old", "skipped"]


def test_skip_controls_runs_only_the_reference(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    _package(root, "a")
    _package(root, "b")
    agents: list[str] = []

    def run_trial(package: Path, agent: str) -> AuditObservation:
        agents.append(agent)
        return _observation(1.0 if package.name == "a" else 0.0)

    out = tmp_path / "out"
    rows, _ = corpus.run_corpus(
        discover_task_packages(root), corpus_root=root, out=out, run_trial=run_trial,
        command=_command, controls=False, clock=_ticks(), today=date(2026, 11, 15),
    )
    assert agents == ["oracle", "oracle"]
    assert [row["class"] for row in rows] == ["clean", "reference_below_one"]
    assert all(row["nop"] == {"reward": None, "status": "skipped", "seconds": 0.0} for row in rows)
    report = (out / "AUDIT.md").read_text()
    assert "no nop control (--skip-controls)" in report
    assert "| a | 1 |  | clean |" in report
    assert corpus.summary_line(rows, controls=False) == (
        "2 tasks: 0 control_paid, 1 reference_below_one, 0 reference_unmeasured, "
        "0 cannot_run, 0 no_reference, 1 clean; controls skipped"
    )


def test_spend_cap_in_the_nop_cell_stops_without_writing_a_row(tmp_path: Path) -> None:
    _package(tmp_path / "corpus", "a")

    def run_trial(package: Path, agent: str) -> AuditObservation:
        if agent == "nop":
            raise SpendCapReached("over the approved $1.00")
        return _observation(1.0)

    rows, stopped = corpus.run_corpus(
        discover_task_packages(tmp_path / "corpus"),
        corpus_root=tmp_path / "corpus",
        out=tmp_path / "out",
        run_trial=run_trial,
        command=_command,
    )
    assert stopped and rows == []
    assert json.loads((tmp_path / "out" / "audit.json").read_text()) == []


def test_summary_line_counts_every_class_in_report_order() -> None:
    rows = [{"class": c} for c in ("clean", "clean", "cannot_run", "no_reference", "control_paid")]
    assert corpus.summary_line(rows) == (
        "5 tasks: 1 control_paid, 0 reference_below_one, 0 reference_unmeasured, "
        "1 cannot_run, 1 no_reference, 2 clean"
    )


def test_reproduce_command_round_trips_through_the_shell(tmp_path: Path) -> None:
    args = argparse.Namespace(
        skip_controls=True, trial_timeout=900, max_usd=2.5, approve="Peter ok 2026-11-15",
        usd_per_execution=0.0025,
    )
    command = corpus.reproduce_command(
        args, corpus_root=tmp_path / "c d", out=tmp_path / "out", tasks=["group/b"]
    )
    assert shlex.split(command) == [
        "uv", "run", "evallab", "qa", "corpus", str(tmp_path / "c d"), "--out", str(tmp_path / "out"),
        "--task", "group/b", "--skip-controls", "--trial-timeout", "900", "--max-usd", "2.5",
        "--approve", "Peter ok 2026-11-15", "--usd-per-execution", "0.0025",
    ]
