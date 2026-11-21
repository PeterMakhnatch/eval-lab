import re

from tidegauge.audit import audit, audit_arm, audit_pair, worst
from tidegauge.load import load_arm, rekey

from harbor_fixture import arm, trial, write_job

CONDITION = re.compile(r"^(.*)-(?:silent|talk)$")


def codes(findings, severity=None):
    return sorted(f.code for f in findings if severity is None or f.severity == severity)


def clean(tmp_path, name="clean"):
    outcomes = {f"task-{i}": [1.0, 0.0, 1.0] for i in range(3)}
    return load_arm(arm(tmp_path, name, outcomes))


def test_a_clean_arm_has_no_finding(tmp_path):
    assert audit_arm(clean(tmp_path)) == []


def test_empty_directory(tmp_path):
    (tmp_path / "empty").mkdir()
    assert codes(audit_arm(load_arm(tmp_path / "empty"))) == ["no-trials"]


def test_invalid_trials_warn_only_when_the_score_moves(tmp_path):
    moved = write_job(tmp_path, "moved", [trial("a", 1.0), trial("a", None, exception="ApiError")])
    (finding,) = [f for f in audit_arm(load_arm(moved)) if f.code == "invalid-trials"]
    assert finding.severity == "warn"
    assert (finding.detail["score_valid"], finding.detail["score_all"]) == (1.0, 0.5)
    same = write_job(tmp_path, "same", [trial("a", 0.0), trial("a", None, exception="ApiError")])
    (finding,) = [f for f in audit_arm(load_arm(same)) if f.code == "invalid-trials"]
    assert finding.severity == "note"


def test_incomplete_job_and_retries(tmp_path):
    job = write_job(tmp_path, "job", [trial("a", 1.0), trial("a", 0.0)], planned=5, retries=2)
    findings = audit_arm(load_arm(job))
    assert "incomplete-job" in codes(findings, "error")
    assert "retried-trials" in codes(findings, "note")


def test_unfinished_and_unreadable(tmp_path):
    job = write_job(tmp_path, "job", [trial("a", 1.0), trial("a", 1.0)])
    (job / "x").mkdir()
    (job / "x" / "result.json").write_text("{")
    (job / "y").mkdir()
    (job / "y" / "config.json").write_text('{"trial_name": "y", "task": {}}')
    assert {"unreadable-result", "unfinished-trials"} <= set(codes(audit_arm(load_arm(job)), "error"))


def test_control_agents_inside_real_attempts_are_an_error(tmp_path):
    job = write_job(tmp_path, "job", [trial("a", 0.0), trial("a", 1.0, agent="oracle")])
    (finding,) = [f for f in audit_arm(load_arm(job)) if f.code == "control-agent-trials"]
    assert finding.severity == "error" and finding.detail["passed"] == 1
    controls = write_job(
        tmp_path, "controls", [trial("a", 1.0, agent="oracle"), trial("a", 1.0, agent="oracle")]
    )
    assert "control-agent-trials" not in codes(audit_arm(load_arm(controls)))


def test_mixed_arm(tmp_path):
    job = write_job(
        tmp_path,
        "job",
        [
            trial("a", 1.0, model="anthropic/claude-opus-4-6", environment="docker"),
            trial("a", 1.0, model="openai/gpt-5.5", environment="daytona", version="unknown"),
        ],
    )
    fields = sorted(f.detail["field"] for f in audit_arm(load_arm(job)) if f.code == "mixed-arm")
    assert fields == ["environment", "model"]  # "unknown" is not a second agent version


def test_limits_attempts_and_single_attempt(tmp_path):
    job = write_job(
        tmp_path,
        "job",
        [trial("a", 1.0, config={"agent_timeout_multiplier": 9.0}), trial("a", 1.0), trial("b", 0.0)],
    )
    found = codes(audit_arm(load_arm(job)))
    assert "non-default-limits" in found and "uneven-attempts" in found
    single = write_job(tmp_path, "single", [trial("a", 1.0), trial("b", 0.0)])
    assert "single-attempt" in codes(audit_arm(load_arm(single)), "note")


def test_task_identity(tmp_path):
    job = write_job(
        tmp_path,
        "job",
        [trial("x/task-1.0", 1.0, checksum="one"), trial("task-1-0", 1.0, checksum="two")],
    )
    found = codes(audit_arm(load_arm(job)))
    assert "merged-task-names" in found and "checksum-drift" in found


