"""MiMo native tool-call normalization on the Terminus-2 route (HAR-90).

The fixtures are verbatim agent turns from the HAR-90 Daytona trials, from
``runs/har90-mimo-*/…/agent/trajectory*.json``, and from the HAR-81 pilot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from evallab.mimo_tool_calls import (
    HARBOR_FALLBACK_RESPONSE,
    MimoToolCallParser,
    executed_keystrokes,
    normalize_mimo_tool_calls,
    prose_completion,
)

# Trial 1: a whole Terminus object behind an unterminated exec wrapper.
WRAPPED_TERMINUS = (
    '<tool_call><function=exec>{\n  "analysis": "The commands keep getting concatenated. Let me try '
    'a different approach - use a single command per response.",\n  "plan": "Run one simple '
    'command.",\n  "commands": [\n    {"keystrokes": "grep -rn \'temp_output\' '
    "/app/vendor/snakemake/src --include='*.py' | head -40\", \"duration\": 0.1}\n  ]\n}"
)
# Trial 2: the same object behind a closed exec_command wrapper.
WRAPPED_TERMINUS_CLOSED = (
    '<tool_call><function=exec_command>{\n  "analysis": "I need to explore the working directory '
    'to understand the task setup.",\n  "plan": "List the /app directory contents to see the '
    'project structure.",\n  "commands": [\n    {\n      "keystrokes": "ls -la /app",\n'
    '      "duration": 0.1\n    }\n  ]\n}\n</function></tool_call>'
)
# 0036-g: the completion call, bare and after a one-line summary (307 turns).
NATIVE_COMPLETE = (
    "<tool_call><function=task_complete><parameter=task_complete>true</parameter>"
    "</function></tool_call>"
)
NATIVE_COMPLETE_SUMMARY = (
    "The repair is complete and verified. The probe passes and `/app/output.json` is written."
    + NATIVE_COMPLETE
)
# Trial 2: native exec_command calls, one pretty-printed and three in a row.
NATIVE_SINGLE = (
    '<tool_call><function=exec_command>{\n  "keystrokes": "pwd",\n  "duration": 0.1\n}\n'
    "</function></tool_call>"
)
NATIVE_MULTI = (
    '<tool_call><function=exec_command>{"keystrokes": "ls -la /app\\n", "duration": 0.1}'
    "</function></tool_call>"
    '<tool_call><function=exec_command>{"keystrokes": "ls -la /app/vendor/snakemake\\n", '
    '"duration": 0.1}</function></tool_call>'
    "<tool_call><function=exec_command>{\"keystrokes\": \"find /app -maxdepth 3 -name '*.py' | "
    'head -50\\n", "duration": 0.1}</function></tool_call>'
)
# Trials 3 and 4: Qwen3-Coder XML arguments to a bash tool, with and without duration.
NATIVE_BASH_MULTI = (
    "<tool_call><function=bash><parameter=command>ls -la /app && ls -la /app/vendor/snakemake"
    "</parameter><parameter=duration>0.1</parameter></function></tool_call>"
    "<tool_call><function=bash><parameter=command>cat /app/workflow_probe.py 2>/dev/null | head -100"
    "</parameter><parameter=duration>0.1</parameter></function></tool_call>"
)
NATIVE_BASH_NO_DURATION = (
    "<tool_call><function=bash><parameter=command>"
    "cat /app/vendor/onnx/onnx/reference/ops/op_tensor_scatter.py</parameter></function></tool_call>"
)
# Trial 0758-d after its summarization: Claude Code-style bash calls that
# carry a description, with text before them (verbatim, cont-1 step 5).
NATIVE_BASH_DESCRIBED = (
    "I'll verify the current state of both files and re-run the workflow to confirm everything "
    "is correct.<tool_call><function=bash><parameter=command>cd /app && sed -n '1,120p' "
    "vendor/onnx/onnx/reference/ops/op_tensor_scatter.py</parameter><parameter=description>Show "
    "the Python reference op source</parameter></function></tool_call><tool_call><function=bash>"
    '<parameter=command>cd /app && grep -n -A 30 "def _run" vendor/onnx/onnx/defs/tensor/defs.cc'
    " | head -60</parameter><parameter=description>Show the C++ pseudocode section</parameter>"
    "</function></tool_call>"
)
# HAR-81 pilot candidate-2684 (head steps 2 and 5): command calls whose body is
# the command text itself.
RAW_COMMAND_PAIR = (
    "<tool_call><function=command>ls -la /app && ls -la /app/vendor/bandit 2>/dev/null</function>"
    "</tool_call><tool_call><function=command>find /app -maxdepth 3 -type f | head -100"
    "</function></tool_call>"
)
RAW_COMMAND_LINES = (
    "<tool_call><function=command>ls -la /app\nls -la /app/vendor/bandit 2>/dev/null\n"
    "find /app -maxdepth 4 -type f 2>/dev/null | head -100</function></tool_call>"
)
# The same trial's step 8: a Terminus object inside the command call, which
# the stock parser already finds (25 such turns parsed).
COMMAND_WRAPPED_TERMINUS = (
    '<tool_call><function=command>{"analysis": "I need to inspect the /app directory to '
    'understand the project structure.","plan": "List the contents of /app and /app/vendor/bandit '
    'to see the files present.","commands": [{"keystrokes": "ls -la /app","duration": 0.1},'
    '{"keystrokes": "ls -la /app/vendor/bandit 2>/dev/null","duration": 0.1},{"keystrokes": '
    '"find /app -maxdepth 4 -type f 2>/dev/null | head -100","duration": 0.1}]}]</function>'
    "</tool_call>"
)
# Trial 2's one turn that stays unparseable: a dangling fragment of a commands list.
DANGLING_FRAGMENT = (
    '<tool_call><function=exec_command>{"keystrokes": "ls -la /app", "duration": 0.1},\n'
    '{"keystrokes": "ls -la /app/vendor/snakemake | head -50", "duration": 0.1},\n'
    "{\"keystrokes\": \"find /app -maxdepth 3 -name '*.py' | head -50\", \"duration\": 0.1}\n]\n}"
)
# Trial 0758-c: a bare Terminus object with raw newlines inside a string,
# followed by the tail of the XML bash call it filled (the opener is absent).
HYBRID_RAW_NEWLINES = (
    '{\n"analysis": "Now I can see the Python bug clearly. In circular mode, the code applies '
    "`np.mod(..., max_sequence_length)` to the ENTIRE cache_idx tuple, which includes prefix "
    "coordinates (batch/head). The fix: only mod the sequence coordinate (the axis coordinate)."
    '\n\nLet me look at the C++ file to find the corresponding circular-mode logic.",\n'
    '"plan": "Find the TensorScatter circular logic in defs.cc.",\n"commands": [\n'
    '{"keystrokes": "grep -n -i \\"tensor_scatter\\\\|circular\\\\|mod(\\" '
    '/app/vendor/onnx/onnx/defs/tensor/defs.cc | head -80\\n", "duration": 0.2}\n]\n}'
    "</parameter><parameter=duration>0.5</parameter></function></tool_call>"
)
# Trial 0036-e: the final answer MiMo repeated 50 times after solving the task.
PROSE_FINAL = (
    'The repair is complete and verified.\n\n## Summary\n\n**Diagnosis.** The two modules disagre'
    'ed about where output flag categories live:\n\n- `rules.py` stored the *unexpanded* rule-l'
    'evel category sets `self.temp_output`, `self.protected_output`, and `self.touch_output` '
    '(built from pattern-level `is_flagged(item, ...)` checks on the wildcard-constrained `An'
    'notatedString`).\n- `jobs.py` reconstructed the *concrete* job sets by membership-testing'
    ' the expanded `IOFile` `f` against those unexpanded rule sets via `if f_ in self.rule.te'
    'mp_output`. Since an expanded `IOFile` is never identical to a rule-level placeholder, t'
    'hat lookup silently dropped every category — so a concrete job lost `temp`/`protected`/`'
    'touch` classification even though the flags were attached to the expanded output object.'
    '\n\n**Repair.**\n\n1. `src/snakemake/rules.py` — removed the unexpanded category bookkeeping'
    ': the `self.temp_output / self.protected_output / self.touch_output` attributes in `Rule'
    '.__init__` and the `is_flagged(item, "temp"/"protected"/"touch")` accumulation block in '
    '`_set_inoutput_item`. Flag classification is now left to the concrete expanded output.\n2'
    '. `src/snakemake/jobs.py` — `Job.__init__` now classifies each concrete output directly '
    'from its expanded `IOFile` via `is_flagged(f, ...)`, filling `self.temp_output`, `self.p'
    'rotected_output`, `self.touch_output`, and `self.pipe_or_service_output`. The old member'
    'ship-based loop was replaced; `__slots__` was kept intact so the job attributes still ex'
    'ist.\n\nNo wildcard constraints were removed, no sample names were special-cased, and no m'
    'arker files were manufactured.\n\n**Verification.** `python3 workflow_probe.py` passes and'
    ' writes `/app/output.json` with schema `workflow-repair-1`, two concrete wildcard jobs ('
    '`alpha`, `beta`), exactly one `touch` output per job, preserved `temp`+`protected` and `'
    'pipe`/`service` categories, and a completed downstream aggregate (`aggregate.complete: t'
    'rue`) with SHA-256 witnesses for both repaired modules.'
)


@pytest.mark.parametrize(
    "raw",
    [
        WRAPPED_TERMINUS,
        WRAPPED_TERMINUS_CLOSED,
        WRAPPED_TERMINUS.replace("different approach - use", "different approach -\nuse"),
    ],
)
def test_wrapped_terminus_object_passes_through_unchanged(raw: str) -> None:
    normalized = normalize_mimo_tool_calls(raw)

    obj = json.loads(raw[raw.index("{") :].removesuffix("\n</function></tool_call>"), strict=False)
    assert json.loads(normalized or "") == obj


def test_bare_object_with_raw_newlines_and_native_tail_is_canonicalized() -> None:
    normalized = json.loads(normalize_mimo_tool_calls(HYBRID_RAW_NEWLINES) or "")

    assert normalized["analysis"].endswith("\n\nLet me look at the C++ file to find the corresponding circular-mode logic.")
    assert normalized["commands"] == [
        {
            "keystrokes": 'grep -n -i "tensor_scatter\\|circular\\|mod(" '
            "/app/vendor/onnx/onnx/defs/tensor/defs.cc | head -80\n",
            "duration": 0.2,
        }
    ]


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param('{"analysis": "a", "plan": "", "commands": []}', id="valid-terminus-json"),
        pytest.param('{"analysis": "a\nb", "plan": "", "commands": []} trailing prose', id="prose-tail"),
        pytest.param('{"analysis": "a\nb", "plan": ""}</parameter></function>', id="not-terminus"),
        pytest.param("The repair is complete and verified.", id="plain-prose"),
    ],
)
def test_bare_responses_needing_no_rewrite_reach_terminus_unchanged(raw: str) -> None:
    assert normalize_mimo_tool_calls(raw) is None


def test_native_command_calls_become_commands_in_order() -> None:
    single = json.loads(normalize_mimo_tool_calls(NATIVE_SINGLE) or "")
    multi = json.loads(normalize_mimo_tool_calls(NATIVE_MULTI) or "")
    bash = json.loads(normalize_mimo_tool_calls(NATIVE_BASH_MULTI) or "")
    bare = json.loads(normalize_mimo_tool_calls(NATIVE_BASH_NO_DURATION) or "")
    described = json.loads(normalize_mimo_tool_calls(NATIVE_BASH_DESCRIBED) or "")

    assert single == {
        "analysis": "",
        "plan": "",
        "commands": [{"keystrokes": "pwd", "duration": 0.1}],
    }
    assert [c["keystrokes"] for c in multi["commands"]] == [
        "ls -la /app\n",
        "ls -la /app/vendor/snakemake\n",
        "find /app -maxdepth 3 -name '*.py' | head -50\n",
    ]
    assert bash["commands"] == [
        {"keystrokes": "ls -la /app && ls -la /app/vendor/snakemake", "duration": 0.1},
        {"keystrokes": "cat /app/workflow_probe.py 2>/dev/null | head -100", "duration": 0.1},
    ]
    assert bare["commands"] == [
        {"keystrokes": "cat /app/vendor/onnx/onnx/reference/ops/op_tensor_scatter.py"}
    ]
    # A description only labels its call: the commands run, the labels go.
    assert described == {
        "analysis": (
            "I'll verify the current state of both files and re-run the workflow to confirm "
            "everything is correct."
        ),
        "plan": "",
        "commands": [
            {"keystrokes": "cd /app && sed -n '1,120p' vendor/onnx/onnx/reference/ops/op_tensor_scatter.py"},
            {
                "keystrokes": 'cd /app && grep -n -A 30 "def _run" vendor/onnx/onnx/defs/tensor/defs.cc'
                " | head -60"
            },
        ],
    }


def test_xml_values_drop_only_the_markup_newlines() -> None:
    raw = "<tool_call><function=bash><parameter=command>\nprintf 'a\\n\\n'\n\n</parameter></function></tool_call>"

    assert json.loads(normalize_mimo_tool_calls(raw) or "")["commands"] == [
        {"keystrokes": "printf 'a\\n\\n'\n"}
    ]


def test_text_before_native_calls_becomes_the_analysis() -> None:
    normalized = normalize_mimo_tool_calls("Check the tree first.\n" + NATIVE_SINGLE)

    assert json.loads(normalized or "")["analysis"] == "Check the tree first."


def test_raw_command_calls_become_commands_in_order() -> None:
    pair = json.loads(normalize_mimo_tool_calls(RAW_COMMAND_PAIR) or "")
    lines = json.loads(normalize_mimo_tool_calls(RAW_COMMAND_LINES) or "")
    mixed = json.loads(
        normalize_mimo_tool_calls("Look first." + RAW_COMMAND_LINES + NATIVE_BASH_NO_DURATION) or ""
    )
    framed = normalize_mimo_tool_calls("<tool_call><function=command>\nmake -j4\n\n</function></tool_call>")

    assert pair == {
        "analysis": "",
        "plan": "",
        "commands": [
            {"keystrokes": "ls -la /app && ls -la /app/vendor/bandit 2>/dev/null"},
            {"keystrokes": "find /app -maxdepth 3 -type f | head -100"},
        ],
    }
    assert lines["commands"] == [
        {
            "keystrokes": "ls -la /app\nls -la /app/vendor/bandit 2>/dev/null\n"
            "find /app -maxdepth 4 -type f 2>/dev/null | head -100"
        }
    ]
    assert mixed["analysis"] == "Look first."
    assert [c["keystrokes"] for c in mixed["commands"]] == [
        lines["commands"][0]["keystrokes"],
        "cat /app/vendor/onnx/onnx/reference/ops/op_tensor_scatter.py",
    ]
    assert json.loads(framed or "")["commands"] == [{"keystrokes": "make -j4\n"}]



@pytest.mark.parametrize(
    "raw",
    [
        pytest.param('{"analysis": "a", "plan": "p", "commands": []}', id="plain-terminus"),
        pytest.param(DANGLING_FRAGMENT, id="dangling-fragment"),
        pytest.param(NATIVE_SINGLE.replace("exec_command", "submit"), id="unknown-function"),
        pytest.param(NATIVE_SINGLE.replace('"keystrokes"', '"cmd"'), id="unknown-argument"),
        pytest.param(NATIVE_SINGLE + "\nWaiting for output.", id="trailing-prose"),
        pytest.param(WRAPPED_TERMINUS_CLOSED + NATIVE_SINGLE, id="object-plus-call"),
        pytest.param(
            NATIVE_SINGLE.replace('"pwd"', '["pwd"]'), id="non-string-keystrokes"
        ),
        pytest.param(
            NATIVE_BASH_NO_DURATION.replace("</parameter>", "</parameter><parameter=cwd>/app</parameter>"),
            id="unknown-xml-parameter",
        ),
        pytest.param(NATIVE_BASH_MULTI.replace(">0.1<", ">fast<", 1), id="non-numeric-duration"),
        pytest.param(
            NATIVE_BASH_NO_DURATION.replace(
                "</parameter>", "</parameter><parameter=command>ls</parameter>"
            ),
            id="repeated-xml-parameter",
        ),
        pytest.param(
            NATIVE_SINGLE.replace('"duration"', '"command": "ls", "duration"'),
            id="keystrokes-and-command",
        ),
        pytest.param(NATIVE_COMPLETE.replace(">true<", ">false<"), id="completion-false"),
        pytest.param(
            NATIVE_COMPLETE.replace("</parameter>", "</parameter><parameter=summary>x</parameter>"),
            id="completion-extra-argument",
        ),
        pytest.param(NATIVE_COMPLETE + NATIVE_BASH_NO_DURATION, id="completion-then-command"),
        pytest.param(NATIVE_BASH_NO_DURATION + NATIVE_COMPLETE, id="command-then-completion"),
        pytest.param("<tool_call><function=task_complete>", id="completion-cut-off"),
        pytest.param(NATIVE_COMPLETE + "\nDone.", id="completion-trailing-prose"),
        pytest.param(COMMAND_WRAPPED_TERMINUS, id="command-call-with-terminus-object"),
        pytest.param("<tool_call><function=command>ls -la /app", id="raw-command-cut-off"),
        pytest.param(
            "<tool_call><function=command> \n</function></tool_call>", id="raw-command-empty"
        ),
        pytest.param(RAW_COMMAND_PAIR + "\nWaiting.", id="raw-command-trailing-prose"),
        pytest.param(
            "<tool_call><function=command><parameter=cmd>ls</parameter></function></tool_call>",
            id="command-call-with-parameters",
        ),
        pytest.param(RAW_COMMAND_PAIR + COMMAND_WRAPPED_TERMINUS, id="raw-command-plus-object"),
    ],
)
def test_other_shapes_are_left_to_terminus(raw: str) -> None:
    assert normalize_mimo_tool_calls(raw) is None


@pytest.mark.parametrize(
    ("keystrokes", "executed"),
    [
        ("pwd", "pwd\n"),
        ("cat > f <<'EOF'\nx\nEOF", "cat > f <<'EOF'\nx\nEOF\n"),
        ("q", "q\n"),
        ("ls -la\n", "ls -la\n"),
        ("printf x\r", "printf x\r"),
        ("", ""),
        ("C-c", "C-c"),
        ("C-M-a", "C-M-a"),
        ("^D", "^D"),
        ("Escape", "Escape"),
        ("S-Up", "S-Up"),
    ],
)
def test_executed_keystrokes_add_enter_except_waits_and_tmux_keys(
    keystrokes: str, executed: str
) -> None:
    assert executed_keystrokes(keystrokes) == executed


@dataclass
class _Command:
    keystrokes: str


@dataclass
class _Result:
    commands: list[_Command]
    error: str = ""
    is_task_complete: bool = False
    analysis: str = ""


class _JsonCommandsParser:
    """Minimal stand-in for Terminus's parser: JSON in, commands out."""

    def parse_response(self, response: str) -> _Result:
        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            return _Result([], "Invalid JSON")
        if not isinstance(data, dict) or "commands" not in data:
            return _Result([], "Missing required fields")
        return _Result(
            [_Command(c["keystrokes"]) for c in data["commands"]],
            is_task_complete=data.get("task_complete") is True,
            analysis=data.get("analysis", ""),
        )


