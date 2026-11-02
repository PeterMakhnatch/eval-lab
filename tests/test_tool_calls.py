"""Pure-function tests for the shared tool-call normaliser (synthetic calls only)."""

from __future__ import annotations

from typing import Any

import pytest

from evallab.tool_calls import (
    call_name_and_arguments,
    normalize_tool_call,
    terminal_shell_input,
    terminal_shell_ready,
)

PROMPT = "root@host:/app# "


@pytest.mark.parametrize(
    ("keystrokes", "command"),
    [
        ("ls -la\n", "ls -la"),
        ("ls -la\r\n", "ls -la"),
        ("C-cpwd\n", "pwd"),
        ("pytest -q Enter", "pytest -q"),
        ('git commit -m "Fix Tab handling"\n', 'git commit -m "Fix Tab handling"'),
    ],
)
def test_terminal_shell_input_strips_controls(keystrokes: str, command: str) -> None:
    assert terminal_shell_input(keystrokes) == command


@pytest.mark.parametrize(
    "keystrokes",
    [
        "pwd",
        "C-c",
        "Enter",
        "",
        "python\nimport os\n",
        "cat > f\nhello\n",
        "vim notes.txt\ni sed -i s/a/b/ f\n",
        "cat > f <<EOF\n",
        "echo 'unterminated\n",
        "ls \\\n",
        "\x1b[A\n",
    ],
)
def test_terminal_shell_input_is_unsure(keystrokes: str) -> None:
    assert terminal_shell_input(keystrokes) is None


@pytest.mark.parametrize(
    "keystrokes",
    [
        "pwd\nls -la\nfind /app -maxdepth 2 -name '*.py' -exec cat {} \\;\n",
        "cat > /tmp/a.sh <<'EOF'\n#!/bin/sh\necho hi\nEOF\nchmod +x /tmp/a.sh\n",
        "python3 -c \"\nimport os\nprint(os.getcwd())\n\"\n",
        "grep -n foo file.txt | sort | head\n",
        "python3 script.py\nvim notes.txt\n",
    ],
)
def test_terminal_shell_input_accepts_complete_multiline_and_heredocs(keystrokes: str) -> None:
    assert terminal_shell_input(keystrokes) == keystrokes.rstrip("\n")


def test_terminal_shell_ready_reads_prompt_and_confirmation_trailer() -> None:
    assert terminal_shell_ready("out\n" + PROMPT)
    assert terminal_shell_ready("out\n\x1b[0m" + PROMPT + "\r\n")
    assert terminal_shell_ready(PROMPT + "\n\nAre you sure you want to mark the task as complete? ...\n")
    assert not terminal_shell_ready("Python 3.12\n>>> ")
    assert not terminal_shell_ready("-- INSERT --")


def test_terminus_keystrokes_become_shell_action() -> None:
    action = normalize_tool_call("bash_command", {"keystrokes": "ls\n", "duration": 1})
    assert (action.kind, action.name, action.text, action.terminal) == ("shell", "bash", "ls", True)
    assert action.arguments == {"command": "ls"}
    assert action.shell_after


def test_command_that_starts_an_interactive_program_ends_the_shell_context() -> None:
    assert not normalize_tool_call("bash_command", {"keystrokes": "python3\n"}).shell_after
    assert not normalize_tool_call("bash_command", {"keystrokes": "vim notes.txt\n"}).shell_after
    assert normalize_tool_call("bash_command", {"keystrokes": "python3 -c 'print(1)'\n"}).shell_after


def test_terminus_keystrokes_without_shell_context_are_unknown_but_displayable() -> None:
    arguments = {"keystrokes": "x = 1\n"}
    action = normalize_tool_call("bash_command", arguments, shell_ready=False)
    assert action.kind == "unknown"
    assert action.name == "bash_command"
    assert dict(action.arguments) == arguments
    assert action.text == "x = 1\n"
    assert action.terminal
    assert not action.shell_after


def test_empty_keystrokes_are_a_wait() -> None:
    assert normalize_tool_call("bash_command", {"keystrokes": "", "duration": 5}).kind == "noop"


def test_complete_and_planning_calls_are_noops() -> None:
    assert normalize_tool_call("mark_task_complete", {}).kind == "noop"
    assert normalize_tool_call("task_complete", {"input": "{}"}).kind == "noop"
    assert normalize_tool_call("sleep", {"sleep_payload": {}}).kind == "noop"
    assert normalize_tool_call("mark_task_complete", {"input": "done"}).kind == "unknown"


@pytest.mark.parametrize(
    ("arguments", "kind", "name"),
    [
        ({"command": "view", "path": "a.py"}, "read", "str_replace_editor"),
        ({"command": "create", "path": "a.py", "file_text": "x = 1\n"}, "write", "str_replace_editor"),
        ({"command": "str_replace", "path": "a.py", "old_str": "a", "new_str": "b"}, "edit", "str_replace_editor"),
        ({"command": "insert", "path": "a.py", "insert_line": 1, "new_str": "b"}, "edit", "str_replace_editor"),
        ({"command": "undo_edit", "path": "a.py"}, "edit", "edit"),
    ],
)
def test_text_editor_commands(arguments: dict[str, Any], kind: str, name: str) -> None:
    action = normalize_tool_call("text_editor", arguments)
    assert (action.kind, action.name, action.path) == (kind, name, "a.py")


@pytest.mark.parametrize(
    "arguments",
    [
        {"command": "create", "path": "a.py"},
        {"command": "str_replace", "path": "a.py", "new_str": "b"},
        {"command": "replace_everything", "path": "a.py"},
        {"command": "view"},
    ],
)
def test_text_editor_malformed_is_unknown(arguments: dict[str, Any]) -> None:
    assert normalize_tool_call("text_editor", arguments).kind == "unknown"


def test_bundle_shapes() -> None:
    shell = normalize_tool_call("shell", {"command": "pwd", "keystrokes": "pwd"})
    assert (shell.kind, shell.text, shell.terminal) == ("shell", "pwd", False)
    assert normalize_tool_call("shell", {"command": "pwd", "keystrokes": "ls"}).kind == "unknown"
    glob = normalize_tool_call("glob", {"path": "src", "pattern": "*.py"})
    assert (glob.kind, glob.text) == ("read", "ls 'src/*.py'")
    notebook = normalize_tool_call(
        "edit_notebook", {"file_path": "n.ipynb", "file_edit_payload": {"source": ["a = 1\n", "b = 2\n"]}}
    )
    assert (notebook.kind, notebook.text, notebook.path) == ("edit", "a = 1\nb = 2\n", "n.ipynb")
    stripped = normalize_tool_call(
        "edit_file", {"file_path": "n.ipynb", "file_edit_payload": {"source": ["# plan", "x = 1", ""]}}
    )
    assert stripped.text == "# plan\nx = 1\n"


def test_unknown_calls_pass_through_unchanged() -> None:
    arguments = {"code": "print(1)"}
    action = normalize_tool_call("execute_code", arguments)
    assert (action.kind, action.name, dict(action.arguments)) == ("unknown", "execute_code", arguments)
    assert normalize_tool_call("anything", "raw string").kind == "unknown"


def test_call_name_and_arguments_handles_both_envelopes() -> None:
    assert call_name_and_arguments({"function_name": "a", "arguments": {"x": 1}}) == ("a", {"x": 1})
    assert call_name_and_arguments({"function": {"name": "b", "arguments": '{"y": 2}'}}) == ("b", {"y": 2})
