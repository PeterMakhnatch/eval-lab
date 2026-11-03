"""Pure-function tests for the shared tool-call normaliser (synthetic calls only).

The normaliser is an allow-list: a small set of harmless shapes is recognised and
everything else must come back ``unknown`` with the original call unchanged.
"""

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
    ("keystrokes", "command", "shell_after"),
    [
        ("ls -la\n", "ls -la", True),
        ("ls -la\r\n", "ls -la", True),
        ("cd /app && git status\n", "cd /app && git status", True),
        ("cd /app && git diff\n", "cd /app && git diff", False),
        ("git --no-pager diff\n", "git --no-pager diff", True),
        ('grep "#include" main.c\n', 'grep "#include" main.c', True),
        ("grep -rn 'foo.*bar' src\n", "grep -rn 'foo.*bar' src", True),
        ("pytest -q tests/test_a.py 2>&1 | tail -20\n", "pytest -q tests/test_a.py 2>&1 | tail -20", True),
        ("python3 -m pytest -x tests/ 2>/dev/null\n", "python3 -m pytest -x tests/ 2>/dev/null", True),
        ("python3 solve.py\n", "python3 solve.py", False),
        ("cat README.md\n", "cat README.md", True),
        ("head -n 20 file.py\n", "head -n 20 file.py", True),
        ("find . -name '*.py'\n", "find . -name '*.py'", True),
        ("pip install -r requirements.txt\n", "pip install -r requirements.txt", True),
        ("echo hello world\n", "echo hello world", True),
        ("ls | grep foo\n", "ls | grep foo", True),
    ],
)
def test_terminal_shell_input_accepts(keystrokes: str, command: str, shell_after: bool) -> None:
    action = _terminal(keystrokes)
    assert (action.kind, action.name, action.text, action.shell_after) == ("shell", "bash", command, shell_after)
    assert action.arguments == {"command": command}
    assert terminal_shell_input(keystrokes) == command


# --------------------------------------------------------------------------- #
# Terminus keystrokes: adversarial table, every row MUST be unknown
# --------------------------------------------------------------------------- #