def _parser(finish_reason: str | None = "stop") -> MimoToolCallParser:
    return MimoToolCallParser(_JsonCommandsParser(), finish_reason=lambda: finish_reason)


@pytest.mark.parametrize(
    ("raw", "sent"),
    [
        (NATIVE_SINGLE, ["pwd\n"]),
        (
            RAW_COMMAND_PAIR,
            [
                "ls -la /app && ls -la /app/vendor/bandit 2>/dev/null\n",
                "find /app -maxdepth 3 -type f | head -100\n",
            ],
        ),
        (WRAPPED_TERMINUS_CLOSED, ["ls -la /app\n"]),
        (
            HYBRID_RAW_NEWLINES,
            ['grep -n -i "tensor_scatter\\|circular\\|mod(" /app/vendor/onnx/onnx/defs/tensor/defs.cc | head -80\n'],
        ),
        (
            NATIVE_BASH_MULTI,
            [
                "ls -la /app && ls -la /app/vendor/snakemake\n",
                "cat /app/workflow_probe.py 2>/dev/null | head -100\n",
            ],
        ),
        ('{"analysis": "", "plan": "", "commands": [{"keystrokes": "make"}]}', ["make\n"]),
    ],
)
def test_parser_executes_every_mimo_command(raw: str, sent: list[str]) -> None:
    parser = _parser()
    result = parser.parse_response(raw)

    assert result.error == ""
    assert [c.keystrokes for c in result.commands] == sent
    assert not result.is_task_complete and not parser.last_prose_completion


