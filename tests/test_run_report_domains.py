"""Domain sections: detection, hospital verifier + chart queries, ATLAS gates, malformed inputs.

Fixture shapes come from the upstream sources, not invented contracts:

- hospital ``verifier/reward.json`` is exactly what
  ``harbor/templates/test.sh`` writes (``sparkcpark/synthetic_hospital``
  @ ``77cc57e``): ``{"reward": float, "steps": int, "submitted": 1.0|0.0}``.
  Chart/submit tool names are the EHR tools in
  ``epic_sim/app/routers/agent.py``; the ``sh-agent call`` wrapping is the
  documented agent path in ``scripts/harbor_export.py``.
- ATLAS ``verifier/grader/info.json`` is the gandalf-finance
  ``EvaluationInfo`` payload (``antoinepangas-hs/gandalf-finance`` @
  ``142dda0``, ``src/gandalf/models.py``); the pass rule mirrors
  ``scripts/pass_at_k.py`` in ``handshake-ai-research/ATLAS-Finance`` @
  ``6060bce`` (required weight >= 3, penalties, section gates fail).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.interpretation import domains
from evallab.interpretation.run_report import build_run_report, render_run_report_markdown


def _result(task_name: str = "trial-task", trial_name: str = "trial", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "trial-id",
        "trial_name": trial_name,
        "task_name": task_name,
        "config": {"agent": {"name": "oracle"}},
        "agent_info": {"name": "oracle"},
        "verifier_result": {"rewards": {"reward": 1.0}},
        "started_at": "2026-09-01T00:00:00Z",
        "finished_at": "2026-09-01T00:10:00Z",
    }
    body.update(overrides)
    return body


def _trial(
    root: Path,
    name: str,
    result: dict[str, Any],
    files: dict[str, Any] | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> Path:
    trial = root / name
    (trial / "agent").mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({**result, "trial_name": name}), encoding="utf-8")
    doc = {
        "schema_version": "ATIF-v1.7",
        "session_id": "s",
        "agent": {"name": "oracle"},
        "steps": steps if steps is not None else [],
    }
    (trial / "agent" / "trajectory.json").write_text(json.dumps(doc), encoding="utf-8")
    for relative, payload in (files or {}).items():
        path = trial / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    return trial


def _hospital_reward(**overrides: Any) -> dict[str, Any]:
    reward: dict[str, Any] = {"reward": 0.75, "steps": 12, "submitted": 1.0}
    reward.update(overrides)
    return reward


def _atlas_info(
    criteria: list[dict[str, Any]], sections: list[dict[str, Any]], **overrides: Any
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "reward": 0.5,
        "raw_score": 5.0,
        "maximum_score": 10.0,
        "criterion_results": criteria,
        "section_results": sections,
    }
    info.update(overrides)
    return info


def _crit(
    text: str, weight: float, met: bool | None, section: str | None = "Section 1", **extra: Any
) -> dict[str, Any]:
    return {
        "criterion": text,
        "weight": weight,
        "section": section,
        "gate": False,
        "met": met,
        "reasoning": "judged",
        "score_contribution": weight if met else 0.0,
        **extra,
    }


def test_plain_trial_has_no_domain(tmp_path: Path) -> None:
    trial = _trial(tmp_path, "trial", _result(), files={"verifier/reward.json": {"reward": 1.0}})
    report = build_run_report(trial)
    assert report["domain"] is None
    assert "## Domain" not in render_run_report_markdown(report)


def test_hospital_detected_by_task_markers(tmp_path: Path) -> None:
    result = _result(task_name="sh-patient-diagnosis")
    result["task_id"] = {"path": "datasets/sh-patient-diagnosis-42"}
    trial = _trial(tmp_path, "sh-patient-diagnosis-42", result, files={"verifier/reward.json": _hospital_reward()})
    report = build_run_report(trial)
    domain = report["domain"]
    assert domain is not None and domain["plugin"] == "synthetic_hospital"
    assert domain["version"] == "1"
    assert domain["status"] == "ok"
    assert (domain["reward"], domain["verifier_steps"], domain["submitted"]) == (0.75, 12, 1.0)
    assert domain["sources"] == ["verifier/reward.json"]
    markdown = render_run_report_markdown(report)
    assert "## Domain: synthetic_hospital" in markdown


def test_hospital_detected_by_verifier_shape_alone(tmp_path: Path) -> None:
    trial = _trial(
        tmp_path, "renamed-trial", _result(), files={"verifier/reward.json": _hospital_reward(reward=0.0, submitted=0.0)}
    )
    domain = build_run_report(trial)["domain"]
    assert domain is not None and domain["plugin"] == "synthetic_hospital"
    assert domain["reward"] == 0.0
    assert domain["submitted"] == 0.0


def _chart_step(step_id: int, calls: list[dict[str, Any]]) -> dict[str, Any]:
    return {"step_id": step_id, "source": "agent", "timestamp": "2026-09-01T00:02:00Z", "tool_calls": calls}


def test_hospital_chart_queries_count_redundant_section_reads(tmp_path: Path) -> None:
    steps = [
        _chart_step(1, [{"function_name": "open_chart", "arguments": {"patient_id": 7}}]),
        _chart_step(2, [{"function_name": "view_section", "arguments": {"section_id": 9}}]),
        _chart_step(3, [{"function_name": "view_section", "arguments": {"section_id": 9}}]),
        _chart_step(
            4,
            [
                {
                    "function_name": "bash",
                    "arguments": {"command": "sh-agent call search_chart '{\"patient_id\": 7, \"query\": \"Chest Pain\"}'"},
                }
            ],
        ),
        _chart_step(
            5,
            [
                {
                    "function_name": "bash",
                    "arguments": {"command": "sh-agent call search_chart '{\"patient_id\": 7, \"query\": \"chest  pain\"}'"},
                }
            ],
        ),
        _chart_step(6, [{"function_name": "submit_diagnosis", "arguments": {"patient_id": 7}}]),
    ]
    trial = _trial(
        tmp_path, "sh-x-1", _result(task_name="sh-evidence-retrieval"), files={"verifier/reward.json": _hospital_reward()}, steps=steps
    )
    queries = build_run_report(trial)["domain"]["chart_section_queries"]
    assert queries["chart_reads"] == 5
    assert queries["distinct_sections"] == 3
    assert queries["redundant_queries"] == 2
    assert {row["tool"] for row in queries["most_repeated"]} == {"view_section", "search_chart"}


def test_hospital_missing_or_malformed_reward_is_unreadable(tmp_path: Path) -> None:
    detected = _result(task_name="sh-patient-diagnosis")
    missing = _trial(tmp_path, "sh-a-1", detected)
    assert build_run_report(missing)["domain"] == {
        "plugin": "synthetic_hospital",
        "version": "1",
        "status": "unreadable",
        "reason": "verifier/reward.json missing",
    }
    wrong_shape = _trial(tmp_path, "sh-a-2", detected, files={"verifier/reward.json": {"reward": 1.0}})
    assert build_run_report(wrong_shape)["domain"]["status"] == "unreadable"
    not_object = _trial(tmp_path, "sh-a-3", detected, files={"verifier/reward.json": [1, 2]})
    assert build_run_report(not_object)["domain"]["status"] == "unreadable"


def test_atlas_detected_and_gates_attributed(tmp_path: Path) -> None:
    criteria = [
        _crit("valuation model correct", 10.0, True, "Model"),
        _crit("sources cited", 3.0, False, "Model"),
        _crit("downstream DCF uses model output", 5.0, False, "Writeup"),
        _crit("fabricated comparable", -2.0, True, "Writeup"),
        _crit("cosmetic formatting", 1.0, False, "Writeup"),
    ]
    sections = [
        {"section": "Model", "score": 10.0, "failed_section_gate_indices": [0], "section_gate_met": False},
        {"section": "Writeup", "score": 0.0, "failed_section_gate_indices": [], "section_gate_met": True},
    ]
    trial = _trial(
        tmp_path,
        "alderwick-env3__task_01__a1",
        _result(task_name="atlas-finance/alderwick-env3__task_01"),
        files={"verifier/grader/info.json": _atlas_info(criteria, sections)},
    )
    report = build_run_report(trial)
    domain = report["domain"]
    assert domain is not None and domain["plugin"] == "atlas_finance"
    assert domain["sources"] == ["verifier/grader/info.json"]
    assert domain["passes"] is False
    by_name = {s["section"]: s for s in domain["sections"]}
    assert by_name["Model"]["verdict"] == "fail"
    assert by_name["Model"]["gate_failed"] is True
    assert by_name["Writeup"]["verdict"] == "fail"
    assert domain["first_failed_gate"] == "Model"
    assert domain["downstream_loss"]["attributed_to"] == "Model"
    assert domain["downstream_loss"]["weight"] == 5.0
    assert domain["downstream_loss"]["criteria"] == ["downstream DCF uses model output"]
    assert any("penalty triggered" in reason for reason in domain["reasons"])
    assert "## Domain: atlas_finance" in render_run_report_markdown(report)


def test_atlas_criterion_gate_indices_do_not_fail_section(tmp_path: Path) -> None:
    """Upstream regression guard: failed_gate_indices names gate-flagged criteria, not the section gate."""
    criteria = [_crit("c", 3.0, True, "Section 1", gate=True)]
    sections = [
        {
            "section": "Section 1",
            "score": 3.0,
            "failed_gate_indices": [15],
            "section_gate_met": True,
            "section_gates_met": True,
            "passed_section_gate_indices": [0],
            "failed_section_gate_indices": [],
        }
    ]
    trial = _trial(
        tmp_path,
        "task__a1",
        _result(),
        files={"verifier/grader/info.json": _atlas_info(criteria, sections)},
    )
    domain = build_run_report(trial)["domain"]
    assert domain is not None and domain["plugin"] == "atlas_finance"
    assert domain["passes"] is True
    assert domain["sections"][0]["gate_failed"] is False


def test_atlas_skipped_and_minor_misses_pass(tmp_path: Path) -> None:
    criteria = [
        _crit("critical ok", 3.0, True),
        _crit("skipped cosmetic", 1.0, None, skipped=True),
        _crit("minor miss", 1.0, False),
    ]
    trial = _trial(
        tmp_path, "task__a2", _result(), files={"verifier/grader/info.json": _atlas_info(criteria, [])}
    )
    domain = build_run_report(trial)["domain"]
    assert domain is not None and domain["passes"] is True


def test_atlas_missing_or_malformed_info_is_unreadable(tmp_path: Path) -> None:
    detected = _result(task_name="atlas-finance/task-01")
    missing = _trial(tmp_path, "atlas-a-1", detected)
    assert build_run_report(missing)["domain"] == {
        "plugin": "atlas_finance",
        "version": "1",
        "status": "unreadable",
        "reason": "verifier/grader/info.json missing",
    }
    broken = _trial(tmp_path, "atlas-a-2", detected, files={"verifier/grader/info.json": {"reward": 1.0}})
    assert build_run_report(broken)["domain"]["status"] == "unreadable"


def test_plugin_exceptions_never_escape_domain_section(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Exploding:
        name = "exploding"
        version = "1"

        def detect(self, trial_dir: Path, result: dict[str, Any]) -> bool:
            return True

        def build(self, trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("boom")

        def render_markdown(self, section: dict[str, Any]) -> list[str]:
            raise AssertionError("unreachable")

    class FlakyDetect:
        name = "flaky"
        version = "1"

        def detect(self, trial_dir: Path, result: dict[str, Any]) -> bool:
            raise RuntimeError("no signal")

        def build(self, trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("unreachable")

        def render_markdown(self, section: dict[str, Any]) -> list[str]:
            raise AssertionError("unreachable")

    monkeypatch.setattr(domains, "PLUGINS", (Exploding(),))
    section = domains.domain_section(tmp_path, {})
    assert section is not None and section["status"] == "unreadable" and "boom" in section["reason"]

    monkeypatch.setattr(domains, "PLUGINS", (FlakyDetect(),))
    assert domains.domain_section(tmp_path, {}) is None


def test_hospital_missing_trajectory_counts_are_none_not_zero(tmp_path: Path) -> None:
    """Regression: with no trajectory the chart counts are unavailable, never zero."""
    trial = _trial(
        tmp_path,
        "sh-x-9",
        _result(task_name="sh-patient-diagnosis"),
        files={"verifier/reward.json": _hospital_reward()},
    )
    (trial / "agent" / "trajectory.json").unlink()
    report = build_run_report(trial)
    queries = report["domain"]["chart_section_queries"]
    assert queries["chart_reads"] is None
    assert queries["distinct_sections"] is None
    assert queries["redundant_queries"] is None
    assert queries["most_repeated"] == []
    assert queries["trajectory_note"]
    markdown = render_run_report_markdown(report)
    assert "Chart reads: unavailable" in markdown
    assert "0 redundant re-reads" not in markdown


def test_atlas_absent_reward_fields_stay_none(tmp_path: Path) -> None:
    """Audit lock: score fields absent from info.json are None, never zero."""
    info = _atlas_info([_crit("c", 3.0, True)], [])
    del info["reward"]
    del info["raw_score"]
    del info["maximum_score"]
    trial = _trial(tmp_path, "task__a3", _result(), files={"verifier/grader/info.json": info})
    domain = build_run_report(trial)["domain"]
    assert domain is not None and domain["passes"] is True
    assert domain["reward"] is None
    assert domain["raw_score"] is None
    assert domain["maximum_score"] is None
