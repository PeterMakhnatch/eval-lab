"""Repeatable blind trace-review workflow for multi-arm evaluation experiments.

Productizes the HAR-128 G6 workflow into three deterministic commands:
1. `evallab review prepare`: sanitize trials into blinded packs, sealed arm map,
   metrics, and rater batches with zero information leaks.
2. `evallab review freeze`: seal rater labels and metrics with SHA256 manifest and timestamp.
3. `evallab review join`: verify freeze integrity, unseal arm map, and generate
   comparison tables, scores, and comprehensive trace report.
"""

from __future__ import annotations

import contextlib
import difflib
import glob
import hashlib
import json
import math
import os
import random
import re
import shutil
import statistics
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.token_flow import _is_edit, _is_ephemeral_only, _touched_paths

__all__ = [
    "FIELDS",
    "NUMERIC_METRICS",
    "BINARY_METRICS",
    "WITHDRAWN_METRICS",
    "wilson",
    "quartiles",
    "first_edit_step",
    "metrics_for_trial",
    "mask_text_content",
    "extract_task_id",
    "leak_scan_file",
    "leak_scan_pack",
    "review_prepare",
    "review_freeze",
    "review_join",
]

#: RATER_GUIDE_v2 evaluated fields
FIELDS: tuple[str, ...] = (
    "stop_reason",
    "first_failure",
    "blame",
    "loop_present",
    "loop_kind",
    "loop_onset",
    "pass_copied",
)

#: Standard numeric comparison metrics
NUMERIC_METRICS: tuple[str, ...] = (
    "agent_steps",
    "tokens_total_proxy",
    "first_edit_step",
    "tokens_after_last_edit_input",
    "turns_after_first_prompt",
    "echo_task_complete_turns",
    "loop_suspicion_score",
    "unparseable",
)

#: Standard binary comparison metrics
BINARY_METRICS: tuple[str, ...] = (
    "passed",
    "loop_break_fired",
    "loop_break_stopped",
    "loop_onset",
    "handshake_confirmed",
    "never_edited",
    "hit_token_ceiling",
)

#: Metrics computed from detectors listed in QUIRKS.md as known-broken.
#: When non-empty, a warning banner is rendered at the top of TABLES.md.
#: Starts empty once EditDetectorFix lands.
WITHDRAWN_METRICS: frozenset[str] = frozenset()

STEP_TOLERANCE = 2
LOOP_TOLERANCE = 5
NOT_EXPRESSED = "not expressed"

NOT_EXPRESSED_BY: dict[str, set[str]] = {
    "evallab": {"blame", "loop_kind"},
    "scout": {"loop_kind", "pass_copied"},
    "docent_opus": set(),
}

DEFAULT_GUIDE_PATH = Path("research/explorations/trace-lab/review/RATER_GUIDE.md")

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
HEX12_RE = re.compile(r"\b[0-9a-f]{12}\b", re.IGNORECASE)
MODEL_SUFFIX_RE = re.compile(r":har\w+", re.IGNORECASE)


def wilson(k: int, n: int) -> tuple[float, float] | tuple[None, None]:
    """Calculate 95% Wilson score interval for k successes out of n trials."""
    if n == 0:
        return (None, None)
    z = 1.96
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def quartiles(values: Sequence[float]) -> str:
    """Format median and IQR [Q1–Q3] string, matching G6 tables convention."""
    if not values:
        return "—"
    if len(values) == 1:
        return f"{values[0]:g} (n=1)"
    q1, q2, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return f"{q2:g} [{q1:g}–{q3:g}] (n={len(values)})"


def step_of(ref: Any) -> int | None:
    """Extract integer step id from step reference string or dict."""
    if ref is None:
        return None
    if isinstance(ref, dict):
        ref = ref.get("ref")
    if isinstance(ref, int):
        return ref
    match = re.search(r"#(\d+)", str(ref)) or re.fullmatch(r"\s*(\d+)\s*", str(ref))
    return int(match.group(1)) if match else None


def first_edit_step(trial_dir: Path) -> int | None:
    """First agent step that edits the repo (importing token_flow helpers)."""
    traj_path = trial_dir / "agent" / "trajectory.json"
    if not traj_path.is_file():
        return None
    try:
        steps = json.loads(traj_path.read_text()).get("steps", [])
    except Exception:
        return None
    for step in (s for s in steps if s.get("source") in ("agent", "assistant")):
        is_edit, _ = _is_edit(step)
        if is_edit and not _is_ephemeral_only(_touched_paths(step)):
            step_id = step.get("step_id")
            return step_id if isinstance(step_id, int) else None
    return None


def metrics_for_trial(trial_dir: Path) -> dict[str, Any]:
    """Compute deterministic G6-style metrics for one trial directory."""
    result_path = trial_dir / "result.json"
    result: dict[str, Any] = {}
    if result_path.is_file():
        with contextlib.suppress(Exception):
            result = json.loads(result_path.read_text())

    processed_candidates = list(trial_dir.parent.glob("processed/trial-*.json"))
    p: dict[str, Any] = {}
    if processed_candidates:
        with contextlib.suppress(Exception):
            p = json.loads(processed_candidates[0].read_text())

    flow = p.get("token_flow") or {}
    hs = p.get("handshake") or {}
    after = flow.get("tokens_after_last_edit") or {}
    loop_break = ((result.get("agent_result") or {}).get("metadata") or {}).get("loop_break") or {}

    verdict = (p.get("counts") or {}).get("verdict")
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    if reward is None:
        reward = result.get("reward")
    if verdict is None:
        if reward is not None:
            verdict = "counted_pass" if float(reward) == 1.0 else "counted_fail"
        else:
            verdict = "excluded"

    agent_steps = p.get("agent_steps")
    if agent_steps is None:
        traj_path = trial_dir / "agent" / "trajectory.json"
        if traj_path.is_file():
            try:
                t_steps = json.loads(traj_path.read_text()).get("steps", [])
                agent_steps = sum(1 for s in t_steps if s.get("source") in ("agent", "assistant"))
            except Exception:
                agent_steps = 0
        else:
            agent_steps = 0

    stop_reason = p.get("stop_reason")
    if stop_reason is None:
        stop_reason = ((result.get("agent_result") or {}).get("metadata") or {}).get("stop_reason")

    tokens_proxy = (p.get("tokens_proxy") or {}).get("total_tokens")
    if tokens_proxy is None:
        tokens_proxy = (result.get("agent_result") or {}).get("n_input_tokens", 0) + (
            result.get("agent_result") or {}
        ).get("n_output_tokens", 0)

    return {
        "verdict": verdict,
        "reward": float(reward) if reward is not None else None,
        "stop_reason": stop_reason,
        "loop_break_fired": bool(loop_break.get("fired")),
        "loop_break_stopped": loop_break.get("stop_call") is not None,
        "handshake_present": bool(hs),
        "handshake_confirmed": hs.get("confirmed") if hs else None,
        "turns_after_first_prompt": hs.get("turns_after_first_prompt") if hs else None,
        "echo_task_complete_turns": hs.get("echo_task_complete_turns") if hs else None,
        "loop_onset": (flow.get("loop_onset") or {}).get("step_id") is not None,
        "loop_suspicion_score": (p.get("loop_suspicion") or {}).get("score", 0),
        "unparseable": (p.get("shape_counts") or {}).get("unparseable", 0),
        "rejection_causes": p.get("rejection_causes") or {},
        "first_edit_step": first_edit_step(trial_dir),
        "tokens_after_last_edit_input": after.get("input_tokens"),
        "tokens_total_proxy": tokens_proxy,
        "agent_steps": agent_steps,
    }


