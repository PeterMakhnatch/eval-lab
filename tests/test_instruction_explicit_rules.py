"""Behavioural tests for the instruction-explicit-rules@1 transform."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.instruction_explicit_rules import (
    INSTRUCTION_REL,
    MARKER,
    RULE_SECTION,
    TRANSFORM_ID,
    build_changes,
    build_instruction,
)
from evallab.task_variants import VariantInvalid

#: Real MiMo instruction shape (002402, header verbatim) so the test pins the
#: contract instead of restating it.
PARENT_INSTRUCTION = """Fix the following issue:

scipy.stats.qmc.LatinHypercube().random() returns samples outside [0, 1] when scramble=False.
"""


def test_rule_quotes_vals_tested_wording_exactly() -> None:
    assert (
        "future or unreachable Git commits, upstream patches, or newer package versions"
        in RULE_SECTION
    )
    assert "Do not use future or unreachable Git commits" in RULE_SECTION


def test_instruction_appends_rule_and_keeps_parent() -> None:
    new_text = build_instruction(PARENT_INSTRUCTION)
    assert new_text.startswith(PARENT_INSTRUCTION.rstrip("\n"))
    assert new_text.endswith(RULE_SECTION)
    assert "LatinHypercube" in new_text
    assert MARKER in new_text


def test_transform_is_idempotent() -> None:
    once = build_instruction(PARENT_INSTRUCTION)
    with pytest.raises(VariantInvalid, match="already carries"):
        build_instruction(once)


def test_only_instruction_changes(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / INSTRUCTION_REL).write_text(PARENT_INSTRUCTION, encoding="utf-8")
    (parent / "environment").mkdir()
    (parent / "environment" / "setup.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    changes, inputs = build_changes(parent)
    assert set(changes) == {INSTRUCTION_REL}
    assert inputs["default_chain"] is False
    assert "upstream patches" in inputs["rule"]


def test_refuses_missing_instruction(tmp_path: Path) -> None:
    with pytest.raises(VariantInvalid, match="no readable instruction.md"):
        build_changes(tmp_path)


def test_transform_id_shape() -> None:
    assert TRANSFORM_ID == "instruction-explicit-rules@1"