def test_task_key_merges_variants_in_different_arms_without_a_finding(tmp_path):
    def keyed(name, condition, **kwargs):
        outcomes = {f"flotilla/t{i}-{condition}": [1.0, 0.0] for i in range(3)}
        return rekey(load_arm(arm(tmp_path, name, outcomes, **kwargs)), CONDITION)

    silent, talk, again = keyed("silent", "silent"), keyed("talk", "talk"), keyed("again", "talk")
    assert sorted(silent.by_task()) == sorted(talk.by_task()) == ["t0", "t1", "t2"]
    # Each variant has its own files. That is the design: no finding in 1 arm, in a pair, or in a group.
    assert audit([silent, talk]) == [] and audit([silent, talk, again]) == []
    # The same task name with other files is still a finding.
    other = keyed("other", "talk", checksum="other-files")
    assert codes(audit([talk, other])) == ["checksum-mismatch"]
    # 1 arm with 2 variants under 1 key: both findings stay.
    both = [
        trial("flotilla/t0-silent", 1.0, finished_at="2026-02-05T16:11:00Z"),
        trial("flotilla/t0-talk", 0.0, finished_at="2026-02-05T16:12:00Z"),
    ]
    mixed = rekey(load_arm(write_job(tmp_path, "mixed", both)), CONDITION)
    assert codes(audit_arm(mixed)) == ["checksum-drift", "merged-task-names"]


def test_trials_that_the_task_key_does_not_match(tmp_path):
    outcomes = {"flotilla/t0-talk": [1.0, 0.0], "flotilla/t1": [1.0, 0.0], "flotilla/t2-solo": [0.0]}
    keyed = rekey(load_arm(arm(tmp_path, "keyed", outcomes)), CONDITION)
    (note,) = [f for f in audit_arm(keyed) if f.code == "unmatched-task-key"]
    assert (note.severity, note.detail) == (
        "note",
        {"count": 3, "names": ["flotilla/t1", "flotilla/t2-solo"]},
    )
    assert note.message == (
        "3 of 5 (60.0%) trials have a task name that --task-key does not match "
        "(flotilla/t1 ×2, flotilla/t2-solo ×1). They keep the key of their own name."
    )
    assert sorted(keyed.by_task()) == ["t0", "t1", "t2-solo"]
    every = rekey(load_arm(arm(tmp_path, "every", {"flotilla/t0-talk": [1.0, 0.0]})), CONDITION)
    assert "unmatched-task-key" not in codes(audit_arm(every))


def test_zero_dispersion(tmp_path):
    flat = load_arm(arm(tmp_path, "flat", {f"t{i}": [float(i % 2)] * 3 for i in range(20)}))
    assert codes(audit_arm(flat)) == ["zero-dispersion"]
    few = load_arm(arm(tmp_path, "few", {f"t{i}": [float(i % 2)] * 3 for i in range(19)}))
    assert codes(audit_arm(few)) == []


def test_duplicate_trials(tmp_path):
    copies = write_job(tmp_path, "copies", [trial("a", 1.0), trial("a", 1.0), trial("b", 1.0)])
    (finding,) = [f for f in audit_arm(load_arm(copies)) if f.code == "duplicate-trials"]
    assert finding.severity == "error" and finding.detail["copies"] == 1


def test_pass_with_exception_usage_and_reward_range(tmp_path):
    job = write_job(
        tmp_path,
        "job",
        [
            trial("a", 1.0, exception="AgentTimeoutError", tokens=None, finished_at="2026-02-05T16:11:00Z"),
            trial("a", 2.0, finished_at="2026-02-05T16:12:00Z"),
            trial("b", None, rewards={"x": 1.0, "y": 0.0}, finished_at="2026-02-05T16:13:00Z"),
        ],
    )
    found = codes(audit_arm(load_arm(job)))
    assert {"pass-with-exception", "missing-usage", "reward-out-of-range", "no-primary-reward"} <= set(found)


def test_pair_checks(tmp_path):
    a = load_arm(
        write_job(
            tmp_path,
            "a",
            [trial("t1", 1.0, checksum="one"), trial("t1", 0.0, checksum="one"), trial("t2", 1.0)],
            harbor_version="0.24.0",
        )
    )
    b = load_arm(
        write_job(
            tmp_path,
            "b",
            [trial("t1", 1.0, checksum="two", environment="modal"), trial("t3", 1.0, environment="modal")],
            harbor_version="0.26.0",
        )
    )
    found = audit_pair(a, b)
    assert codes(found) == [
        "attempts-mismatch",
        "checksum-mismatch",
        "condition-mismatch",
        "condition-mismatch",
        "task-set-mismatch",
    ]
    keys = sorted(f.detail["key"] for f in found if f.code == "condition-mismatch")
    assert keys == ["Harbor version", "environment"]


def test_audit_of_two_arms_runs_the_pair_checks(tmp_path):
    a, b = clean(tmp_path, "a"), clean(tmp_path, "b")
    assert audit([a, b]) == []
    assert worst([]) is None
    other = load_arm(arm(tmp_path, "other", {"task-0": [1.0, 0.0]}))
    assert "task-set-mismatch" in codes(audit([a, other]))


def test_audit_of_many_arms_groups_them(tmp_path):
    same = [clean(tmp_path, name) for name in ("a", "b", "c")]
    assert audit(same) == []
    odd = load_arm(arm(tmp_path, "odd", {"task-0": [1.0, 0.0]}, checksum="other", environment="modal"))
    findings = audit([*same, odd])
    assert codes(findings) == ["environments", "task-sets", "task-versions"]
    groups = next(f.detail["groups"] for f in findings if f.code == "environments")
    assert groups == [["a", "b", "c"], ["odd"]]
