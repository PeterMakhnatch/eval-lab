"""MiMo native tool-call normalization on the Terminus-2 route (HAR-90).

The fixtures are verbatim agent turns from the HAR-90 Daytona trials, from
``runs/har90-mimo-*/…/agent/trajectory*.json``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from evallab.mimo_tool_calls import (
    MimoToolCallParser,
    executed_keystrokes,
    normalize_mimo_tool_calls,
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


def test_xml_values_drop_only_the_markup_newlines() -> None:
    raw = "<tool_call><function=bash><parameter=command>\nprintf 'a\\n\\n'\n\n</parameter></function></tool_call>"

    assert json.loads(normalize_mimo_tool_calls(raw) or "")["commands"] == [
        {"keystrokes": "printf 'a\\n\\n'\n"}
    ]


def test_text_before_native_calls_becomes_the_analysis() -> None:
    normalized = normalize_mimo_tool_calls("Check the tree first.\n" + NATIVE_SINGLE)

    assert json.loads(normalized or "")["analysis"] == "Check the tree first."


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


class _JsonCommandsParser:
    """Minimal stand-in for Terminus's parser: JSON in, commands out."""

    def parse_response(self, response: str) -> _Result:
        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            return _Result([], "Invalid JSON")
        if not isinstance(data, dict) or "commands" not in data:
            return _Result([], "Missing required fields")
        return _Result([_Command(c["keystrokes"]) for c in data["commands"]])


@pytest.mark.parametrize(
    ("raw", "sent"),
    [
        (NATIVE_SINGLE, ["pwd\n"]),
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
    result = MimoToolCallParser(_JsonCommandsParser()).parse_response(raw)

    assert result.error == ""
    assert [c.keystrokes for c in result.commands] == sent


def test_parser_reports_unnormalizable_turns_as_parse_errors() -> None:
    result = MimoToolCallParser(_JsonCommandsParser()).parse_response(DANGLING_FRAGMENT)

    assert result.error and result.commands == []
