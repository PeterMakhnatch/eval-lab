"""Native source-patch replay contracts."""

from __future__ import annotations

import difflib

import pytest


def test_source_only_patch_drops_environment_artefacts_but_keeps_source():
    from evallab.ref_repair import source_only_patch

    patch = (
        "diff --git a/.venv/bin/activate b/.venv/bin/activate\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/.venv/bin/activate\n@@ -0,0 +1 @@\n+x\n"
        "diff --git a/pkg/mod.py b/pkg/mod.py\n--- a/pkg/mod.py\n+++ b/pkg/mod.py\n"
        "@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/talon.log b/talon.log\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/talon.log\n@@ -0,0 +1 @@\n+noise\n"
    )
    cleaned = source_only_patch(patch)
    assert "pkg/mod.py" in cleaned
    assert ".venv" not in cleaned
    assert "talon.log" not in cleaned


@pytest.mark.parametrize(
    "metadata",
    ["new file mode 120000", "old mode 100644\nnew mode 100755", "GIT binary patch"],
)
def test_byte_only_replay_refuses_unsupported_file_types(metadata):
    from evallab.ref_repair import _checked_patch_paths

    with pytest.raises(ValueError):
        _checked_patch_paths("diff --git a/pkg/item.py b/pkg/item.py\n" + metadata + "\n")


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
