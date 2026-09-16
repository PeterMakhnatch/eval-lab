"""Behavioural checks for the policy-driven RLM harness (skipped without dspy).

The module must stay importable without dspy so default collection still sees
it; every test is skipped, not omitted, when the optional runtime is absent.
"""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess

import pytest

from evallab.rlm.policies import resolve_policy

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("dspy") is None, reason="dspy is not installed in this environment"
)


def _h():
    from evallab.rlm import harness

    return harness


def _repl_history():
    from dspy.primitives.repl_types import REPLHistory

    return REPLHistory


def _parse_error():
    from dspy.utils.exceptions import AdapterParseError

    return AdapterParseError


def _history(n: int):
    history = _repl_history()()
    for index in range(n):
        history = history.append(
            reasoning=f"r{index}", code=f"print({index})", output=f"out-{index}-" + "x" * 50
        )
    return history


def test_history_window_masks_only_older_outputs_and_keeps_code() -> None:
    rlm = _h().LabRlm("context, query -> answer", resolve_policy("orchestrator-mask4"))
    full = _history(7)
    view = rlm._history_view(full)
    assert len(view) == 7
    for index, entry in enumerate(view.entries):
        assert entry.code == f"print({index})"
        if index < 3:
            assert entry.output == _h().MASKED_OUTPUT_MARKER.format(
                chars=len(full.entries[index].output)
            )
        else:
            assert entry.output == full.entries[index].output
    assert rlm._history_view(_history(4)).entries == _history(4).entries  # nothing to mask
    assert (
        _h().LabRlm("context, query -> answer", resolve_policy("stock"))._history_view(full) is full
    )


def test_iteration_label_reports_remaining_budget_only_when_enabled() -> None:
    plain = _h().LabRlm("context, query -> answer", resolve_policy("orchestrator"))
    remind = _h().LabRlm("context, query -> answer", resolve_policy("orchestrator-remind"))
    assert plain._iteration_label(0) == "1/20"
    assert remind._iteration_label(0).startswith("1/20 (19 iterations remain")
    assert "LAST iteration" in remind._iteration_label(19)


def test_marker_history_renders_field_markers_and_composes_with_masking() -> None:
    MarkerHistory = _h().MarkerHistory

    stock = _h().LabRlm("context, query -> answer", resolve_policy("stock"))
    markers = _h().LabRlm("context, query -> answer", resolve_policy("stock-markers"))
    full = _history(3)
    assert "Reasoning: r0" in stock._history_view(full).format()
    rendered = markers._history_view(full).format()
    assert "Reasoning:" not in rendered and "Code:\n" not in rendered
    assert rendered.count("[[ ## code ## ]]") == 3 and "[[ ## reasoning ## ]]\nr2" in rendered
    assert isinstance(markers._history_view(full), MarkerHistory)
    masked = resolve_policy("stock-markers").derive("m", "masked markers", history_window=1)
    view = _h().LabRlm("context, query -> answer", masked)._history_view(full).format()
    assert view.count("masked by harness policy") == 2 and "out-2-" in view


def test_policy_addenda_compose_into_action_instructions() -> None:
    def tool(x: str) -> str:
        """A tool."""
        return x

    bench = _h().LabRlm("context, query -> answer", resolve_policy("orchestrator-bridge"))
    harbor = _h().LabRlm(
        "instruction, file_tree -> solution", resolve_policy("orchestrator-bridge"), tools=[tool]
    )
    assert "isolated sandbox" not in bench.generate_action.signature.instructions
    assert "isolated sandbox" in harbor.generate_action.signature.instructions
    assert harbor.generate_action.signature.instructions.startswith("As a Recursive Language Model")
    override = resolve_policy("orchestrator").derive(
        "g", "gepa", action_instructions_override="NEW INSTRUCTIONS"
    )
    assert (
        _h().LabRlm("context, query -> answer", override).generate_action.signature.instructions
        == "NEW INSTRUCTIONS"
    )


