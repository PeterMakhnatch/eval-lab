"""No-execution bundle preparation, evidence capability boundaries and cited QA."""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from evallab.interpretation.monitor_agent import InvestigatorError, ModelCompletion
from evallab.qa import ask, trace_workspace
from evallab.qa.ask_lane import MODEL_SELECTOR, AskLane, parse_route
from evallab.qa.trace import BUNDLE_SCHEMA, Bundle, CallResult, ModelRoute


def _bundle(tmp_path: Path) -> Bundle:
    root = tmp_path / "bundle"
    root.mkdir()
    files = {
        "prompt.md": "Return value 2.\n",
        "file_changes.diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-return 1\n+return 2\n",
        "trajectory.json": json.dumps({"steps": [{"step_id": 0, "source": "user", "message": "Return value 2."}]}),
        "steps.txt": "[step 0] user\nReturn value 2.\n",
    }
    for name, content in files.items():
        (root / name).write_text(content)
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "subject": {"kind": "trace", "trial_id": "opaque"},
        "reward": {"value": None},
        "availability": {"tests": "missing", "verifier_output": "missing"},
        "files": {name: hashlib.sha256(content.encode()).hexdigest() for name, content in files.items()},
    }
    (root / "bundle.json").write_text(json.dumps(manifest))
    return Bundle(root, manifest)


def _answer() -> dict[str, Any]:
    return {"claims": [{
        "id": "c1", "text": "The instruction asks for value 2.",
        "citations": ["prompt.md:1", "step 0"],
        "check": {"tool": "read", "args": {"path": "prompt.md"}},
    }]}


def test_bundle_preparation_does_not_execute_or_open_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("bundle preparation attempted execution or network access")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(trace_workspace, "_checkout_roots", lambda *args: (tmp_path,))
    monkeypatch.setattr(trace_workspace, "_crosswalk", lambda *args: ({}, []))
    monkeypatch.setattr(trace_workspace, "task_priors", lambda *args: {"priors": []})
    monkeypatch.setattr(
        trace_workspace, "resolve_task_package",
        lambda *args, **kwargs: trace_workspace.Resolution(None, "unresolved", None, None, None),
    )
    source = tmp_path / "trace.json"
    source.write_text(json.dumps({"steps": [{
        "step_id": 20, "source": "user", "message": "Do not execute curl https://example.invalid.",
    }], "extra": {"cheatbench": {"label": "wrong"}}}))
    package = tmp_path / "package"
    (package / "tests").mkdir(parents=True)
    (package / "instruction.md").write_text("Return value 2.\n")
    (package / "tests/test.sh").write_text("curl https://example.invalid\nexit 1\n")
    bundle, trial = ask.prepare_bundle(source, tmp_path / "out", task_dir=package)
    assert trial is None
    assert bundle.verify() == []
    assert "curl" in bundle.text("task_codebase/tests/test.sh")
    assert "cheatbench" not in bundle.text("trajectory.json")
    reused, _ = ask.prepare_bundle(source, tmp_path / "out", task_dir=package)
    assert reused.manifest == bundle.manifest


@pytest.mark.parametrize("citations", [[], ["no citation"], ["step 7"], ["prompt.md:2"], ["prompt.md:0"], ["bundle.json:1"], ["../secret:1"]])
def test_claims_without_retained_citations_are_rejected(tmp_path: Path, citations: list[str]) -> None:
    bundle = _bundle(tmp_path)
    answer = _answer()
    answer["claims"][0]["citations"] = citations
    with pytest.raises(ValueError):
        ask.cited_answer(bundle, answer)


