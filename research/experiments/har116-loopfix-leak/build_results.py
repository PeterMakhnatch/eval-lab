"""HAR-116 results builder: per-run results.jsonl from finished runs only.

Reads the finished HAR-116 trial directories (read-only) and emits one JSON
row per run with reward, upstream-fetch flags, native Harbor tokens, calls,
stop reason, loop-break detail, loop kind, and fetch/bypass quotes. No new trials.

Usage (from the repo root):
    uv run --no-sync python research/experiments/har116-loopfix-leak/build_results.py
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from evallab import upstream_fetch as uf

HERE = Path(__file__).resolve().parent
RESULTS_LIVE = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har116-live/runs")
RESULTS_PUB = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-10-01")

PART_A_TASKS = [
    "000383",
    "000495",
    "000587",
    "001161",
    "001181",
    "001832",
    "001896",
    "002256",
    "002391",
    "002864",
]
PART_B_TASKS = ["000146", "000226", "000927", "002308", "002402"]

CAP_MARKER = "output limited to"
CONFIRM_RE = re.compile(r"Are you sure you want to mark the task as complete\?")
HOSTS_RE = re.compile(r"/etc/hosts")
INDEX_RE = re.compile(r"--(?:extra-)?index-url|PIP_INDEX_URL|pip\s+config\s+.*index")
MIRROR_RE = re.compile(r"pypi\.org|pythonhosted\.org|mirrors?\.", re.IGNORECASE)
GITCLONE_RE = re.compile(r"git\s+clone")
GITHUB_FETCH_RE = re.compile(r"(curl|wget)[^\n]*github", re.IGNORECASE)
WS = re.compile(r"\s+")


def collapse(text: str, limit: int = 300) -> str:
    """Whitespace-collapsed excerpt of ``text``."""
    return WS.sub(" ", text).strip()[:limit]


def parse_reward(text: str) -> float | None:
    """Parse verifier reward.txt content."""
    try:
        return float(text.strip().split()[0])
    except (ValueError, IndexError):
        return None


def load_trial(live_job: Path) -> tuple[Path | None, str]:
    """Find the single trial dir under a live job dir."""
    cands = sorted(p for p in live_job.iterdir() if p.is_dir() and "__" in p.name)
    if len(cands) != 1:
        return None, f"expected 1 trial dir, found {len(cands)}"
    return cands[0], ""


def load_processed(pub_job: Path) -> tuple[dict | None, str]:
    """Load the single processed trial JSON from a published job copy."""
    procd = pub_job / "processed"
    cands = sorted(procd.glob("trial-*.json")) if procd.is_dir() else []
    if len(cands) != 1:
        return None, f"expected 1 processed trial json, found {len(cands)}"
    try:
        return json.loads(cands[0].read_text(encoding="utf-8")), ""
    except (OSError, ValueError) as exc:
        return None, f"processed json unreadable: {exc}"


def load_trajectory(trial_dir: Path) -> tuple[dict | None, str]:
    """Load agent/trajectory.json from a trial dir."""
    path = trial_dir / "agent" / "trajectory.json"
    if not path.is_file():
        return None, "no agent/trajectory.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except (OSError, ValueError) as exc:
        return None, f"trajectory unreadable: {exc}"


def agent_steps(trajectory: dict) -> list[dict]:
    """Agent-source steps in order."""
    steps = trajectory.get("steps")
    if not isinstance(steps, list):
        return []
    return [s for s in steps if isinstance(s, dict) and str(s.get("source", "")).lower() == "agent"]


def step_keystrokes(step: dict) -> list[str]:
    """Shell keystrokes issued by one agent step."""
    out = []
    calls = step.get("tool_calls")
    if not isinstance(calls, list):
        return out
    for call in calls:
        if not isinstance(call, dict):
            continue
        args = call.get("arguments")
        keys = args.get("keystrokes") if isinstance(args, dict) else None
        if isinstance(keys, str) and keys.strip():
            out.append(keys)
    return out


def step_observation(step: dict) -> str:
    """Collapsed observation text of one agent step."""
    obs = step.get("observation")
    if isinstance(obs, dict):
        results = obs.get("results")
        if isinstance(results, list):
            texts = []
            for item in results:
                if isinstance(item, dict) and isinstance(item.get("content"), str):
                    texts.append(item["content"])
            return collapse(" ".join(texts), 2000)
    if isinstance(obs, str):
        return collapse(obs, 2000)
    return ""


def full_text_trajectory(trajectory: dict) -> list[tuple[int | None, str, str]]:
    """(step_id, source, text) for every step incl. harness prompts."""
    rows: list[tuple[int | None, str, str]] = []
    steps = trajectory.get("steps")
    if not isinstance(steps, list):
        return rows
    for step in steps:
        if not isinstance(step, dict):
            continue
        sid = step.get("step_id")
        sid = sid if isinstance(sid, int) else None
        parts = []
        if isinstance(step.get("message"), str):
            parts.append(step["message"])
        for keys in step_keystrokes(step):
            parts.append(keys)
        obs = step_observation(step)
        if obs:
            parts.append(obs)
        rows.append((sid, str(step.get("source", "")), "\n".join(parts)))
    return rows


def median_call_seconds(trajectory: dict) -> float | None:
    """Median seconds between consecutive agent-step timestamps."""
    stamps = []
    for step in agent_steps(trajectory):
        raw = step.get("timestamp")
        if not isinstance(raw, str):
            continue
        try:
            stamps.append(datetime.fromisoformat(raw))
        except ValueError:
            continue
    gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:], strict=False) if b >= a]
    if not gaps:
        return None
    gaps.sort()
    mid = len(gaps) // 2
    return gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2


def call_of_step(prompt_series: list, step_id: int | None) -> int | None:
    """Map an ATIF step id to the 1-based agent call via prompt_series."""
    if step_id is None:
        return None
    for row in prompt_series:
        if isinstance(row, dict) and row.get("step_id") == step_id:
            call = row.get("call_index")
            return call if isinstance(call, int) else None
    return None


def loop_kind_of(trial_dir: Path, processed: dict, stop: str | None) -> dict:
    """Apply Traces' har119 loop_kind.py rules to one finished run."""
    import sys

    sys.path.insert(0, str(HERE.parent.parent / "explorations" / "trace-lab" / "har119"))
    import loop_kind

    flow = {
        "token_flow": processed.get("token_flow", {}),
        "trial": processed.get("trial_name"),
        "source": "har116",
        "arm": None,
        "task": processed.get("task_name"),
        "reward": processed.get("reward"),
    }
    try:
        return loop_kind.analyse(trial_dir, flow, stop)
    except Exception as exc:  # noqa: BLE001 - record, don't crash the build
        return {"loop_kind": None, "error": f"loop_kind failed: {exc}"}


