"""Deterministic tests for Trace Lab Scout rules (HAR-109, $0).

Covers ``scout/rules.py`` (computed probe-03 rules on Eval Lab records)
and ``scout/import_evallab.py`` (Eval Lab metadata + tool calls). No model
calls, no launches, no uploads. HAR-81 trial directories are resolved like
``scout/raw_trials.py`` (dispatch-528 / dispatch-531 worktrees); tests skip
when those worktrees are absent. ``har81/capabilities.jsonl`` is used only
as a read-only expected-value reference -- the rules never read it.

Run from the Eval Lab worktree root::

    uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 -- \\
        python -m pytest research/explorations/trace-lab/scout/tests/ \\
        -p no:cacheprovider -o addopts=''
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCOUT = HERE.parent
TRACE_LAB = SCOUT.parent
for _p in (str(SCOUT), str(TRACE_LAB / "probe-03-capabilities")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import capabilities as cap  # noqa: E402 -- path set above
import rules  # noqa: E402

R528 = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs")
R531 = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs")
CAP_JSONL = TRACE_LAB / "probe-03-capabilities" / "har81" / "capabilities.jsonl"

GOLDEN_JOB = "har81-l-d-a2-arvo-18737"
GOLDEN_TRIAL = "har81-l-d-a2-arvo-18737__2kbVhKB"


def _trial_dir(job: str, trial: str) -> Path:
    for base in (R528, R531):
        candidate = base / job / trial
        if candidate.is_dir():
            return candidate
    pytest.skip(f"trial dir absent: {job}/{trial}")


def _cap_row(trial: str) -> dict:
    if not CAP_JSONL.is_file():
        pytest.skip("capabilities.jsonl absent")
    for line in CAP_JSONL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("trial") == trial:
            return row
    pytest.skip(f"no capabilities row for {trial}")
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Pure rule units (synthetic steps, no trial dirs).
# ---------------------------------------------------------------------------


def test_harness_accepted_terminal_output_is_accepted():
    value, rule = cap.harness_accepted("New Terminal Output:\nuser@host:~$ ls\n")
    assert (value, rule) == ("true", "H-ACC-TERM-TRUE")


def test_harness_accepted_parse_error_is_rejected():
    value, rule = cap.harness_accepted("Previous response had parsing errors: ...")
    assert (value, rule) == ("false", "H-ACC-PARSEERR-FALSE")


def test_harness_accepted_empty_is_unknown():
    assert cap.harness_accepted(None) == ("unknown", "H-ACC-UNKNOWN")
    assert cap.harness_accepted("") == ("unknown", "H-ACC-UNKNOWN")


def test_classify_shape_prose():
    shape, rule = cap.classify_shape("The task is complete.", 0)
    assert (shape, rule) == ("prose", "SHAPE-PROSE")


def test_classify_shape_broken_native_is_unparseable():
    shape, _ = cap.classify_shape("<function=write><parameter=path>x</parameter>", 0)
    assert shape == "unparseable"


def test_identical_runs_counts_steps_not_ids():
    seq = [("head", {"step_id": i, "message": "same"}) for i in (5, 6, 7, 8, 9, 10, 11, 12, 13, 14)]
    seq.append(("head", {"step_id": 15, "message": "different"}))
    runs = cap.identical_runs(seq)
    assert len(runs) == 1
    assert runs[0]["length"] == 10
    assert (runs[0]["start"], runs[0]["end"]) == (5, 14)


def test_identical_runs_ignores_short_runs():
    seq = [("head", {"step_id": i, "message": "same"}) for i in range(1, 10)]
    assert cap.identical_runs(seq) == []


# ---------------------------------------------------------------------------
# Golden trial: computed rules match the hand-verified expectations.
# ---------------------------------------------------------------------------


def test_golden_outcome_is_submit_contract_unclear():
    trial_dir = _trial_dir(GOLDEN_JOB, GOLDEN_TRIAL)
    out = rules.analyze_trial_rules(trial_dir)["outcome"]
    assert out["rule_id"] == "R-COMP-03"
    assert (out["tag"], out["attribution"]) == ("completion", "unclear")
    assert out["evidence_step_refs"] == ["head#28", "head#118"]


def test_golden_first_failure_is_null():
    trial_dir = _trial_dir(GOLDEN_JOB, GOLDEN_TRIAL)
    assert rules.analyze_trial_rules(trial_dir)["first_failure"] is None


def test_golden_stop_is_input_ceiling():
    trial_dir = _trial_dir(GOLDEN_JOB, GOLDEN_TRIAL)
    stop = rules.analyze_trial_rules(trial_dir)["stop"]
    assert stop["reason"] == "ceiling:input_tokens"
    assert stop["exception_type"] == "TrialBudgetExhaustedError"
    assert stop["natural_completion"] is False

def test_golden_handshake_never_confirmed():
    trial_dir = _trial_dir(GOLDEN_JOB, GOLDEN_TRIAL)
    handshake = rules.analyze_trial_rules(trial_dir)["handshake"]
    assert handshake is not None
    assert handshake["first_prompt_ref"] == "head#22"
    assert handshake["confirmed"] is False
    assert handshake["echo_task_complete_turns"] == 95


def test_golden_loop_span():
    trial_dir = _trial_dir(GOLDEN_JOB, GOLDEN_TRIAL)
    loops = rules.analyze_trial_rules(trial_dir)["loops"]
    assert loops["spans"] == [
        {"kind": "identical", "length": 91, "span": ["head#28", "head#118"]}
    ]


def test_golden_wedge_empty():
    trial_dir = _trial_dir(GOLDEN_JOB, GOLDEN_TRIAL)
    assert rules.analyze_trial_rules(trial_dir)["wedge"]["stretches"] == []


# ---------------------------------------------------------------------------
# Spot checks across rule families (expected values from the frozen file).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "trial",
    [
        "har81-l-d-a2-arvo-42496599__rzayj9j",  # R-COMP-03 engaged + wedge
        "har81-l-d-a2-candidate-1789-secu__4kHk8vj",  # R-ENV-02
        "har81-l-d-a2-candidate-1634-soft__B8Jt3cw",  # R-COMP-02
        "har81-l-d-a4-candidate-1271-medi__wCBkiuG",  # R-PLAN-01
    ],
)
def test_spot_outcome_matches_frozen_row(trial):
    row = _cap_row(trial)
    job = Path(row["trial_dir"]).parent.name
    out = rules.analyze_trial_rules(_trial_dir(job, trial))["outcome"]
    frozen = row["outcome_relevant_failure"]
    assert out["rule_id"] == frozen["rule_id"]
    assert (out["tag"], out["attribution"]) == (frozen["tag"], frozen["attribution"])
    assert out["evidence_step_refs"] == frozen["evidence_step_refs"]


def test_full_44_rule_ids_match_frozen_rows():
    if not CAP_JSONL.is_file():
        pytest.skip("capabilities.jsonl absent")
    checked = 0
    for line in CAP_JSONL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        trial_dir = Path(row["trial_dir"])
        if not trial_dir.is_dir():
            continue
        got = rules.analyze_trial_rules(trial_dir)
        frozen_first = row["first_failure"]
        got_first = got["first_failure"]
        assert (got_first or {}).get("rule_id") == (frozen_first or {}).get("rule_id"), row["trial"]
        assert got["outcome"]["rule_id"] == row["outcome_relevant_failure"]["rule_id"], row["trial"]
        assert got["stop"]["reason"] == row["stop"]["reason"], row["trial"]
        checked += 1
    assert checked == 44