def test_parser_reports_unnormalizable_turns_as_parse_errors() -> None:
    result = _parser().parse_response(DANGLING_FRAGMENT)

    assert result.error and result.commands == []


def test_stopped_prose_final_answer_completes_the_task() -> None:
    parser = _parser("stop")
    result = parser.parse_response(PROSE_FINAL)

    assert result.error == ""
    assert result.is_task_complete and result.commands == []
    assert result.analysis == PROSE_FINAL
    assert parser.last_prose_completion


@pytest.mark.parametrize("finish_reason", ["length", None, "abort", "tool_calls"])
def test_prose_ends_the_episode_only_when_the_completion_stopped(
    finish_reason: str | None,
) -> None:
    parser = _parser(finish_reason)
    result = parser.parse_response(PROSE_FINAL)

    assert result.error and not result.is_task_complete
    assert not parser.last_prose_completion



@pytest.mark.parametrize(
    ("raw", "analysis"),
    [
        pytest.param(NATIVE_COMPLETE, "", id="bare"),
        pytest.param(
            NATIVE_COMPLETE_SUMMARY,
            "The repair is complete and verified. The probe passes and `/app/output.json` is written.",
            id="after-summary",
        ),
        pytest.param(
            '<tool_call><function=task_complete>{"task_complete": true}</function></tool_call>',
            "",
            id="json-argument",
        ),
        pytest.param("<tool_call><function=task_complete></function></tool_call>", "", id="no-arguments"),
    ],
)
def test_native_completion_call_completes_the_task(raw: str, analysis: str) -> None:
    parser = _parser("length")
    result = parser.parse_response(raw)

    assert result.error == ""
    assert result.is_task_complete and result.commands == []
    assert result.analysis == analysis
    assert not parser.last_prose_completion