def build_row(job: str, part: str, task: str, arm: str) -> dict:
    """Build one results row for a finished job. Missing data is None + reason."""
    row: dict = {
        "job": job,
        "part": part,
        "task": task,
        "arm": arm,
        "local_path": f"~/Developer/eval-lab-results/2026-10-01/HAR-116-{job}",
        "reward_raw": None,
        "reward_zero_rule": None,
        "upstream_findings": [],
        "stop_processed": None,
        "stop_label": None,
        "exception": None,
        "input_tokens": None,
        "output_tokens": None,
        "calls": None,
        "wall_hours": None,
        "median_call_s": None,
        "loop_onset": None,
        "last_edit": None,
        "loop_break": None,
        "post_nudge_next": [],
        "loop_kind": None,
        "loop_kind_detail": {},
        "break_before_edit": None,
        "cap_markers": {"count": 0, "first_call": None},
        "confirm_prompt_step": None,
        "fetch_quotes": [],
        "bypass_quotes": [],
        "hosts_attempt": False,
        "missing": [],
    }
    live_job = RESULTS_LIVE / job
    pub_job = RESULTS_PUB / f"HAR-116-{job}"
    if not live_job.is_dir():
        row["missing"].append(f"live job dir absent: {live_job}")
    if not pub_job.is_dir():
        row["missing"].append(f"published job dir absent: {pub_job}")

    trial_dir, problem = load_trial(live_job) if live_job.is_dir() else (None, "no live dir")
    if trial_dir is None:
        row["missing"].append(problem)
        return row
    row["trial"] = trial_dir.name

    # These historical rows use native Harbor totals, not the proxy ledger.
    try:
        result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        row["missing"].append(f"native result unreadable: {exc}")
    else:
        native = result.get("agent_result") if isinstance(result, dict) else None
        if isinstance(native, dict):
            row["input_tokens"] = native.get("n_input_tokens")
            row["output_tokens"] = native.get("n_output_tokens")
        if row["input_tokens"] is None or row["output_tokens"] is None:
            row["missing"].append("native token totals unavailable")

    exc_path = trial_dir / "exception.txt"
    if exc_path.is_file():
        text = exc_path.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            row["exception"] = collapse(text.splitlines()[-1], 300)

    reward_path = trial_dir / "verifier" / "reward.txt"
    if reward_path.is_file():
        row["reward_raw"] = parse_reward(reward_path.read_text(encoding="utf-8", errors="replace"))
    else:
        row["missing"].append("no verifier/reward.txt")

    processed, problem = load_processed(pub_job) if pub_job.is_dir() else (None, "no pub dir")
    if processed is None:
        row["missing"].append(problem)
    else:
        row["stop_processed"] = processed.get("stop_reason")
        row["calls"] = processed.get("agent_steps")
        row["wall_hours"] = processed.get("trial_wall_hours")
        flow = processed.get("token_flow") or {}
        onset = flow.get("loop_onset") or {}
        row["loop_onset"] = {
            "call": onset.get("call_index"),
            "step": onset.get("step_id"),
            "detector": onset.get("detector"),
            "detail": (onset.get("detail") or "")[:200],
        }
        edit = flow.get("last_useful_edit") or {}
        series = flow.get("prompt_series") or []
        edit_step = edit.get("step_id")
        row["last_edit"] = {
            "step": edit_step,
            "call": call_of_step(series, edit_step),
            "reason": edit.get("reason"),
        }

    trajectory, problem = load_trial_trajectory(trial_dir)
    if trajectory is None:
        row["missing"].append(problem)
        if row["exception"]:
            row["stop_label"] = "crashed (infra)"
        return row

    calls = agent_steps(trajectory)
    if row["calls"] is None:
        row["calls"] = len(calls)
    row["median_call_s"] = median_call_seconds(trajectory)

    extra = (trajectory.get("final_metrics") or {}).get("extra") or {}
    row["loop_break"] = extra.get("loop_break")
    lb = row["loop_break"] or {}

    if lb.get("stop_call") is not None:
        row["stop_label"] = "LoopBreakStop"
    elif row["stop_processed"] == "ceiling:input_tokens":
        row["stop_label"] = "TrialBudgetExhausted"
    elif row["stop_processed"] == "task_complete_confirmed":
        row["stop_label"] = "agent finished (confirmed)"
    elif row["stop_processed"]:
        row["stop_label"] = str(row["stop_processed"])
    if row["exception"] and row["reward_raw"] is None:
        row["stop_label"] = "crashed (infra)"

    nudge = lb.get("nudge_call")
    if isinstance(nudge, int) and nudge >= 1:
        for offset in (1, 2):
            idx = (nudge - 1) + offset
            if 0 <= idx < len(calls):
                step = calls[idx]
                keys = step_keystrokes(step)
                row["post_nudge_next"].append(
                    {
                        "call": nudge + offset,
                        "step_id": step.get("step_id"),
                        "keystrokes": [collapse(k) for k in keys][:3],
                        "outcome": step_observation(step)[:300],
                    }
                )

    edit_call = (row["last_edit"] or {}).get("call")
    stop_call = lb.get("stop_call")
    if not lb.get("fired"):
        row["break_before_edit"] = "n/a (break never fired)"
    elif edit_call is None:
        row["break_before_edit"] = "unknown (no recorded edit)"
    elif stop_call is not None:
        # True only when the stop precedes the last useful edit: live work cut.
        row["break_before_edit"] = stop_call <= edit_call
    else:
        # Nudge fired but the run continued: nothing was cut.
        row["break_before_edit"] = f"continued past nudge {nudge}, edit at {edit_call} (no cut)"

    texts = full_text_trajectory(trajectory)
    cap_calls = []
    for sid, _source, text in texts:
        if CAP_MARKER in text:
            cap_calls.append(sid)
    row["cap_markers"] = {
        "count": len(cap_calls),
        "first_step": cap_calls[0] if cap_calls else None,
    }
    for sid, _source, text in texts:
        if CONFIRM_RE.search(text):
            row["confirm_prompt_step"] = sid
            break

    try:
        commands = uf.commands_from_trial(trial_dir)
    except Exception as exc:  # noqa: BLE001 - record, don't crash the build
        row["missing"].append(f"commands_from_trial failed: {exc}")
        commands = []
    findings = uf.detect_upstream_fetch(commands)
    row["upstream_findings"] = [
        {
            "step": f.step_index,
            "kind": f.kind,
            "excerpt": f.excerpt,
            "names_task_repo": f.names_task_repo,
        }
        for f in findings
    ]
    row["reward_zero_rule"] = 0.0 if findings else row["reward_raw"]

    obs_by_step: dict[int, str] = {}
    for step in calls:
        sid = step.get("step_id")
        if isinstance(sid, int):
            obs_by_step[sid] = step_observation(step)
    for sid, keys in commands:
        if HOSTS_RE.search(keys):
            row["hosts_attempt"] = True
            row["bypass_quotes"].append(
                {"step": sid, "class": "etc-hosts", "command": collapse(keys)}
            )
        if INDEX_RE.search(keys):
            row["bypass_quotes"].append(
                {"step": sid, "class": "pip-index-url", "command": collapse(keys)}
            )
        if GITCLONE_RE.search(keys):
            row["bypass_quotes"].append(
                {"step": sid, "class": "git-clone", "command": collapse(keys)}
            )
        if GITHUB_FETCH_RE.search(keys):
            row["bypass_quotes"].append(
                {"step": sid, "class": "curl-github", "command": collapse(keys)}
            )
        elif MIRROR_RE.search(keys):
            row["bypass_quotes"].append(
                {"step": sid, "class": "mirror-url", "command": collapse(keys)}
            )
    for item in row["upstream_findings"]:
        sid = item["step"]
        row["fetch_quotes"].append(
            {
                "step": sid,
                "kind": item["kind"],
                "command": item["excerpt"],
                "outcome": obs_by_step.get(sid, "")[:400],
            }
        )

    if row["exception"] and row["reward_raw"] is None:
        row["loop_kind_detail"] = {"reason": "crashed run, no agent trajectory to classify"}
        row["loop_kind"] = None
    elif processed is not None:
        row["loop_kind_detail"] = loop_kind_of(trial_dir, processed, row["stop_processed"])
        row["loop_kind"] = row["loop_kind_detail"].get("loop_kind")

    return row


