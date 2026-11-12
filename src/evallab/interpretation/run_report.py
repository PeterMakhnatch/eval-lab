"""Run report: one comprehensive, cited account of a Harbor trial or job.

``build_run_report(trial_dir)`` reads a trial's ``result.json`` and its ATIF
trajectory (following continuation segments and subagent references) and
returns a JSON-ready dictionary with schema ``evallab.run_report/v1``.
``render_run_report_markdown`` renders exactly that dictionary, so a person and
a model read the same facts. ``build_job_report`` rolls a job's trials up.

Rules the report keeps:

- Every total names its source. A missing input is ``None`` with a reason in
  ``data_quality``, never a silent zero.
- Tool status is tri-state. ``ok``/``error`` need a harness signal (exit code,
  error flag, envelope, code-mode script status, rejection message); strong
  output-text signals mark ``error`` with ``evidence: "output_text"``; Reef
  native tools (``execute``/``run_bash``/``write_file``/``read_file``)
  additionally fail on a leading ``timed out after 60s`` or ``refused: …``
  line (same ``output_text`` channel); anything else is ``unknown``.
- A *revisit* is an action whose exact signature (tool plus normalized
  command/arguments) already occurred in the run. An *exact revisit* also got
  the same normalized result back: the agent was at the same spot and nothing
  had changed. Polls (empty keystrokes, wait tools) are excluded.
- Excerpts pass through ``sanitize_excerpt`` (hidden verifier paths and
  secret-like tokens are redacted).
- Deterministic: no wall clock, network, or model calls.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from statistics import mean, median
from typing import Any, Protocol

from evallab.traj import (
    resolve_trial_target,
)

# Hosts whose content is upstream source or release artifacts.

_FETCH_TOOLS = frozenset({"curl", "wget", "aria2c"})


# Reading a fetched artifact back into the transcript.
_READ_PROGRAMS = frozenset(
    {"cat", "head", "tail", "less", "more", "sed", "awk", "unzip", "tar", "zcat", "gunzip", "bzcat"}
)

# A command that changes files (an edit), used only to order the copied-pass flag.
_EDIT_PROGRAMS = frozenset({"sed", "patch", "tee", "cp", "mv", "install"})
_EDIT_TOOLS = frozenset(
    {
        "edit",
        "write",
        "write_file",
        "str_replace",
        "str_replace_editor",
        "apply_patch",
        "create_file",
        "file_write",
        "notebook_edit",
    }
)

_HOST_RE = re.compile(r"https?://([^/\s\"'`]+)", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s\"'`|;>&)]+", re.IGNORECASE)
_PIN_RE = re.compile(r"^[A-Za-z0-9_.-]+(==|>=|<=|!=|~=|<|>)[^\s]+$")
_PKG_NAME_RE = re.compile(r"^([A-Za-z0-9_.-]+)")
_PATH_RE = re.compile(r"(?:^|[\s'\"=])(/[^\s'\"|;>&]+)")


_WRITE_REDIRECT_RE = re.compile(r"(?<![-=])>>?\s*([^\s|;&>'\"]+)")
_OPEN_WRITE_RE = re.compile(r"open\(\s*([A-Za-z_]\w*|['\"][^'\"]+['\"])\s*,\s*['\"][wa]")
_OPEN_DOT_WRITE_RE = re.compile(
    r"open\(\s*([A-Za-z_]\w*|['\"][^'\"]+['\"])[^)]*\)\s*\.\s*write\s*\("
)
_NAME_WRITE_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\.\s*(?:write_text|write_bytes|write)\s*\(")
_PATH_WRITE_RE = re.compile(
    r"Path\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\.\s*(?:write_text|write_bytes|open)\s*\("
)
_SED_FILE_RE = re.compile(r"\bsed\s+(?:-[^\s|;&]*\s+)*-[^\s|;&]*i[^\s|;&]*\s+(.*?)(?:\s*[|;&]|\s*$)")
_TEE_FILE_RE = re.compile(r"\btee\s+(?:-[^\s|;&]+\s+)*([^\s|;&]+)")
_COPY_DEST_RE = re.compile(r"\b(?:cp|mv|install)\b(.*?)(?:\s*[|;&]|\s*$)")


_HOSPITAL_NAME = "synthetic_hospital"
_HOSPITAL_VERSION = "1"

REWARD_PATH = Path("verifier/reward.json")

_CHART_TOOLS = frozenset(
    {
        "open_chart",
        "view_encounters",
        "view_encounter_detail",
        "view_section",
        "view_results",
        "search_chart",
        "view_problem_list",
        "view_medications",
    }
)
_SUBMIT_TOOLS = frozenset(
    {
        "submit_diagnosis",
        "submit_summary",
        "submit_pre_read",
        "submit_rankings",
    }
)
_SH_CALL_RE = re.compile(r"sh-agent\s+call\s+([A-Za-z_]\w*)(.*)$", re.DOTALL)
_SH_SUBMIT_RE = re.compile(r"sh-agent\s+submit\b")
_MAX_CHAIN_DOCS = 32


def _task_markers(result: dict[str, Any], trial_dir: Path) -> list[str]:
    """Identity strings that name the task: task dir basename, task_name, trial name."""
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


def _reward_shape(trial_dir: Path) -> bool:
    """True when verifier/reward.json has the hospital verifier's exact contract."""
    try:
        payload = json.loads((trial_dir / REWARD_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("reward"), (int, float))
        and isinstance(payload.get("steps"), int)
        and isinstance(payload.get("submitted"), (int, float))
    )


