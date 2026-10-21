"""The adaptive reviewer controller, driven through the provider-neutral driver seam.

A scripted driver stands in for any lane. The clock is injected, so wall-clock
budgets are exercised without sleeping. What is checked is what the session
actually executed, what it retained, and where it stopped.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import evallab.qa.env as env_module
from evallab.gepa_optimizer import codex_transport
from evallab.gepa_optimizer.codex_transport import CodexSession, CodexTransport, CodexTransportError
from evallab.qa import env_agent
from evallab.qa.agent_lanes import DriverError
from evallab.qa.env import EnvTools
from evallab.qa.env_agent import (
    MAX_OBSERVATION_CHARS,
    AuditBudget,
    EnvAuditSession,
    QuotaUnavailable,
    _bounded,
    parse_action,
    weekly_codex_used,
)
from evallab.qa.env_playbooks import defect_classes
from evallab.qa.models import load_findings_dir

THREAD = "00000000-0000-4000-8000-000000000001"
FINAL = json.dumps({"final": {"summary": "Reviewed the source only.", "limitations": ["No runtime executed."]}})


def call(*calls: tuple[str, dict[str, Any]]) -> str:
    return json.dumps({"calls": [{"tool": tool, "args": args} for tool, args in calls]})


def finding(**override: Any) -> tuple[str, dict[str, Any]]:
    args: dict[str, Any] = {
        "defect_class": "weak_check",
        "summary": "The check accepts 7 although the instruction requires 42.",
        "severity": "high", "fault": "eval",
        "evidence_refs": ["instruction.md:1", "tests/check.py:1-2"],
    }
    args.update(override)
    return "record-finding", args


def budget(seconds: float = 60, tokens: int = 10_000, calls: int = 6, usd: float = 0) -> AuditBudget:
    return AuditBudget(seconds, tokens, calls, usd)


# -- scripted provider, clock and quota --------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@dataclass
class Turn:
    response: str | Callable[[list[str]], str]
    tokens: tuple[int | None, int | None] = (10, 5)
    usd: float | None = 0.0
    elapsed: float = 1.0
    actual_model: str | None = None
    usage_known: bool = True


@dataclass
class Fail:
    error: Exception
    elapsed: float = 0.0


class FakeDriver:
    """The whole driver surface the controller may rely on; replies are scripted."""

    def __init__(self, script: list[Turn | Fail], clock: Clock, lane: str) -> None:
        self.lane = lane
        self.script = list(script)
        self.clock = clock
        self.thread_id: str | None = None
        self.invocations = 0
        self.facts: dict[str, Any] = {"lane": lane, "provider": "scripted"}
        self.cumulative: dict[str, int] = {}
        self.prompts: list[str] = []
        self.timeouts: list[float] = []
        self._usd = 0.0
        self._usd_known = True
        self._usage_complete = True

    def accounting(self) -> dict[str, Any]:
        return {
            "lane": self.lane, "metered_usd": self._usd if self._usd_known else None,
            "cost_basis": "scripted", "usage_complete": self._usage_complete,
        }

    def turn(self, prompt: str, *, timeout_seconds: float) -> dict[str, Any]:
        self.prompts.append(prompt)
        self.timeouts.append(timeout_seconds)
        if not self.script:
            pytest.fail("the controller asked for a model turn the scenario did not allow")
        step = self.script.pop(0)
        self.invocations += 1
        self.clock.now += step.elapsed
        if isinstance(step, Fail):
            self._usage_complete = False
            raise step.error
        self.thread_id = THREAD
        names = ("input_tokens", "output_tokens")
        usage = dict(zip(names, step.tokens, strict=True))
        for name, value in usage.items():
            if type(value) is int:
                self.cumulative[name] = self.cumulative.get(name, 0) + value
        if step.usd is None:
            self._usd_known = False
        else:
            self._usd += step.usd
        response = step.response(self.prompts) if callable(step.response) else step.response
        return {
            "response": response, "invocation": self.invocations, "thread_id": THREAD,
            "directory": f"turn-{self.invocations:03d}", "usage": usage,
            "cumulative_usage": dict(self.cumulative), "text_selection": "last_agent_message",
            "usage_known": step.usage_known, "actual_model": step.actual_model,
            "provider": "scripted", "metered_usd": step.usd, "cost_known": step.usd is not None,
        }


class Quota:
    """Owner-injected weekly reading: the last value repeats; ``None`` is unavailable."""

    def __init__(self, *readings: float | None) -> None:
        self.readings = list(readings)
        self.calls: list[float] = []

    def __call__(self, remaining: float) -> float:
        self.calls.append(remaining)
        value = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
        if value is None:
            raise QuotaUnavailable("weekly reading missing")
        return value


@dataclass
class Audit:
    session: EnvAuditSession
    driver: FakeDriver
    root: Path

    def run(self) -> dict[str, Any]:
        return self.session.run()

    def events(self, name: str) -> list[dict[str, Any]]:
        lines = self.session.journal_path.read_text().splitlines()
        return [e for e in map(json.loads, lines) if e["event"] == name]

    @property
    def findings_dir(self) -> Path:
        return self.root / "audit" / "findings"

    def observation(self, prompt_index: int) -> dict[str, Any]:
        return json.loads(self.driver.prompts[prompt_index])


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    stub = SimpleNamespace(monotonic=clock.monotonic)
    monkeypatch.setattr(env_agent, "time", stub)
    monkeypatch.setattr(env_module, "time", stub)
    return clock


@pytest.fixture
def build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Callable[..., Audit]:
    root = tmp_path.resolve()
    task = root / "package"
    (task / "tests").mkdir(parents=True)
    (task / "instruction.md").write_text("Return exactly the integer 42.\n")
    (task / "tests" / "check.py").write_text("answer = 7\nassert isinstance(answer, int)\n")

    def _build(
        script: list[Turn | Fail], *, lane: str = "codex", limits: AuditBudget | None = None,
        quota: Quota | None = None, **kwargs: Any,
    ) -> Audit:
        driver = FakeDriver(script, clock, lane)
        monkeypatch.setattr(env_agent, "make_driver", lambda **_: driver)
        session = EnvAuditSession(
            task_dir=task, out_dir=root / "audit", repo_root=root,
            model="codex/test" if lane == "codex" else "vendor/model-x",
            budget=limits or budget(), quota_reader=quota or Quota(0.1), lane=lane, **kwargs,
        )
        return Audit(session, driver, root)

    return _build


# -- quota admission -----------------------------------------------------------------------


@pytest.mark.parametrize("reading,ceiling,admitted", [
    (0.35, None, False), (0.8, None, False), (None, None, False),
    (0.4, 0.5, True), (0.5, 0.5, False),
])
def test_codex_turn_needs_an_available_reading_below_the_owner_ceiling(
    build: Callable[..., Audit], reading: float | None, ceiling: float | None, admitted: bool,
) -> None:
    extra = {} if ceiling is None else {"codex_weekly_ceiling": ceiling}
    audit = build([Turn(FINAL)], quota=Quota(reading), **extra)
    result = audit.run()
    if admitted:
        assert result["status"] == "final"
        assert audit.driver.invocations == 1
        return
    assert result["status"] == "quota_stop"
    assert audit.driver.prompts == []
    assert result["final"] is None
    assert result["session_id"] is None
    assert result["recorded_findings"] == []
    retained = json.loads(audit.session.record_path.read_text())
    assert retained["status"] == "quota_stop"
    assert retained["used"]["tokens"] == 0


def test_quota_is_read_before_every_codex_turn_and_can_close_mid_session(
    build: Callable[..., Audit],
) -> None:
    quota = Quota(0.10, 0.20, 0.36)
    audit = build([Turn(call(("task-info", {}))), Turn(call(("task-info", {}))), Turn(FINAL)], quota=quota)
    result = audit.run()
    assert result["status"] == "quota_stop"
    assert audit.driver.invocations == 2
    assert result["used"]["tool_calls"] == 2
    assert [r["weekly_used_fraction"] for r in result["quota"]["readings"]] == [0.10, 0.20, 0.36]
    assert [r["before_invocation"] for r in result["quota"]["readings"]] == [1, 2, 3]
    assert result["final"] is None


@pytest.mark.parametrize("lane", ["omp", "openai"])
def test_non_codex_lanes_never_consult_the_codex_quota(build: Callable[..., Audit], lane: str) -> None:
    quota = Quota(0.99)
    audit = build([Turn(FINAL)], lane=lane, quota=quota)
    result = audit.run()
    assert result["status"] == "final"
    assert quota.calls == []
    assert result["lane"] == lane
    assert result["quota"]["readings"] == []


# -- wall clock, tokens and tool allowance -------------------------------------------------


def test_no_model_turn_starts_when_too_little_time_remains(build: Callable[..., Audit]) -> None:
    quota = Quota(0.1)
    audit = build([Turn(FINAL)], limits=budget(seconds=10), quota=quota)
    result = audit.run()
    assert result["status"] == "time_budget_exhausted"
    assert audit.driver.prompts == []
    assert quota.calls == []


def test_each_turn_receives_only_the_wall_clock_that_is_left(build: Callable[..., Audit]) -> None:
    quota = Quota(0.1)
    task_info = Turn(call(("task-info", {})), elapsed=20)
    audit = build([task_info, Turn(task_info.response, elapsed=20), Turn(task_info.response, elapsed=20)],
                  quota=quota)
    result = audit.run()
    assert audit.driver.timeouts == [60, 40, 20]
    assert quota.calls == [60, 40, 20]
    assert result["status"] == "time_budget_exhausted"
    # The third reply arrived exactly at the deadline: its action is discarded.
    assert result["used"]["tool_calls"] == 2


@pytest.mark.parametrize("tokens,elapsed,action,expected", [
    (100, 1, "final", "final"),
    (101, 1, "final", "token_budget_exhausted"),
    (101, 1, "tool", "token_budget_exhausted"),
    (10, 60, "final", "time_budget_exhausted"),
    (10, 60, "tool", "time_budget_exhausted"),
])
def test_a_completed_over_budget_turn_discards_its_action(
    build: Callable[..., Audit], tokens: int, elapsed: float, action: str, expected: str,
) -> None:
    response = FINAL if action == "final" else call(("task-info", {}))
    audit = build([Turn(response, tokens=(tokens - 1, 1), elapsed=elapsed)], limits=budget(tokens=100))
    result = audit.run()
    assert result["status"] == expected
    assert result["used"]["tokens"] == tokens
    assert result["used"]["tool_calls"] == 0
    assert audit.events("action") == [] and audit.events("tool_call") == []
    assert result["recorded_findings"] == []
    assert (result["final"] is not None) == (expected == "final")


def test_a_turn_that_exactly_spends_the_token_budget_acts_but_no_further_turn_starts(
    build: Callable[..., Audit],
) -> None:
    audit = build([Turn(call(("task-info", {})), tokens=(99, 1))], limits=budget(tokens=100))
    result = audit.run()
    assert result["status"] == "token_budget_exhausted"
    assert result["used"]["tool_calls"] == 1
    assert audit.driver.invocations == 1


def test_token_budget_is_cumulative_across_turns(build: Callable[..., Audit]) -> None:
    task_info = call(("task-info", {}))
    audit = build([Turn(task_info, tokens=(50, 10)), Turn(task_info, tokens=(50, 10))],
                  limits=budget(tokens=100))
    result = audit.run()
    assert result["status"] == "token_budget_exhausted"
    assert result["used"]["tokens"] == 120
    assert result["used"]["tool_calls"] == 1


def test_tool_allowance_rejects_oversized_batches_and_ends_on_the_next_call(
    build: Callable[..., Audit],
) -> None:
    info = ("task-info", {})
    audit = build([Turn(call(info, info, info)), Turn(call(info, info)), Turn(call(info))],
                  limits=budget(calls=2))
    result = audit.run()
    assert result["status"] == "tool_budget_exhausted"
    assert result["used"]["tool_calls"] == 2
    assert audit.driver.invocations == 3
    # The oversized batch ran nothing and the model was told what remained.
    assert audit.observation(1)["budgets"]["remaining_tool_calls"] == 2
    spent = audit.observation(2)
    assert len(spent["results"]) == 2
    assert spent["budgets"]["remaining_tool_calls"] == 0


def test_a_final_is_still_accepted_after_the_tool_allowance_is_spent(build: Callable[..., Audit]) -> None:
    audit = build([Turn(call(("task-info", {}))), Turn(FINAL)], limits=budget(calls=1))
    result = audit.run()
    assert result["status"] == "final"
    assert result["used"]["tool_calls"] == 1
    assert result["final"]["limitations"] == ["No runtime executed."]


def test_one_unusable_reply_is_survivable_and_nothing_in_it_runs(build: Callable[..., Audit]) -> None:
    audit = build([Turn(call(("bash", {"command": "true"}))), Turn(FINAL)])
    result = audit.run()
    assert result["status"] == "final"
    assert result["used"]["tool_calls"] == 0
    assert [e["kind"] for e in result["errors"]] == ["invalid_action"]


def test_two_consecutive_unusable_replies_stop_without_executing_anything(
    build: Callable[..., Audit],
) -> None:
    audit = build([Turn("I will inspect the files first."), Turn(call(("bash", {"command": "true"})))])
    result = audit.run()
    assert result["status"] == "invalid_action"
    assert result["used"]["tool_calls"] == 0
    assert audit.events("action") == []
    assert result["final"] is None


def test_a_wall_clock_that_expires_inside_a_batch_stops_before_the_next_call(
    build: Callable[..., Audit], clock: Clock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow_execution(self: EnvTools, name: str, args: dict[str, Any]) -> dict[str, Any]:
        clock.now += 100
        return self._observe(name, {"status": "error", "executed": True, "reward": None})

    monkeypatch.setattr(EnvTools, "_execute", slow_execution)
    audit = build([Turn(call(("run-reference", {}), ("task-info", {})))])
    result = audit.run()
    assert result["status"] == "time_budget_exhausted"
    assert result["used"]["tool_calls"] == 1
    assert [e["tool"] for e in audit.events("tool_call")] == ["run-reference"]


@pytest.mark.parametrize("usd,valid", [(0, True), (0.5, True), (-0.01, False), (float("nan"), False),
                                       (float("inf"), False), (True, False)])
def test_model_spend_cap_is_finite_and_nonnegative(usd: Any, valid: bool) -> None:
    if valid:
        assert AuditBudget(60, 100, 2, usd).max_usd == usd
    else:
        with pytest.raises(ValueError):
            AuditBudget(60, 100, 2, usd)


# -- provider failure and unknown accounting ----------------------------------------------


@pytest.mark.parametrize("error", [DriverError("provider refused"), OSError("socket closed"), ValueError("bad reply")])
def test_a_failed_turn_keeps_earlier_evidence_but_makes_token_usage_unknown(
    build: Callable[..., Audit], error: Exception,
) -> None:
    audit = build([Turn(call(("task-info", {}))), Fail(error)])
    result = audit.run()
    assert result["status"] == "model_error"
    assert result["final"] is None
    assert result["used"]["tool_calls"] == 1
    assert len(audit.events("tool_call")) == 1
    assert result["used"]["tokens"] is None
    assert result["used"]["tokens_known"] is False
    assert [e["kind"] for e in result["errors"]] == ["model_error"]
    assert json.loads(audit.session.record_path.read_text())["used"]["tokens"] is None


def test_a_first_failed_turn_never_reports_free_zero_usage(build: Callable[..., Audit]) -> None:
    result = build([Fail(DriverError("transport failed before usable usage arrived"))]).run()
    assert result["status"] == "model_error"
    assert result["used"]["tokens"] is None
    assert result["session_id"] is None


def test_a_failure_that_consumed_the_remaining_clock_is_a_time_stop(build: Callable[..., Audit]) -> None:
    result = build([Fail(DriverError("timed out"), elapsed=60)]).run()
    assert result["status"] == "time_budget_exhausted"


@pytest.mark.parametrize("tokens,known", [
    ((None, None), False), ((10, None), True), ((True, 5), True), ((-1, 5), True),
])
def test_unknown_token_usage_stops_before_executing_the_returned_action(
    build: Callable[..., Audit], tokens: tuple[Any, Any], known: bool,
) -> None:
    audit = build([Turn(call(("task-info", {})), tokens=tokens, usage_known=known)])
    result = audit.run()
    assert result["status"] == "model_error"
    assert result["used"]["tokens"] is None
    assert result["used"]["tokens_known"] is False
    assert result["used"]["tool_calls"] == 0
    assert audit.events("action") == []
    assert result["final"] is None


def test_unknown_spend_stops_before_executing_the_action_and_stays_null(build: Callable[..., Audit]) -> None:
    audit = build([Turn(call(("task-info", {})), usd=None)], lane="openai", limits=budget(usd=1.0))
    result = audit.run()
    assert result["status"] == "model_error"
    assert result["spend"]["metered_usd"] is None
    assert result["spend"]["known"] is False
    assert result["used"]["tokens"] == 15  # usage itself was reported
    assert result["used"]["tool_calls"] == 0
    assert audit.events("action") == []


def test_known_metered_run_retains_spend_models_and_the_per_turn_record(build: Callable[..., Audit]) -> None:
    audit = build(
        [Turn(call(("task-info", {})), usd=0.10, actual_model="vendor/model-x-2026"),
         Turn(FINAL, usd=0.15, actual_model="vendor/model-x-2026")],
        lane="openai", limits=budget(usd=1.0),
    )
    result = audit.run()
    assert result["status"] == "final"
    assert result["schema"] == "qa.env.audit/v2"
    assert result["spend"]["metered_usd"] == pytest.approx(0.25) and result["spend"]["known"] is True
    assert result["actual_model"] == "vendor/model-x-2026"
    assert result["model"] == "vendor/model-x" and result["model_invocations"] == 2
    assert result["session_id"] == THREAD
    assert result["used"]["tokens"] == 30
    assert [t["tokens"] for t in result["usage"]["per_turn"]] == [15, 15]
    assert result["usage"]["cumulative_reported"] == {"input_tokens": 20, "output_tokens": 10}
    turns = audit.events("model_turn")
    assert [t["actual_model"] for t in turns] == ["vendor/model-x-2026"] * 2
    assert [t["metered_usd"] for t in turns] == [0.10, 0.15]
    assert [t["requested_model"] for t in turns] == ["vendor/model-x"] * 2
    # The retained session record is the returned one.
    assert json.loads(audit.session.record_path.read_text()) == json.loads(json.dumps(result, default=str))


# -- findings and evidence -----------------------------------------------------------------


def test_several_defects_including_one_class_are_recorded_with_incremental_cost(
    build: Callable[..., Audit],
) -> None:
    second_weak = finding(summary="The check never compares against 42.", evidence_refs=["tests/check.py:2"])
    oracle = finding(defect_class="overstrict_oracle", summary="Rejects other valid integers.",
                     evidence_refs=["tests/check.py:1"])
    audit = build(
        [Turn(call(finding(), second_weak), usd=0.10), Turn(call(oracle), usd=0.15), Turn(FINAL, usd=0.05)],
        lane="openai", limits=budget(usd=1.0),
    )
    result = audit.run()
    assert result["status"] == "final"
    assert len(result["recorded_findings"]) == 3
    stored = load_findings_dir(audit.findings_dir)
    assert sorted((f.root_cause[0], f.category) for f in stored) == [
        ("overstrict_oracle", "broken"), ("weak_check", "weak"), ("weak_check", "weak"),
    ]
    for item in stored:
        assert item.subject.task_id == "package"
        assert item.subject.task_digest == audit.session.tools.task_digest
        assert item.refutation.status == "unverified" and item.refutation.by is None
        assert item.measurement == "not_run"
        assert item.reviewer.model == "vendor/model-x"
    receipts = [r["receipt"] for r in result["recorded_findings"]]
    assert [r["finding"]["reviewer"]["cost_usd"] for r in receipts] == pytest.approx([0.10, 0.0, 0.15])
    assert all(r["status"] == "suspected" and r["evidence_basis"] == ["source_citation"] for r in receipts)
    for entry in result["recorded_findings"]:
        path = Path(entry["receipt"]["path"])
        digest = hashlib.sha256(json.dumps(entry["receipt"]["finding"], sort_keys=True).encode()).hexdigest()
        assert path == audit.findings_dir / f"{digest}.json"
        assert path.is_file()
        journaled = audit.session.journal_path.read_text().splitlines()[entry["journal_seq"] - 1]
        assert json.loads(journaled)["tool"] == "record-finding"


def test_a_repeated_source_review_record_is_one_unverified_claim(build: Callable[..., Audit]) -> None:
    audit = build([Turn(call(finding(), finding())), Turn(FINAL)])
    result = audit.run()
    receipts = [r["receipt"] for r in result["recorded_findings"]]
    assert len(receipts) == 2 and receipts[0]["path"] == receipts[1]["path"]
    only, = load_findings_dir(audit.findings_dir)
    assert len(list(audit.findings_dir.glob("*.json"))) == 1
    assert only.refutation.status == "unverified" and only.measurement == "not_run"
    assert all(r["evidence_basis"] == ["source_citation"] for r in receipts)


@pytest.mark.parametrize("override", [
    {"evidence_refs": []},
    {"evidence_refs": ["tests/check.py:9"]},
    {"evidence_refs": ["../outside.txt:1"]},
    {"evidence_refs": ["witness:" + "a" * 32]},
    {"evidence_refs": "tests/check.py:1"},
    {"defect_class": "invented_class"},
    {"severity": "catastrophic"},
    {"refutation": {"status": "verified"}},
    {"subject": {"kind": "task", "task_id": "someone-else"}},
])
def test_a_finding_without_resolvable_evidence_or_with_host_fields_is_an_error_observation(
    build: Callable[..., Audit], override: dict[str, Any],
) -> None:
    audit = build([Turn(call(finding(**override))), Turn(FINAL)])
    result = audit.run()
    assert result["status"] == "final"
    assert result["recorded_findings"] == []
    assert not audit.findings_dir.exists()
    assert result["used"]["tool_calls"] == 1  # the failed call still spent allowance
    assert audit.observation(1)["results"][0]["ok"] is False


@pytest.mark.parametrize("status,recorded", [("measured", True), ("error", False)])
def test_only_a_measured_execution_can_be_cited_as_a_witness(
    build: Callable[..., Audit], monkeypatch: pytest.MonkeyPatch, status: str, recorded: bool,
) -> None:
    def execution(self: EnvTools, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return self._observe(name, {"status": status, "executed": True, "reward": 1,
                                    "task": {"digest": self.task_digest}})

    def cite(prompts: list[str]) -> str:
        result = json.loads(prompts[-1])["results"][0]["observation"]["result"]
        witness = "witness:" + Path(result["observation_path"]).stem
        return call(finding(evidence_refs=[witness, "tests/check.py:1"]))

    monkeypatch.setattr(EnvTools, "_execute", execution)
    audit = build([Turn(call(("run-reference", {}))), Turn(cite), Turn(FINAL)])
    result = audit.run()
    assert result["status"] == "final"
    assert result["used"]["tool_calls"] == 2
    if not recorded:
        assert result["recorded_findings"] == []
        return
    receipt, = (r["receipt"] for r in result["recorded_findings"])
    assert receipt["evidence_basis"] == ["executed_witness", "source_citation"]
    assert receipt["finding"]["stage"] == "execution_review"
    assert receipt["finding"]["measurement"] == "measured"
    assert receipt["finding"]["refutation"]["status"] == "unverified"


# -- generic hackbook through the loop -------------------------------------------------------


def _catalog(path: Path) -> Path:
    rows = [{"id": f"TS{i:02d}", "key": key, "name": key, "definition": f"Generic {key} definition."}
            for i, key in enumerate(defect_classes(), 1)]
    path.write_text(json.dumps({"version": 1, "task_side": rows}))
    return path


def test_a_hackbook_match_is_taxonomy_evidence_and_not_a_finding(build: Callable[..., Audit], tmp_path: Path) -> None:
    catalog = _catalog(tmp_path.resolve() / "hackbook.json")
    audit = build([Turn(call(("hackbook", {"defect_class": "weak_check"}))), Turn(FINAL)], hackbook_path=catalog)
    result = audit.run()
    seen = audit.observation(1)["results"][0]["observation"]["result"]
    assert seen["evidence_basis"] == "generic_taxonomy"
    assert [m["key"] for m in seen["matches"]] == ["weak_check"]
    assert result["status"] == "final" and result["recorded_findings"] == []
    assert not audit.findings_dir.exists()


def test_hackbook_is_an_error_observation_when_the_owner_configured_none(build: Callable[..., Audit]) -> None:
    audit = build([Turn(call(("hackbook", {}))), Turn(FINAL)])
    result = audit.run()
    assert result["status"] == "final"
    assert audit.observation(1)["results"][0]["ok"] is False


# -- action parsing and observation bounds -------------------------------------------------


@pytest.mark.parametrize("action", [
    {"calls": [{"tool": "bash", "args": {"command": "true"}}]},
    {"calls": [], "final": {"summary": "clean", "limitations": []}},
    {"calls": [{"tool": "task-info", "args": "not an object"}]},
    {"final": {"summary": "", "limitations": []}},
    {"calls": [{"tool": "record-finding", "args": {}}] * 9},
])
def test_invalid_actions_cannot_expand_capabilities_or_create_an_empty_final(action: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_action(json.dumps(action))


def test_large_observation_retains_identity_needed_to_record_cited_evidence() -> None:
    result = _bounded({
        "tool": "task-info", "task_id": "package", "task_digest": "sha256:bound",
        "result": {"source": "x" * (MAX_OBSERVATION_CHARS + 1)},
    })
    assert result["truncated"] is True
    assert result["task_id"] == "package"
    assert result["task_digest"] == "sha256:bound"
    assert result["tool"] == "task-info"


# -- Codex subscription reading and thread binding ------------------------------------------


@pytest.mark.parametrize("value", [None, True, "0.1", float("nan"), -0.1, 1.1])
def test_invalid_quota_readings_are_unavailable_not_zero(
    monkeypatch: pytest.MonkeyPatch, value: Any,
) -> None:
    body = {"reports": [{"provider": "openai-codex", "limits": [
        {"window": {"id": "7d"}, "amount": {"usedFraction": value}},
    ]}]}
    monkeypatch.setattr(
        "evallab.qa.env_agent.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, json.dumps(body), ""),
    )
    with pytest.raises(QuotaUnavailable):
        weekly_codex_used(20)


def test_resume_cannot_silently_switch_to_another_native_thread(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    session = CodexSession(CodexTransport(repo_root=root, model="codex/test"), directory=root / "session")
    events = root / "events.jsonl"
    first = "00000000-0000-4000-8000-000000000001"
    second = "00000000-0000-4000-8000-000000000002"
    events.write_text(json.dumps({"type": "thread.started", "thread_id": first}) + "\n")
    session._bind_thread(events, required=True)
    events.write_text(json.dumps({"type": "thread.started", "thread_id": second}) + "\n")
    with pytest.raises(CodexTransportError, match="different thread"):
        session._bind_thread(events, required=True)
    assert session.thread_id == first


def test_preflight_time_cannot_extend_the_model_turn_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve()
    executable = root / "probe-only-cli"
    executable.write_text("unit-test executable identity; never executed")
    monkeypatch.setattr(codex_transport, "CODEX_EXECUTABLE", executable)
    clock = [100.0]
    monkeypatch.setattr(codex_transport.time, "monotonic", lambda: clock[0])
    transport = CodexTransport(repo_root=root, model="codex/test")

    def preflight(**kwargs: Any) -> dict[str, Any]:
        clock[0] += 2
        return {"text_only": {"ephemeral": True}}

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("model process started after preflight spent the entire time budget")

    monkeypatch.setattr(transport, "preflight", preflight)
    monkeypatch.setattr(codex_transport, "_execute", forbidden)
    session = CodexSession(transport, directory=root / "session")
    with pytest.raises(CodexTransportError, match="deadline"):
        session.turn("An ordinary review action", timeout_seconds=1)
    assert session.thread_id is None
