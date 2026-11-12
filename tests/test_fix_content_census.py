"""Behavior tests for task QA's test-path classifier."""

from __future__ import annotations

from evallab.fix_content_census import is_test_path


def test_test_paths_never_carry_fix_content() -> None:
    assert is_test_path("tests/test_core.py")
    assert is_test_path("pkg/tests/helpers.py")
    assert is_test_path("web/src/app.spec.ts")
    assert is_test_path("conftest.py")
    assert is_test_path("mimo_test_command.sh")
    assert not is_test_path("pkg/core.py")
    assert not is_test_path("src/main.go")