def _hospital_detect(trial_dir: Path, result: dict[str, Any]) -> bool:
    for marker in _task_markers(result, trial_dir):
        lowered = marker.lower()
        if lowered.startswith("sh-") or "synthetic-hospital" in lowered or "synthetic_hospital" in lowered:
            return True
    return _reward_shape(trial_dir)


def _hospital_unreadable(reason: str) -> dict[str, Any]:
    return {"plugin": _HOSPITAL_NAME, "version": _HOSPITAL_VERSION, "status": "unreadable", "reason": reason}


def _read_reward(trial_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    """The verifier file, or (None, reason) when it is missing or malformed."""
    path = trial_dir / REWARD_PATH
    if not path.is_file():
        return None, "verifier/reward.json missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"verifier/reward.json unreadable ({type(exc).__name__})"
    if not isinstance(payload, dict):
        return None, "verifier/reward.json is not an object"
    reward = payload.get("reward")
    steps = payload.get("steps")
    submitted = payload.get("submitted")
    if (
        isinstance(reward, bool)
        or not isinstance(reward, (int, float))
        or isinstance(steps, bool)
        or not isinstance(steps, int)
        or isinstance(submitted, bool)
        or not isinstance(submitted, (int, float))
    ):
        return None, "verifier/reward.json lacks {reward, steps, submitted}"
    return {"reward": float(reward), "steps": steps, "submitted": float(submitted)}, None


def _norm_query(value: Any) -> str:
    return _WS_RE.sub(" ", str(value).lower()).strip()


def _section_key(tool: str, args: Any) -> tuple[str, ...]:
    """Finest chart address a read call names (documented in the module docstring)."""
    params = args if isinstance(args, dict) else {}
    lowered = {str(key).lower(): value for key, value in params.items()}
    if tool == "view_section" and lowered.get("section_id") is not None:
        return (tool, str(lowered["section_id"]))
    if tool == "view_encounter_detail" and lowered.get("encounter_id") is not None:
        return (tool, str(lowered["encounter_id"]))
    if tool == "search_chart":
        return (tool, str(lowered.get("patient_id")), _norm_query(lowered.get("query", "")))
    if tool == "view_results":
        return (tool, str(lowered.get("patient_id")), str(lowered.get("result_type", "")).lower())
    return (tool, str(lowered.get("patient_id", "")))


def _split_sh_call(command: str) -> tuple[str | None, Any] | None:
    """Parse ``sh-agent call <tool> '<json>'``; None when not an sh-agent call."""
    match = _SH_CALL_RE.search(command)
    if not match:
        return None
    tool, rest = match.group(1), match.group(2).strip()
    if not rest:
        return (tool, {})
    if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in ("'", '"'):
        rest = rest[1:-1]
    try:
        return (tool, json.loads(rest))
    except ValueError:
        return (tool, {"_raw": rest})


def _iter_tool_calls(trial_dir: Path) -> tuple[list[tuple[str, Any]], str | None]:
    """(function_name, arguments) pairs across the trajectory chain.

    Returns ([], reason) when the trajectory is absent or unreadable; the
    caller reports zeros with that reason rather than failing.
    """
    try:
        _, traj_path, _ = resolve_trial_target(trial_dir, repo_root=trial_dir, explicit_runs_root=trial_dir)
    except Exception as exc:
        return [], f"trajectory target unresolvable ({type(exc).__name__})"
    if traj_path is None or not traj_path.is_file():
        return [], "trajectory file missing"
    calls: list[tuple[str, Any]] = []
    seen: set[Path] = set()
    current: Path | None = traj_path
    while current is not None and len(seen) < _MAX_CHAIN_DOCS:
        resolved = current.resolve()
        if resolved in seen:
            break
        seen.add(resolved)
        try:
            doc = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return calls, "a continuation segment is unreadable"
        if not isinstance(doc, dict):
            return calls, "a continuation segment is not an object"
        for step in doc.get("steps") or []:
            if not isinstance(step, dict):
                continue
            for call in step.get("tool_calls") or []:
                if isinstance(call, dict):
                    calls.append((str(call.get("function_name") or "unknown"), call.get("arguments")))
        ref = doc.get("continued_trajectory_ref")
        current = (resolved.parent / ref).resolve() if isinstance(ref, str) and ref else None
    return calls, None


def _chart_reads(calls: list[tuple[str, Any]]) -> tuple[list[tuple[str, tuple[str, ...], str]], list[str]]:
    """Map tool calls to (tool, section key, arg source); collect submit tools."""
    reads: list[tuple[str, tuple[str, ...], str]] = []
    submits: list[str] = []
    for name, args in calls:
        if name in _SUBMIT_TOOLS:
            submits.append(name)
            continue
        if name in _CHART_TOOLS:
            reads.append((name, _section_key(name, args), "native"))
            continue
        command: str | None = None
        if isinstance(args, dict):
            for key in ("command", "cmd", "input"):
                value = args.get(key)
                if isinstance(value, str) and "sh-agent" in value:
                    command = value
                    break
        elif isinstance(args, str) and "sh-agent" in args:
            command = args
        if command is None:
            continue
        if _SH_SUBMIT_RE.search(command):
            submits.append("submit (sh-agent)")
            continue
        parsed = _split_sh_call(command)
        if parsed is None:
            continue
        inner, inner_args = parsed
        if inner in _SUBMIT_TOOLS:
            submits.append(inner)
        elif inner in _CHART_TOOLS:
            reads.append((inner, _section_key(inner, inner_args), "sh-agent"))
    return reads, submits


def _hospital_build(trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    reward, problem = _read_reward(trial_dir)
    if reward is None:
        return _hospital_unreadable(str(problem))
    sources = [REWARD_PATH.as_posix()]
    calls, traj_problem = _iter_tool_calls(trial_dir)
    reads, submits = _chart_reads(calls)
    if traj_problem is not None:
        # The trajectory was not fully available: any count over it would be a
        # fabricated zero (or a silent undercount on a partial chain), so every
        # trajectory-derived measure stays None with the reason.
        chart_section: dict[str, Any] = {
            "chart_reads": None,
            "distinct_sections": None,
            "redundant_queries": None,
            "most_repeated": [],
            "trajectory_note": traj_problem,
        }
        witnessed: list[str] | None = None
    else:
        counts: dict[tuple[str, tuple[str, ...]], list[int]] = {}
        for index, (tool, key, _source) in enumerate(reads):
            counts.setdefault((tool, key), []).append(index)
        repeated: list[tuple[str, list[str], int]] = sorted(
            ((tool, list(key), len(steps)) for (tool, key), steps in counts.items() if len(steps) > 1),
            key=lambda row: (-row[2], row[0]),
        )[:5]
        distinct = len(counts)
        chart_section = {
            "chart_reads": len(reads),
            "distinct_sections": distinct,
            "redundant_queries": len(reads) - distinct,
            "most_repeated": [
                {"tool": tool, "key": key, "reads": reads, "redundant": reads - 1}
                for tool, key, reads in repeated
            ],
        }
        witnessed = sorted(set(submits))
    return {
        "plugin": _HOSPITAL_NAME,
        "version": _HOSPITAL_VERSION,
        "status": "ok",
        "sources": sources,
        "reward": reward["reward"],
        "verifier_steps": reward["steps"],
        "submitted": reward["submitted"],
        "submitted_via_trajectory": witnessed,
        "chart_section_queries": chart_section,
    }


def _hospital_markdown(section: dict[str, Any]) -> list[str]:
    if section.get("status") == "unreadable":
        return [f"- Hospital verifier output unreadable ({section.get('reason')})."]
    lines = [
        f"- Verifier: reward {section['reward']:g}, {section['verifier_steps']} steps, "
        f"submitted {section['submitted']:g}.",
    ]
    witnessed = section.get("submitted_via_trajectory") or []
    note = (section.get("chart_section_queries") or {}).get("trajectory_note")
    if witnessed:
        lines.append("- Submission seen in the trajectory via " + ", ".join(f"`{tool}`" for tool in witnessed) + ".")
    elif note:
        lines.append(f"- No trajectory ({note}): submission witnessed by the verifier alone.")
    else:
        lines.append("- No submit call in the trajectory (the verifier closed the episode).")
    queries = section.get("chart_section_queries") or {}
    if queries.get("trajectory_note"):
        lines.append(f"- Chart reads: unavailable ({queries['trajectory_note']}).")
        return lines
    lines.append(
        f"- Chart reads: {queries.get('chart_reads', 0)} across "
        f"{queries.get('distinct_sections', 0)} distinct sections; "
        f"{queries.get('redundant_queries', 0)} redundant re-reads."
    )
    for row in queries.get("most_repeated") or []:
        lines.append(f"  - {row['reads']}× `{row['tool']}` {row['key']} ({row['redundant']} redundant)")
    return lines


class SyntheticHospitalPlugin:
    """Hospital verifier evidence in the native run report."""

    name = _HOSPITAL_NAME
    version = _HOSPITAL_VERSION

    def detect(self, trial_dir: Path, result: dict[str, Any]) -> bool:
        return _hospital_detect(trial_dir, result)

    def build(self, trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
        return _hospital_build(trial_dir, result)

    def render_markdown(self, section: dict[str, Any]) -> list[str]:
        return _hospital_markdown(section)


_ATLAS_NAME = "atlas_finance"
_ATLAS_VERSION = "1"

INFO_PATH = Path("verifier/grader/info.json")

MIN_WEIGHT = 3.0
_TASK_SLUG_RE = re.compile(r"-env\d+__task_\d+", re.IGNORECASE)


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


def _atlas_detect(trial_dir: Path, result: dict[str, Any]) -> bool:
    for marker in _task_markers(result, trial_dir):
        lowered = marker.lower()
        if "atlas" in lowered or _TASK_SLUG_RE.search(marker):
            return True
    return _info_shape(trial_dir)


def _atlas_unreadable(reason: str) -> dict[str, Any]:
    return {"plugin": _ATLAS_NAME, "version": _ATLAS_VERSION, "status": "unreadable", "reason": reason}


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


def _atlas_build(trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    info, problem = _read_info(trial_dir)
    if info is None:
        return _atlas_unreadable(str(problem))
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
        "plugin": _ATLAS_NAME,
        "version": _ATLAS_VERSION,
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


def _atlas_markdown(section: dict[str, Any]) -> list[str]:
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
    """Finance verifier evidence in the native run report."""

    name = _ATLAS_NAME
    version = _ATLAS_VERSION

    def detect(self, trial_dir: Path, result: dict[str, Any]) -> bool:
        return _atlas_detect(trial_dir, result)

    def build(self, trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
        return _atlas_build(trial_dir, result)

    def render_markdown(self, section: dict[str, Any]) -> list[str]:
        return _atlas_markdown(section)


SIDECAR_DIR = "ceo_bench"
META_FILE = "meta.json"


def _read_sidecar(trial_dir: Path, name: str) -> dict[str, Any]:
    try:
        data = json.loads((trial_dir / SIDECAR_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{SIDECAR_DIR}/{name} unreadable: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{SIDECAR_DIR}/{name} is not an object")
    return data


def _money(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unknown"
    return f"${value:,.2f}"


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []

class CeoBenchPlugin:
    """Cash trajectory, bankruptcy, forecast error, no-op weeks, split spend."""

    name = "ceo_bench"
    version = "1"

    def detect(self, trial_dir: Path, result: dict[str, Any]) -> bool:
        try:
            meta = _read_sidecar(Path(trial_dir), META_FILE)
        except ValueError:
            return False
        return meta.get("plugin") == self.name

    def build(self, trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
        trial = Path(trial_dir)
        try:
            meta = _read_sidecar(trial, META_FILE)
            cash_doc = _read_sidecar(trial, "cash_daily.json")
            spend_doc = _read_sidecar(trial, "spend.json")
            forecasts_doc = _read_sidecar(trial, "forecasts.json")
            weeks_doc = _read_sidecar(trial, "weeks.json")
        except ValueError as exc:
            return {
                "plugin": self.name,
                "version": self.version,
                "status": "unreadable",
                "reason": str(exc),
            }
        daily = _as_list(cash_doc.get("daily"))
        rows = [r for r in daily if isinstance(r, dict)]
        cashes = [r["cash"] for r in rows if isinstance(r.get("cash"), (int, float))]
        weekly = [r for i, r in enumerate(rows) if i % 7 == 0]
        if rows and (not weekly or weekly[-1] is not rows[-1]):
            weekly = [*weekly, rows[-1]]
        forecast_rows = _as_list(forecasts_doc.get("rows"))
        scored = [
            r for r in forecast_rows
            if isinstance(r, dict) and isinstance(r.get("abs_error"), (int, float))
        ]
        abs_errors = [float(r["abs_error"]) for r in scored]
        worst = sorted(scored, key=lambda r: float(r["abs_error"]), reverse=True)[:5]
        weeks = _as_list(weeks_doc.get("weeks"))
        noop = _as_list(weeks_doc.get("noop_weeks"))
        bankruptcy = _as_dict(meta.get("bankruptcy"))
        sources = [f"{SIDECAR_DIR}/{name}" for name in
                   ("meta.json", "cash_daily.json", "spend.json", "forecasts.json", "weeks.json")]
        return {
            "plugin": self.name,
            "version": self.version,
            "sources": sources,
            "run_id": meta.get("run_id"),
            "model": meta.get("model"),
            "outcome": meta.get("outcome"),
            "bankruptcy": {
                "bankrupt": bool(bankruptcy.get("bankrupt")),
                "day": bankruptcy.get("day"),
            },
            "cash": {
                "source": cash_doc.get("source"),
                "initial": meta.get("initial_cash"),
                "final": meta.get("final_cash"),
                "min": min(cashes) if cashes else None,
                "min_day": next(
                    (r.get("day") for r in rows if r.get("cash") == min(cashes)), None
                ) if cashes else None,
                "days_tracked": len(rows),
                "weekly": [{"day": r.get("day"), "cash": r.get("cash")} for r in weekly],
            },
            "spend": {
                "agent": spend_doc.get("agent"),
                "simulator": spend_doc.get("simulator"),
                "separation_rule": spend_doc.get("separation_rule"),
            },
            "forecasts": {
                "status": forecasts_doc.get("status"),
                "reason": forecasts_doc.get("reason"),
                "n_forecasts": len([r for r in forecast_rows if isinstance(r, dict)]),
                "n_scored": len(scored),
                "mae": round(mean(abs_errors), 2) if abs_errors else None,
                "median_abs_error": round(median(abs_errors), 2) if abs_errors else None,
                "worst": [
                    {
                        "submit_day": r.get("submit_day"),
                        "horizon_days": r.get("horizon_days"),
                        "target_day": r.get("target_day"),
                        "predicted_value": r.get("predicted_value"),
                        "actual_cash": r.get("actual_cash"),
                        "abs_error": r.get("abs_error"),
                    }
                    for r in worst
                ],
            },
            "weeks": {
                "n_weeks": len(weeks),
                "n_noop": len(noop),
                "noop_weeks": noop,
                "rule": weeks_doc.get("rule"),
            },
        }

    def render_markdown(self, section: dict[str, Any]) -> list[str]:
        if section.get("status") == "unreadable":
            return [f"- Domain section unreadable ({section.get('reason')})."]
        lines: list[str] = []
        bankruptcy = section.get("bankruptcy") or {}
        cash = section.get("cash") or {}
        if bankruptcy.get("bankrupt"):
            lines.append(
                f"- Bankrupt on sim day {bankruptcy.get('day')} "
                f"(cash {_money(cash.get('min'))})."
            )
        else:
            lines.append(f"- No bankruptcy (outcome: {section.get('outcome')}).")
        lines.append(
            f"- Cash {_money(cash.get('initial'))} initial -> "
            f"{_money(cash.get('final'))} final "
            f"(min {_money(cash.get('min'))} on day {cash.get('min_day')}; "
            f"{cash.get('days_tracked')} days tracked, source {cash.get('source')})."
        )
        spend = section.get("spend") or {}
        agent = spend.get("agent") or {}
        simulator = spend.get("simulator") or {}
        if agent.get("cost_usd") is not None:
            agent_part = (
                f"Agent spend {_money(agent.get('cost_usd'))} "
                f"(source {agent.get('source')})"
            )
        else:
            agent_part = f"Agent spend unavailable: {agent.get('reason') or 'unknown'}"
        if simulator.get("cost_usd") is not None:
            simulator_part = f"simulator spend {_money(simulator.get('cost_usd'))}"
        else:
            simulator_part = (
                f"simulator spend unavailable: {simulator.get('reason') or 'unknown'}"
            )
        lines.append(
            f"- {agent_part}; {simulator_part} metered separately, "
            "never merged into agent cost."
        )
        forecasts = section.get("forecasts") or {}
        if forecasts.get("status") == "ok":
            lines.append(
                f"- Forecasts: {forecasts.get('n_scored')} of "
                f"{forecasts.get('n_forecasts')} scored, "
                f"MAE {_money(forecasts.get('mae'))}."
            )
        else:
            lines.append(f"- Forecasts unavailable ({forecasts.get('reason')}).")
        weeks = section.get("weeks") or {}
        lines.append(
            f"- No-op weeks: {weeks.get('n_noop')} of {weeks.get('n_weeks')} "
            f"(weeks {weeks.get('noop_weeks')})."
        )
        return lines


class DomainPlugin(Protocol):
    """A benchmark-specific section of the run report."""

    name: str  # stable id, e.g. "synthetic_hospital"
    version: str  # plugin section schema version, e.g. "1"

    def detect(self, trial_dir: Path, result: dict[str, Any]) -> bool:
        """True when this plugin owns the trial (task identity or verifier shape)."""
        ...

    def build(self, trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
        """Build the section dict. Must include ``plugin``, ``version`` and ``sources``.

        ``sources`` lists trial-relative paths actually read. On malformed
        input return ``{"plugin", "version", "status": "unreadable", "reason"}``.
        """
        ...

    def render_markdown(self, section: dict[str, Any]) -> list[str]:
        """Render the section body (the ``## Domain: <name>`` heading is added by the caller)."""
        ...


PLUGINS: tuple[DomainPlugin, ...] = (
    SyntheticHospitalPlugin(),
    AtlasFinancePlugin(),
    CeoBenchPlugin(),
)


DEFAULT_TIMELINE_LIMIT = 60
TARGET_CHARS = 160

# Argument keys that carry the command text, compared case-insensitively.
_SHELL_KEYS = frozenset({"command", "cmd", "keystrokes", "commandline"})
_DELEGATION_TOOLS = frozenset(
    {"task", "agent", "spawn_agent", "delegate", "subagent", "dispatch_agent", "new_task"}
)
_POLL_TOOLS = frozenset({"wait", "sleep", "poll", "wait_agent"})
_CODE_MODE_CALL_RE = re.compile(r"\btools\.([A-Za-z_]\w*)\s*\(")
_PATCH_FILE_RE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: ([^\n\\\"']+)")
_CODE_MODE_STATUS_RE = re.compile(r"^\s*Script (completed|failed)\b")
_WALL_TIME_RE = re.compile(r"Wall time:?\s*[\d.]+\s*(?:seconds|s)\b", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_EXIT_PREFIX_RE = re.compile(r"^\s*exit (-?\d+)\b")
# Error-text inference uses the taxonomy's shared pattern (D3): the report and
# the trajectory outline infer errors from command output through one regex.
# Reef native harness text-only failures (``reef/harness/runners/native/seed.py``:
# ``run_bash``/``execute`` return ``"timed out after 60s"`` on tool timeout and
# the native tools return ``"refused: <reason>"`` when they decline to run).
# Both arrive with no exit code, error flag, or envelope, and successful
# outputs never lead with these lines (normal results start with ``exit N``,
# ``wrote N …``, or file text), so a leading match names the error while
# deeper mentions stay ``unknown`` data. Scoped to Reef's native tool names so
# other tools' successful outputs that mention errors are unaffected.
_REEF_NATIVE_TOOLS = frozenset({"execute", "run_bash", "write_file", "read_file"})
_REEF_TIMEOUT_LEAD_RE = re.compile(r"^timed out after \d+s")
_REEF_REFUSED_LEAD_RE = re.compile(r"^refused:")
_COMPACTION_MIN_PROMPT = 8_000


# --------------------------------------------------------------------------- #
# Normalized views
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


_WRAPPER_PROGRAMS = frozenset({"sudo", "doas", "env", "nice", "nohup", "time", "timeout", "stdbuf"})


_PROGRAM_NAME_RE = re.compile(r"^[A-Za-z_][\w.+-]*$")


# --------------------------------------------------------------------------- #
# Calls, outputs, and status
# --------------------------------------------------------------------------- #


_PAYLOAD_OK = frozenset({"ok", "success", "succeeded", "completed"})
_PAYLOAD_ERROR = frozenset({"error", "failed", "failure"})


_DAYTONA_DIMENSIONS = ("cpu", "memory_gib", "disk_gib", "gpu", "concurrent_sandboxes")
_DEFAULT_DAYTONA_SAFETY_FRACTION = 0.8


def _daytona_block(snapshot: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    block = snapshot.get(key)
    return block if isinstance(block, Mapping) else {}


def _daytona_latest_snapshot(usage: Mapping[str, Any]) -> tuple[Mapping[str, Any], str]:
    """Latest capacity snapshot and which evidence field it came from.

    Prefers the disappearance-time current usage, then the last monitor
    sample, then the admission snapshot: the reading closest to the trial's
    end names the pressure the trial actually ran under.
    """
    disappearance = usage.get("disappearance")
    if isinstance(disappearance, Mapping):
        current = disappearance.get("current_usage")
        if isinstance(current, Mapping):
            return current, "disappearance current usage"
    last = usage.get("last_observation")
    if isinstance(last, Mapping):
        return last, "last monitor observation"
    admission = usage.get("admission")
    if isinstance(admission, Mapping):
        return admission, "admission snapshot"
    return {}, "unavailable (no snapshot recorded)"


def _daytona_number(value: Any) -> str:
    if isinstance(value, bool):
        return "unavailable"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return "unavailable"


def _daytona_pressure_note(snapshot: Any) -> str:
    if not isinstance(snapshot, Mapping):
        return "pressure unknown (no snapshot recorded)"
    pressure = snapshot.get("at_or_near_limit")
    if isinstance(pressure, list) and pressure:
        return "at/near limit: " + ", ".join(str(dim) for dim in pressure)
    return "no dimension at/near limit"


def render_daytona_usage_lines(usage: dict[str, Any] | None) -> list[str]:
    """Human-visible Daytona capacity section, or [] when no evidence exists.

    Missing evidence stays missing: dimensions without data render as
    unavailable, never zero. A disappearance beside recent near-limit
    pressure is capacity correlation, never a confirmed provider cause.
    """
    if not isinstance(usage, dict):
        return []
    snapshot, provenance = _daytona_latest_snapshot(usage)
    limits = _daytona_block(snapshot, "limits")
    used = _daytona_block(snapshot, "used")
    pending = _daytona_block(snapshot, "pending")
    requested = _daytona_block(snapshot, "requested")
    safety = snapshot.get("safety_fraction")
    if isinstance(safety, bool) or not isinstance(safety, (int, float)):
        safety = _DEFAULT_DAYTONA_SAFETY_FRACTION
    else:
        safety = float(safety)
    rows: list[list[str]] = []
    for dim in _DAYTONA_DIMENSIONS:
        quota = limits.get(dim)
        cap = (
            quota * safety
            if isinstance(quota, (int, float)) and not isinstance(quota, bool)
            else None
        )
        rows.append(
            [
                dim,
                _daytona_number(quota),
                _daytona_number(cap),
                _daytona_number(used.get(dim)),
                _daytona_number(pending.get(dim)),
                _daytona_number(requested.get(dim)),
            ]
        )
    inventory = snapshot.get("inventory")
    live = str(len(inventory)) if isinstance(inventory, list) else "unavailable"
    lines = [
        "",
        "## Environment capacity (Daytona)",
        f"- Source: {snapshot.get('source') or 'unknown source'} at "
        f"{snapshot.get('observed_at') or 'unknown time'} (snapshot: {provenance}; "
        f"region {snapshot.get('region') or 'unknown'}, "
        f"class {snapshot.get('sandbox_class') or 'unknown'}).",
    ]
    lines += _table(
        ["Dimension", "Raw quota", f"Safety cap ({safety:.0%})", "Used", "Pending", "Requested"],
        rows,
    )
    lines.append(f"- Live sandboxes in provider inventory: {live}.")
    monitor_error = usage.get("monitor_error")
    if isinstance(monitor_error, str) and monitor_error.strip():
        lines.append(
            f"- Monitor gap: {monitor_error}. Usage since then is unavailable, never zero."
        )
    disappearance = usage.get("disappearance")
    if isinstance(disappearance, dict):
        name = (
            disappearance.get("sandbox_name")
            or disappearance.get("sandbox_id")
            or "unknown sandbox"
        )
        when = disappearance.get("observed_at") or "unknown time"
        cause = disappearance.get("cause")
        strength = disappearance.get("evidence_strength")
        if cause == "usage_limit_pressure":
            lines.append(
                f"- Sandbox `{name}` disappeared at {when}: capacity pressure was observed "
                f"nearby (cause `{cause}`; evidence `{strength}`). Correlation with the "
                "disappearance, not a confirmed provider deletion cause."
            )
        else:
            lines.append(
                f"- Sandbox `{name}` disappeared at {when}: cause unknown "
                f"(cause `{cause}`; evidence `{strength}`). No capacity evidence either way."
            )
        basis = disappearance.get("pressure_source")
        lines.append(
            "- Pressure at disappearance: current "
            f"{_daytona_pressure_note(disappearance.get('current_usage'))}; last sample "
            f"{_daytona_pressure_note(disappearance.get('last_observation'))}"
            f" (pressure basis: {basis if basis in ('current_usage', 'last_observation') else 'unrecorded'})."
        )
        if cause != "usage_limit_pressure":
            if disappearance.get("error_type"):
                lines.append(f"- Disappearance probe error: `{disappearance['error_type']}`.")
            if disappearance.get("read_error"):
                lines.append(f"- Usage read error: {disappearance['read_error']}.")
    return lines


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


# A command cycle is a block of 2-3 distinct normalized commands repeated
# back-to-back at least 3 full times. Signatures reuse the identical-run
# normalization (tool plus whitespace-collapsed command). Only shell actions
# with substantive text count: completion claims such as
# ``mark_task_complete: {}`` interleave with real commands but are not
# commands cycling.
_MISSING_DEPENDENCY_RE = re.compile(r"ModuleNotFoundError|No module named [\w.]+")


# A shell command that only echoes the completion token (the model retrying
# "task_complete" in prose after the harness demanded the real call).
_COMPLETION_ECHO_RE = re.compile(r"""['"]?echo\s+['"]?task_complete['"]?['"]?""")


_COMPLETION_STOPS = frozenset({"task_complete", "prose_completion"})


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _cell(text: Any) -> str:
    return _fmt(text).replace("|", "\\|").replace("\n", " ")


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines.extend("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows)
    return lines


_UNSAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")
