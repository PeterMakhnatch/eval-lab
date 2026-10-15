"""Behavioral regressions for defensive task QA (controls, rubric, evidence, scoring).

All instruments are deterministic fakes: no Docker, no model calls, no network,
and no exploit generation or execution.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from evallab.cli import parser as build_root_parser
from evallab.qa import models as shared_models
from evallab.qa import score as shared_score
from evallab.qa.answer_key import load_answer_key as load_shared_answer_key
from evallab.qa.task import (
    AnswerKeyItem,
    Check,
    CodexLane,
    Finding,
    LaneReply,
    QATaskConfig,
    QuotaStop,
    Refutation,
    Reproducer,
    Reviewer,
    Signal,
    Subject,
    TaskReport,
    bundle_verifier_aware,
    finalize,
    finite_reward,
    import_evidence,
    load_findings,
    load_rubric,
    observation_check,
    run_task,
    score_command,
    score_findings,
    scoreboard_row,
    submission_to_script,
    validate_triplet,
)
from evallab.registry import task_directory_digest

RUBRIC = """\
[[criteria]]
name = "verifiable"
description = "Verifier is reliable"
guidance = "PASS if deterministic."

[[criteria]]
name = "functional_verification"
description = "Tests verify actual behaviour"
guidance = "FAIL if tests only check strings."