def test_unparseable_action_becomes_a_recoverable_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rlm = _h().LabRlm("context, query -> answer", resolve_policy("stock"))
    signature = rlm.generate_action.signature

    def explode(self, **_: object):
        raise _parse_error()(
            adapter_name="ChatAdapter", signature=signature, lm_response="garbled {{"
        )

    monkeypatch.setattr(type(rlm.generate_action), "__call__", explode)
    outcome = rlm._execute_iteration(
        repl=None,
        variables=[],
        history=_repl_history()(),
        iteration=0,
        input_args={},
        output_field_names=["answer"],
    )
    assert isinstance(outcome, _repl_history()) and len(outcome) == 1
    assert outcome.entries[0].output.startswith(
        "[Error] Your previous response could not be parsed"
    )
    assert "garbled {{" in outcome.entries[0].output
    assert rlm.parse_failures == 1


def test_lenient_policy_salvages_history_mirroring_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    salvage_action = _h().salvage_action

    drifted = "Reasoning: parsed 1,428 rows; verifying.\n\nCode:\n```python\nimport re\nprint(len(context))\n```\n"
    assert salvage_action(drifted) == (
        "parsed 1,428 rows; verifying.",
        "import re\nprint(len(context))",
    )
    marked = '[[ ## reasoning ## ]]\nBoth agree: 4.\nCode:\n```python\nSUBMIT("4")\n```\n[[ ## completed ## ]]'
    assert salvage_action(marked) == ("Both agree: 4.", 'SUBMIT("4")')
    assert salvage_action("no code here") is None
    assert salvage_action("```python\n\n```") is None

    rlm = _h().LabRlm("context, query -> answer", resolve_policy("stock-lenient"))
    signature = rlm.generate_action.signature

    def explode(self, **_: object):
        raise _parse_error()(adapter_name="ChatAdapter", signature=signature, lm_response=drifted)

    executed: list[str] = []
    monkeypatch.setattr(type(rlm.generate_action), "__call__", explode)
    monkeypatch.setattr(
        rlm, "_execute_code", lambda repl, code, input_args: executed.append(code) or "ok"
    )
    outcome = rlm._execute_iteration(
        repl=None,
        variables=[],
        history=_repl_history()(),
        iteration=0,
        input_args={},
        output_field_names=["answer"],
    )
    assert executed == ["import re\nprint(len(context))"]
    assert isinstance(outcome, _repl_history()) and outcome.entries[0].output == "ok"
    assert (rlm.salvaged_actions, rlm.parse_failures) == (1, 0)


def test_lm_usage_sums_history_and_prices_api_equivalent() -> None:
    class FakeLm:
        history = [
            {"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}},
            {
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 1_000_000,
                    "completion_tokens_details": {"reasoning_tokens": 400},
                }
            },
            "not-a-dict",
        ]

    usage = _h().lm_usage(FakeLm())  # type: ignore[arg-type]
    assert (usage.calls, usage.input_tokens, usage.output_tokens, usage.reasoning_tokens) == (
        2,
        1_000_000,
        1_000_000,
        400,
    )
    assert usage.cost_usd == pytest.approx(1.40 + 4.40)
    assert _h().lm_usage(None).calls == 0


def test_container_python_tool_quotes_arbitrary_source() -> None:
    from harbor.environments.base import ExecResult

    from evallab.harbor_rlm import ContainerPythonBridge

    class FakeEnv:
        async def exec(
            self, command: str, cwd: str | None = None, timeout_sec: int = 30
        ) -> ExecResult:
            done = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
            return ExecResult(stdout=done.stdout, stderr=done.stderr, return_code=done.returncode)

    async def run() -> str:
        loop = asyncio.get_running_loop()
        bridge = ContainerPythonBridge(FakeEnv(), loop, cwd="/tmp")
        code = "print(repr('it''s $HOME `x` \\\\ \"q\"'))\nprint(1+1)"
        return await loop.run_in_executor(None, lambda: bridge.run_python(code))

    output = asyncio.run(run())
    assert output.splitlines() == ["'its $HOME `x` \\\\ \"q\"'", "2"]
    assert [t.__name__ for t in ContainerPythonBridge(None, None).get_tools()][-1] == "run_python"  # type: ignore[arg-type]