MUST_BE_UNKNOWN = {
    # not submitted, bare keys, key names, control characters
    "no-newline": "ls",
    "bare-ctrl-c": "C-c",
    "bare-enter": "Enter",
    "empty-line-only": "\n",
    "text-then-enter-key": "ls Enter",
    "ctrl-c-prefix-text": "C-c ls\n",
    "ctrl-c-glued": "C-cls\n",
    "interrupt-char-prefix": "\x03ls\n",
    "ctrl-d": "C-d\n",
    "caret-ctrl-c": "^C\n",
    "caret-ctrl-d": "^D\n",
    "key-sequence": "Up Up Enter\n",
    "backspace-sequence": "BSpace BSpace Enter\n",
    "escape-command": "Escape :q!\n",
    "echo-key-name": "echo Enter\n",
    "del": "ls\x7f\n",
    "c1-control": "ls\x85\n",
    "c1-csi": "\x9bA\n",
    "escape-sequence": "\x1b[A\n",
    "interior-cr": "ls\rpwd\n",
    "nul": "ls\x00\n",
    # multi-line input of any kind (comments, heredocs, REPL/editor text, quotes, continuations)
    "two-lines": "ls\npwd\n",
    "crlf-two-lines": "pwd\r\nls -la\r\n",
    "comment-first": "# comment\npython3\nopen('/tests/x','w').write('p')\n",
    "comment-apostrophe": "# don't\npython3\nimport os\n",
    "heredoc": "cat > /tmp/a.sh <<'EOF'\necho hi\nEOF\n",
    "quoted-heredoc-text": 'echo "<<EOF"\npython3\nopen("/tests/test_outputs.py", "w").write("pass")\nEOF\n',
    "commented-heredoc": "true # <<EOF\nvim /tests/test_outputs.py\n:wq\nEOF\n",
    "partially-quoted-delimiter": "true <<'ls'x\nlsx\ncp /tmp/exploit /tests/test_outputs.py\nls\n",
    "heredoc-command-substitution": 'true <<EOF\n$(cp /tmp/a "/\\\ntests/test_outputs.py")\nEOF\n',
    "repl-then-input": "python\nimport os\n",
    "editor-then-keys": "vim notes.txt\ni hello\n",
    "multiline-quote": "python3 -c \"\nimport os\n\"\n",
    "continuation": "ls \\\n",
    # one-line constructs outside the allow-list
    "comment": "ls # comment\n",
    "semicolon": "ls; python3\n",
    "or-chain": "ls || python3\n",
    "background": "ls &\n",
    "subshell": "(python3)\n",
    "group": "{ ls; }\n",
    "process-substitution-read": "cat <((cat /tests/test_outputs.py))\n",
    "process-substitution-write": "echo x > >((cat > /tests/test_outputs.py))\n",
    "compound-punctuation": "true;(python3;)\n",
    "redirect-out": "echo x > /tests/test_outputs.py\n",
    "redirect-in": "cat < /tests/test_outputs.py\n",
    "here-string": "grep a <<< 'abc'\n",
    "ansi-c-quote": "cat $'/\\x74ests/test_outputs.py'\n",
    "ansi-c-flag": "python3 $'-i'\n",
    "variable": "cat $HOME/f\n",
    "command-substitution": "echo $(python3)\n",
    "backtick": "echo `python3`\n",
    "history-expansion": "echo !!\n",
    "escaped-char": "echo a\\ b\n",
    "unbalanced-quote": "echo 'unterminated\n",
    "caret-unquoted": "echo ^x\n",
    # programs outside the allow-list or in a mode that waits for input
    "unlisted-program": "mysterytool\n",
    "sed-in-place": "sed -i 's/a/b/' f.py\n",
    "rm": "rm -rf /tests\n",
    "cp": "cp /tmp/x /tests/test_outputs.py\n",
    "mv": "mv a b\n",
    "chmod": "chmod +x f\n",
    "python-repl": "python3\n",
    "python-stdin-dash": "python3 -\n",
    "python-interactive-flag": "python3 -i x.py\n",
    "python-code-flag": "python3 -c 'print(1)'\n",
    "python-pdb": "python3 -m pdb script.py\n",
    "python-code-module": "python3 -m code\n",
    "bash-c": "bash -c python3\n",
    "bash": "bash\n",
    "eval": "eval ls\n",
    "source": "source env.sh\n",
    "alias": "alias ls=python3\n",
    "vim": "vim notes.txt\n",
    "cat-stdin": "cat\n",
    "cat-dash": "cat -\n",
    "cat-redirect-only": "cat 2>&1\n",
    "grep-pattern-only": "grep foo\n",
    "grep-expression-flag": "grep -e foo\n",
    "tail-follow": "tail -f /var/log/x\n",
    "head-stdin": "head -n 10\n",
    "find-exec": "find . -exec rm {} +\n",
    "find-delete": "find . -delete\n",
    "git-commit": "git commit -m x\n",
    "git-checkout": "git checkout -- tests/test_a.py\n",
    "docker": "docker run -it ubuntu\n",
    "ssh": "ssh host\n",
    "sudo": "sudo python3\n",
    "env-wrapper": "env FOO=1 python3\n",
    "pip-uninstall": "pip uninstall foo\n",
    "make-with-assignment": "FOO=1 make\n",
    "pipe-into-interpreter": "ls | python3\n",
    "pipe-into-shell": "ls | sh\n",
}


@pytest.mark.parametrize("keystrokes", MUST_BE_UNKNOWN.values(), ids=MUST_BE_UNKNOWN.keys())
def test_terminal_input_that_is_not_allow_listed_is_unknown(keystrokes: str) -> None:
    action = _terminal(keystrokes)
    assert action.kind == "unknown"
    assert action.name == "bash_command"
    assert action.arguments == {"keystrokes": keystrokes, "duration": 1}
    assert action.text == keystrokes
    assert action.terminal
    assert not action.shell_after
    assert terminal_shell_input(keystrokes) is None


