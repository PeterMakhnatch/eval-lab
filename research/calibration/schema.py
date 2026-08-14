"""Local contracts for sealed keys and trajectory labels.

EVIDENCE must not edit BUILDER-owned `src/evallab/schemas.py`. Brief 09
will later grow a pydantic calibration record there; until then these dict
shapes are the consume contract documented in README.md.
"""

from __future__ import annotations

from typing import Any

from .rubrics import VERDICTS, all_criterion_names


def make_criterion_cell(verdict: str, rationale: str) -> dict[str, str]:
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be yes/no, got {verdict!r}")
    line = rationale.strip()
    if not line or "\n" in line:
        raise ValueError("rationale must be a single non-empty line")
    return {"verdict": verdict, "rationale": line}


def make_answer_key(
    *,
    family: str,
    doc_id: str,
    variant: str,
    source: str | None,
    verdicts: dict[str, dict[str, tuple[str, str]]],
) -> dict[str, Any]:
    """verdicts: dimension -> criterion -> (yes|no, rationale)."""
    criteria: dict[str, dict[str, dict[str, str]]] = {}
    for dimension, name in all_criterion_names(family):
        try:
            verdict, rationale = verdicts[dimension][name]
        except KeyError as exc:
            raise KeyError(f"{family}/{doc_id} missing {dimension}.{name}") from exc
        criteria.setdefault(dimension, {})[name] = make_criterion_cell(verdict, rationale)
    return {
        "schema_version": 1,
        "family": family,
        "document": f"{doc_id}.md",
        "document_id": doc_id,
        "variant": variant,
        "source": source,
        "verdict_convention": (
            "Judge pre-inversion yes/no. Negated EF/AQ items record whether "
            "the named flaw is present, not the post-inversion score."
        ),
        "criteria": criteria,
    }


def make_trajectory_label(
    *,
    trial_name: str,
    source: str,
    primary_category: str,
    summary: str,
    path: str,
    step: int | None,
    supports: str,
    agent: str | None = None,
    reward: float | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "trial_name": trial_name,
        "source": source,
        "primary_category": primary_category,
        "summary": summary,
        "evidence": [
            {
                "path": path,
                "step": step,
                "supports": supports,
            }
        ],
    }
    if agent is not None:
        payload["agent"] = agent
    if reward is not None:
        payload["reward"] = reward
    if notes is not None:
        payload["notes"] = notes
    return payload