def mask_text_content(
    text: str,
    mask_texts: Sequence[str] = (),
    mask_regexes: Sequence[str] = (),
    *,
    mask_uuids: bool = False,
) -> str:
    """Mask text by replacing user mask strings, regexes, and optionally UUIDs."""
    for m in mask_texts:
        if m:
            text = text.replace(m, "")
    for r in mask_regexes:
        if r:
            text = re.sub(r, "", text)
    if mask_uuids:
        text = UUID_RE.sub("<UUID>", text)
        text = HEX12_RE.sub("<HEX12>", text)
    return text


def sanitize_value(
    val: Any,
    replacements: Sequence[tuple[str, str]],
    regex_replacements: Sequence[tuple[str, str]] = (),
) -> Any:
    """Recursively sanitize strings inside nested dicts/lists before JSON serialization."""
    if isinstance(val, str):
        for old, new in replacements:
            if old:
                val = val.replace(old, new)
        for pattern, new in regex_replacements:
            if pattern:
                val = re.sub(pattern, new, val)
        return val
    elif isinstance(val, dict):
        return {
            k: sanitize_value(v, replacements, regex_replacements)
            for k, v in val.items()
        }
    elif isinstance(val, list):
        return [sanitize_value(elem, replacements, regex_replacements) for elem in val]
    return val


def sanitize_trajectory_step(
    step: dict[str, Any],
    replacements: Sequence[tuple[str, str]],
    regex_replacements: Sequence[tuple[str, str]] = (),
) -> dict[str, Any]:
    """Retain only essential ATIF step keys and sanitize string contents."""
    allowed_keys = (
        "step_id",
        "source",
        "message",
        "reasoning_content",
        "tool_calls",
        "observation",
    )
    cleaned: dict[str, Any] = {}
    for key in allowed_keys:
        if key in step:
            cleaned[key] = sanitize_value(step[key], replacements, regex_replacements)
    return cleaned


