"""Read the file sections of a MiMo hidden-test patch."""

from __future__ import annotations

import re


def patch_sections(patch_text: str) -> list[tuple[str, str]]:
    """``(path, section text)`` for every ``diff --git`` file section."""
    sections: list[tuple[str, str]] = []
    for match in re.finditer(
        r"(?m)^diff --git a/(\S+) b/\S+.*?(?=^diff --git |\Z)", patch_text, re.S
    ):
        sections.append((match.group(1), match.group(0)))
    return sections
