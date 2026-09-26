"""synthetic_hospital domain section (plugin ``synthetic_hospital``, version ``1``).

Upstream: ``sparkcpark/synthetic_hospital``, Harbor export
``scripts/harbor_export.py`` (commit ``77cc57e`` in
``/Users/petermakhnatch/Developer/.sources/synthetic_hospital``).

The Harbor verifier (``harbor/templates/test.sh``) writes
``verifier/reward.json`` as ``{"reward": float, "steps": int,
"submitted": 1.0 | 0.0}``; an episode the agent never submitted is closed
and scores 0. The section reports that file verbatim.

Redundant chart-section queries are a domain revisit metric defined from the
trajectory's tool calls. Chart reads are the EHR tools from
``epic_sim/app/routers/agent.py`` (``open_chart``, ``view_encounters``,
``view_encounter_detail``, ``view_section``, ``view_results``,
``search_chart``, ``view_problem_list``, ``view_medications``), seen either
as native tool calls or wrapped in the documented agent shell path
``sh-agent call <tool> '<json>'`` (``harbor_export.py`` instruction text).
Each read maps to a section key — the finest chart address the call names
(``section_id`` > ``encounter_id`` > ``(patient_id, query)`` >
``(patient_id, result_type)`` > ``(tool, patient_id)``). A repeat is a read
whose key already occurred; ``redundant_queries`` is reads minus distinct
keys. Submit calls (``submit_diagnosis``, ``submit_summary``,
``submit_pre_read``, ``submit_rankings``, or ``sh-agent submit``) are
reported as the trajectory-side submission witness.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from evallab.traj import resolve_trial_target

NAME = "synthetic_hospital"
VERSION = "1"

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
_WS_RE = re.compile(r"\s+")
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


def detect(trial_dir: Path, result: dict[str, Any]) -> bool:
    for marker in _task_markers(result, trial_dir):
        lowered = marker.lower()
        if lowered.startswith("sh-") or "synthetic-hospital" in lowered or "synthetic_hospital" in lowered:
            return True
    return _reward_shape(trial_dir)


def _unreadable(reason: str) -> dict[str, Any]:
    return {"plugin": NAME, "version": VERSION, "status": "unreadable", "reason": reason}


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


def build(trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    reward, problem = _read_reward(trial_dir)
    if reward is None:
        return _unreadable(str(problem))
    sources = [REWARD_PATH.as_posix()]
    calls, traj_problem = _iter_tool_calls(trial_dir)
    reads, submits = _chart_reads(calls)
    counts: dict[tuple[str, tuple[str, ...]], list[int]] = {}
    for index, (tool, key, _source) in enumerate(reads):
        counts.setdefault((tool, key), []).append(index)
    repeated: list[tuple[str, list[str], int]] = sorted(
        ((tool, list(key), len(steps)) for (tool, key), steps in counts.items() if len(steps) > 1),
        key=lambda row: (-row[2], row[0]),
    )[:5]
    most_repeated = [
        {"tool": tool, "key": key, "reads": reads, "redundant": reads - 1}
        for tool, key, reads in repeated
    ]
    distinct = len(counts)
    chart_section: dict[str, Any] = {
        "chart_reads": len(reads),
        "distinct_sections": distinct,
        "redundant_queries": len(reads) - distinct,
        "most_repeated": most_repeated,
    }
    if traj_problem is not None:
        chart_section["trajectory_note"] = traj_problem
    return {
        "plugin": NAME,
        "version": VERSION,
        "status": "ok",
        "sources": sources,
        "reward": reward["reward"],
        "verifier_steps": reward["steps"],
        "submitted": reward["submitted"],
        "submitted_via_trajectory": sorted(set(submits)),
        "chart_section_queries": chart_section,
    }


def render_markdown(section: dict[str, Any]) -> list[str]:
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
    lines.append(
        f"- Chart reads: {queries.get('chart_reads', 0)} across "
        f"{queries.get('distinct_sections', 0)} distinct sections; "
        f"{queries.get('redundant_queries', 0)} redundant re-reads."
    )
    for row in queries.get("most_repeated") or []:
        lines.append(f"  - {row['reads']}× `{row['tool']}` {row['key']} ({row['redundant']} redundant)")
    if queries.get("trajectory_note"):
        lines.append(f"- Trajectory note: {queries['trajectory_note']}.")
    return lines


class SyntheticHospitalPlugin:
    """DomainPlugin implementation (registered in ``domains.PLUGINS``)."""

    name = NAME
    version = VERSION

    def detect(self, trial_dir: Path, result: dict[str, Any]) -> bool:
        return detect(trial_dir, result)

    def build(self, trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
        return build(trial_dir, result)

    def render_markdown(self, section: dict[str, Any]) -> list[str]:
        return render_markdown(section)