def leak_scan_file(
    file_path: Path,
    rel_name: str,
    forbidden_tokens: Sequence[str],
    forbidden_regexes: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Scan a single file for forbidden leak tokens or regexes."""
    leaks: list[dict[str, Any]] = []
    scan_strings: set[str] = set()
    for tok in forbidden_tokens:
        if not tok or len(tok.strip()) == 0:
            continue
        scan_strings.add(tok)
        escaped = json.dumps(tok)[1:-1]
        if escaped != tok:
            scan_strings.add(escaped)

    compiled_regexes = [re.compile(r) for r in forbidden_regexes if r]

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return leaks

    for s in scan_strings:
        if s in content:
            idx = content.find(s)
            start = max(0, idx - 40)
            end = min(len(content), idx + len(s) + 40)
            snippet = content[start:end].replace("\n", "\\n")
            leaks.append(
                {
                    "file": rel_name,
                    "token": s,
                    "snippet": snippet,
                }
            )
    for creg in compiled_regexes:
        match = creg.search(content)
        if match:
            snippet = match.group(0)[:80].replace("\n", "\\n")
            leaks.append(
                {
                    "file": rel_name,
                    "token": creg.pattern,
                    "snippet": snippet,
                }
            )
    return leaks


def leak_scan_pack(
    pack_dir: Path,
    forbidden_tokens: Sequence[str],
    forbidden_regexes: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Scan all files in a pack directory for forbidden leak tokens or regexes."""
    leaks: list[dict[str, Any]] = []
    for root, _, files in os.walk(pack_dir):
        for f in sorted(files):
            file_path = Path(root) / f
            rel_path = file_path.relative_to(pack_dir).as_posix()
            leaks.extend(
                leak_scan_file(
                    file_path,
                    f"{pack_dir.name}/{rel_path}",
                    forbidden_tokens,
                    forbidden_regexes,
                )
            )
    return leaks


def extract_task_id(res_file: Path, trial_name: str, job_name: str) -> str:
    """Derive deterministic task id from task_name or string fields. Never str() a dict."""
    if res_file.is_file():
        with contextlib.suppress(Exception):
            r_payload = json.loads(res_file.read_text(encoding="utf-8"))
            task_name = r_payload.get("task_name")
            if isinstance(task_name, str) and task_name.strip():
                match_6d = re.search(r"(\d{6})", task_name)
                if match_6d:
                    return match_6d.group(1)
                task_base = task_name.split("/")[-1].strip()
                if task_base:
                    cleaned = re.sub(r"^(?:format-code-)?task-", "", task_base)
                    return cleaned or task_base

            task_id_field = r_payload.get("task_id")
            if isinstance(task_id_field, str) and task_id_field.strip():
                match_6d = re.search(r"(\d{6})", task_id_field)
                if match_6d:
                    return match_6d.group(1)
                return task_id_field.strip()

    t_match = re.search(r"(\d{6})", trial_name) or re.search(r"(\d{6})", job_name)
    if t_match:
        return t_match.group(1)

    raise ValueError(
        f"Could not derive task id for trial '{trial_name}' (job '{job_name}'). "
        "Expected task_name or string task_id in result.json, or 6-digit id in trial/job name."
    )


def rater_view(row: dict[str, Any]) -> dict[str, Any]:
    span = row.get("loop_span")
    return {
        "stop_reason": row.get("stop_reason"),
        "first_failure": step_of(row.get("first_failure")),
        "blame": row.get("blame"),
        "loop_present": row.get("loop_kind") != "none",
        "loop_kind": row.get("loop_kind"),
        "loop_onset": step_of(span[0]) if span else None,
        "pass_copied": row.get("pass_copied"),
    }


def tool_view(tool: str, row: dict[str, Any]) -> dict[str, Any]:
    if tool == "docent_opus" and row.get("raw_error"):
        return dict.fromkeys(FIELDS, NOT_EXPRESSED)
    span = row.get("loop_span")
    step = row.get("first_failure_step")
    if tool == "evallab":
        step = row.get("raw_first_failure_step")
    view = {
        "stop_reason": row.get("stop_reason") or NOT_EXPRESSED,
        "first_failure": step if isinstance(step, int) else step_of(row.get("first_failure_ref")),
        "blame": row.get("blame") or NOT_EXPRESSED,
        "loop_present": bool(span),
        "loop_kind": row.get("loop_kind") or NOT_EXPRESSED,
        "loop_onset": step_of(span[0]) if span else None,
        "pass_copied": row.get("pass_copied"),
    }
    for field in NOT_EXPRESSED_BY.get(tool, set()):
        view[field] = NOT_EXPRESSED
    return view


def match_field(field: str, a: Any, b: Any, passed: bool) -> bool | None:
    if field == "pass_copied" and not passed:
        return None
    if field == "first_failure":
        if a is None or b is None:
            return a is None and b is None
        return abs(a - b) <= STEP_TOLERANCE
    if field == "loop_onset":
        return None if a is None or b is None else abs(a - b) <= LOOP_TOLERANCE
    return a == b


def compare_views(
    left: dict[str, dict[str, Any]],
    right: dict[str, dict[str, Any]],
    passed: dict[str, bool],
    only: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in FIELDS:
        hits = total = unexpressed = 0
        rows = []
        for trial in left:
            if only is not None and (trial, field) not in only:
                continue
            a, b = left[trial][field], right[trial][field]
            if b == NOT_EXPRESSED:
                unexpressed += 1
                continue
            ok = match_field(field, a, b, passed[trial])
            if ok is None:
                continue
            total += 1
            hits += int(ok)
            rows.append({"trial": trial, "rater": a, "other": b, "match": ok})
        lo, hi = wilson(hits, total)
        out[field] = {
            "agree": hits,
            "n": total,
            "ci95": [lo, hi],
            "not_expressed": unexpressed,
            "rows": rows,
        }
    return out


def review_prepare(
    job_dirs: Sequence[Path] | None = None,
    jobs_glob: str | None = None,
    arm_regex: str = r"-(?P<arm>stock|tuned|gepa)(-r\d+)?__",
    mask_text_files: Sequence[Path] = (),
    mask_regexes: Sequence[str] = (),
    out_dir: Path = Path("review"),
    raters: int = 2,
    per_agent: int = 6,
    seed: str | None = None,
    id_prefix: str = "b-",
) -> int:
    """Execute `evallab review prepare`."""
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Discover jobs
    discovered_jobs: list[Path] = []
    if job_dirs:
        for jd in job_dirs:
            p = jd.resolve()
            if p.is_dir():
                discovered_jobs.append(p)
    if jobs_glob:
        for g_path in sorted(glob.glob(jobs_glob)):
            p = Path(g_path).resolve()
            if p.is_dir() and p not in discovered_jobs:
                discovered_jobs.append(p)

    if not discovered_jobs:
        raise ValueError("No job directories found. Provide --job-dir or --jobs-glob.")

    # 2. Discover trials
    compiled_arm_re = re.compile(arm_regex)
    raw_trials: list[dict[str, Any]] = []

    for jd in sorted(discovered_jobs):
        # A trial directory is a subfolder containing agent/trajectory.json or result.json
        candidate_subdirs = [d for d in jd.iterdir() if d.is_dir()]
        for sub in candidate_subdirs:
            traj_file = sub / "agent" / "trajectory.json"
            res_file = sub / "result.json"
            if not traj_file.is_file() and not res_file.is_file():
                continue

            trial_name = sub.name
            arm_match = compiled_arm_re.search(trial_name) or compiled_arm_re.search(jd.name)
            if not arm_match:
                raise ValueError(
                    f"Could not extract arm from trial {trial_name} or job {jd.name} using regex {arm_regex}"
                )
            arm = arm_match.group("arm")

            task = extract_task_id(res_file, trial_name, jd.name)
            raw_trials.append(
                {
                    "trial_dir": sub,
                    "job_dir": jd,
                    "trial_name": trial_name,
                    "arm": arm,
                    "task": str(task),
                }
            )
    if not raw_trials:
        raise ValueError(f"No trial directories discovered under {len(discovered_jobs)} jobs.")

    # 3. Read mask texts
    mask_texts: list[str] = []
    for mtf in mask_text_files:
        p = mtf.resolve()
        if p.is_file():
            content = p.read_text(encoding="utf-8")
            if content:
                mask_texts.append(content)
        else:
            raise FileNotFoundError(f"Mask text file not found: {p}")

    # 4. Prompt identity check across arms per task
    task_arm_prompts: dict[str, dict[str, str]] = {}
    for item in raw_trials:
        sub = item["trial_dir"]
        traj_file = sub / "agent" / "trajectory.json"
        if traj_file.is_file():
            with contextlib.suppress(Exception):
                t_data = json.loads(traj_file.read_text())
                steps = t_data.get("steps", [])
                if steps and "message" in steps[0]:
                    task_arm_prompts.setdefault(item["task"], {})[item["arm"]] = steps[0]["message"]

    for task_id, arm_prompts in sorted(task_arm_prompts.items()):
        masked_prompts = {
            arm: mask_text_content(
                p, mask_texts=mask_texts, mask_regexes=mask_regexes, mask_uuids=True
            )
            for arm, p in arm_prompts.items()
        }
        arms_list = list(masked_prompts.keys())
        if len(arms_list) > 1:
            ref_arm = arms_list[0]
            ref_prompt = masked_prompts[ref_arm]
            for other_arm in arms_list[1:]:
                other_prompt = masked_prompts[other_arm]
                if other_prompt != ref_prompt:
                    diff = list(
                        difflib.unified_diff(
                            ref_prompt.splitlines(keepends=True),
                            other_prompt.splitlines(keepends=True),
                            fromfile=f"task_{task_id}_{ref_arm}",
                            tofile=f"task_{task_id}_{other_arm}",
                        )
                    )
                    diff_span = "".join(diff[:40])
                    print(
                        f"Prompt difference detected on task {task_id} between {ref_arm} and {other_arm}:\n{diff_span}",
                        file=sys.stderr,
                    )
                    raise ValueError(
                        f"First prompts of task {task_id} differ across arms ({ref_arm} vs {other_arm}). Differing span:\n{diff_span}"
                    )

    # 5. Deterministic shuffling and ID assignment
    actual_seed = seed if seed is not None else os.urandom(16).hex()
    rng = random.Random(actual_seed)
    shuffled_trials = list(raw_trials)
    rng.shuffle(shuffled_trials)

    pad_width = max(2, len(str(len(shuffled_trials))))
    packs_dir = out_dir / "packs"
    packs_dir.mkdir(parents=True, exist_ok=True)

    sealed_arm_map: dict[str, dict[str, Any]] = {}

    for idx, item in enumerate(shuffled_trials, start=1):
        oid = f"{id_prefix}{idx:0{pad_width}d}"
        td = item["trial_dir"]
        jd = item["job_dir"]
        traj_file = td / "agent" / "trajectory.json"
        traj_sha = (
            hashlib.sha256(traj_file.read_bytes()).hexdigest()
            if traj_file.is_file()
            else ""
        )

        sealed_arm_map[oid] = {
            "arm": item["arm"],
            "trial": item["trial_name"],
            "task": item["task"],
            "job_path": str(jd.resolve()),
            "trajectory_sha256": traj_sha,
            "seed": str(actual_seed),
        }

        # 6. Build pack
        pack_path = packs_dir / oid
        pack_path.mkdir(parents=True, exist_ok=True)

        replacements: list[tuple[str, str]] = [
            (item["trial_name"], "TRIAL"),
            (jd.name, "JOB"),
        ]
        # Also strip trial stem if name has double underscore
        if "__" in item["trial_name"]:
            replacements.append((item["trial_name"].split("__")[0], "TRIAL"))
        for m in mask_texts:
            replacements.append((m, ""))
            if m.strip() and m.strip() != m:
                replacements.append((m.strip(), ""))

        regex_replacements: list[tuple[str, str]] = [
            (MODEL_SUFFIX_RE.pattern, ""),
        ]
        for r in mask_regexes:
            regex_replacements.append((r, ""))

        # 6a. trajectory.json
        if traj_file.is_file():
            try:
                traj_json = json.loads(traj_file.read_text(encoding="utf-8"))
                steps = traj_json.get("steps", [])
                cleaned_steps = [
                    sanitize_trajectory_step(s, replacements, regex_replacements)
                    for s in steps
                ]
                (pack_path / "trajectory.json").write_text(
                    json.dumps({"steps": cleaned_steps}, indent=1, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            except Exception as exc:
                print(f"warning: error sanitizing trajectory for {oid}: {exc}", file=sys.stderr)

        # 6b. result_summary.json
        res_file = td / "result.json"
        res_summary: dict[str, Any] = {
            "task": f"format-code-task-{item['task']}",
            "reward": 0.0,
            "exception_type": None,
            "exception_message": None,
            "n_episodes": 0,
            "n_input_tokens": 0,
            "n_output_tokens": 0,
            "stop_reason": None,
            "prose_completions": 0,
            "loop_break": {
                "fired": False,
                "nudge_call": None,
                "detector": None,
                "stop_call": None,
                "model_next": None,
            },
            "summarization_count": 0,
        }
        if res_file.is_file():
            try:
                r_obj = json.loads(res_file.read_text(encoding="utf-8"))
                if r_obj.get("task_name"):
                    res_summary["task"] = sanitize_value(
                        r_obj["task_name"], replacements, regex_replacements
                    )
                v_res = r_obj.get("verifier_result") or {}
                rewards = v_res.get("rewards") or {}
                if "reward" in rewards:
                    res_summary["reward"] = float(rewards["reward"])
                elif "reward" in r_obj:
                    res_summary["reward"] = float(r_obj["reward"])

                exc_info = r_obj.get("exception_info") or {}
                res_summary["exception_type"] = exc_info.get("exception_type")
                if exc_info.get("exception_message"):
                    res_summary["exception_message"] = sanitize_value(
                        exc_info["exception_message"], replacements, regex_replacements
                    )

                ag_res = r_obj.get("agent_result") or {}
                res_summary["n_input_tokens"] = ag_res.get("n_input_tokens", 0)
                res_summary["n_output_tokens"] = ag_res.get("n_output_tokens", 0)

                meta = ag_res.get("metadata") or {}
                res_summary["n_episodes"] = meta.get("n_episodes", 0)
                res_summary["stop_reason"] = meta.get("stop_reason") or r_obj.get("stop_reason")
                res_summary["prose_completions"] = meta.get("prose_completions", 0)
                res_summary["summarization_count"] = meta.get("summarization_count", 0)
                lb = meta.get("loop_break")
                if isinstance(lb, dict):
                    res_summary["loop_break"] = {
                        "fired": bool(lb.get("fired")),
                        "nudge_call": lb.get("nudge_call"),
                        "detector": lb.get("detector"),
                        "stop_call": lb.get("stop_call"),
                        "model_next": lb.get("model_next"),
                    }
            except Exception as exc:
                print(f"warning: error building result summary for {oid}: {exc}", file=sys.stderr)

        (pack_path / "result_summary.json").write_text(
            json.dumps(res_summary, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        # 6c. exception.txt
        exc_file = td / "exception.txt"
        if exc_file.is_file():
            with contextlib.suppress(Exception):
                exc_text = exc_file.read_text(encoding="utf-8", errors="replace")
                sanitized_exc = sanitize_value(exc_text, replacements, regex_replacements)
                (pack_path / "exception.txt").write_text(sanitized_exc, encoding="utf-8")

        # 6d. verifier/
        ver_dir = td / "verifier"
        if ver_dir.is_dir():
            pack_ver_dir = pack_path / "verifier"
            pack_ver_dir.mkdir(parents=True, exist_ok=True)
            for v_entry in ver_dir.iterdir():
                if v_entry.is_file():
                    dest_file = pack_ver_dir / v_entry.name
                    try:
                        v_content = v_entry.read_text(encoding="utf-8")
                        sanitized_v = sanitize_value(v_content, replacements, regex_replacements)
                        dest_file.write_text(sanitized_v, encoding="utf-8")
                    except UnicodeDecodeError:
                        shutil.copy2(v_entry, dest_file)
    # 7. Write rater_batches.json (before leak scan so it is audited)
    all_oids = sorted(sealed_arm_map.keys())
    rater_names = [f"rater_{chr(ord('a') + i)}" for i in range(raters)]
    rater_batches: list[dict[str, Any]] = []

    for rater in rater_names:
        shuffled_for_rater = list(all_oids)
        rng.shuffle(shuffled_for_rater)
        for b_idx in range(0, len(shuffled_for_rater), per_agent):
            batch_slice = shuffled_for_rater[b_idx : b_idx + per_agent]
            batch_num = b_idx // per_agent + 1
            batch_name = f"{rater.capitalize()}_{batch_num:02d}"
            items = [
                {
                    "id": oid,
                    "pack_path": str((packs_dir / oid).resolve()),
                    "task": sealed_arm_map[oid]["task"],
                }
                for oid in batch_slice
            ]
            prompt_lines = [
                f"You are {rater}. Label these {len(items)} runs:",
                *[f"- `{it['pack_path']}` (task {it['task']})" for it in items],
                "",
                'Return `{"labels": [<objects>]}` according to RATER_GUIDE.md.',
            ]
            rater_batches.append(
                {
                    "rater": rater,
                    "batch_index": batch_num,
                    "name": batch_name,
                    "ids": batch_slice,
                    "items": items,
                    "task_prompt": "\n".join(prompt_lines),
                }
            )

    batches_path = out_dir / "rater_batches.json"
    batches_path.write_text(json.dumps(rater_batches, indent=2) + "\n", encoding="utf-8")

    # 8. Write PACK_FORMAT.md (before leak scan so it is audited)
    pack_format_content = """# Blind Evidence Pack Format

Packs are sanitized trial bundles with all arm tokens, model identifiers, and job/trial names removed.

## Contents of `packs/<id>/`
- `trajectory.json`: ATIF steps keeping only `step_id`, `source`, `message`, `reasoning_content`, `tool_calls`, and `observation`.
- `result_summary.json`:
  - `task`: task identifier
  - `reward`: verifier reward (0.0 to 1.0)
  - `exception_type`: exception class if aborted
  - `exception_message`: exception summary
  - `n_episodes`: total interaction turns
  - `n_input_tokens`: total input tokens
  - `n_output_tokens`: total output tokens
  - `stop_reason`: reason run halted
  - `prose_completions`: count of prose-only completions
  - `loop_break`: lf2 loop detector state (`fired`, `nudge_call`, `detector`, `stop_call`, `model_next`)
  - `summarization_count`: count of context summarizations
- `exception.txt`: sanitized Python traceback or execution exception if present.
- `verifier/`: sanitized test runner logs and outputs (`agent.diff`, `apply.log`, `reward.txt`, `test-stdout.txt`, `test_output.log`).
"""
    (out_dir / "PACK_FORMAT.md").write_text(pack_format_content, encoding="utf-8")

    # 9. Leak Scan across all packs AND everything handed to raters
    forbidden_tokens = set(mask_texts)
    for info in sealed_arm_map.values():
        forbidden_tokens.add(info["trial"])
        if "__" in info["trial"]:
            forbidden_tokens.add(info["trial"].split("__")[0])
        forbidden_tokens.add(Path(info["job_path"]).name)
        forbidden_tokens.add(info["arm"])
    all_leaks: list[dict[str, Any]] = []
    total_files_scanned = 0
    for p_entry in packs_dir.iterdir():
        if p_entry.is_dir():
            for _, _, fnames in os.walk(p_entry):
                total_files_scanned += len(fnames)
            leaks = leak_scan_pack(p_entry, list(forbidden_tokens), mask_regexes)
            all_leaks.extend(leaks)

    for aux_name in ("rater_batches.json", "PACK_FORMAT.md"):
        aux_file = out_dir / aux_name
        if aux_file.is_file():
            total_files_scanned += 1
            all_leaks.extend(
                leak_scan_file(
                    aux_file,
                    aux_name,
                    list(forbidden_tokens),
                    mask_regexes,
                )
            )

    leak_scan_path = out_dir / "leak_scan.json"
    leak_scan_data = {
        "scanned_files": total_files_scanned,
        "leaks_found": len(all_leaks),
        "leaks": all_leaks,
    }
    leak_scan_path.write_text(json.dumps(leak_scan_data, indent=2) + "\n", encoding="utf-8")

    if all_leaks:
        leak_msg = "\n".join(
            f"  {leak['file']}: leaked '{leak['token']}' (snippet: {leak['snippet']})"
            for leak in all_leaks[:10]
        )
        raise ValueError(f"Leak scan failed with {len(all_leaks)} leaks:\n{leak_msg}")

    # 10. Write SEALED_arm_map.json (mode 0400)
    sealed_map_path = out_dir / "SEALED_arm_map.json"
    if sealed_map_path.exists():
        os.chmod(sealed_map_path, 0o600)
    sealed_map_path.write_text(
        json.dumps(sealed_arm_map, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(sealed_map_path, 0o400)

    # 11. Write metrics_blind.jsonl
    metrics_rows: list[dict[str, Any]] = []
    for oid in sorted(sealed_arm_map):
        info = sealed_arm_map[oid]
        trial_dir = Path(info["job_path"]) / info["trial"]
        m_row = {"id": oid, "task": info["task"], **metrics_for_trial(trial_dir)}
        metrics_rows.append(m_row)

    metrics_path = out_dir / "metrics_blind.jsonl"
    metrics_path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in metrics_rows),
        encoding="utf-8",
    )
    return 0


def review_freeze(
    review_dir: Path,
    labels_dir: Path,
    guide_path: Path = DEFAULT_GUIDE_PATH,
) -> int:
    """Execute `evallab review freeze`."""
    review_dir = review_dir.resolve()
    labels_dir = labels_dir.resolve()

    if not review_dir.is_dir():
        raise FileNotFoundError(f"Review directory not found: {review_dir}")
    if not labels_dir.is_dir():
        raise FileNotFoundError(f"Labels directory not found: {labels_dir}")

    frozen_at_path = review_dir / "FROZEN_AT"
    manifest_path = review_dir / "MANIFEST.sha256"

    if frozen_at_path.exists() or manifest_path.exists():
        raise FileExistsError(
            f"Review directory {review_dir} is already frozen; refusing to overwrite."
        )

    # 1. Discover pack IDs from packs/ or SEALED_arm_map.json or metrics_blind.jsonl
    packs_dir = review_dir / "packs"
    expected_ids: set[str] = set()
    if packs_dir.is_dir():
        expected_ids = {d.name for d in packs_dir.iterdir() if d.is_dir()}
    elif (review_dir / "metrics_blind.jsonl").is_file():
        expected_ids = {
            json.loads(line)["id"]
            for line in (review_dir / "metrics_blind.jsonl").read_text().splitlines()
            if line.strip()
        }
    elif (review_dir / "SEALED_arm_map.json").is_file():
        expected_ids = set(json.loads((review_dir / "SEALED_arm_map.json").read_text()).keys())
    elif (review_dir / "arm_map.json").is_file():
        expected_ids = set(json.loads((review_dir / "arm_map.json").read_text()).keys())

    if not expected_ids:
        raise ValueError(f"No pack IDs found in {review_dir} to freeze against.")

    # 2. Check labels for rater_a and rater_b
    valid_raters = ("rater_a", "rater_b")
    for rater in valid_raters:
        r_dir = labels_dir / rater
        if not r_dir.is_dir():
            raise FileNotFoundError(f"Required rater directory missing: {r_dir}")

    required_fields = {"trial", "stop_reason", "first_failure", "blame", "loop_kind", "pass_copied"}
    valid_stop_reasons = {
        "model_finished",
        "loop_break",
        "request_ceiling",
        "token_ceiling",
        "infra_error",
        "agent_timeout",
        "other",
    }
    valid_blames = {"model", "harness", "task", "infra", "none"}
    valid_loop_kinds = {"completion-claim", "repetition", "none"}

    for rater in valid_raters:
        r_dir = labels_dir / rater
        for oid in sorted(expected_ids):
            l_file = r_dir / f"{oid}.json"
            if not l_file.is_file():
                raise FileNotFoundError(
                    f"Missing label file for rater {rater} on trial {oid}: {l_file}"
                )
            try:
                data = json.loads(l_file.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ValueError(f"Invalid JSON in label file {l_file}: {exc}") from exc

            missing = required_fields - set(data.keys())
            if missing:
                raise ValueError(f"Label file {l_file} missing required fields: {sorted(missing)}")
            if data["stop_reason"] not in valid_stop_reasons:
                raise ValueError(f"Label file {l_file} invalid stop_reason: {data['stop_reason']}")
            if data["blame"] not in valid_blames:
                raise ValueError(f"Label file {l_file} invalid blame: {data['blame']}")
            if data["loop_kind"] not in valid_loop_kinds:
                raise ValueError(f"Label file {l_file} invalid loop_kind: {data['loop_kind']}")
            if data["pass_copied"] not in (True, False, None):
                raise ValueError(f"Label file {l_file} invalid pass_copied: {data['pass_copied']}")

    # 3. Read guide sha256
    guide_file = guide_path.resolve()
    if not guide_file.is_file():
        # Fallback relative to repo root if path was relative
        guide_file = Path.cwd() / guide_path
    guide_sha = (
        hashlib.sha256(guide_file.read_bytes()).hexdigest()
        if guide_file.is_file()
        else "unknown"
    )

    # 4. Build manifest lines
    manifest_entries: list[tuple[str, str]] = []

    # If labels_dir is inside review_dir, use relative path from review_dir; else copy or reference
    # Convention: freeze expects labels under review_dir / "labels" or mirrors them
    dest_labels_dir = review_dir / "labels"
    if labels_dir != dest_labels_dir:
        dest_labels_dir.mkdir(parents=True, exist_ok=True)
        for rater in valid_raters:
            (dest_labels_dir / rater).mkdir(parents=True, exist_ok=True)
            for f in sorted((labels_dir / rater).glob("*.json")):
                shutil.copy2(f, dest_labels_dir / rater / f.name)

    for rater in valid_raters:
        for f in sorted((dest_labels_dir / rater).glob("*.json")):
            rel = f"labels/{rater}/{f.name}"
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
            manifest_entries.append((digest, rel))

    for aux_name in ("metrics_blind.jsonl", "leak_scan.json"):
        aux_file = review_dir / aux_name
        if aux_file.is_file():
            digest = hashlib.sha256(aux_file.read_bytes()).hexdigest()
            manifest_entries.append((digest, aux_name))

    manifest_content = "".join(f"{d}  {p}\n" for d, p in manifest_entries)
    manifest_path.write_text(manifest_content, encoding="utf-8")

    # 5. Build FROZEN_AT
    now_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    frozen_at_content = f"""{now_utc}
Frozen before the arm map is joined.
- guide: {guide_file} (sha256 {guide_sha})
- labels: {len(expected_ids)} runs x 2 blind raters (rater_a, rater_b) under {guide_file.name}
- manifest_entries: {len(manifest_entries)} files
"""
    frozen_at_path.write_text(frozen_at_content, encoding="utf-8")

    print(f"evallab review freeze complete: sealed {len(manifest_entries)} files in {manifest_path}")
    return 0


def verify_manifest(review_dir: Path) -> str:
    """Verify MANIFEST.sha256 and return manifest sha256."""
    manifest_path = review_dir / "MANIFEST.sha256"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Freeze manifest missing in {review_dir}: MANIFEST.sha256")

    manifest_bytes = manifest_path.read_bytes()
    for line in manifest_bytes.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        digest, name = line.split(maxsplit=1)
        target = review_dir / name
        if not target.is_file():
            raise FileNotFoundError(f"File {name} listed in MANIFEST.sha256 missing in {review_dir}")
        actual_digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual_digest != digest:
            raise ValueError(f"File {name} changed after freeze ({actual_digest} != {digest})")
    return hashlib.sha256(manifest_bytes).hexdigest()


def compare_arms(
    arm_map: dict[str, dict[str, Any]],
    metrics_rows: Sequence[dict[str, Any]],
    labels_dir: Path,
    baseline: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Generate TABLES.md and tables.json structure across N arms."""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    by_id = {r["id"]: r for r in metrics_rows}

    for oid, info in sorted(arm_map.items()):
        if oid not in by_id:
            continue
        r = dict(by_id[oid])
        if r.get("verdict") == "excluded":
            continue  # infra: missing, never zero

        r["passed"] = r.get("verdict") == "counted_pass"
        r["never_edited"] = r.get("first_edit_step") is None
        r["hit_token_ceiling"] = r.get("stop_reason") == "ceiling:input_tokens"
        r["arm"] = info["arm"]
        r["task"] = info["task"]

        # Agreed labels
        for f in ("stop_reason", "loop_kind", "blame"):
            fa = labels_dir / "rater_a" / f"{oid}.json"
            fb = labels_dir / "rater_b" / f"{oid}.json"
            if fa.is_file() and fb.is_file():
                la = json.loads(fa.read_text()).get(f)
                lb = json.loads(fb.read_text()).get(f)
                r[f"label_{f}"] = la if la == lb else "disagree"
            else:
                r[f"label_{f}"] = "disagree"

        rows[(r["task"], r["arm"])] = r

    distinct_arms = {info["arm"] for info in arm_map.values()}
    if distinct_arms == {"stock", "tuned", "gepa"}:
        canonical_arms = ["stock", "tuned", "gepa"]
    else:
        canonical_arms = sorted(distinct_arms)

    if baseline is None:
        baseline = canonical_arms[0] if canonical_arms else "stock"
    if baseline in canonical_arms:
        arms = [baseline] + [a for a in canonical_arms if a != baseline]
    else:
        arms = canonical_arms

    tasks = sorted({t for t, _ in rows})
    out: list[str] = ["# G6 tables (generated by g6_compare.py after the label freeze)", ""]

    if WITHDRAWN_METRICS:
        joined_metrics = ", ".join(f"`{m}`" for m in sorted(WITHDRAWN_METRICS))
        out.append(
            f"> **Withdrawn rows:** {joined_metrics}. "
            "Their edit detector (`token_flow._is_edit`) counts read-only `awk 'NR>=…'` commands as edits; "
            "see `QUIRKS.md`. The numbers are kept as generated for audit."
        )
        out.append("")

    out += ["## Per arm", "", "| metric | " + " | ".join(arms) + " |", "|---|---|" + "---|" * (len(arms) - 1)]

    n_row = [str(sum(1 for (_, a) in rows if a == arm)) for arm in arms]
    out.append("| scored cells | " + " | ".join(n_row) + " |")

    for m in BINARY_METRICS:
        cells = []
        for arm in arms:
            vals = [r[m] for (_, a), r in rows.items() if a == arm and r.get(m) is not None]
            cells.append(f"{sum(bool(v) for v in vals)}/{len(vals)}")
        out.append(f"| {m} | " + " | ".join(cells) + " |")

    for m in NUMERIC_METRICS:
        cells = []
        for arm in arms:
            vals = [float(r[m]) for (_, a), r in rows.items() if a == arm and r.get(m) is not None]
            cells.append(quartiles(vals))
        out.append(f"| {m} (median [IQR]) | " + " | ".join(cells) + " |")

    for f in ("label_stop_reason", "label_loop_kind", "label_blame"):
        for arm in arms:
            counts: dict[str, int] = {}
            for (_, a), r in rows.items():
                if a == arm:
                    val_str = str(r.get(f))
                    counts[val_str] = counts.get(val_str, 0) + 1
            counts_str = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
            out.append(f"| {f} · {arm} | {counts_str} |" + " |" * (len(arms) - 1))

    # Paired comparisons against baseline
    for arm in arms:
        if arm == baseline:
            continue
        pairs = [t for t in tasks if (t, baseline) in rows and (t, arm) in rows]
        out += ["", f"## Paired {arm} − {baseline} ({len(pairs)} tasks with both cells)", ""]
        out += ["| metric | median diff [IQR] | flips stock→arm: F→T / T→F |", "|---|---|---|"]
        for m in NUMERIC_METRICS:
            diffs = [
                float(rows[(t, arm)][m]) - float(rows[(t, baseline)][m])
                for t in pairs
                if rows[(t, arm)].get(m) is not None and rows[(t, baseline)].get(m) is not None
            ]
            out.append(f"| {m} | {quartiles(diffs)} | |")
        for m in BINARY_METRICS:
            up = sum(1 for t in pairs if not rows[(t, baseline)].get(m) and rows[(t, arm)].get(m))
            down = sum(1 for t in pairs if rows[(t, baseline)].get(m) and not rows[(t, arm)].get(m))
            out.append(f"| {m} | | {up} / {down} |")

    out += [
        "",
        "## Tasks whose pass/fail differs between arms",
        "",
        "| task | " + " | ".join(arms) + " |",
        "|---|---|" + "---|" * (len(arms) - 1),
    ]

    differing_tasks: list[dict[str, Any]] = []
    for t in tasks:
        res = [rows[(t, a)]["passed"] if (t, a) in rows else None for a in arms]
        non_none = [x for x in res if x is not None]
        if len(set(non_none)) > 1:
            cell = ["missing" if x is None else ("pass" if x else "fail") for x in res]
            ids = [rows[(t, a)]["id"] if (t, a) in rows else "—" for a in arms]
            out.append(
                f"| {t} | "
                + " | ".join(f"{c} ({i})" for c, i in zip(cell, ids, strict=True))
                + " |"
            )
            differing_tasks.append(
                {
                    "task": t,
                    "results": {a: cell[i] for i, a in enumerate(arms)},
                    "ids": {a: ids[i] for i, a in enumerate(arms)},
                }
            )

    tables_json = {
        "arms": arms,
        "baseline": baseline,
        "withdrawn_metrics": list(sorted(WITHDRAWN_METRICS)),
        "differing_tasks": differing_tasks,
    }

    return "\n".join(out) + "\n", tables_json


def score_predictions(
    labels_dir: Path,
    predictions_dir: Path,
) -> tuple[str, dict[str, Any]]:
    """Score model / rule predictions against frozen labels (Wilson 95% CIs)."""
    trials = sorted(p.stem for p in (labels_dir / "rater_a").glob("*.json"))
    raters: dict[str, dict[str, Any]] = {
        r: {t: json.loads((labels_dir / r / f"{t}.json").read_text()) for t in trials}
        for r in ("rater_a", "rater_b")
    }

    passed = {
        t: raters["rater_a"][t].get("pass_copied") is not None
        or raters["rater_b"][t].get("pass_copied") is not None
        for t in trials
    }

    a = {t: rater_view(raters["rater_a"][t]) for t in trials}
    b = {t: rater_view(raters["rater_b"][t]) for t in trials}
    agreed = {
        (t, f) for t in trials for f in FIELDS if match_field(f, a[t][f], b[t][f], passed[t]) is True
    }

    scores: dict[str, Any] = {"rater_a_vs_rater_b": compare_views(a, b, passed)}

    for path in sorted(predictions_dir.glob("*.jsonl")):
        tool = path.stem
        rows_map: dict[str, Any] = {}
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                with contextlib.suppress(Exception):
                    obj = json.loads(line)
                    trial_key = obj.get("trial") or obj.get("id")
                    if trial_key:
                        rows_map[trial_key] = obj

        missing = [t for t in trials if t not in rows_map]
        if missing:
            print(f"warning: {tool}: {len(missing)} trials missing from predictions", file=sys.stderr)
        common_trials = [t for t in trials if t in rows_map]
        views = {t: tool_view(tool, rows_map[t]) for t in common_trials}
        scores[f"{tool}_vs_agreed"] = compare_views(a, views, passed, only=agreed)

    lines = [
        f"# HAR-128 part 2 scores: {labels_dir.name} ({len(trials)} runs)",
        "",
        f"Cell = agree/n [95% Wilson CI]. first_failure within ±{STEP_TOLERANCE} steps (both null agrees); "
        f"loop onset within ±{LOOP_TOLERANCE}; pass_copied on passes only. Tool rows score only cells where "
        "both raters agree. `—` = not expressed.",
        "",
        "| comparison | " + " | ".join(FIELDS) + " |",
        "|---|---|" + "---|" * (len(FIELDS) - 1),
    ]

    for name, fields_dict in scores.items():
        cells = []
        for field in FIELDS:
            c = fields_dict[field]
            if c["n"] == 0:
                cells.append("—" if c.get("not_expressed") else "0/0")
            else:
                lo, hi = c["ci95"]
                cells.append(f"{c['agree']}/{c['n']} [{lo:.2f}–{hi:.2f}]")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")

    return "\n".join(lines) + "\n", scores


def get_git_commit(path: Path) -> str | None:
    """Best-effort git commit hash for a path."""
    import subprocess
    try:
        proc = subprocess.run(
            ["git", "-C", str(path.parent), "log", "-1", "--format=%H", "--", str(path.name)],
            capture_output=True,
            text=True,
            check=False,
        )
        commit = proc.stdout.strip()
        if commit and len(commit) == 40:
            return commit
        # Fallback to current HEAD
        proc_head = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        head = proc_head.stdout.strip()
        return head if head and len(head) == 40 else None
    except Exception:
        return None


def generate_report(
    review_dir: Path,
    arm_map: dict[str, dict[str, Any]],
    metrics_rows: Sequence[dict[str, Any]],
    labels_dir: Path,
    manifest_sha: str,
) -> str:
    distinct_arms = {info["arm"] for info in arm_map.values()}
    if distinct_arms == {"stock", "tuned", "gepa"}:
        arms = ["stock", "tuned", "gepa"]
    else:
        arms = sorted(distinct_arms)
    by_id = {r["id"]: r for r in metrics_rows}

    # 1. Run counts per arm
    total_runs: dict[str, int] = {a: 0 for a in arms}
    scored_runs: dict[str, int] = {a: 0 for a in arms}
    excluded_runs: dict[str, int] = {a: 0 for a in arms}

    for oid, info in arm_map.items():
        arm = info["arm"]
        total_runs[arm] += 1
        m = by_id.get(oid, {})
        if m.get("verdict") == "excluded":
            excluded_runs[arm] += 1
        else:
            scored_runs[arm] += 1

    # 2. Passes: counts verdict vs raters' genuine pass
    counts_passes: dict[str, int] = {a: 0 for a in arms}
    rater_genuine_passes: dict[str, int] = {a: 0 for a in arms}

    for oid, info in arm_map.items():
        arm = info["arm"]
        m = by_id.get(oid, {})
        if m.get("verdict") == "counted_pass":
            counts_passes[arm] += 1
            fa = labels_dir / "rater_a" / f"{oid}.json"
            fb = labels_dir / "rater_b" / f"{oid}.json"
            pca = json.loads(fa.read_text()).get("pass_copied") if fa.is_file() else None
            pcb = json.loads(fb.read_text()).get("pass_copied") if fb.is_file() else None
            # Genuine pass: passed and not copied (both raters agree pass_copied is False)
            if pca is False and pcb is False:
                rater_genuine_passes[arm] += 1

    # 3. Differing tasks
    rows_by_task_arm: dict[tuple[str, str], dict[str, Any]] = {}
    for oid, info in arm_map.items():
        m = by_id.get(oid, {})
        if m.get("verdict") == "excluded":
            continue
        rows_by_task_arm[(info["task"], info["arm"])] = {
            "id": oid,
            "passed": m.get("verdict") == "counted_pass",
            "verdict": m.get("verdict"),
        }

    all_tasks = sorted({info["task"] for info in arm_map.values()})
    differing_task_ids: list[str] = []
    for t in all_tasks:
        res = [rows_by_task_arm[(t, a)]["passed"] if (t, a) in rows_by_task_arm else None for a in arms]
        non_none = [x for x in res if x is not None]
        if len(set(non_none)) > 1:
            differing_task_ids.append(t)

    # 4. Provenance
    frozen_at_file = review_dir / "FROZEN_AT"
    frozen_timestamp = "unknown"
    if frozen_at_file.is_file():
        frozen_lines = frozen_at_file.read_text().splitlines()
        if frozen_lines:
            frozen_timestamp = frozen_lines[0].strip()

    freeze_commit = get_git_commit(frozen_at_file) or "uncommitted (working tree)"

    lines: list[str] = [
        "# Blind Trace Review Report",
        "",
        "## Summary & Provenance",
        "",
        f"- **Freeze Timestamp:** `{frozen_timestamp}`",
        f"- **Freeze Commit:** `{freeze_commit}`",
        f"- **Manifest SHA256:** `{manifest_sha}`",
        "",
        "## Run Counts & Pass Rates",
        "",
        "| arm | total runs | scored runs | excluded (infra) | counts verdict (`counted_pass`) | raters' genuine passes |",
        "|---|---|---|---|---|---|",
    ]

    for arm in arms:
        n_scored = scored_runs[arm]
        cp_str = f"{counts_passes[arm]}/{n_scored}" if n_scored else "0/0"
        rg_str = f"{rater_genuine_passes[arm]}/{n_scored}" if n_scored else "0/0"
        lines.append(
            f"| {arm} | {total_runs[arm]} | {n_scored} | {excluded_runs[arm]} | {cp_str} | {rg_str} |"
        )

    lines += [
        "",
        "## Differing Tasks Trace Explanation",
        "",
        f"Found {len(differing_task_ids)} tasks whose pass/fail verdict differs across arms.",
        "",
    ]

    for t in differing_task_ids:
        lines.append(f"### Task {t}")
        lines.append("")
        for arm in arms:
            oids = [oid for oid, info in arm_map.items() if info["task"] == t and info["arm"] == arm]
            if not oids:
                lines.append(f"- **{arm}:** missing from cohort")
                continue
            oid = oids[0]
            m = by_id.get(oid, {})
            verdict = m.get("verdict", "unknown")
            fa = labels_dir / "rater_a" / f"{oid}.json"
            fb = labels_dir / "rater_b" / f"{oid}.json"
            da = json.loads(fa.read_text()) if fa.is_file() else {}
            db = json.loads(fb.read_text()) if fb.is_file() else {}

            lk_a, lk_b = da.get("loop_kind"), db.get("loop_kind")
            lk = lk_a if lk_a == lk_b else "disagree"

            bl_a, bl_b = da.get("blame"), db.get("blame")
            bl = bl_a if bl_a == bl_b else "disagree"

            pc_a, pc_b = da.get("pass_copied"), db.get("pass_copied")
            pc = pc_a if pc_a == pc_b else "disagree"

            ff_a, ff_b = da.get("first_failure"), db.get("first_failure")
            if ff_a is None and ff_b is None:
                ff_summary = "None (earned pass)"
            elif isinstance(ff_a, dict) and isinstance(ff_b, dict):
                ref_a = ff_a.get("ref")
                ref_b = ff_b.get("ref")
                step_a = step_of(ref_a)
                step_b = step_of(ref_b)
                if step_a is not None and step_b is not None and abs(step_a - step_b) <= STEP_TOLERANCE:
                    ff_summary = f"{ref_a} ({ff_a.get('what')})"
                else:
                    ff_summary = f"disagree (rater_a: {ref_a}; rater_b: {ref_b})"
            else:
                ff_summary = "disagree"

            lines.append(f"#### Arm: `{arm}` (pack `{oid}`, verdict: `{verdict}`)")
            lines.append(f"- **Agreed loop kind:** `{lk}`")
            lines.append(f"- **Agreed blame:** `{bl}`")
            lines.append(f"- **Agreed pass copied:** `{pc}`")
            lines.append(f"- **First failure:** {ff_summary}")

            # Quotes
            quotes_a = da.get("evidence") or []
            quotes_b = db.get("evidence") or []
            if quotes_a or quotes_b:
                lines.append("- **Rater Evidence Quotes:**")
                for q in quotes_a[:4]:
                    lines.append(f"  - [rater_a {q.get('ref')}]: \"{q.get('quote', '').strip()}\"")
                for q in quotes_b[:4]:
                    lines.append(f"  - [rater_b {q.get('ref')}]: \"{q.get('quote', '').strip()}\"")
            lines.append("")

    return "\n".join(lines) + "\n"


def review_join(
    review_dir: Path,
    baseline: str | None = None,
    predictions_dir: Path | None = None,
    out_tables: Path | None = None,
    out_report: Path | None = None,
) -> int:
    """Execute `evallab review join`."""
    review_dir = review_dir.resolve()
    if not review_dir.is_dir():
        raise FileNotFoundError(f"Review directory not found: {review_dir}")

    # 1. Verify manifest
    manifest_sha = verify_manifest(review_dir)

    # 2. Read sealed arm map
    sealed_map_file = review_dir / "SEALED_arm_map.json"
    if not sealed_map_file.is_file():
        sealed_map_file = review_dir / "arm_map.json"
    if not sealed_map_file.is_file():
        raise FileNotFoundError(f"Sealed arm map missing in {review_dir} (SEALED_arm_map.json or arm_map.json)")

    arm_map: dict[str, dict[str, Any]] = json.loads(sealed_map_file.read_text(encoding="utf-8"))

    # 3. Read metrics_blind.jsonl
    metrics_path = review_dir / "metrics_blind.jsonl"
    if not metrics_path.is_file():
        raise FileNotFoundError(f"Metrics file missing in {review_dir}: metrics_blind.jsonl")
    metrics_rows = [
        json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]

    labels_dir = review_dir / "labels"
    if not labels_dir.is_dir():
        raise FileNotFoundError(f"Labels directory missing in {review_dir}: labels/")

    # 4. Generate comparison tables
    tables_md, tables_json = compare_arms(arm_map, metrics_rows, labels_dir, baseline=baseline)

    target_tables = out_tables or (review_dir / "TABLES.md")
    target_tables.write_text(tables_md, encoding="utf-8")
    (review_dir / "tables.json").write_text(
        json.dumps(tables_json, indent=2) + "\n", encoding="utf-8"
    )

    # 5. Score predictions if present
    pred_dir = predictions_dir.resolve() if predictions_dir else (review_dir / "predictions")
    if pred_dir.is_dir() and any(pred_dir.glob("*.jsonl")):
        scores_md, scores_json = score_predictions(labels_dir, pred_dir)
        (review_dir / "scores.md").write_text(scores_md, encoding="utf-8")
        (review_dir / "scores.json").write_text(
            json.dumps(scores_json, indent=2, default=str) + "\n", encoding="utf-8"
        )
        print(f"Scored tool predictions against labels -> {review_dir / 'scores.md'}")

    # 6. Generate REPORT.md
    report_md = generate_report(review_dir, arm_map, metrics_rows, labels_dir, manifest_sha)
    target_report = out_report or (review_dir / "REPORT.md")
    target_report.write_text(report_md, encoding="utf-8")

    print(f"evallab review join complete: wrote {target_tables} and {target_report}")
    return 0
