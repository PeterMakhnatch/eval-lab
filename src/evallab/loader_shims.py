"""Harbor task loader shims for external task packs (TW, FACET).

These shims operate at our load boundary (staging mirror) and NEVER rewrite
underlying pack files, preserving archive integrity and hash pins.
"""

from __future__ import annotations

import re


def resolve_tw_memory_conflict(toml_text: str) -> tuple[str, str]:
    """Resolve conflicting legacy memory and modern memory_mb in TerminalWorld task.toml.

    Policy: modern `memory_mb` takes precedence over deprecated `memory` string.
    Drops legacy `memory = ...` line, leaving canonical `memory_mb = ...`.
    """
    has_legacy = bool(re.search(r"^\s*memory\s*=", toml_text, re.MULTILINE))
    has_modern = bool(re.search(r"^\s*memory_mb\s*=", toml_text, re.MULTILINE))
    if has_legacy and has_modern:
        cleaned = re.sub(r"^\s*memory\s*=.*$\n?", "", toml_text, flags=re.MULTILINE)
        return cleaned, "policy:memory_mb_precedence_dropped_legacy_memory"
    return toml_text, "unchanged"


def resolve_facet_task_name(toml_text: str, task_id: str, org: str = "facet") -> tuple[str, str]:
    """Resolve single-component task name in FACET task.toml for Harbor 0.21.0 org/name rule.

    Policy: rewrite [task].name to '{org}/{task_id}' for unique per-task namespacing.
    """
    qualified = f"{org}/{task_id}"
    cleaned = re.sub(
        r'(^\s*name\s*=\s*)"FACET-Terminal"',
        rf'\1"{qualified}"',
        toml_text,
        flags=re.MULTILINE,
    )
    return cleaned, f"policy:namespaced_as_{qualified}"
