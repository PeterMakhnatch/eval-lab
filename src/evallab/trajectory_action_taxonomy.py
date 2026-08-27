"""Deterministic mechanical action taxonomy and classification for agent trajectories.

Defines canonical ActionDomain, ActionSubtype, and deterministic classification over tool calls,
Bash commands, and file operations with zero LLM dependence.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


class ActionDomain(str, Enum):
    """High-level functional domain of an agent action."""

    FILE_SYSTEM = "file_system"
    ENVIRONMENT_INSPECTION = "environment_inspection"
    CODE_EXECUTION = "code_execution"
    PROCESS_CONTROL = "process_control"
    NETWORK_COMMUNICATION = "network_communication"
    PACKAGE_MANAGEMENT = "package_management"
    VERSION_CONTROL = "version_control"
    NAVIGATION = "navigation"
    INTERACTION = "interaction"
    OTHER = "other"


class ActionSubtype(str, Enum):
    """Fine-grained operational subtype of an agent action."""

    READ_FILE = "read_file"
    EDIT_FILE = "edit_file"
    CREATE_FILE = "create_file"
    DELETE_FILE = "delete_file"
    FIND_FILE = "find_file"
    SEARCH_CONTENT = "search_content"
    LIST_DIR = "list_dir"
    INSPECT_ENV = "inspect_env"
    RUN_TEST = "run_test"
    RUN_SCRIPT = "run_script"
    BUILD_COMPILE = "build_compile"
    GIT_STATUS_DIFF = "git_status_diff"
    GIT_MUTATE = "git_mutate"
    INSTALL_PACKAGE = "install_package"
    DOWNLOAD_HTTP = "download_http"
    PROCESS_MANAGE = "process_manage"
    NAVIGATE = "navigate"
    MESSAGE_USER = "message_user"
    THINK = "think"
    OTHER = "other"


@dataclass(frozen=True)
class ActionClassification:
    """Deterministic classification result for an individual action/tool call."""

    domain: ActionDomain
    subtype: ActionSubtype
    is_read_only: bool
    is_state_modifying: bool
    is_diagnostic: bool
    is_test_execution: bool
    is_edit: bool
    primary_command: str | None = None
    target_paths: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()


# Regex patterns for bash classification
_REDIRECT_OUT_PATTERN = re.compile(r"(?:>>?|>\|)\s*([^\s;&|]+)")
_SED_INPLACE_PATTERN = re.compile(r"\bsed\s+.*-(?:i|e\s+.*-i)")

_EDIT_TOOL_NAMES = frozenset({"edit", "write", "patch", "str_replace_editor", "replace", "create_file", "save_file"})
_READ_TOOL_NAMES = frozenset({"read", "view", "cat", "read_file", "view_file", "open_file", "show"})
_SEARCH_TOOL_NAMES = frozenset({"grep", "rg", "search", "find", "glob", "file_search"})


def _extract_tokens_safe(command: str) -> list[str]:
    """Extract shell command tokens safely without raising on unmatched quotes."""
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return [t.strip() for t in command.split() if t.strip()]


def classify_bash_command(command_str: str) -> ActionClassification:
    """Deterministically classify a raw bash / shell command string."""
    cmd = command_str.strip()
    if not cmd:
        return ActionClassification(
            domain=ActionDomain.OTHER,
            subtype=ActionSubtype.OTHER,
            is_read_only=True,
            is_state_modifying=False,
            is_diagnostic=False,
            is_test_execution=False,
            is_edit=False,
        )

    tokens = _extract_tokens_safe(cmd)
    if not tokens:
        tokens = [cmd]

    first_tok = tokens[0].lower()
    # Strip env variable assignments prefix (e.g., FOO=1 bar)
    while "=" in first_tok and len(tokens) > 1 and not first_tok.startswith(("./", "/", "~")):
        tokens = tokens[1:]
        first_tok = tokens[0].lower()

    # Extract base command without path
    base_cmd = first_tok.rsplit("/", 1)[-1]

    # Check for redirection targets
    redirect_matches = _REDIRECT_OUT_PATTERN.findall(cmd)
    target_paths = tuple(redirect_matches)

    # 1. Navigation
    if base_cmd in {"cd", "pushd", "popd"}:
        return ActionClassification(
            domain=ActionDomain.NAVIGATION,
            subtype=ActionSubtype.NAVIGATE,
            is_read_only=True,
            is_state_modifying=False,
            is_diagnostic=False,
            is_test_execution=False,
            is_edit=False,
            primary_command=base_cmd,
            target_paths=tuple(tokens[1:2]) if len(tokens) > 1 else (),
        )

    # 2. File Editing and In-Place Modifications
    if (
        base_cmd in {"sed", "awk", "perl"}
        and any(arg.startswith("-i") or arg == "--in-place" for arg in tokens)
    ) or (
        base_cmd in {"tee", "sponge", "patch"}
    ) or (
        base_cmd in {"nano", "vim", "vi", "emacs", "ed"}
    ) or (
        redirect_matches and base_cmd in {"echo", "printf", "cat", "python", "python3"}
    ):
        return ActionClassification(
            domain=ActionDomain.FILE_SYSTEM,
            subtype=ActionSubtype.EDIT_FILE,
            is_read_only=False,
            is_state_modifying=True,
            is_diagnostic=False,
            is_test_execution=False,
            is_edit=True,
            primary_command=base_cmd,
            target_paths=target_paths or tuple(t for t in tokens[1:] if not t.startswith("-") and "." in t),
        )

    # 3. File Creation / Deletion
    if base_cmd in {"touch", "truncate"}:
        return ActionClassification(
            domain=ActionDomain.FILE_SYSTEM,
            subtype=ActionSubtype.CREATE_FILE,
            is_read_only=False,
            is_state_modifying=True,
            is_diagnostic=False,
            is_test_execution=False,
            is_edit=True,
            primary_command=base_cmd,
            target_paths=tuple(t for t in tokens[1:] if not t.startswith("-")),
        )

    if base_cmd in {"rm", "unlink", "rmdir", "shred"}:
        return ActionClassification(
            domain=ActionDomain.FILE_SYSTEM,
            subtype=ActionSubtype.DELETE_FILE,
            is_read_only=False,
            is_state_modifying=True,
            is_diagnostic=False,
            is_test_execution=False,
            is_edit=True,
            primary_command=base_cmd,
            target_paths=tuple(t for t in tokens[1:] if not t.startswith("-")),
        )

    # 4. Read File
    if base_cmd in {"cat", "head", "tail", "less", "more", "nl", "od", "hexdump", "xxd", "bat", "tac"}:
        return ActionClassification(
            domain=ActionDomain.FILE_SYSTEM,
            subtype=ActionSubtype.READ_FILE,
            is_read_only=True,
            is_state_modifying=False,
            is_diagnostic=True,
            is_test_execution=False,
            is_edit=False,
            primary_command=base_cmd,
            target_paths=tuple(t for t in tokens[1:] if not t.startswith("-")),
        )

    # 5. Search Content & Find File
    if base_cmd in {"grep", "rg", "ag", "ack", "egrep", "fgrep"}:
        return ActionClassification(
            domain=ActionDomain.FILE_SYSTEM,
            subtype=ActionSubtype.SEARCH_CONTENT,
            is_read_only=True,
            is_state_modifying=False,
            is_diagnostic=True,
            is_test_execution=False,
            is_edit=False,
            primary_command=base_cmd,
        )

    if base_cmd in {"find", "locate", "which", "whereis", "fd"}:
        return ActionClassification(
            domain=ActionDomain.FILE_SYSTEM,
            subtype=ActionSubtype.FIND_FILE,
            is_read_only=True,
            is_state_modifying=False,
            is_diagnostic=True,
            is_test_execution=False,
            is_edit=False,
            primary_command=base_cmd,
        )

    # 6. List Directory
    if base_cmd in {"ls", "dir", "tree", "vdir"}:
        return ActionClassification(
            domain=ActionDomain.FILE_SYSTEM,
            subtype=ActionSubtype.LIST_DIR,
            is_read_only=True,
            is_state_modifying=False,
            is_diagnostic=True,
            is_test_execution=False,
            is_edit=False,
            primary_command=base_cmd,
        )

    # 7. Environment Inspection
    if base_cmd in {"env", "printenv", "export", "set", "whoami", "id", "uname", "hostname", "pwd", "date", "uptime"}:
        return ActionClassification(
            domain=ActionDomain.ENVIRONMENT_INSPECTION,
            subtype=ActionSubtype.INSPECT_ENV,
            is_read_only=True,
            is_state_modifying=False,
            is_diagnostic=True,
            is_test_execution=False,
            is_edit=False,
            primary_command=base_cmd,
        )

    # 8. Version Control (Git)
    if base_cmd == "git":
        subcmd = tokens[1].lower() if len(tokens) > 1 else ""
        if subcmd in {"status", "diff", "log", "show", "branch", "tag", "remote"}:
            return ActionClassification(
                domain=ActionDomain.VERSION_CONTROL,
                subtype=ActionSubtype.GIT_STATUS_DIFF,
                is_read_only=True,
                is_state_modifying=False,
                is_diagnostic=True,
                is_test_execution=False,
                is_edit=False,
                primary_command=f"git {subcmd}",
            )
        return ActionClassification(
            domain=ActionDomain.VERSION_CONTROL,
            subtype=ActionSubtype.GIT_MUTATE,
            is_read_only=False,
            is_state_modifying=True,
            is_diagnostic=False,
            is_test_execution=False,
            is_edit=True,
            primary_command=f"git {subcmd}" if subcmd else "git",
        )

    # 9. Test Execution
    if (
        base_cmd in {"pytest", "pytest-3", "nosetests", "tox"}
        or (base_cmd in {"python", "python3"} and any("test" in tok for tok in tokens[1:]))
        or (base_cmd in {"cargo", "go", "npm", "yarn", "pnpm"} and len(tokens) > 1 and tokens[1] == "test")
    ):
        return ActionClassification(
            domain=ActionDomain.CODE_EXECUTION,
            subtype=ActionSubtype.RUN_TEST,
            is_read_only=False,
            is_state_modifying=True,
            is_diagnostic=True,
            is_test_execution=True,
            is_edit=False,
            primary_command=base_cmd,
        )

    # 10. Package Management
    if (
        base_cmd in {"pip", "pip3", "conda", "poetry", "uv"}
        or (base_cmd in {"npm", "yarn", "pnpm", "cargo", "apt", "apt-get", "dpkg", "yum", "brew"} and any(act in tokens[1:3] for act in {"install", "add", "remove", "update"}))
    ):
        return ActionClassification(
            domain=ActionDomain.PACKAGE_MANAGEMENT,
            subtype=ActionSubtype.INSTALL_PACKAGE,
            is_read_only=False,
            is_state_modifying=True,
            is_diagnostic=False,
            is_test_execution=False,
            is_edit=False,
            primary_command=base_cmd,
        )

    # 11. Network Communication
    if base_cmd in {"curl", "wget", "fetch", "http", "ping", "ssh", "scp", "rsync", "nc", "netcat"}:
        return ActionClassification(
            domain=ActionDomain.NETWORK_COMMUNICATION,
            subtype=ActionSubtype.DOWNLOAD_HTTP,
            is_read_only=False,
            is_state_modifying=True,
            is_diagnostic=False,
            is_test_execution=False,
            is_edit=False,
            primary_command=base_cmd,
        )

    # 12. Build / Compilation
    if base_cmd in {"make", "cmake", "ninja", "gcc", "g++", "clang", "clang++", "rustc", "tsc", "mvn", "gradle"}:
        return ActionClassification(
            domain=ActionDomain.CODE_EXECUTION,
            subtype=ActionSubtype.BUILD_COMPILE,
            is_read_only=False,
            is_state_modifying=True,
            is_diagnostic=False,
            is_test_execution=False,
            is_edit=False,
            primary_command=base_cmd,
        )

    # 13. Process Control
    if base_cmd in {"ps", "top", "htop", "pgrep", "pkill", "kill", "sleep", "wait", "jobs", "bg", "fg"}:
        return ActionClassification(
            domain=ActionDomain.PROCESS_CONTROL,
            subtype=ActionSubtype.PROCESS_MANAGEMENT if hasattr(ActionSubtype, "PROCESS_MANAGEMENT") else ActionSubtype.PROCESS_MANAGE,
            is_read_only=base_cmd in {"ps", "top", "htop", "pgrep", "jobs"},
            is_state_modifying=base_cmd in {"pkill", "kill", "sleep"},
            is_diagnostic=base_cmd in {"ps", "top", "htop", "pgrep"},
            is_test_execution=False,
            is_edit=False,
            primary_command=base_cmd,
        )

    # 14. General script execution
    if base_cmd in {"python", "python3", "node", "ruby", "perl", "bash", "sh", "zsh"} or base_cmd.startswith("./"):
        return ActionClassification(
            domain=ActionDomain.CODE_EXECUTION,
            subtype=ActionSubtype.RUN_SCRIPT,
            is_read_only=False,
            is_state_modifying=True,
            is_diagnostic=False,
            is_test_execution=False,
            is_edit=False,
            primary_command=base_cmd,
        )

    return ActionClassification(
        domain=ActionDomain.OTHER,
        subtype=ActionSubtype.OTHER,
        is_read_only=False,
        is_state_modifying=True,
        is_diagnostic=False,
        is_test_execution=False,
        is_edit=False,
        primary_command=base_cmd,
    )


def classify_action(
    function_name: str,
    arguments: Mapping[str, Any] | str | None = None,
    tool_command: str | None = None,
) -> ActionClassification:
    """Deterministically classify a tool call or action by function name and arguments."""
    fn_lower = function_name.strip().lower()

    # If it's a bash/shell/exec/terminal tool, dispatch to bash classifier
    if fn_lower in {"bash", "shell", "exec", "terminal", "execute", "command", "cmd", "run_command"}:
        cmd_str = ""
        if isinstance(arguments, dict):
            cmd_str = str(arguments.get("command") or arguments.get("cmd") or arguments.get("script") or "")
        elif isinstance(arguments, str):
            cmd_str = arguments
        elif tool_command:
            cmd_str = tool_command

        if cmd_str:
            return classify_bash_command(cmd_str)

        return ActionClassification(
            domain=ActionDomain.CODE_EXECUTION,
            subtype=ActionSubtype.RUN_SCRIPT,
            is_read_only=False,
            is_state_modifying=True,
            is_diagnostic=False,
            is_test_execution=False,
            is_edit=False,
            primary_command=fn_lower,
        )

    # Direct edit / write tools
    if fn_lower in _EDIT_TOOL_NAMES:
        target_path: str | None = None
        if isinstance(arguments, dict):
            target_path = str(arguments.get("path") or arguments.get("file_path") or arguments.get("target") or "")

        return ActionClassification(
            domain=ActionDomain.FILE_SYSTEM,
            subtype=ActionSubtype.EDIT_FILE,
            is_read_only=False,
            is_state_modifying=True,
            is_diagnostic=False,
            is_test_execution=False,
            is_edit=True,
            primary_command=fn_lower,
            target_paths=(target_path,) if target_path else (),
        )

    # Direct read / view tools
    if fn_lower in _READ_TOOL_NAMES:
        target_path = None
        if isinstance(arguments, dict):
            target_path = str(arguments.get("path") or arguments.get("file_path") or arguments.get("target") or "")

        return ActionClassification(
            domain=ActionDomain.FILE_SYSTEM,
            subtype=ActionSubtype.READ_FILE,
            is_read_only=True,
            is_state_modifying=False,
            is_diagnostic=True,
            is_test_execution=False,
            is_edit=False,
            primary_command=fn_lower,
            target_paths=(target_path,) if target_path else (),
        )

    # Direct search / grep tools
    if fn_lower in _SEARCH_TOOL_NAMES:
        return ActionClassification(
            domain=ActionDomain.FILE_SYSTEM,
            subtype=ActionSubtype.SEARCH_CONTENT if "grep" in fn_lower or "search" in fn_lower else ActionSubtype.FIND_FILE,
            is_read_only=True,
            is_state_modifying=False,
            is_diagnostic=True,
            is_test_execution=False,
            is_edit=False,
            primary_command=fn_lower,
        )

    # Interaction / messaging tools
    if fn_lower in {"ask", "message", "reply", "user_input", "finish", "complete", "submit"}:
        return ActionClassification(
            domain=ActionDomain.INTERACTION,
            subtype=ActionSubtype.MESSAGE_USER,
            is_read_only=True,
            is_state_modifying=False,
            is_diagnostic=False,
            is_test_execution=False,
            is_edit=False,
            primary_command=fn_lower,
        )

    # Fallback to bash classification if tool_command is provided
    if tool_command:
        return classify_bash_command(tool_command)

    return ActionClassification(
        domain=ActionDomain.OTHER,
        subtype=ActionSubtype.OTHER,
        is_read_only=False,
        is_state_modifying=True,
        is_diagnostic=False,
        is_test_execution=False,
        is_edit=False,
        primary_command=fn_lower,
    )
