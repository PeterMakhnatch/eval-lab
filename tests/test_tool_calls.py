"""Pure-function tests for the shared tool-call normaliser (synthetic calls only)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from evallab.tool_calls import (
    call_name_and_arguments,
    normalize_tool_call,
    terminal_shell_input,
    terminal_shell_ready,
)

PROMPT = "root@host:/app# "


def _terminal(keystrokes: str, *, shell_ready: bool = True) -> Any:
    return normalize_tool_call("bash_command", {"keystrokes": keystrokes, "duration": 1}, shell_ready=shell_ready)


# --------------------------------------------------------------------------- #
# Terminus keystrokes: accepted
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("keystrokes", "command"),
    [
        ("ls -la\n", "ls -la"),
        ("ls -la\r\n", "ls -la"),
        ("\x03pwd\n", "pwd"),
        ('git commit -m "Fix Tab handling"\n', 'git commit -m "Fix Tab handling"'),
        ("pwd\n\n\nls\n", "pwd\n\n\nls"),
        ("pwd\nls -la\nfind /app -name '*.py' -exec cat {} \\;\n", "pwd\nls -la\nfind /app -name '*.py' -exec cat {} \\;"),
        ("sed -i 's/a#b/c/' f\nls\n", "sed -i 's/a#b/c/' f\nls"),
        ("grep -n foo file.txt | sort | head\nls\n", "grep -n foo file.txt | sort | head\nls"),
        ("grep a <<< 'abc'\nls\n", "grep a <<< 'abc'\nls"),
        ("python3 -c 'print(1)'\nls\n", "python3 -c 'print(1)'\nls"),
        ("python3 script.py\nvim notes.txt\n", "python3 script.py\nvim notes.txt"),
        ("cat > /tmp/a.sh <<'EOF'\n#!/bin/sh\necho hi\nEOF\nchmod +x /tmp/a.sh\n",
         "cat > /tmp/a.sh <<'EOF'\n#!/bin/sh\necho hi\nEOF\nchmod +x /tmp/a.sh"),
        ("cat <<-EOF > f\n\tx\n\tEOF\nls\n", "cat <<-EOF > f\n\tx\n\tEOF\nls"),
        ("cat <<\\EOF > f\nbody with 'apostrophe\nEOF\nls\n", "cat <<\\EOF > f\nbody with 'apostrophe\nEOF\nls"),
    ],
)
def test_terminal_shell_input_accepts(keystrokes: str, command: str) -> None:
    assert terminal_shell_input(keystrokes) == command


# --------------------------------------------------------------------------- #
# Terminus keystrokes: adversarial table, every row MUST be unknown
# --------------------------------------------------------------------------- #

MUST_BE_UNKNOWN = {
    # not submitted, bare keys, key names mixed with text (typed literally)
    "no-newline": "pwd",
    "bare-ctrl-c": "C-c",
    "bare-enter": "Enter",
    "empty-line-only": "\n",
    "text-then-enter-key": "ls Enter",
    "ctrl-c-prefix-text": "C-c ls\n",
    "ctrl-c-glued": "C-cpwd\n",
    "ctrl-d": "C-d\n",
    "ctrl-z-line": "ls\nC-z\n",
    "escape-then-vim-command": "vim x\nEscape\n:wq\n",
    "arrow-key": "Up\n",
    # control and C1 characters
    "del": "ls\x7f\n",
    "c1-control": "ls\x85\n",
    "c0-escape-sequence": "\x1b[A\n",
    "interior-cr": "ls\rpwd\n",
    "nul": "ls\x00\n",
    # comments swallow or desynchronise following lines
    "comment-first": "# comment\npython3\nopen('/tests/x','w').write('p')\n",
    "comment-apostrophe": "# don't\npython3\nimport os\n",
    "trailing-comment": "pwd # check dir\ncat > /tests/test_outputs.py\ndef test_ok(): pass\n",
    "comment-with-quote": "pwd # comment\necho \"unterminated quote\n",
    # heredoc spoofing and openness
    "quoted-heredoc-text": 'echo "<<EOF"\npython3\nopen("/tests/test_outputs.py", "w").write("pass")\nEOF\n',
    "commented-heredoc": "true # <<EOF\nvim /tests/test_outputs.py\n:%s/False/True/g\n:wq\nEOF\n",
    "indented-terminator": "cat > /tmp/a <<EOF\n  EOF\n",
    "backslash-delimiter-unterminated": "cat > /tmp/a <<\\EOF\n",
    "partial-quoted-delimiter": "cat > /tmp/a <<'E'OF\nE\n",
    "unterminated-heredoc": "cat > f <<EOF\nbody\n",
    "trailing-space-terminator": "cat > f <<EOF\nbody\nEOF \n",
    "heredoc-without-delimiter": "cat <<\n",
    # interactive programs and stdin readers before the last line
    "repl-then-input": "python\nimport os\n",
    "repl-py312": "python3.12\nimport os\n",
    "editor-then-keys": "vim notes.txt\ni sed -i s/a/b/ f\n",
    "cat-to-file-stdin": "cat > f\nhello\n",
    "cat-dash": "cat -\nx\n",
    "cat-fd-redirect": "cat 2>&1\nhello\n",
    "subshell-repl": "(python3)\nimport os\n",
    "command-substitution": "echo $(python3)\nimport os\n",
    "sudo-flag-with-operand": "sudo -u root python3\nimport os\n",
    "env-flag-with-operand": "env -C /app python3\nx\n",
    "env-wrapper": "env FOO=1 python3\nx\n",
    "interpreter-dash": "python3 -\nprint(1)\n",
    "interpreter-interactive-flag": "python3 -i x.py\nx\n",
    "ssh": "ssh host\nls\n",
    "su": "su - developer\nid\n",
    "unlisted-program": "mysterytool\nls\n",
    "tail-follow": "tail -f /var/log/x\nls\n",
    "git-pager": "git log\nls\n",
    "docker-interactive": "docker run -it ubuntu\nls\n",
    "package-prompt": "apt-get install -f foo\nls\n",
    # quoting and continuation
    "unbalanced-quote": "echo 'unterminated\n",
    "multiline-quote": "python3 -c \"\nimport os\nprint(os.getcwd())\n\"\n",
    "continuation": "ls \\\n",
    "continuation-mid": "ls \\\n-la\n",
    "trailing-escaped-backslash": "echo a\\\n",
}


@pytest.mark.parametrize("keystrokes", MUST_BE_UNKNOWN.values(), ids=MUST_BE_UNKNOWN.keys())
def test_terminal_input_that_is_not_certainly_shell_is_unknown(keystrokes: str) -> None:
    action = _terminal(keystrokes)
    assert action.kind == "unknown"
    assert action.name == "bash_command"
    assert action.arguments == {"keystrokes": keystrokes, "duration": 1}
    assert action.text == keystrokes
    assert action.terminal
    assert not action.shell_after


def test_terminal_input_is_unknown_without_shell_context() -> None:
    assert _terminal("ls\n", shell_ready=False).kind == "unknown"


def test_empty_keystrokes_are_a_wait_that_keeps_the_context() -> None:
    assert _terminal("").kind == "noop"
    assert _terminal("", shell_ready=True).shell_after
    assert not _terminal("", shell_ready=False).shell_after


# --------------------------------------------------------------------------- #
# Shell context
# --------------------------------------------------------------------------- #


def test_terminal_shell_ready_reads_prompt_and_confirmation_trailer() -> None:
    assert terminal_shell_ready("out\n" + PROMPT)
    assert terminal_shell_ready("out\n\x1b[0m" + PROMPT + "\r\n")
    assert terminal_shell_ready("out\nroot@host:/app#\n\n\n\n")
    assert terminal_shell_ready("out\n(venv) user@host:~$ ")
    assert terminal_shell_ready(PROMPT + "\n\nAre you sure you want to mark the task as complete? ...\n")


@pytest.mark.parametrize(
    "observation",
    [
        "Python 3.12\n>>> ",
        "-- INSERT --",
        "",
        "root@host:/app# ls\nfoo\nuser@host:~$ echo hi #",
        "root@host:/app# cat > f",
        "total 4\nprice: user@host:/x costs 5$",
        "root@host:/app# python3\nPython 3.12\n",
        "echo done #",
    ],
)
def test_terminal_shell_ready_is_false_unless_the_last_line_is_an_empty_prompt(observation: str) -> None:
    assert not terminal_shell_ready(observation)


def test_terminal_shell_ready_is_linear_on_blank_lines() -> None:
    start = time.perf_counter()
    assert not terminal_shell_ready("x" + "\n" * 200_000 + "y")
    assert terminal_shell_ready("root@host:/app#" + "\n" * 200_000)
    assert time.perf_counter() - start < 2


def test_terminus_keystrokes_become_shell_action() -> None:
    action = _terminal("ls\n")
    assert (action.kind, action.name, action.text, action.terminal) == ("shell", "bash", "ls", True)
    assert action.arguments == {"command": "ls"}
    assert action.shell_after


def test_command_that_starts_an_interactive_program_ends_the_shell_context() -> None:
    assert not _terminal("python3\n").shell_after
    assert not _terminal("vim notes.txt\n").shell_after
    assert not _terminal("ls\ncat > f\n").shell_after
    assert _terminal("python3 -c 'print(1)'\n").shell_after


# --------------------------------------------------------------------------- #
# Other transports
# --------------------------------------------------------------------------- #


def test_complete_and_planning_calls_are_noops() -> None:
    assert normalize_tool_call("mark_task_complete", {}).kind == "noop"
    assert normalize_tool_call("task_complete", {"input": "{}"}).kind == "noop"
    assert normalize_tool_call("sleep", {"sleep_payload": {}, "explanation": "wait"}).kind == "noop"
    assert normalize_tool_call("TodoWrite", {"input": "{}"}).kind == "noop"


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("mark_task_complete", {"command": "rm -rf /tests"}),
        ("mark_task_complete", {"input": "done"}),
        ("task_complete", {"keystrokes": "rm x\n"}),
        ("sleep", {"command": "rm -rf /tests"}),
        ("wait", {"wait_payload": {}, "code": "import os"}),
        ("exit", {"keystrokes": "ls\n"}),
        ("submit_final_answer", {"script": "x"}),
    ],
)
def test_noop_names_with_executable_arguments_are_unknown(name: str, arguments: dict[str, Any]) -> None:
    assert normalize_tool_call(name, arguments).kind == "unknown"


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
        {"command": "insert", "path": "a.py"},
        {"command": "replace_everything", "path": "a.py"},
        {"command": "view"},
        {"command": "view", "path": ""},
        {"command": "view", "path": "   "},
        {"command": "view", "path": "."},
        {"command": "view", "path": "a/.."},
        {"command": "view", "path": "a\nb"},
        {"command": "view", "path": 3},
    ],
)
def test_text_editor_malformed_is_unknown(arguments: dict[str, Any]) -> None:
    assert normalize_tool_call("text_editor", arguments).kind == "unknown"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("tests/../tests/test_core.py", "tests/test_core.py"),
        ("./tests/test_core.py", "tests/test_core.py"),
        ("//tests//test_core.py", "/tests/test_core.py"),
        ("  /tests/./x.py  ", "/tests/x.py"),
        ("../tests/x.py", "../tests/x.py"),
    ],
)
def test_editor_and_notebook_paths_are_normalised_in_action_and_arguments(raw: str, clean: str) -> None:
    editor = normalize_tool_call("text_editor", {"command": "view", "path": raw})
    assert editor.path == clean
    assert editor.arguments["path"] == clean
    notebook = normalize_tool_call("edit_file", {"file_path": raw, "file_edit_payload": {"source": ["x"]}})
    assert notebook.path == clean
    assert notebook.arguments["path"] == clean


def test_bundle_shapes() -> None:
    shell = normalize_tool_call("shell", {"command": "pwd", "keystrokes": "pwd"})
    assert (shell.kind, shell.text, shell.terminal) == ("shell", "pwd", False)
    assert normalize_tool_call("shell", {"command": "pwd", "keystrokes": "ls"}).kind == "unknown"
    glob = normalize_tool_call("glob", {"path": "src", "pattern": "*.py"})
    assert (glob.kind, glob.text, glob.path) == ("read", "ls -- 'src/*.py'", "src/*.py")
    notebook = normalize_tool_call(
        "edit_notebook", {"file_path": "n.ipynb", "file_edit_payload": {"source": ["a = 1\n", "b = 2\n"]}}
    )
    assert (notebook.kind, notebook.text, notebook.path) == ("edit", "a = 1\nb = 2\n", "n.ipynb")
    stripped = normalize_tool_call(
        "edit_file", {"file_path": "n.ipynb", "file_edit_payload": {"source": ["# plan", "x = 1", ""]}}
    )
    assert stripped.text == "# plan\nx = 1\n"


def test_glob_with_several_paths_keeps_every_path_in_the_command() -> None:
    glob = normalize_tool_call("glob", {"path": "src;/tests", "pattern": "*.py", "i": "list"})
    assert glob.kind == "read"
    assert glob.text == "ls -- 'src/*.py' '/tests/*.py'"
    assert glob.path is None


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"glob": "/tests/test_outputs.py"},
        {"file_pattern": "*.py"},
        {"path": ""},
        {"path": ";;;"},
        {"path": "  "},
        {"path": None, "pattern": "*.py"},
        {"path": "src", "pattern": 3},
        {"path": "src", "command": "rm -rf /"},
        {"path": "src;", "pattern": "a\nb"},
    ],
)
def test_glob_with_unrecognised_arguments_is_unknown(arguments: dict[str, Any]) -> None:
    assert normalize_tool_call("glob", arguments).kind == "unknown"


def test_glob_path_that_looks_like_an_option_is_not_an_ls_option() -> None:
    assert normalize_tool_call("glob", {"path": "-la"}).text == "ls -- -la"


def test_unknown_calls_pass_through_unchanged() -> None:
    arguments = {"code": "print(1)"}
    action = normalize_tool_call("execute_code", arguments)
    assert (action.kind, action.name, dict(action.arguments)) == ("unknown", "execute_code", arguments)
    assert normalize_tool_call("anything", "raw string").kind == "unknown"


def test_call_name_and_arguments_handles_both_envelopes() -> None:
    assert call_name_and_arguments({"function_name": "a", "arguments": {"x": 1}}) == ("a", {"x": 1})
    assert call_name_and_arguments({"function": {"name": "b", "arguments": '{"y": 2}'}}) == ("b", {"y": 2})
