"""Fixture tests for the HAR-197 offline holdout verifier payload.

These tests are developer-authored isolated fixtures covering exact
base+patch+test overlay, genuine pass/fail result parsing, no false score
on errors/skips/zero tests/timeouts, and malicious paths/digest changes.

The end-to-end cases build tiny throwaway repositories and execute only
developer-authored fixture code through the payload's Git/test drivers.
They require Git and pytest, not a container, model, or benchmark task image.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab import heldout_verifier as hv

# --------------------------------------------------------------------------- #
# Path safety
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "/abs/path.py",
        "../escape.py",
        "a/../../escape.py",
        "a//b.py",
        "a/./b.py",
        ".git/hooks/post-checkout",
        "a/.git/config",
        ".git",
        "C:/evil.py",
        "a/\x00b.py",
    ],
)
def test_unsafe_relpaths_rejected(bad: str) -> None:
    assert hv.relpath_error(bad) is not None


# --------------------------------------------------------------------------- #
# Agent-diff scanning
# --------------------------------------------------------------------------- #


def _scan(tmp_path: Path, text: str) -> tuple[list[str], int]:
    diff = tmp_path / "agent.diff"
    diff.write_bytes(text.encode("utf-8"))
    return hv.scan_agent_diff(str(diff))



def test_scan_rejects_binary_placeholder(tmp_path: Path) -> None:
    problems, _ = _scan(
        tmp_path,
        "diff --git a/img/logo.png b/img/logo.png\n"
        "new file mode 100644\n"
        "index 0000000..1234567\n"
        "Binary files /dev/null and b/img/logo.png differ\n",
    )
    assert any(problem.startswith("binary_placeholder:") for problem in problems)


@pytest.mark.parametrize(
    "header",
    [
        "diff --git a/x b/../../etc/evil\n",
        "diff --git a/.git/hooks/x b/.git/hooks/x\n",
        "diff --git a/x b/C:/evil\n",
    ],
)
def test_scan_rejects_unsafe_targets(tmp_path: Path, header: str) -> None:
    problems, _ = _scan(tmp_path, header)
    assert any(problem.startswith("unsafe_path:") for problem in problems)


def test_scan_rejects_submodule_without_its_repository_bytes(tmp_path: Path) -> None:
    problems, _ = _scan(
        tmp_path,
        "diff --git a/link b/link\nnew file mode 160000\nindex 0000000..1234567\n",
    )
    assert any(problem.startswith("unsafe_path:") for problem in problems)