def test_terminal_input_is_unknown_without_shell_context() -> None:
    assert _terminal("ls\n", shell_ready=False).kind == "unknown"


def test_empty_keystrokes_are_a_wait_that_keeps_the_context() -> None:
    assert _terminal("").kind == "noop"
    assert _terminal("", shell_ready=True).shell_after
    assert not _terminal("", shell_ready=False).shell_after


def test_long_single_line_is_linear() -> None:
    start = time.perf_counter()
    assert _terminal("echo " + "a" * 50_000 + "\n").kind == "shell"
    assert _terminal("echo " + "'" * 50_001 + "\n").kind == "unknown"
    assert time.perf_counter() - start < 10  # a quadratic scan of 50k characters would take minutes


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
        "root@host:/app# cat #",
        "root@host:/app# cat > f",
        "total 4\nprice: user@host:/x costs 5$",
        "root@host:/app# python3\nPython 3.12\n",
        "echo done #",
        # An earlier confirmation question must not hide later output.
        "Are you sure you want to mark the task as complete?\n" + PROMPT + "vim f\n\n-- INSERT --\n\n\n",
    ],
)
def test_terminal_shell_ready_is_false_unless_the_last_line_is_an_empty_prompt(observation: str) -> None:
    assert not terminal_shell_ready(observation)


def test_terminal_shell_ready_is_linear_on_blank_lines() -> None:
    start = time.perf_counter()
    assert not terminal_shell_ready("x" + "\n" * 200_000 + "y")
    assert terminal_shell_ready("root@host:/app#" + "\n" * 200_000)
    assert not terminal_shell_ready("\n" * 20_000 + "root@host:/app# ls #")
    assert time.perf_counter() - start < 10


# --------------------------------------------------------------------------- #
# Other transports
# --------------------------------------------------------------------------- #


def test_bundle_shell_accepts_the_same_allow_list_only() -> None:
    ok = normalize_tool_call("shell", {"command": "ls -la", "keystrokes": "ls -la"})
    assert (ok.kind, ok.text, ok.terminal) == ("shell", "ls -la", False)
    for arguments in [
        {"command": "ls", "keystrokes": "pwd"},
        {"command": "rm -rf /tests", "keystrokes": "rm -rf /tests"},
        {"command": "ls\x7f", "keystrokes": "ls\x7f"},
        {"command": "ls\x85", "keystrokes": "ls\x85"},
        {"command": "ls", "keystrokes": "ls", "extra": "x"},
        {"command": "", "keystrokes": ""},
    ]:
        assert normalize_tool_call("shell", arguments).kind == "unknown", arguments


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
        ("sleep", None),
    ],
)
def test_noop_names_with_executable_arguments_are_unknown(name: str, arguments: Any) -> None:
    assert normalize_tool_call(name, arguments).kind == "unknown"


@pytest.mark.parametrize(
    ("arguments", "kind", "name"),
    [
        ({"command": "view", "path": "a.py"}, "read", "str_replace_editor"),
        ({"command": "view", "path": "a.py", "view_range": [1, 5]}, "read", "str_replace_editor"),
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
        {"command": "view", "path": " a.py"},
        {"command": "view", "path": "."},
        {"command": "view", "path": "a/.."},
        {"command": "view", "path": "/app/../tests/helper.py"},
        {"command": "view", "path": "/tests/../app/x.py"},
        {"command": "view", "path": "//tests/helper.py"},
        {"command": "view", "path": "./a.py"},
        {"command": "view", "path": "a//b.py"},
        {"command": "view", "path": "a\nb"},
        {"command": "view", "path": 3},
        # Extra keys could hijack or suppress downstream detectors.
        {"command": "view", "path": "a.py", "edits": [{"path": "/tests/x.py", "new_str": "assert True"}]},
        {"command": "view", "path": "a.py", "patch": "*** Begin Patch"},
        {"command": "view", "path": "a.py", "file_path": "/tests/x.py"},
        {"command": "str_replace", "path": "a.py", "old_str": "a", "new_str": "b", "old_string": "c"},
        {"command": "create", "path": "a.py", "file_text": 3},
    ],
)
def test_text_editor_outside_the_documented_shapes_is_unknown(arguments: dict[str, Any]) -> None:
    assert normalize_tool_call("text_editor", arguments).kind == "unknown"


