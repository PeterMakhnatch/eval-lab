"""Native source-patch replay contracts."""

from __future__ import annotations

import difflib


def test_supported_line_witness_replays_complete_submission():
    from evallab.ref_subset import apply_reference

    base = "def total(values):\n    return 0\n"
    candidate = "def total(values):\n    return sum(values[:-1])\n"
    patch = "diff --git a/pkg/mod.py b/pkg/mod.py\n" + "".join(
        difflib.unified_diff(
            base.splitlines(keepends=True),
            candidate.splitlines(keepends=True),
            fromfile="a/pkg/mod.py",
            tofile="b/pkg/mod.py",
        )
    )
    applied = apply_reference(patch, {"pkg/mod.py": base.encode()})
    namespace = {}
    exec(applied["pkg/mod.py"], namespace)
    assert namespace["total"]([1, 2, 3]) == 3
