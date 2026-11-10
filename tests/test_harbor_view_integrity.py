"""Viewer integrity fallbacks and fail-open detector paths (HAR-376).

The viewer shows integrity 0/1 per trial. These tests pin that a broken stored
verdict falls back to computed provenance, that agent trajectories are parsed
defensively, and that every raising detector is listed in ``problems`` (the
detectors fail open by design, so the problem list is the only trace).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.harbor_view import (
    RULE_COPY_CHECK,
    RULE_GRADER_TAMPER,
    RULE_UPSTREAM_FETCH,
    _iter_trial_dirs,
    _raw_agent_steps,
    compute_hits,
    stored_hits,
    trial_integrity,
)

COPY_FLAG = {
    "kind": "copied_code",
    "matched_lines": 3,
    "added_lines": 4,
    "examples": ["copied line"],
}


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _job_with_trial(tmp_path: Path, *, record: object = None) -> tuple[Path, Path]:
    job = tmp_path / "job"
    trial = job / "trial-a"
    _write_json(trial / "result.json", {"trial_name": "trial-a"})
    (trial / "agent").mkdir()
    if record is not None:
        _write_json(job / "processed" / "trial-trial-a.json", record)
    return job, trial


def _confirmed_upstream_flag(target: str = "github.com/org/repo") -> dict[str, Any]:
    """A flag that the real ``confirmed_fetch`` accepts."""
    origin = {
        "target": target,
        "document": "fix.patch",
        "artifact": "/tmp/fix.patch",
        "step": 5,
        "call_id": "call-1",
    }
    read = {
        "target": target,
        "document": "fix.patch",
        "artifact": "/tmp/fix.patch",
        "step": 6,
        "call_id": "call-2",
        "acquisition_proof": "artifact_read",
    }
    return {
        "kind": "upstream_fetch",
        "outcome": "succeeded",
        "target": target,
        "document": "fix.patch",
        "call_id": "call-1",
        "step": 5,
        "outcome_evidence": [origin, read],
        "evidence": "curl -o /tmp/fix.patch ...",
    }


def _raise(exc: Exception):
    def _detector(*_args: object, **_kwargs: object) -> object:
        raise exc

    return _detector


@pytest.fixture
def quiet_detectors(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """All detectors report nothing; individual tests override one."""
    monkeypatch.setattr("evallab.copy_check.copy_check", lambda _trial: None)
    monkeypatch.setattr(
        "evallab.probe03.analyze_trial_core",
        lambda _trial, _job: {"agent_seq": [], "info": {}},
    )
    monkeypatch.setattr(
        "evallab.upstream_fetch.assess_upstream_fetch", lambda *_a, **_k: []
    )
    monkeypatch.setattr("evallab.tripwires.grader_tamper_hits", lambda _steps: [])
    return monkeypatch


# ---------------------------------------------------------------------------
# Trial discovery
# ---------------------------------------------------------------------------


def test_iter_trial_dirs_unreadable_job_is_empty(tmp_path: Path) -> None:
    assert _iter_trial_dirs(tmp_path / "missing") == []
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x", encoding="utf-8")
    assert _iter_trial_dirs(not_a_dir) == []


# ---------------------------------------------------------------------------
# Stored verdict mapping and fallback
# ---------------------------------------------------------------------------


def test_stored_hits_unusable_records_return_none(tmp_path: Path) -> None:
    job = tmp_path / "job"
    assert stored_hits(job, "a") == (None, "no stored processed record")

    _write_json(job / "processed" / "trial-a.json", ["not", "a", "dict"])
    assert stored_hits(job, "a") == (None, "no stored processed record")

    _write_json(
        job / "processed" / "trial-a.json",
        {"taint": [], "errors": {"analysis": "Traceback: boom"}},
    )
    assert stored_hits(job, "a") == (None, "stored analysis failed: Traceback: boom")

    _write_json(job / "processed" / "trial-a.json", {"taint": {"kind": "x"}})
    assert stored_hits(job, "a") == (None, "stored record has no taint list")


def test_stored_hits_maps_only_contract_flags(tmp_path: Path) -> None:
    job = tmp_path / "job"
    unconfirmed = dict(_confirmed_upstream_flag(), outcome="attempted")
    _write_json(
        job / "processed" / "trial-a.json",
        {
            "taint": [
                "not-a-dict",
                {"kind": "guard_reject"},
                unconfirmed,
                COPY_FLAG,
                _confirmed_upstream_flag(),
            ],
            "errors": {},
        },
    )
    hits, note = stored_hits(job, "a")
    assert note == "stored"
    assert hits is not None
    assert [(h["id"], h["source"]) for h in hits] == [
        (RULE_COPY_CHECK, "stored"),
        (RULE_UPSTREAM_FETCH, "stored"),
    ]
    assert hits[0]["evidence"]["source_artifact"] == "agent/trajectory.json"
    assert hits[1]["evidence"] == {
        "target": "github.com/org/repo",
        "excerpt": "curl -o /tmp/fix.patch ...",
    }


@pytest.mark.parametrize(
    ("record", "note"),
    [
        (
            {"taint": [], "errors": {"analysis": "probe crashed"}},
            "stored analysis failed: probe crashed",
        ),
        ({"taint": "corrupt", "errors": {}}, "stored record has no taint list"),
    ],
)
def test_broken_stored_record_falls_back_to_computed(
    tmp_path: Path,
    quiet_detectors: pytest.MonkeyPatch,
    record: dict[str, Any],
    note: str,
) -> None:
    # The stored taint is empty or corrupt; only recomputation can see the copy.
    quiet_detectors.setattr("evallab.copy_check.copy_check", lambda _trial: COPY_FLAG)
    job, trial = _job_with_trial(tmp_path, record=record)

    result = trial_integrity(trial, job)

    assert result["provenance"] == f"computed ({note})"
    assert result["integrity"] == 0
    assert result["fired"] == [RULE_COPY_CHECK]
    assert [h["source"] for h in result["hits"]] == ["computed"]
    assert result["problems"] == []


# ---------------------------------------------------------------------------
# Agent step parsing
# ---------------------------------------------------------------------------


def test_raw_agent_steps_without_agent_dir(tmp_path: Path) -> None:
    assert _raw_agent_steps(tmp_path) == []


def test_raw_agent_steps_parses_json_and_jsonl_defensively(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    agent.mkdir()
    step_json = {"source": "agent", "step_id": 1, "tool_calls": []}
    user_step = {"source": "user", "step_id": 2, "tool_calls": []}
    _write_json(
        agent / "trajectory.json",
        [
            {"steps": [step_json, "not-a-step", user_step]},
            {"step_id": 3, "tool_calls": [{"function_name": "bash"}]},
            {"step_id": 4, "message": "no tool calls"},
            "not-a-doc",
        ],
    )
    (agent / "trajectory.jsonl").write_text(
        "\n".join(
            [
                "",
                "{not json",
                json.dumps({"source": "agent", "step_id": 5, "tool_calls": []}),
                "   ",
                json.dumps({"steps": [{"source": "system", "step_id": 6}]}),
                json.dumps(7),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    steps = _raw_agent_steps(tmp_path)

    # Non-agent sources are dropped; a missing source counts as agent.
    assert [s["step_id"] for s in steps] == [1, 3, 5]


def test_raw_agent_steps_skips_bad_and_unreadable_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "trajectory.json").write_text("{truncated", encoding="utf-8")
    jsonl = agent / "trajectory.jsonl"
    jsonl.write_text(json.dumps({"step_id": 1, "tool_calls": []}), encoding="utf-8")
    assert [s["step_id"] for s in _raw_agent_steps(tmp_path)] == [1]

    real_read_text = Path.read_text

    def _read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == jsonl:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)
    assert _raw_agent_steps(tmp_path) == []


# ---------------------------------------------------------------------------
# Computed detectors
# ---------------------------------------------------------------------------


def test_raising_detectors_are_listed_as_problems(
    tmp_path: Path, quiet_detectors: pytest.MonkeyPatch
) -> None:
    quiet_detectors.setattr(
        "evallab.copy_check.copy_check", _raise(RuntimeError("copy boom"))
    )
    quiet_detectors.setattr(
        "evallab.upstream_fetch.assess_upstream_fetch",
        _raise(KeyError("agent_seq")),
    )
    quiet_detectors.setattr(
        "evallab.tripwires.grader_tamper_hits", _raise(ValueError("tamper boom"))
    )
    job, trial = _job_with_trial(tmp_path)

    result = trial_integrity(trial, job)

    # Fail open: no rule fires, but every failure stays visible by type name.
    assert result["integrity"] == 1
    assert result["fired"] == []
    assert result["provenance"] == "computed (no stored processed record)"
    assert result["problems"] == [
        "copy_check: RuntimeError: copy boom",
        "upstream_fetch: KeyError: 'agent_seq'",
        "grader_tamper: ValueError: tamper boom",
    ]


def test_raising_upstream_analysis_is_listed_as_problem(
    tmp_path: Path, quiet_detectors: pytest.MonkeyPatch
) -> None:
    quiet_detectors.setattr(
        "evallab.probe03.analyze_trial_core", _raise(OSError("no trajectory"))
    )
    job, trial = _job_with_trial(tmp_path)

    hits, problems = compute_hits(trial, job)

    assert hits == []
    assert problems == ["upstream_fetch: OSError: no trajectory"]


def test_compute_hits_keeps_first_confirmed_upstream_fetch(
    tmp_path: Path, quiet_detectors: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def _assess(agent_seq: Any, info: Any, *, trial_dir: Path) -> list[Any]:
        seen.update(agent_seq=agent_seq, info=info, trial_dir=trial_dir)
        return [
            "not-a-dict",
            dict(_confirmed_upstream_flag("github.com/a/unconfirmed"), outcome="failed"),
            _confirmed_upstream_flag("github.com/a/first"),
            _confirmed_upstream_flag("github.com/a/second"),
        ]

    quiet_detectors.setattr(
        "evallab.probe03.analyze_trial_core",
        lambda _trial, _job: {"agent_seq": [("agent", {})], "info": {"k": 1}},
    )
    quiet_detectors.setattr("evallab.upstream_fetch.assess_upstream_fetch", _assess)
    quiet_detectors.setattr(
        "evallab.copy_check.copy_check", lambda _trial: {"kind": "guard_reject"}
    )
    job, trial = _job_with_trial(tmp_path)

    hits, problems = compute_hits(trial, job)

    assert problems == []
    assert seen == {"agent_seq": [("agent", {})], "info": {"k": 1}, "trial_dir": trial}
    assert [(h["id"], h["source"]) for h in hits] == [(RULE_UPSTREAM_FETCH, "computed")]
    assert hits[0]["evidence"]["target"] == "github.com/a/first"


def test_compute_hits_marks_copy_hit_computed(
    tmp_path: Path, quiet_detectors: pytest.MonkeyPatch
) -> None:
    quiet_detectors.setattr("evallab.copy_check.copy_check", lambda _trial: COPY_FLAG)
    job, trial = _job_with_trial(tmp_path)

    hits, problems = compute_hits(trial, job)

    assert problems == []
    assert [(h["id"], h["source"]) for h in hits] == [(RULE_COPY_CHECK, "computed")]


# ---------------------------------------------------------------------------
# Stored verdict plus computed rechecks
# ---------------------------------------------------------------------------


def test_stored_empty_taint_recheck_failure_is_listed(
    tmp_path: Path, quiet_detectors: pytest.MonkeyPatch
) -> None:
    quiet_detectors.setattr(
        "evallab.copy_check.copy_check", _raise(OSError("diff too large"))
    )
    job, trial = _job_with_trial(tmp_path, record={"taint": [], "errors": {}})

    result = trial_integrity(trial, job)

    assert result["provenance"] == "stored"
    assert result["integrity"] == 1
    assert result["problems"] == ["copy_check: OSError: diff too large"]


def test_stored_grader_tamper_failure_is_listed(
    tmp_path: Path, quiet_detectors: pytest.MonkeyPatch
) -> None:
    quiet_detectors.setattr(
        "evallab.tripwires.grader_tamper_hits", _raise(TypeError("bad step"))
    )
    job, trial = _job_with_trial(tmp_path, record={"taint": [], "errors": {}})

    result = trial_integrity(trial, job)

    assert result["provenance"] == "stored"
    assert result["problems"] == ["grader_tamper: TypeError: bad step"]


def test_stored_copy_hit_skips_recheck_and_adds_tamper_once(
    tmp_path: Path, quiet_detectors: pytest.MonkeyPatch
) -> None:
    quiet_detectors.setattr(
        "evallab.copy_check.copy_check", _raise(AssertionError("must not rerun"))
    )
    tamper = [
        {"paths": ["tests/test_core.py"], "excerpt": "x" * 300},
        {"paths": ["tests/test_other.py"], "excerpt": "y"},
    ]
    quiet_detectors.setattr("evallab.tripwires.grader_tamper_hits", lambda _s: tamper)
    job, trial = _job_with_trial(tmp_path, record={"taint": [COPY_FLAG], "errors": {}})

    result = trial_integrity(trial, job)

    assert result["problems"] == []
    assert result["provenance"] == "stored+computed"
    assert result["fired"] == [RULE_COPY_CHECK, RULE_GRADER_TAMPER]
    assert [(h["id"], h["source"]) for h in result["hits"]] == [
        (RULE_COPY_CHECK, "stored"),
        (RULE_GRADER_TAMPER, "computed"),
    ]
    assert result["hits"][1]["evidence"] == {
        "n_steps": 2,
        "paths": ["tests/test_core.py"],
        "excerpt": "x" * 200,
    }
