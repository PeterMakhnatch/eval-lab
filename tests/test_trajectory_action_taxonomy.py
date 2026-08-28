"""Tests for deterministic mechanical action taxonomy and command classification."""

from __future__ import annotations

from evallab.trajectory_action_taxonomy import (
    ActionDomain,
    ActionSubtype,
    classify_action,
    classify_bash_command,
)


def test_classify_file_system_modifications() -> None:
    """Verify classification of editing, writing, creating, and deleting files."""
    # sed -i
    c1 = classify_bash_command("sed -i 's/foo/bar/g' src/main.py")
    assert c1.domain == ActionDomain.FILE_SYSTEM
    assert c1.subtype == ActionSubtype.EDIT_FILE
    assert c1.is_edit is True
    assert c1.is_state_modifying is True
    assert c1.is_read_only is False

    # echo redirection
    c2 = classify_bash_command("echo 'print(1)' > test.py")
    assert c2.domain == ActionDomain.FILE_SYSTEM
    assert c2.subtype == ActionSubtype.EDIT_FILE
    assert c2.is_edit is True
    assert "test.py" in c2.target_paths

    # touch
    c3 = classify_bash_command("touch new_file.txt")
    assert c3.domain == ActionDomain.FILE_SYSTEM
    assert c3.subtype == ActionSubtype.CREATE_FILE
    assert c3.is_edit is True
    assert "new_file.txt" in c3.target_paths

    # rm
    c4 = classify_bash_command("rm -rf build/")
    assert c4.domain == ActionDomain.FILE_SYSTEM
    assert c4.subtype == ActionSubtype.DELETE_FILE
    assert c4.is_edit is True


def test_classify_file_system_reads_and_searches() -> None:
    """Verify classification of file reads, searches, and listings."""
    # cat
    c1 = classify_bash_command("cat src/main.py")
    assert c1.domain == ActionDomain.FILE_SYSTEM
    assert c1.subtype == ActionSubtype.READ_FILE
    assert c1.is_read_only is True
    assert c1.is_diagnostic is True
    assert c1.is_edit is False

    # grep
    c2 = classify_bash_command("grep -rn 'TODO' src/")
    assert c2.domain == ActionDomain.FILE_SYSTEM
    assert c2.subtype == ActionSubtype.SEARCH_CONTENT
    assert c2.is_read_only is True

    # find
    c3 = classify_bash_command("find . -name '*.py'")
    assert c3.domain == ActionDomain.FILE_SYSTEM
    assert c3.subtype == ActionSubtype.FIND_FILE
    assert c3.is_read_only is True

    # ls
    c4 = classify_bash_command("ls -la")
    assert c4.domain == ActionDomain.FILE_SYSTEM
    assert c4.subtype == ActionSubtype.LIST_DIR
    assert c4.is_read_only is True


def test_classify_execution_testing_compilation() -> None:
    """Verify classification of test execution, scripts, and builds."""
    # pytest
    c1 = classify_bash_command("pytest tests/test_core.py -vv")
    assert c1.domain == ActionDomain.CODE_EXECUTION
    assert c1.subtype == ActionSubtype.RUN_TEST
    assert c1.is_test_execution is True

    # python script
    c2 = classify_bash_command("python run_eval.py --depth 3")
    assert c2.domain == ActionDomain.CODE_EXECUTION
    assert c2.subtype == ActionSubtype.RUN_SCRIPT
    assert c2.is_test_execution is False

    # make
    c3 = classify_bash_command("make -j4 build")
    assert c3.domain == ActionDomain.CODE_EXECUTION
    assert c3.subtype == ActionSubtype.BUILD_COMPILE


def test_classify_environment_navigation_and_git() -> None:
    """Verify classification of env checks, navigation, and git operations."""
    # env inspection
    c1 = classify_bash_command("uname -a")
    assert c1.domain == ActionDomain.ENVIRONMENT_INSPECTION
    assert c1.subtype == ActionSubtype.INSPECT_ENV
    assert c1.is_read_only is True

    # cd navigation
    c2 = classify_bash_command("cd /var/log")
    assert c2.domain == ActionDomain.NAVIGATION
    assert c2.subtype == ActionSubtype.NAVIGATE
    assert c2.target_paths == ("/var/log",)

    # git status (read-only)
    c3 = classify_bash_command("git status --short")
    assert c3.domain == ActionDomain.VERSION_CONTROL
    assert c3.subtype == ActionSubtype.GIT_STATUS_DIFF
    assert c3.is_read_only is True

    # git commit (mutating)
    c4 = classify_bash_command("git commit -m 'initial fix'")
    assert c4.domain == ActionDomain.VERSION_CONTROL
    assert c4.subtype == ActionSubtype.GIT_MUTATE
    assert c4.is_state_modifying is True


def test_classify_direct_tool_calls() -> None:
    """Verify classification of structured tool calls by function name and args."""
    # write tool
    t1 = classify_action("write", {"path": "src/patch.py", "content": "x = 1"})
    assert t1.domain == ActionDomain.FILE_SYSTEM
    assert t1.subtype == ActionSubtype.EDIT_FILE
    assert t1.is_edit is True
    assert t1.target_paths == ("src/patch.py",)

    # read tool
    t2 = classify_action("read", {"path": "README.md"})
    assert t2.domain == ActionDomain.FILE_SYSTEM
    assert t2.subtype == ActionSubtype.READ_FILE
    assert t2.is_read_only is True

    # bash tool wrapping curl
    t3 = classify_action("bash", {"command": "curl -s http://example.com"})
    assert t3.domain == ActionDomain.NETWORK_COMMUNICATION
    assert t3.subtype == ActionSubtype.DOWNLOAD_HTTP

    # user interaction tool
    t4 = classify_action("ask", {"question": "Should I proceed?"})
    assert t4.domain == ActionDomain.INTERACTION
    assert t4.subtype == ActionSubtype.MESSAGE_USER
