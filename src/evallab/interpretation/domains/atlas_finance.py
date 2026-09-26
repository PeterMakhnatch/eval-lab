"""ATLAS-Finance domain section (plugin ``atlas_finance``, version ``1``).

Upstream: ``handshake-ai-research/ATLAS-Finance`` (commit ``6060bce`` in
``/Users/petermakhnatch/Developer/.sources/ATLAS-Finance``). Each trial's
verifier runs the gandalf-finance LLM judge
(``antoinepangas-hs/gandalf-finance``, commit ``142dda0`` in
``/Users/petermakhnatch/Developer/.sources/gandalf-finance``), which writes
the weighted rubric score to ``verifier/reward.json`` and the per-criterion
verdicts to ``verifier/grader/info.json`` (an ``EvaluationInfo`` payload:
``reward``, ``raw_score``, ``maximum_score``, ``criterion_results`` with
``criterion``/``weight``/``section``/``gate``/``met``/``skipped``/
``score_contribution``, and ``section_results`` with ``section``,
``section_gate_met``/``section_gates_met`` and
``failed_section_gate_indices``).

The section mirrors the benchmark's binary pass rule (``scripts/pass_at_k.py``:
every criterion with weight >= 3 met, no penalty — negative weight —
triggered, no section gate failed; unevaluated required/penalty criteria
fail the trial). Per-section verdicts group criteria by their ``section``
field in rubric order; ``failed_gate_indices`` indexes gate-flagged
*criteria*, not the section gate, so only ``failed_section_gate_indices``
and ``section_gate(s)_met`` fail a section. Downstream loss is attributed
to the first failed gate: required criteria unmet in later sections count
toward ``downstream_loss`` under that gate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

NAME = "atlas_finance"
VERSION = "1"

INFO_PATH = Path("verifier/grader/info.json")

MIN_WEIGHT = 3.0
_TASK_SLUG_RE = re.compile(r"-env\d+__task_\d+", re.IGNORECASE)


def _task_markers(result: dict[str, Any], trial_dir: Path) -> list[str]:
    markers: list[str] = [trial_dir.name]
    task_name = result.get("task_name")
    if isinstance(task_name, str) and task_name:
        markers.append(task_name)
    task_id = result.get("task_id")
    if isinstance(task_id, dict) and isinstance(task_id.get("path"), str):
        markers.append(Path(str(task_id["path"])).name)
    elif isinstance(task_id, str):
        markers.append(Path(task_id).name)
    return markers


def _info_shape(trial_dir: Path) -> bool:
    """True when verifier/grader/info.json has the gandalf EvaluationInfo shape."""
    try:
        payload = json.loads((trial_dir / INFO_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("criterion_results"), list)
        and isinstance(payload.get("section_results"), list)
    )


def detect(trial_dir: Path, result: dict[str, Any]) -> bool:
    for marker in _task_markers(result, trial_dir):
        lowered = marker.lower()
        if "atlas" in lowered or _TASK_SLUG_RE.search(marker):
            return True
    return _info_shape(trial_dir)


def _unreadable(reason: str) -> dict[str, Any]:
    return {"plugin": NAME, "version": VERSION, "status": "unreadable", "reason": reason}


def _read_info(trial_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = trial_dir / INFO_PATH
    if not path.is_file():
        return None, "verifier/grader/info.json missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"verifier/grader/info.json unreadable ({type(exc).__name__})"
    if not isinstance(payload, dict):
        return None, "verifier/grader/info.json is not an object"
    criteria = payload.get("criterion_results")
    sections = payload.get("section_results")
    if not isinstance(criteria, list) or not isinstance(sections, list):
        return None, "verifier/grader/info.json lacks criterion_results/section_results"
    return payload, None


def _weight(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _section_failed(entry: dict[str, Any]) -> bool:
    """Section-gate failure per pass_at_k: section-gate indices/flags only."""
    if entry.get("failed_section_gate_indices"):
        return True
    return entry.get("section_gate_met") is False or entry.get("section_gates_met") is False


def _criterion_problems(
    entry: dict[str, Any], *, label: str
) -> tuple[list[str], list[str], list[str]]:
    """(required_missed, penalties_triggered, unevaluated) reason strings for one criterion."""
    weight = _weight(entry.get("weight"))
    met = entry.get("met")
    text = str(entry.get("criterion", ""))[:90]
    context = f"{label}: {text}" if label else text
    if entry.get("skipped"):
        return [], [], []
    if met is None:
        if weight >= MIN_WEIGHT or weight < 0:
            return [], [], [f"unevaluated criterion (w={weight:g}): {context}"]
        return [], [], []
    if weight >= MIN_WEIGHT and not met:
        return [f"required criterion not met (w={weight:g}): {context}"], [], []
    if weight < 0 and met:
        return [], [f"penalty triggered (w={weight:g}): {context}"], []
    return [], [], []


def build(trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    info, problem = _read_info(trial_dir)
    if info is None:
        return _unreadable(str(problem))
    criteria = [c for c in info["criterion_results"] if isinstance(c, dict)]
    raw_sections = [s for s in info["section_results"] if isinstance(s, dict)]

    by_section: dict[str, list[dict[str, Any]]] = {}
    for entry in criteria:
        section = entry.get("section")
        by_section.setdefault(section if isinstance(section, str) else "unsectioned", []).append(entry)

    sections: list[dict[str, Any]] = []
    order = [s.get("section") for s in raw_sections if isinstance(s.get("section"), str)]
    for name in order + [n for n in by_section if n not in order]:
        gate_entry = next((s for s in raw_sections if s.get("section") == name), {})
        gate_failed = _section_failed(gate_entry)
        missed: list[str] = []
        penalties: list[str] = []
        unevaluated: list[str] = []
        required = sum(1 for c in by_section.get(name, []) if _weight(c.get("weight")) >= MIN_WEIGHT)
        for entry in by_section.get(name, []):
            miss, pen, uneval = _criterion_problems(entry, label=name)
            missed.extend(miss)
            penalties.extend(pen)
            unevaluated.extend(uneval)
        if gate_failed:
            missed.append(f"section gate failed: {name}")
        verdict = "fail" if (missed or penalties or unevaluated) else "pass"
        sections.append(
            {
                "section": name,
                "verdict": verdict,
                "gate_failed": gate_failed,
                "gated_out": gate_entry.get("gated_out") is True,
                "criteria": len(by_section.get(name, [])),
                "required": required,
                "required_missed": missed,
                "penalties_triggered": penalties,
                "unevaluated": unevaluated,
            }
        )

    failed_gates = [s["section"] for s in sections if s["gate_failed"]]
    first_gate = failed_gates[0] if failed_gates else None
    downstream_loss: dict[str, Any] = {"attributed_to": first_gate, "weight": 0.0, "criteria": []}
    if first_gate is not None:
        position = next(i for i, s in enumerate(sections) if s["section"] == first_gate)
        for entry in [c for s in sections[position + 1 :] for c in by_section.get(s["section"], [])]:
            weight = _weight(entry.get("weight"))
            if weight >= MIN_WEIGHT and entry.get("met") is False and not entry.get("skipped"):
                downstream_loss["weight"] += weight
                downstream_loss["criteria"].append(str(entry.get("criterion", ""))[:90])
        downstream_loss["weight"] = round(downstream_loss["weight"], 4)

    all_problems = [p for s in sections for p in s["required_missed"] + s["penalties_triggered"] + s["unevaluated"]]
    return {
        "plugin": NAME,
        "version": VERSION,
        "status": "ok",
        "sources": [INFO_PATH.as_posix()],
        "reward": info.get("reward") if isinstance(info.get("reward"), (int, float)) else None,
        "raw_score": info.get("raw_score") if isinstance(info.get("raw_score"), (int, float)) else None,
        "maximum_score": info.get("maximum_score") if isinstance(info.get("maximum_score"), (int, float)) else None,
        "passes": not all_problems,
        "reasons": all_problems,
        "sections": sections,
        "first_failed_gate": first_gate,
        "downstream_loss": downstream_loss,
    }


def render_markdown(section: dict[str, Any]) -> list[str]:
    if section.get("status") == "unreadable":
        return [f"- Finance grader output unreadable ({section.get('reason')})."]
    lines = [
        f"- Grader: {'PASS' if section['passes'] else 'FAIL'}"
        + (
            f" — reward {section['reward']:g}"
            if isinstance(section.get("reward"), (int, float))
            else ""
        )
        + ".",
    ]
    for entry in section.get("sections") or []:
        lines.append(
            f"  - {entry['section']}: **{entry['verdict']}** "
            f"({entry['criteria']} criteria, {entry['required']} required"
            + (", gate failed" if entry["gate_failed"] else "")
            + (", gated out" if entry["gated_out"] else "")
            + ")."
        )
        for problem in entry["required_missed"] + entry["penalties_triggered"] + entry["unevaluated"]:
            lines.append(f"    - {problem}")
    loss = section.get("downstream_loss") or {}
    if loss.get("attributed_to") is not None:
        lines.append(
            f"- Downstream loss attributed to the first failed gate "
            f"`{loss['attributed_to']}`: weight {loss['weight']:g} across "
            f"{len(loss['criteria'])} later required criteria."
        )
    return lines


class AtlasFinancePlugin:
    """DomainPlugin implementation (registered in ``domains.PLUGINS``)."""

    name = NAME
    version = VERSION

    def detect(self, trial_dir: Path, result: dict[str, Any]) -> bool:
        return detect(trial_dir, result)

    def build(self, trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
        return build(trial_dir, result)

    def render_markdown(self, section: dict[str, Any]) -> list[str]:
        return render_markdown(section)