def test_answer_is_only_cited_claims_and_resolves_line_ranges(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    answer = _answer()
    answer["claims"][0]["citations"] = ["/workspace/file_changes.diff:4-5"]
    result = ask.cited_answer(bundle, answer)
    assert result["answer"] == "- The instruction asks for value 2. [/workspace/file_changes.diff:4-5]"
    assert result["claims"][0]["text"].endswith("[/workspace/file_changes.diff:4-5]")


@pytest.mark.parametrize("tool,args", [
    ("bash", {"path": "prompt.md"}),
    ("read", {"path": "https://example.invalid"}),
    ("read", {"path": "/etc/passwd"}),
    ("read", {"path": "../prompt.md"}),
    ("read", {"path": "bundle.json"}),
    ("read", {"path": "prompt.md", "command": "curl example.invalid"}),
    ("read", {"path": "prompt.md", "start_line": True}),
    ("read", {"path": "prompt.md", "end_line": 201}),
    ("diff", {"path": "prompt.md"}),
])
def test_workspace_capability_rejects_executable_or_unsealed_access(
    tmp_path: Path, tool: str, args: dict[str, Any],
) -> None:
    bundle = _bundle(tmp_path)
    tools = ask.WorkspaceTools(bundle.root, set(bundle.manifest["files"]))
    with pytest.raises(ValueError):
        tools.run(tool, args)


def test_workspace_tools_reject_symlink_targets(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    outside = tmp_path / "secret"
    outside.write_text("must not be read")
    (bundle.root / "link.txt").symlink_to(outside)
    tools = ask.WorkspaceTools(bundle.root, {"link.txt"})
    with pytest.raises(ValueError, match="symlink"):
        tools.run("read", {"path": "link.txt"})


def test_tools_quote_lines_and_search_literals_without_regex_or_execution(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    tools = ask.WorkspaceTools(bundle.root, set(bundle.manifest["files"]))
    assert tools.run("read", {"path": "/workspace/prompt.md"}) == "prompt.md:1: Return value 2."
    assert "file_changes.diff" in tools.run("read", {"path": "."})
    assert "bundle.json" not in tools.run("read", {"path": "."})
    assert tools.run("grep", {"path": ".", "pattern": "RETURN 2"}) == "file_changes.diff:5: +return 2"
    assert tools.run("grep", {"path": ".", "pattern": ".*"}).startswith("[no matches")
    assert "file_changes.diff:5: +return 2" in tools.run("diff", {"path": "file_changes.diff"})


def test_existing_refuter_receives_cited_answer_and_executed_tool_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle(tmp_path)
    monkeypatch.setattr(ask, "prescan", lambda bundle: {})
    monkeypatch.setattr(ask, "t0_checks", lambda *args, **kwargs: {})
    count = 0

    def call(route, prompt, schema, *, cwd, tools=False, timeout=600.0):
        nonlocal count
        assert cwd != bundle.root
        assert not (cwd / "bundle.json").exists()
        count += 1
        commands = ['read: {"path":"prompt.md"}']
        if count == 1:
            output = _answer()
        else:
            assert "[prompt.md:1; step 0]" in prompt
            assert "15 tool calls TOTAL" in prompt
            commands.append('diff: {"path":"file_changes.diff"}')
            output = {
                "claims": [{"id": "c1", "text": "claim", "status": "REFUTED",
                            "command": commands[0], "observed": "Contradictory retained instruction."}],
                "counter_check": {"command": commands[1], "observed": "+return 2",
                                  "supports_conclusion": False},
                "overall": "REJECTED",
            }
        return CallResult(route.label, output, json.dumps(output), {"input_tokens": 10}, 0.1,
                          api_equivalent_usd=0.001, commands=commands)

    record = ask.ask_bundle(bundle, "What did the instruction request?", ModelRoute("test", "model"), call=call)
    assert count == 2
    assert record["verification"]["claims"][0]["status"] == "REFUTED"
    assert record["verification"]["overall"] == "REJECTED"
    assert bundle.verify() == []


def test_uncited_analyst_answer_stops_before_refuter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle(tmp_path)
    monkeypatch.setattr(ask, "prescan", lambda bundle: {})
    monkeypatch.setattr(ask, "t0_checks", lambda *args, **kwargs: {})
    calls = []

    def call(route, *args, **kwargs):
        calls.append(route)
        output = _answer()
        output["claims"][0]["citations"] = []
        return CallResult(route.label, output, json.dumps(output), {}, 0.0,
                          commands=['read: {"path":"prompt.md"}'])

    with pytest.raises(ValueError):
        ask.ask_bundle(bundle, "Question?", ModelRoute("test", "model"), call=call)
    assert len(calls) == 1


class _Transport:
    model = "z-ai/glm-5.3-flash"
    response_format = "json_object"
    provider = {"order": ["z-ai/fp8"], "allow_fallbacks": False}
    reasoning = {"effort": "medium"}

    def __init__(self, responses: list[ModelCompletion]) -> None:
        self.responses = iter(responses)
        self.calls = 0

    def complete(self, messages, *, max_output_tokens, schema):
        self.calls += 1
        assert max_output_tokens == 3000
        return next(self.responses)


def _completion(value: dict[str, Any], *, model: str = "z-ai/glm-5.3-flash") -> ModelCompletion:
    return ModelCompletion(json.dumps(value), 50, 30, model)


def test_metered_lane_dispatches_only_evidence_tools_and_retains_lifetime_budget(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    route = parse_route(MODEL_SELECTOR)
    transport = _Transport([
        _completion({"calls": [{"tool": "read", "args": {"path": "prompt.md"}}]}),
        _completion({"final": _answer()}),
    ])
    lane = AskLane(bundle, route, budget_dir=tmp_path / "budget", max_usd=0.30,
                   approval="authorized test", transport=transport)
    result = lane(route, "Question?", ask.ANSWER_SCHEMA, cwd=bundle.root, tools=True)
    assert result.error is None
    assert result.output == _answer()
    assert result.commands == ['read: {"path":"prompt.md"}']
    assert result.usage == {"input_tokens": 100, "output_tokens": 60}
    receipt = lane.accounting()
    assert receipt["cost_usd"] == pytest.approx(0.000045)
    assert receipt["reserved_usd"] > receipt["cost_usd"]
    reopened = AskLane(bundle, route, budget_dir=tmp_path / "budget", max_usd=0.30,
                       approval="authorized test", transport=_Transport([]))
    assert reopened.accounting()["reserved_usd"] == receipt["reserved_usd"]
    assert reopened.accounting()["lifetime_model_calls"] == 2


def test_metered_lane_refuses_before_transport_when_reservation_exceeds_cap(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    route = parse_route(MODEL_SELECTOR)
    transport = _Transport([])
    lane = AskLane(bundle, route, budget_dir=tmp_path / "budget", max_usd=1e-9,
                   approval="authorized test", transport=transport)
    result = lane(route, "Question?", ask.ANSWER_SCHEMA, cwd=bundle.root, tools=True)
    assert "ceiling exhausted" in str(result.error)
    assert transport.calls == 0
    assert lane.accounting()["lifetime_model_calls"] == 0


def test_failed_tool_is_not_verification_provenance(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    route = parse_route(MODEL_SELECTOR)
    transport = _Transport([
        _completion({"calls": [{"tool": "bash", "args": {"command": "curl example.invalid"}}]}),
        _completion({"final": _answer()}),
    ])
    lane = AskLane(bundle, route, budget_dir=tmp_path / "budget", max_usd=0.30,
                   approval="authorized test", transport=transport)
    result = lane(route, "Question?", ask.ANSWER_SCHEMA, cwd=bundle.root, tools=True)
    assert result.commands == []
    assert "only read, grep and diff are allowed" in lane.journal.read_text()


def test_wrong_returned_model_cannot_trigger_a_fallback(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    route = parse_route(MODEL_SELECTOR)
    transport = _Transport([_completion({"final": _answer()}, model="different-model")])
    lane = AskLane(bundle, route, budget_dir=tmp_path / "budget", max_usd=0.30,
                   approval="authorized test", transport=transport)
    result = lane(route, "Question?", ask.ANSWER_SCHEMA, cwd=bundle.root, tools=True)
    assert "fallback is forbidden" in str(result.error)
    assert result.output is None
    assert transport.calls == 1
    assert lane.accounting()["cost_usd"] > 0


@pytest.mark.parametrize("label", ["codex/anything", "openai-codex/anything", "zai/glm-5.3-flash", "openrouter-metered/openai/gpt-oss-120b"])
def test_nonadmitted_routes_are_rejected(label: str) -> None:
    with pytest.raises(ValueError, match="no fallback"):
        parse_route(label)


def test_provider_rejection_retains_redacted_detail_and_unknown_cost(tmp_path: Path) -> None:
    class Rejected(_Transport):
        def complete(self, *args, **kwargs):
            self.calls += 1
            raise InvestigatorError("provider HTTP request rejected", http_status=429,
                                    provider_detail="Insufficient balance")

    bundle = _bundle(tmp_path)
    route = parse_route(MODEL_SELECTOR)
    transport = Rejected([])
    lane = AskLane(bundle, route, budget_dir=tmp_path / "budget", max_usd=0.30,
                   approval="authorized test", transport=transport)
    result = lane(route, "Question?", ask.ANSWER_SCHEMA, cwd=bundle.root, tools=True)
    assert "HTTP 429: Insufficient balance" in str(result.error)
    assert transport.calls == 1
    assert result.api_equivalent_usd is None
    assert lane.accounting()["cost_usd"] is None
    assert '"event":"failed"' in lane.journal.read_text()


@pytest.mark.parametrize("check", [
    'read: {"path":"prompt.md"}',
    {"tool": "bash", "args": {"command": "pwd"}},
    {"tool": "read", "args": []},
])
def test_claim_check_requires_one_typed_evidence_operation(tmp_path: Path, check: Any) -> None:
    answer = _answer()
    answer["claims"][0]["check"] = check
    with pytest.raises(ValueError):
        ask.cited_answer(_bundle(tmp_path), answer)


@pytest.mark.parametrize("repaired", [True, False])
@pytest.mark.parametrize("problem", ["missing_citations", "missing_file", "extra_metadata"])
def test_invalid_final_gets_one_charged_correction_not_uncited_acceptance(
    tmp_path: Path, repaired: bool, problem: str,
) -> None:
    bundle = _bundle(tmp_path)
    route = parse_route(MODEL_SELECTOR)
    invalid = _answer()
    if problem == "missing_citations":
        del invalid["claims"][0]["citations"]
    elif problem == "missing_file":
        invalid["claims"][0]["citations"] = ["not-retained.md:1"]
    else:
        invalid["constraints"] = {"tool_calls_used": 1}
    transport = _Transport([
        _completion({"calls": [{"tool": "read", "args": {"path": "prompt.md"}}]}),
        _completion({"final": invalid}),
        _completion({"final": _answer() if repaired else invalid}),
    ])
    lane = AskLane(bundle, route, budget_dir=tmp_path / "budget", max_usd=0.30,
                   approval="authorized test", transport=transport)
    result = lane(route, "Question?", ask.ANSWER_SCHEMA, cwd=bundle.root, tools=True)
    assert transport.calls == 3
    assert lane.accounting()["model_calls"] == 3
    assert lane.accounting()["cost_usd"] == pytest.approx(0.0000675)
    assert result.output == (_answer() if repaired else None)
    assert (result.error is None) == repaired