def test_mapping_flag_follows_the_latest_turn() -> None:
    parser = _parser("stop")
    parser.parse_response(PROSE_FINAL)
    parser.parse_response(NATIVE_SINGLE)

    assert not parser.last_prose_completion


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty"),
        pytest.param("  \n", id="blank"),
        pytest.param(HARBOR_FALLBACK_RESPONSE, id="harbor-fallback"),
        pytest.param("<think>The task is done.</think>\n", id="reasoning-only"),
        pytest.param("<think>Still checking the diff", id="unterminated-reasoning"),
        pytest.param("All done.</tool_call>", id="stray-closing-markup"),
        pytest.param(DANGLING_FRAGMENT, id="dangling-fragment"),
        pytest.param(
            'Finished: {"analysis": "done", "plan": "", "commands": []}', id="json-object"
        ),
        pytest.param("Result {} recorded.", id="empty-json-object"),
    ],
)
def test_turns_that_are_not_prose_never_complete(raw: str) -> None:
    assert prose_completion(raw) is None


@pytest.mark.parametrize(
    ("raw", "answer"),
    [
        ("<think>Verify once more.</think>\nThe fix is in place.", "The fix is in place."),
        ("Verified the diff.</think>All checks pass.", "All checks pass."),
        (
            "Patched `if (n) { return n; }` in util.c.",
            "Patched `if (n) { return n; }` in util.c.",
        ),
        (PROSE_FINAL, PROSE_FINAL),
    ],
)
def test_prose_answer_is_the_text_after_the_reasoning(raw: str, answer: str) -> None:
    assert prose_completion(raw) == answer
