"""Defect-class playbooks for the open QA environment agent.

The playbooks are Markdown files shipped inside the ``evallab.qa`` package
(``playbooks/``) and read through :mod:`importlib.resources`, so an installed
wheel loads them the same way a source checkout does.

Classes are the task-side classes of defect taxonomy v1 (2026-10-20,
``research-context/bridge/2026-10-20-open-qa/taxonomy.yaml``). A class is
identified by its taxonomy ``key`` (``weak_check``); the taxonomy's ``TSnn``
row id appears only in each playbook heading.
"""

from __future__ import annotations

from importlib import resources

# Taxonomy v1 task-side order (TS01..TS11). Each key names ``playbooks/<key>.md``.
_DEFECT_CLASSES: tuple[str, ...] = (
    "weak_check",
    "grader_logic_bug",
    "answer_reachable",
    "grader_tamperable",
    "unstated_requirement",
    "instruction_grader_contradiction",
    "overstrict_oracle",
    "wrong_answer_key",
    "env_mismatch_or_flaky",
    "infeasible_as_written",
    "exploitable_llm_judge",
)
_COMMON = "common"


def defect_classes() -> tuple[str, ...]:
    """Canonical defect-class ids accepted as ``record-finding`` ``defect_class``."""
    return _DEFECT_CLASSES


def load_playbooks() -> str:
    """Shared rules followed by one playbook per defect class, as one system-context block."""
    root = resources.files(__package__).joinpath("playbooks")
    parts = [
        root.joinpath(f"{name}.md").read_text(encoding="utf-8").strip()
        for name in (_COMMON, *_DEFECT_CLASSES)
    ]
    return "\n\n".join(parts) + "\n"