[[criteria]]
name = "task_clarity"
description = "Editorial clarity criterion outside DEFECT_CRITERIA"
guidance = "PASS if clear."
"""


def _package(root: Path, name: str = "fake-task") -> Path:
    package = root / name
    (package / "tests").mkdir(parents=True)
    (package / "solution").mkdir()
    (package / "environment").mkdir()
    (package / "task.toml").write_text(
        f'schema_version = "1.0"\n[task]\nname = "bench/{name}"\n',
        encoding="utf-8",
    )
    (package / "instruction.md").write_text("Write the answer to /app/out.txt.\n", encoding="utf-8")
    (package / "tests" / "test.sh").write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
    (package / "solution" / "solve.sh").write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
    (package / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    return package


def _obs(status: str, facts: dict[str, object], reason: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(status=status, facts=facts, reason=reason, sources=())


class FakeBackend:
    def __init__(
        self,
        *,
        oracle: tuple[object, ...] = (1.0, 1.0, 1.0),
        nop: tuple[object, ...] = (0.0, 0.0, 0.0),
        control_status: str = "executed",
        leak_status: str = "executed",
        leak_facts: dict[str, object] | None = None,
        blocker: str | None = None,
        shortcut_rewards: tuple[object, ...] = (1.0,),
        replay_reward: object = 1.0,
        ref_subset_report: dict[str, object] | None = None,
    ) -> None:
        self.oracle = list(oracle)
        self.nop = list(nop)
        self.control_status = control_status
        self.leak_status = leak_status
        self.leak_facts: dict[str, object] = (
            {"has_future_history": False, "coverage": "no-repository", "job_path": "runs/leak"}
            if leak_facts is None
            else leak_facts
        )
        self.blocker = blocker
        self.shortcut_rewards = list(shortcut_rewards)
        self.replay_reward = replay_reward
        self.ref_subset_report = ref_subset_report or {
            "status": "not_applicable",
            "verdict": "unknown",
            "reason": "fake: no multi-block reference repair",
        }
        self.control_calls: list[str] = []
        self.shortcut_calls: list[dict[str, object]] = []
        self.replay_calls: list[Reproducer] = []
        self.ref_subset_calls = 0

    def resolve(self, package: Path) -> SimpleNamespace:
        package = Path(package).resolve()
        return SimpleNamespace(
            task_id=f"bench/{package.name}",
            package_digest=task_directory_digest(package),
            harbor_digest="sha256:harbor",
        )

    def resource_blocker(self, package: Path) -> str | None:
        return self.blocker

    def static(self, task: object) -> SimpleNamespace:
        return _obs(
            "recorded",
            {
                "coverage": "harbor-test-source",
                "hack_scan": {"available": True, "findings": []},
            },
        )

    def control(self, task: object, agent: str, *, timeout_seconds: int) -> SimpleNamespace:
        self.control_calls.append(agent)
        values = self.oracle if agent == "oracle" else self.nop
        item = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(item, SimpleNamespace):
            return item
        if self.control_status != "executed":
            return _obs(self.control_status, {"reward": item}, reason="infra failure")
        return _obs(
            "executed", {"reward": item, "job_path": f"runs/{agent}-{len(self.control_calls)}"}
        )

    def leak(self, task: object, *, run_dir: Path, timeout_seconds: int) -> SimpleNamespace:
        return _obs(self.leak_status, dict(self.leak_facts))

    def grade_shortcut(
        self,
        package: Path,
        submission: dict[str, object],
        *,
        digest: str,
        run_dir: Path,
        timeout_seconds: int,
    ) -> tuple[SimpleNamespace, Reproducer]:
        self.shortcut_calls.append(submission)
        reward = (
            self.shortcut_rewards.pop(0)
            if len(self.shortcut_rewards) > 1
            else self.shortcut_rewards[0]
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        script = run_dir / "shortcut.sh"
        script.write_text("#!/usr/bin/env bash\ntrue\n", encoding="utf-8")
        return (
            _obs(
                "executed",
                {
                    "reward": reward,
                    "job_path": str(run_dir / "trial"),
                    "verifier_stdout": "fake verifier stdout",
                    "agent_stdout": "fake agent stdout",
                },
            ),
            Reproducer(kind="command", path=str(script), expected_reward=1.0),
        )

    def replay_reproducer(
        self,
        package: Path,
        reproducer: Reproducer,
        *,
        digest: str,
        run_dir: Path,
        timeout_seconds: int,
    ) -> SimpleNamespace:
        self.replay_calls.append(reproducer)
        if self.replay_reward is None:
            return _obs(
                "failed",
                {"reward": None, "exception": "fake replay unavailable"},
                reason="fake replay unavailable",
            )
        return _obs(
            "executed",
            {"reward": self.replay_reward, "job_path": str(run_dir / "trial")},
        )

    def ref_subset(
        self,
        package: Path,
        *,
        digest: str,
        run_dir: Path,
        ref_patch: Path | None = None,
        timeout_seconds: int = 1800,
    ) -> dict[str, object]:
        self.ref_subset_calls += 1
        return dict(self.ref_subset_report)


class FakeLane:
    model = "codex/fake"

    def __init__(self, replies: list[dict[str, object] | str | Exception] | None = None) -> None:
        default_vote = {
            "criteria": {
                "verifiable": {"outcome": "pass", "reason": "deterministic"},
                "functional_verification": {"outcome": "pass", "reason": "checks output"},
                "task_clarity": {"outcome": "pass", "reason": "clear prompt"},
            }
        }
        self.replies = list(replies) if replies is not None else [default_vote] * 3
        self.prompts: list[str] = []
        self.labels: list[str] = []

    def complete(self, prompt: str, *, label: str) -> LaneReply:
        self.prompts.append(prompt)
        self.labels.append(label)
        item = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(item, Exception):
            raise item
        text = item if isinstance(item, str) else json.dumps(item)
        return LaneReply(text=text, tokens=10, model=self.model)


def _config(tmp_path: Path, **overrides: object) -> QATaskConfig:
    rubric = tmp_path / "rubric.toml"
    rubric.write_text(RUBRIC, encoding="utf-8")
    values: dict[str, object] = {"rubric_path": rubric, "repeats": 3, "samples": 3}
    values.update(overrides)
    return QATaskConfig(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "invalid",
    [True, False, None, "1.0", math.nan, math.inf, -math.inf, -0.01, 1.01, [1.0]],
)
def test_finite_reward_rejects_bool_nonfinite_and_out_of_bounds(invalid: object) -> None:
    assert finite_reward(invalid) is None


def test_observation_check_fails_closed_on_failed_status_exception_or_bad_reward(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path, stages=("S0",))
    # Failed control status with reward 1.0 is still unusable.
    failed_with_reward = _obs("failed", {"reward": 1.0}, reason="container exited")
    check = observation_check(failed_with_reward, agent="oracle", config=cfg)
    assert check.status == "ran_without_usable_result"
    assert check.verdict == "unknown"

    # Executed status with an exception recorded alongside reward 1.0 is unusable.
    with_exception = _obs("executed", {"reward": 1.0, "exception": "TimeoutError"})
    check_exc = observation_check(with_exception, agent="oracle", config=cfg)
    assert check_exc.status == "ran_without_usable_result"
    assert check_exc.verdict == "unknown"

    # Boolean and non-finite rewards are rejected.
    for bad_reward in (True, False, math.nan, math.inf, -0.1, 1.2):
        check_bad = observation_check(
            _obs("executed", {"reward": bad_reward}), agent="oracle", config=cfg
        )
        assert check_bad.status == "ran_without_usable_result"
        assert check_bad.verdict == "unknown"


def test_absent_controls_or_rubric_never_give_vacuous_pass() -> None:
    subject = Subject(kind="task", task_id="bench/t", task_digest="sha256:1")
    empty_report = TaskReport(
        subject=subject,
        package="/tmp/t",
        requested_stages=["S0"],
        started_at="2026-10-14T00:00:00+00:00",
        checks={"static": Check(stage="S0", status="measured", scored=False)},
    )
    finalize(empty_report, [])
    assert empty_report.quality_complete is False
    assert empty_report.verdict == "unknown"

    unrun_rubric = TaskReport(
        subject=subject,
        package="/tmp/t",
        requested_stages=["S1"],
        started_at="2026-10-14T00:00:00+00:00",
        checks={"rubric": Check(stage="S1", status="not_run", reason="model lane disabled")},
    )
    finalize(unrun_rubric, [])
    assert unrun_rubric.quality_complete is False
    assert unrun_rubric.verdict == "unknown"


def test_s0_runs_all_three_oracle_and_three_nop_even_when_first_fails(tmp_path: Path) -> None:
    package = _package(tmp_path)
    backend = FakeBackend(oracle=(0.0, 1.0, 1.0), nop=(0.0, 0.5, 0.0))
    report = run_task(
        package,
        config=_config(tmp_path, stages=("S0",)),
        run_dir=tmp_path / "run",
        backend=backend,
        lane=None,
    )
    assert backend.control_calls == ["oracle", "oracle", "oracle", "nop", "nop", "nop"]
    for idx in (1, 2, 3):
        assert f"oracle-{idx}" in report.checks
        assert f"nop-{idx}" in report.checks
    assert report.checks["oracle-1"].verdict == "fail"
    assert report.checks["oracle-2"].verdict == "pass"
    assert report.checks["nop-2"].verdict == "fail"
    assert report.verdict == "fail"
    assert report.quality_complete is True

    findings, _ = load_findings(tmp_path / "run")
    assert {f.id.split(".")[-1] for f in findings} == {"oracle-1", "nop-2"}
    assert {f.category for f in findings} == {"broken", "weak"}
    assert all(f.refutation.status == "verified" for f in findings)
    assert all(f.reproducer.kind == "harbor_trial" for f in findings)


@pytest.mark.parametrize(
    ("leak_status", "leak_facts"),
    [
        ("failed", {"has_future_history": False, "coverage": "no-repository"}),
        ("executed", {"has_future_history": False, "coverage": "partial-scan"}),
        ("executed", {"has_future_history": False}),
        ("executed", {"has_future_history": None, "coverage": "probe-image-inspection"}),
    ],
)
def test_s0_leak_incomplete_status_or_coverage_stays_unknown(
    tmp_path: Path, leak_status: str, leak_facts: dict[str, object]
) -> None:
    package = _package(tmp_path)
    backend = FakeBackend(leak_status=leak_status, leak_facts=leak_facts)
    report = run_task(
        package,
        config=_config(tmp_path, stages=("S0",)),
        run_dir=tmp_path / "run",
        backend=backend,
        lane=None,
    )
    assert report.checks["leak"].status == "ran_without_usable_result"
    assert report.checks["leak"].verdict == "unknown"
    assert report.quality_complete is False
    assert report.verdict == "unknown"


def test_s0_measured_leak_records_signal_and_prevents_pass_without_fake_finding(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    backend = FakeBackend(
        leak_status="executed",
        leak_facts={"has_future_history": True, "coverage": "probe-image-inspection"},
    )
    report = run_task(
        package,
        config=_config(tmp_path, stages=("S0",)),
        run_dir=tmp_path / "run",
        backend=backend,
        lane=None,
    )
    assert report.checks["leak"].status == "measured"
    assert report.checks["leak"].verdict == "fail"
    assert report.finding_ids == []
    assert len(report.signals) == 1
    assert report.signals[0].category == "reachable"
    assert report.verdict == "unknown"


def test_s1_includes_all_rubric_criteria_and_two_thirds_flag_stays_signal_not_verified(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    criteria = load_rubric(_config(tmp_path).rubric_path)
    assert [c["name"] for c in criteria] == [
        "verifiable",
        "functional_verification",
        "task_clarity",
    ]

    fail_vote = {
        "criteria": {
            "verifiable": {"outcome": "pass", "reason": "ok"},
            "functional_verification": {
                "outcome": "fail",
                "category": "weak",
                "reason": "tests/test.sh only runs true",
            },
            "task_clarity": {"outcome": "pass", "reason": "clear"},
        }
    }
    pass_vote = {
        "criteria": {
            "verifiable": {"outcome": "pass", "reason": "ok"},
            "functional_verification": {"outcome": "pass", "reason": "ok"},
            "task_clarity": {"outcome": "not_applicable", "reason": "n/a"},
        }
    }
    lane = FakeLane([fail_vote, fail_vote, pass_vote])
    report = run_task(
        package,
        config=_config(tmp_path, stages=("S0", "S1")),
        run_dir=tmp_path / "run",
        backend=FakeBackend(),
        lane=lane,
    )
    assert len(lane.prompts) == 3
    for name in ("verifiable", "functional_verification", "task_clarity"):
        assert name in lane.prompts[0]
        assert f"rubric:{name}" in report.checks
        assert report.checks[f"rubric:{name}"].status == "measured"

    assert report.checks["rubric:verifiable"].verdict == "pass"
    assert report.checks["rubric:task_clarity"].verdict == "pass"
    assert report.checks["rubric:functional_verification"].verdict == "fail"
    # 2/3 rubric failure creates a Signal on the report, no verified Finding, and verdict stays unknown.
    assert report.finding_ids == []
    assert len(report.signals) == 1
    assert report.signals[0].category == "weak"
    assert report.verdict == "unknown"
    findings, _ = load_findings(tmp_path / "run")
    assert findings == []


def test_s1_malformed_or_incomplete_sample_cannot_pass(tmp_path: Path) -> None:
    package = _package(tmp_path)
    complete_pass = {
        "criteria": {
            "verifiable": {"outcome": "pass", "reason": "ok"},
            "functional_verification": {"outcome": "pass", "reason": "ok"},
            "task_clarity": {"outcome": "pass", "reason": "ok"},
        }
    }
    # Sample 3 omits task_clarity -> incomplete review -> must not count toward 3-sample majority.
    incomplete = {
        "criteria": {
            "verifiable": {"outcome": "pass", "reason": "ok"},
            "functional_verification": {"outcome": "pass", "reason": "ok"},
        }
    }
    lane = FakeLane([complete_pass, complete_pass, incomplete])
    report = run_task(
        package,
        config=_config(tmp_path, stages=("S1",)),
        run_dir=tmp_path / "run-incomplete",
        backend=FakeBackend(),
        lane=lane,
    )
    assert report.checks["rubric-sample-3"].status == "ran_without_usable_result"
    for name in ("verifiable", "functional_verification", "task_clarity"):
        assert report.checks[f"rubric:{name}"].status == "ran_without_usable_result"
        assert report.checks[f"rubric:{name}"].verdict == "unknown"
    assert report.quality_complete is False
    assert report.verdict == "unknown"

    # Malformed non-JSON sample also prevents pass.
    malformed_lane = FakeLane([complete_pass, complete_pass, "not-json-at-all"])
    report_malformed = run_task(
        package,
        config=_config(tmp_path, stages=("S1",)),
        run_dir=tmp_path / "run-malformed",
        backend=FakeBackend(),
        lane=malformed_lane,
    )
    assert report_malformed.checks["rubric-sample-3"].status == "ran_without_usable_result"
    assert report_malformed.quality_complete is False
    assert report_malformed.verdict == "unknown"


def test_s1_no_model_and_quota_stop_record_not_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = _package(tmp_path)
    no_model_report = run_task(
        package,
        config=_config(tmp_path, stages=("S1",)),
        run_dir=tmp_path / "run-no-model",
        backend=FakeBackend(),
        lane=None,
    )
    assert no_model_report.checks["rubric"].status == "not_run"
    assert no_model_report.quality_complete is False
    assert no_model_report.verdict == "unknown"

    quota_lane = FakeLane([QuotaStop("weekly quota at 60%; ceiling is 60%")])
    quota_report = run_task(
        package,
        config=_config(tmp_path, stages=("S1",)),
        run_dir=tmp_path / "run-quota",
        backend=FakeBackend(),
        lane=quota_lane,
    )
    assert quota_report.checks["rubric-sample-1"].status == "not_run"
    assert quota_report.checks["rubric-sample-2"].status == "not_run"
    assert quota_report.checks["rubric-sample-3"].status == "not_run"
    assert quota_report.checks["rubric:verifiable"].status == "not_run"
    assert quota_report.quality_complete is False
    assert quota_report.verdict == "unknown"

    lane = CodexLane(repo_root=tmp_path, scratch_root=tmp_path / "codex")

    def fake_run(fraction: object) -> SimpleNamespace:
        doc = {
            "reports": [
                {
                    "provider": "openai-codex",
                    "limits": [{"window": {"id": "7d"}, "amount": {"usedFraction": fraction}}],
                }
            ]
        }
        return SimpleNamespace(stdout=json.dumps(doc))

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_run(0.59))
    assert lane.gate() == 0.59
    for blocked in (0.60, 0.85, True, None):
        monkeypatch.setattr("subprocess.run", lambda *a, _b=blocked, **kw: fake_run(_b))
        with pytest.raises(QuotaStop):
            lane.gate()


def test_finding_requires_reproducer_and_import_binds_subject_digest_and_downgrades_verified(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    digest = task_directory_digest(package)
    subject = Subject(kind="task", task_id="bench/fake-task", task_digest=digest)

    with pytest.raises(ValidationError):
        Finding(
            id="fake.no-reproducer",
            subject=subject,
            stage="S2",
            verdict="fail",
            category="weak",
            severity="high",
            fault="eval",
            evidence_refs=["ref"],
            reproducer=None,  # type: ignore[arg-type]
            refutation=Refutation(status="unverified", by="test", reason="none"),
            reviewer=Reviewer(agent="external"),
        )

    claimed_verified = Finding(
        id="external.claim-1",
        subject=subject,
        stage="S3",
        verdict="fail",
        category="tamperable",
        severity="high",
        fault="eval",
        evidence_refs=["runs/ext/trial-1"],
        reproducer=Reproducer(kind="patch", path="runs/ext/sub.patch", expected_reward=1.0),
        refutation=Refutation(status="verified", by="external-harness", reason="claimed"),
        reviewer=Reviewer(agent="external"),
    )
    ev_dir = tmp_path / "evidence"
    ev_dir.mkdir()
    ev_file = ev_dir / "claim.json"
    ev_file.write_text(claimed_verified.model_dump_json(by_alias=True, indent=2), encoding="utf-8")

    # Mismatched digest or task_id is rejected on import.
    wrong_digest = Subject(kind="task", task_id="bench/fake-task", task_digest="sha256:other")
    with pytest.raises(ValueError, match="evidence does not bind"):
        import_evidence(ev_file, wrong_digest)

    wrong_task = Subject(kind="task", task_id="bench/other-task", task_digest=digest)
    with pytest.raises(ValueError, match="evidence does not bind"):
        import_evidence(ev_file, wrong_task)

    triplet_file = tmp_path / "triplet.json"
    triplet_file.write_text(
        json.dumps(
            {
                "subject": subject.model_dump(mode="json"),
                "wrong_submission": {"status": "measured", "reward": 1.0},
                "oracle": {"status": "measured", "reward": 1.0},
                "nop": {"status": "measured", "reward": 0.0},
            }
        ),
        encoding="utf-8",
    )
    # Matching subject+digest imports into S2 and downgrades verified -> unverified.
    # Executed S2 runs with no lane (shortcut not_run); S3 replay is unusable in this fake,
    # so the imported candidate stays unverified and never counts as a pass.
    report = run_task(
        package,
        config=_config(tmp_path, stages=("S0", "S2", "S3")),
        run_dir=tmp_path / "run",
        backend=FakeBackend(replay_reward=None),
        lane=None,
        evidence=ev_dir,
        triplet=triplet_file,
    )
    assert report.checks["imported-evidence"].status == "measured"
    assert report.checks["shortcut-agent"].status == "not_run"
    assert report.checks["supplied-triplet"].status == "measured"
    assert report.checks["supplied-triplet"].verdict == "pass"
    assert report.checks["fresh-replay"].status == "ran_without_usable_result"
    assert report.quality_complete is False
    assert report.verdict == "unknown"

    findings, _ = load_findings(tmp_path / "run")
    (imported,) = findings
    assert imported.id == "external.claim-1"
    assert imported.stage == "S2"
    assert imported.refutation.status == "unverified"
    assert imported.refutation.by == "qa-task/S3"


@pytest.mark.parametrize(
    ("wrong", "oracle", "nop", "expected"),
    [
        (0.01, 1.0, 0.0, "pass"),
        (1.0, 1.0, 0.0, "pass"),
        (0.0, 1.0, 0.0, "fail"),
        (1.0, 0.99, 0.0, "fail"),
        (1.0, 1.0, 0.01, "fail"),
    ],
)
def test_validate_triplet_numeric_boundaries(
    wrong: float, oracle: float, nop: float, expected: str
) -> None:
    triplet = {
        "wrong_submission": {"status": "measured", "reward": wrong},
        "oracle": {"status": "measured", "reward": oracle},
        "nop": {"status": "measured", "reward": nop},
    }
    assert validate_triplet(triplet) == expected


@pytest.mark.parametrize(
    "bad_triplet",
    [
        {},
        {
            "wrong_submission": {"status": "not_run", "reward": 1.0},
            "oracle": {"status": "measured", "reward": 1.0},
            "nop": {"status": "measured", "reward": 0.0},
        },
        {
            "wrong_submission": {"status": "measured", "reward": True},
            "oracle": {"status": "measured", "reward": 1.0},
            "nop": {"status": "measured", "reward": 0.0},
        },
        {
            "wrong_submission": {"status": "measured", "reward": 1.0},
            "oracle": {"status": "measured", "reward": math.nan},
            "nop": {"status": "measured", "reward": 0.0},
        },
    ],
)
def test_validate_triplet_rejects_unmeasured_or_invalid_records(
    bad_triplet: dict[str, object],
) -> None:
    assert validate_triplet(bad_triplet) == "unknown"


def _exploit_reply() -> dict[str, object]:
    return {
        "status": "exploit",
        "category": "weak",
        "mechanism_hypothesis": "tests/test_outputs.py does not check X",
        "summary": "shortcut submission",
        "submission": {"kind": "command", "path": "/app/x.py", "content": "true"},
    }


def _no_bypass_reply() -> dict[str, object]:
    return {
        "status": "no_credible_bypass",
        "category": "weak",
        "mechanism_hypothesis": "verifier compares hashed outputs",
        "summary": "no shortcut found",
        "submission": None,
    }


def test_s2_shortcut_retries_with_grader_feedback_and_s3_confirms_triplet(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    backend = FakeBackend(shortcut_rewards=(0.0, 1.0), replay_reward=1.0)
    lane = FakeLane([_exploit_reply(), _exploit_reply()])
    report = run_task(
        package,
        config=_config(tmp_path, stages=("S0", "S2", "S3")),
        run_dir=tmp_path / "run",
        backend=backend,
        lane=lane,
    )
    assert len(backend.shortcut_calls) == 2
    assert len(lane.prompts) == 2
    assert "Sequential Attempt Feedback" in lane.prompts[1]
    assert "fake verifier stdout" in lane.prompts[1]
    assert report.checks["shortcut-agent"].status == "measured"
    assert report.checks["shortcut-agent"].verdict == "fail"
    assert report.checks["ref-subset"].status == "measured"
    assert report.checks["fresh-replay"].verdict == "fail"
    assert report.quality_complete is True
    assert report.verdict == "fail"
    findings, _ = load_findings(tmp_path / "run")
    (finding,) = findings
    assert finding.refutation.status == "verified"
    assert finding.refutation.by == "qa-task/S3"
    assert len(backend.replay_calls) == 1


def test_s2_two_no_bypass_replies_stop_early_and_clean_task_passes(tmp_path: Path) -> None:
    package = _package(tmp_path)
    backend = FakeBackend()
    lane = FakeLane([_no_bypass_reply(), _no_bypass_reply(), _exploit_reply()])
    report = run_task(
        package,
        config=_config(tmp_path, stages=("S0", "S2", "S3")),
        run_dir=tmp_path / "run",
        backend=backend,
        lane=lane,
    )
    assert len(lane.prompts) == 2
    assert backend.shortcut_calls == []
    assert report.checks["shortcut-agent"].verdict == "pass"
    assert report.checks["fresh-replay"].verdict == "pass"
    assert report.verdict == "pass"
    assert report.quality_complete is True
    findings, _ = load_findings(tmp_path / "run")
    assert findings == []


@pytest.mark.parametrize(
    ("replay_reward", "expected_status", "expected_verdict"),
    [(1.0, "verified", "fail"), (0.0, "refuted", "pass")],
)
def test_s3_refutes_or_confirms_ref_subset_candidate(
    tmp_path: Path, replay_reward: float, expected_status: str, expected_verdict: str
) -> None:
    package = _package(tmp_path)
    minimal = tmp_path / "minimal.patch"
    minimal.write_text("--- a/app.py\n+++ b/app.py\n", encoding="utf-8")
    backend = FakeBackend(
        replay_reward=replay_reward,
        ref_subset_report={
            "status": "measured",
            "verdict": "fail",
            "reason": "proper subset passes",
            "kept_fraction": 0.25,
            "minimal_patch": str(minimal),
            "actionable": True,
        },
    )
    lane = FakeLane([_no_bypass_reply(), _no_bypass_reply()])
    report = run_task(
        package,
        config=_config(tmp_path, stages=("S0", "S2", "S3")),
        run_dir=tmp_path / "run",
        backend=backend,
        lane=lane,
    )
    assert report.checks["ref-subset"].verdict == "fail"
    findings, _ = load_findings(tmp_path / "run")
    (finding,) = findings
    assert finding.category == "weak"
    assert finding.refutation.status == expected_status
    assert report.verdict == expected_verdict


def test_bundle_verifier_aware_excludes_solution_cheat_and_golden_contents(tmp_path: Path) -> None:
    package = _package(tmp_path)
    (package / "solution" / "solve.sh").write_text("SECRET_SOLUTION", encoding="utf-8")
    (package / "tests" / "test_outputs.py").write_text("def test_x(): pass\n", encoding="utf-8")
    golden = package / "tests" / "golden-files"
    golden.mkdir()
    (golden / "answer.csv").write_text("SECRET_GOLDEN", encoding="utf-8")
    cheat = package / "cheat"
    cheat.mkdir()
    (cheat / "x.py").write_text("SECRET_CHEAT", encoding="utf-8")
    bundle = bundle_verifier_aware(package)
    blob = json.dumps(bundle)
    assert "SECRET_SOLUTION" not in blob
    assert "SECRET_GOLDEN" not in blob
    assert "SECRET_CHEAT" not in blob
    assert "tests/test_outputs.py" in bundle["files"]
    assert any("golden-files/answer.csv" in item for item in bundle["omitted"])


def test_submission_to_script_converts_kinds_and_rejects_bad_json(tmp_path: Path) -> None:
    built = submission_to_script(
        {"kind": "file", "path": "/app/out.txt", "content": "hello"}, out_dir=tmp_path / "a"
    )
    assert built is not None
    script, reproducer = built
    assert "/app/out.txt" in script and reproducer.kind == "command"
    assert (
        submission_to_script(
            {"kind": "json", "path": "/app/out.json", "content": "{bad"}, out_dir=tmp_path / "b"
        )
        is None
    )
    patch_built = submission_to_script(
        {"kind": "patch", "path": "/app", "content": "--- a/x\n+++ b/x\n"},
        out_dir=tmp_path / "c",
    )
    assert patch_built is not None
    assert patch_built[1].kind == "patch"
    assert (
        submission_to_script(
            {"kind": "file", "path": "relative/out.txt", "content": "x"}, out_dir=tmp_path / "d"
        )
        is None
    )


def _make_finding(
    *,
    finding_id: str,
    task_id: str,
    task_digest: str,
    status: str,
    category: str = "weak",
) -> Finding:
    return Finding(
        id=finding_id,
        subject=Subject(kind="task", task_id=task_id, task_digest=task_digest),
        stage="S0",
        verdict="fail",
        category=category,  # type: ignore[arg-type]
        severity="high",
        fault="eval",
        evidence_refs=["runs/control-1"],
        reproducer=Reproducer(kind="harbor_trial", path="runs/control-1", expected_reward=0.0),
        refutation=Refutation(status=status, by="test", reason="test"),  # type: ignore[arg-type]
        reviewer=Reviewer(agent="qa-task"),
    )


def test_score_findings_enforces_digest_pin_separates_flag_verified_category_and_coverage(
    tmp_path: Path,
) -> None:
    key_dir = tmp_path / "key"
    key_dir.mkdir()
    items = [
        # Positive 1: verified finding matches expected category ("weak").
        AnswerKeyItem(
            key_id="k1",
            item_id="item-cat-match",
            subject=Subject(kind="task", task_id="bench/cat-match", task_digest="sha256:cat"),
            package={"name": "cat-match", "root_hint": str(tmp_path)},
            expected_verdict="fail",
            expected_category="weak",
            expected_categories=["weak"],
            root_cause=[{"id": "rc-cat-1", "summary": "passive provenance"}],
            label_source="audit",
            label_strength="verified",
            split="heldout",
        ),
        # Positive 2: verified finding has different category ("broken" vs expected "weak").
        AnswerKeyItem(
            key_id="k1",
            item_id="item-cat-mismatch",
            subject=Subject(
                kind="task", task_id="bench/cat-mismatch", task_digest="sha256:mismatch"
            ),
            package={"name": "cat-mismatch", "root_hint": str(tmp_path)},
            expected_verdict="fail",
            expected_category="weak",
            expected_categories=["weak"],
            root_cause=[{"id": "rc-2", "summary": "passive provenance"}],
            label_source="audit",
            label_strength="verified",
            split="heldout",
        ),
        # Positive 3: no matching report or findings in run dir -> unobserved and unmeasured, never pass.
        AnswerKeyItem(
            key_id="k1",
            item_id="item-missing",
            subject=Subject(kind="task", task_id="bench/missing", task_digest="sha256:miss"),
            package={"name": "missing", "root_hint": str(tmp_path)},
            expected_verdict="fail",
            expected_category="weak",
            expected_categories=["weak"],
            root_cause=[{"id": "rc-miss-1"}],
            label_source="audit",
            label_strength="verified",
            split="heldout",
        ),
        # Negative 1: clean task has only an S1 signal (and incomplete measurement) -> flagged false alarm, not verified.
        AnswerKeyItem(
            key_id="k1",
            item_id="item-clean-flagged",
            subject=Subject(kind="task", task_id="bench/clean", task_digest="sha256:clean"),
            package={"name": "clean", "root_hint": str(tmp_path)},
            expected_verdict="pass",
            label_source="audit",
            label_strength="reviewed",
            split="heldout",
        ),
    ]
    (key_dir / "items.jsonl").write_text(
        "".join(i.model_dump_json(by_alias=True) + "\n" for i in items),
        encoding="utf-8",
    )

    run_dir = tmp_path / "run"
    (run_dir / "findings").mkdir(parents=True)
    findings = [
        _make_finding(
            finding_id="f-cat",
            task_id="bench/cat-match",
            task_digest="sha256:cat",
            status="verified",
            category="weak",
        ),
        _make_finding(
            finding_id="f-cat-mismatch",
            task_id="bench/cat-mismatch",
            task_digest="sha256:mismatch",
            status="verified",
            category="broken",
        ),
        # Mismatched digest on bench/clean must be ignored for scoring and counted in digest_mismatches.
        _make_finding(
            finding_id="f-clean-stale",
            task_id="bench/clean",
            task_digest="sha256:stale-digest",
            status="verified",
            category="broken",
        ),
    ]
    for finding in findings:
        (run_dir / "findings" / f"{finding.id}.json").write_text(
            finding.model_dump_json(by_alias=True, indent=2),
            encoding="utf-8",
        )

    reports = [
        TaskReport(
            subject=Subject(kind="task", task_id="bench/cat-match", task_digest="sha256:cat"),
            package="/tmp/cat-match",
            requested_stages=["S0"],
            started_at="t",
            finished_at="t",
            verdict="fail",
            quality_complete=True,
            checks={"oracle-1": Check(stage="S0", status="measured", verdict="fail")},
        ),
        TaskReport(
            subject=Subject(
                kind="task", task_id="bench/cat-mismatch", task_digest="sha256:mismatch"
            ),
            package="/tmp/cat-mismatch",
            requested_stages=["S0"],
            started_at="t",
            finished_at="t",
            verdict="fail",
            quality_complete=True,
            checks={"oracle-1": Check(stage="S0", status="measured", verdict="fail")},
        ),
        TaskReport(
            subject=Subject(kind="task", task_id="bench/clean", task_digest="sha256:clean"),
            package="/tmp/clean",
            requested_stages=["S0", "S1"],
            started_at="t",
            finished_at="t",
            verdict="unknown",
            quality_complete=False,
            signals=[
                Signal(
                    id="clean.rubric-functional_verification",
                    category="weak",
                    evidence_refs=["suspicious test"],
                    reviewer=Reviewer(agent="qa-task", model="codex/fake"),
                )
            ],
            checks={
                "rubric:functional_verification": Check(
                    stage="S1", status="measured", verdict="fail"
                )
            },
        ),
        # Mismatched-digest signal-only report on bench/missing must increment digest_mismatches and not count as observed.
        TaskReport(
            subject=Subject(
                kind="task", task_id="bench/missing", task_digest="sha256:stale-report"
            ),
            package="/tmp/missing",
            requested_stages=["S1"],
            started_at="t",
            finished_at="t",
            verdict="unknown",
            quality_complete=False,
            signals=[
                Signal(
                    id="missing.rubric-verifiable",
                    category="broken",
                    evidence_refs=["stale"],
                    reviewer=Reviewer(agent="qa-task"),
                )
            ],
        ),
    ]
    for idx, rep in enumerate(reports):
        rep_dir = run_dir / "tasks" / f"t{idx}"
        rep_dir.mkdir(parents=True)
        (rep_dir / "report.json").write_text(
            rep.model_dump_json(by_alias=True, indent=2),
            encoding="utf-8",
        )

    score = score_findings(key_dir=key_dir, findings_dir=run_dir, split="heldout")
    # No finding names a root cause: not measured, never a 0.
    assert score["verified_root_cause_match"] is None
    assert score["flagged_root_cause_match"] is None
    assert score["items"] == 4
    assert score["positives"] == 3
    assert score["negatives"] == 1
    assert score["digest_mismatches"] == 2
    assert score["verified_recall"] == pytest.approx(2 / 3)
    assert score["flagged_recall"] == pytest.approx(2 / 3)
    assert score["category_match_recall"] == pytest.approx(1 / 3)
    # Stale verified finding on bench/clean was excluded by digest; S1 signal only triggers flagged false alarm.
    assert score["verified_false_alarm_rate"] == 0.0
    assert score["flagged_false_alarm_rate"] == 1.0
    # 3 of 4 items have matching reports (observed=3/4), and 2 of 4 have quality_complete=True (measured=2/4).
    assert score["observed_fraction"] == pytest.approx(3 / 4)
    assert score["measured_fraction"] == pytest.approx(2 / 4)
    rows_by_id = {row["item_id"]: row for row in score["rows"]}
    assert rows_by_id["item-cat-match"]["labelled_cause"] is True
    assert rows_by_id["item-missing"]["observed"] is False
    assert rows_by_id["item-missing"]["measured"] is False
    assert rows_by_id["item-missing"]["flagged_hit"] is False
    assert rows_by_id["item-cat-match"]["category_match"] is True
    assert rows_by_id["item-cat-mismatch"]["category_match"] is False

    # Once findings name causes, a verified hit naming the wrong cause (or none) is a miss.
    named = findings[0].model_copy(update={"root_cause": ["RC-cat-1"]})
    (run_dir / "findings" / f"{named.id}.json").write_text(
        named.model_dump_json(by_alias=True, indent=2), encoding="utf-8"
    )
    rescored = score_findings(key_dir=key_dir, findings_dir=run_dir, split="heldout")
    assert rescored["verified_root_cause_match"] == pytest.approx(1 / 2)
    row = scoreboard_row(rescored, level="verified")
    assert row.root_cause_match == pytest.approx(1 / 2)


def test_run_task_findings_load_and_score_through_shared_qa_contract(tmp_path: Path) -> None:
    package = _package(tmp_path)
    run_dir = tmp_path / "run"
    backend = FakeBackend(oracle=(0.0, 1.0, 1.0), nop=(0.0, 0.0, 0.0))
    run_task(
        package,
        config=_config(tmp_path, stages=("S0",)),
        run_dir=run_dir,
        backend=backend,
        lane=None,
    )
    loaded = shared_models.load_findings_dir(run_dir / "findings")
    assert len(loaded) == 1
    assert loaded[0].subject.task_id == "bench/fake-task"
    assert loaded[0].verdict == "fail"
    assert loaded[0].category == "broken"

    key_dir = tmp_path / "shared-key"
    key_dir.mkdir()
    (key_dir / "README.md").write_text("# Shared key\n", encoding="utf-8")
    shared_item = shared_models.AnswerKeyItem(
        key_id="shared-key",
        item_id="shared-key:bench/fake-task",
        subject=shared_models.FindingSubject(
            kind="task",
            task_id="bench/fake-task",
            task_digest=task_directory_digest(package),
        ),
        expected_category="broken",
        expected_verdict="fail",
        label_source="unit-test",
        split="heldout",
    )
    (key_dir / "items.jsonl").write_text(
        shared_item.model_dump_json(by_alias=True) + "\n",
        encoding="utf-8",
    )
    key = load_shared_answer_key(key_dir)
    rows = shared_score.score_findings(key, loaded, split="heldout")
    all_cat = next(r for r in rows if r.category == "all")
    assert all_cat.n_items == 1
    assert all_cat.n_measured == 1
    assert all_cat.recall == 1.0
    # S0 findings name no root cause: not measured (n/a), never a category stand-in.
    assert all_cat.root_cause_match is None
    assert "category_match=1/1" in all_cat.notes


def test_findings_survive_appledouble_files_from_a_macos_archive(tmp_path: Path) -> None:
    # A tarball made on macOS without COPYFILE_DISABLE=1 extracts a binary `._<name>`
    # metadata twin next to every file; neither loader may read those as findings.
    package = _package(tmp_path)
    run_dir = tmp_path / "run"
    run_task(
        package,
        config=_config(tmp_path, stages=("S0",)),
        run_dir=run_dir,
        backend=FakeBackend(oracle=(0.0, 1.0, 1.0), nop=(0.0, 0.0, 0.0)),
        lane=None,
    )
    expected, reports = load_findings(run_dir)
    appledouble = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        \x00\x02\xff\xfe"
    for path in [p for p in run_dir.rglob("*") if p.is_file()]:
        (path.parent / f"._{path.name}").write_bytes(appledouble)
    assert load_findings(run_dir) == (expected, reports)
    assert len(shared_models.load_findings_dir(run_dir / "findings")) == len(expected)


def test_root_parser_exposes_qa_commands_and_task_score_scoreboard_behavior(
    tmp_path: Path,
) -> None:
    root_parser = build_root_parser()
    parsed_task = root_parser.parse_args(
        ["qa", "task", "--task", str(tmp_path / "pkg"), "--no-model"]
    )
    assert parsed_task.qa_cmd == "task"
    parsed_shared_score = root_parser.parse_args(
        ["qa", "score", "--key", "k", "--findings", str(tmp_path / "f")]
    )
    assert parsed_shared_score.qa_cmd == "score"

    key_dir = tmp_path / "key"
    key_dir.mkdir()
    item = AnswerKeyItem(
        key_id="k1",
        item_id="item-1",
        subject=Subject(kind="task", task_id="bench/t1", task_digest="sha256:d1"),
        package={"name": "t1", "root_hint": str(tmp_path)},
        expected_verdict="fail",
        expected_category="broken",
        expected_categories=["broken"],
        label_source="audit",
        label_strength="verified",
        split="heldout",
    )
    (key_dir / "items.jsonl").write_text(
        item.model_dump_json(by_alias=True) + "\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    (run_dir / "findings").mkdir(parents=True)
    finding = _make_finding(
        finding_id="f-1",
        task_id="bench/t1",
        task_digest="sha256:d1",
        status="verified",
        category="broken",
    )
    (run_dir / "findings" / "f-1.json").write_text(
        finding.model_dump_json(by_alias=True, indent=2),
        encoding="utf-8",
    )
    (run_dir / "tasks" / "t1").mkdir(parents=True)
    report = TaskReport(
        subject=Subject(kind="task", task_id="bench/t1", task_digest="sha256:d1"),
        package="/tmp/t1",
        requested_stages=["S0"],
        started_at="t",
        finished_at="t",
        verdict="fail",
        quality_complete=True,
        checks={"oracle-1": Check(stage="S0", status="measured", verdict="fail")},
    )
    (run_dir / "tasks" / "t1" / "report.json").write_text(
        report.model_dump_json(by_alias=True, indent=2),
        encoding="utf-8",
    )

    scoreboard = tmp_path / "scoreboard.csv"
    args_no_board = root_parser.parse_args(
        ["qa", "task-score", "--key", str(key_dir), "--findings", str(run_dir)]
    )
    assert args_no_board.qa_cmd == "task-score"
    assert score_command(args_no_board, tmp_path) == 0
    assert not scoreboard.exists()

    args_with_board = root_parser.parse_args(
        [
            "qa",
            "task-score",
            "--key",
            str(key_dir),
            "--findings",
            str(run_dir),
            "--scoreboard",
            str(scoreboard),
            "--level",
            "verified",
        ]
    )
    assert score_command(args_with_board, tmp_path) == 0
    assert scoreboard.is_file()
    with scoreboard.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert tuple(reader.fieldnames or ()) == shared_score.SCOREBOARD_COLUMNS
        (row,) = list(reader)
    assert row["key_id"] == "k1"
    assert row["system"] == "qa-task@verified"
    assert row["root_cause_match"] == shared_score.NOT_AVAILABLE