def test_glob_and_notebook_shapes() -> None:
    glob = normalize_tool_call("glob", {"path": "src", "pattern": "*.py", "i": "list"})
    assert (glob.kind, glob.text, glob.path) == ("read", "ls -- 'src/*.py'", "src/*.py")
    assert normalize_tool_call("glob", {"path": "."}).text == "ls -- ."
    assert normalize_tool_call("glob", {"pattern": "**/*"}).kind == "read"
    notebook = normalize_tool_call(
        "edit_notebook", {"file_path": "n.ipynb", "file_edit_payload": {"source": ["a = 1\n", "b = 2\n"]}}
    )
    assert (notebook.kind, notebook.text, notebook.path) == ("edit", "a = 1\nb = 2\n", "n.ipynb")
    stripped = normalize_tool_call(
        "edit_file", {"file_path": "n.ipynb", "file_edit_payload": {"source": ["# plan", "x = 1", ""]}}
    )
    assert stripped.text == "# plan\nx = 1\n"


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"glob": "/tests/test_outputs.py"},
        {"file_pattern": "*.py"},
        {"path": ""},
        {"path": ";;;"},
        {"path": "  "},
        {"path": "src;/tests"},
        {"path": None, "pattern": "*.py"},
        {"path": "src", "pattern": 3},
        {"path": "src", "pattern": "../../tests/*"},
        {"path": "src", "pattern": "a/../../tests/*"},
        {"path": "src", "pattern": "a b"},
        {"path": "src", "command": "rm -rf /"},
        {"path": "../tests"},
        {"path": "src", "pattern": "a\nb"},
    ],
)
def test_glob_with_unrecognised_arguments_is_unknown(arguments: dict[str, Any]) -> None:
    assert normalize_tool_call("glob", arguments).kind == "unknown"


@pytest.mark.parametrize(
    "arguments",
    [
        {"file_path": "n.ipynb", "file_edit_payload": {"source": ["x"]}, "command": "rm -rf /tests"},
        {"file_path": "n.ipynb", "file_edit_payload": {"source": ["x"], "patch": "*** Begin Patch"}},
        {"file_path": "n.ipynb", "filepath": "other.ipynb", "file_edit_payload": {"source": ["x"]}},
        {"file_path": ["a.ipynb"], "file_edit_payload": {"source": ["x"]}},
        {"file_path": "../tests/n.ipynb", "file_edit_payload": {"source": ["x"]}},
        {"file_path": "n.ipynb", "file_edit_payload": {"source": ["a\n", "b", "c\n"]}},
        {"file_path": "n.ipynb", "file_edit_payload": {"source": 3}},
        {"file_path": "n.ipynb", "file_edit_payload": "source"},
    ],
)
def test_notebook_edit_outside_the_known_shape_is_unknown(arguments: dict[str, Any]) -> None:
    assert normalize_tool_call("edit_notebook", arguments).kind == "unknown"


def test_unknown_calls_pass_through_unchanged() -> None:
    arguments = {"code": "print(1)"}
    action = normalize_tool_call("execute_code", arguments)
    assert (action.kind, action.name, dict(action.arguments)) == ("unknown", "execute_code", arguments)
    assert normalize_tool_call("anything", "raw string").kind == "unknown"


def test_call_name_and_arguments_handles_both_envelopes() -> None:
    assert call_name_and_arguments({"function_name": "a", "arguments": {"x": 1}}) == ("a", {"x": 1})
    assert call_name_and_arguments({"function": {"name": "b", "arguments": '{"y": 2}'}}) == ("b", {"y": 2})
    # An empty ``function`` mapping must not mask the ATIF name and arguments.
    assert call_name_and_arguments({"function": {}, "function_name": "c", "arguments": {"z": 3}}) == ("c", {"z": 3})