def load_trial_trajectory(trial_dir: Path) -> tuple[dict | None, str]:
    """Wrapper keeping build_row readable."""
    return load_trajectory(trial_dir)


def summarize(rows: list[dict]) -> dict:
    """Arm totals and per-task summaries (Part A only)."""
    part_a = [r for r in rows if r["part"] == "A" and r["arm"] in ("baseline", "loopfix-r2")]
    by_task: dict[str, dict] = {}
    for task in PART_A_TASKS:
        base = next((r for r in part_a if r["task"] == task and r["arm"] == "baseline"), None)
        fix = next((r for r in part_a if r["task"] == task and r["arm"] == "loopfix-r2"), None)
        by_task[task] = {
            "baseline_reward": base["reward_raw"] if base else None,
            "loopfix_reward": fix["reward_raw"] if fix else None,
            "baseline_tokens": base["input_tokens"] if base else None,
            "loopfix_tokens": fix["input_tokens"] if fix else None,
            "token_saving": (
                (base["input_tokens"] - fix["input_tokens"])
                if base
                and fix
                and base["input_tokens"] is not None
                and fix["input_tokens"] is not None
                else None
            ),
            "pass_delta": (
                (fix["reward_raw"] - base["reward_raw"])
                if base and fix and base["reward_raw"] is not None and fix["reward_raw"] is not None
                else None
            ),
        }
    totals = {}
    for arm in ("baseline", "loopfix-r2"):
        arm_rows = [r for r in part_a if r["arm"] == arm]
        toks = [r["input_tokens"] for r in arm_rows if r["input_tokens"] is not None]
        totals[arm] = {
            "passes": sum(1 for r in arm_rows if r["reward_raw"] == 1.0),
            "runs": len(arm_rows),
            "input_tokens": sum(toks),
        }
    return {"by_task": by_task, "totals": totals}


def main() -> int:
    """Build results.jsonl and print the summary tables."""
    jobs: list[tuple[str, str, str, str]] = []
    for task in PART_A_TASKS:
        jobs.append((f"har116-a-{task}-baseline", "A", task, "baseline"))
        jobs.append((f"har116-a-{task}-loopfix", "A", task, "loopfix"))
        jobs.append((f"har116-a-{task}-loopfix-r2", "A", task, "loopfix-r2"))
    for task in PART_B_TASKS:
        jobs.append((f"har116-b-{task}-original", "B", task, "original"))
        jobs.append((f"har116-b-{task}-leakclosed", "B", task, "leakclosed"))

    rows = [build_row(job, part, task, arm) for job, part, task, arm in jobs]
    out = HERE / "results.jsonl"
    out.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    summary = summarize(rows)
    (HERE / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    problems = [(r["job"], r["missing"]) for r in rows if r["missing"]]
    print(json.dumps({"rows": len(rows), "rows_with_gaps": problems}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
