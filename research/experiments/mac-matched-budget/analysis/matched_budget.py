import argparse
import json
import re
import sys
from bisect import bisect_left, bisect_right
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from statistics import fmean

SCHEMA = "mac-matched-budget/v1"
MAX_ROWS = 1_000_000
MAX_TRIALS = 500
MAX_STEPS = 100_000
EXACT_DECIMALS = 6
FRAME_DECIMALS = 4
MONOTONE_TASKS = {
    "bespokelabsai/holdout": (
        "upstream/tasks/holdout/task/tests/verify.py: grade is 0..1 and never decreasing; "
        "tests/hidden/holdout/engine.py Game._score only raises self.best"
    ),
}
GAME_TOOL = re.compile(r"^mcp__.*holdout_(status|look|events|act)$")
SHELL_GAME = re.compile(r"holdout|api/game")
SHELL_ACT = re.compile(r"holdout\.py['\"]?\s+act\b|api/game/act|call\(\s*['\"]act['\"]")
LABEL_SECONDS = re.compile(r"^([mhs])(\d+(?:\.\d+)?)$")
UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0}
COUNTERS = (
    "usd", "input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens", "requests",
    "act_requests", "act_requests_lower", "act_requests_upper", "game_calls", "game_messages",
    "board_sends",
)
BUDGETS = ("wall_s", "usd", "output_tokens", "act_requests", "game_calls")
EQUAL_BUDGETS = ("usd", "output_tokens", "act_requests", "game_calls")
RESUME_EVENTS = ("holdout_resume", "holdout_clock_resumed", "resumed")

SOURCES = {
    "arm": "result.json config.environment.kwargs (also config.json): n_agents == 1 -> solo; "
    "no_messages_policy.py -> silent; coord_guard.py -> guard; otherwise team (override: --arm)",
    "n_agents": "result.json config.environment.kwargs.n_agents; names: result.json "
    "agent_result.metadata.agent_names (agent-N -> game seat name)",
    "model_calls": "proxy-usage.jsonl rows: ts (UTC, written when the response ended), agent (seat), "
    "tokens.input, tokens.cache_read (cached), tokens.cache_write + cache_write_1h, tokens.output, "
    "usd, status; cross-check agent/proxy-usage.json total and result.json agent_result.cost_usd",
    "marks": "result.json agent_result.metadata.marks[] (mark, mark_s, elapsed_s, final, status, "
    "ended_early); score marks/<mark>/reward.txt; graded state marks/<mark>/details.json wall",
    "final_score": "verifier/reward.txt; verifier/details.json (wall, frozen, calls, score_times)",
    "score_timeline": "verifier/replay.jsonl frames (wall, score rounded to 4 dp, every 5 game "
    "minutes), verifier/details.json score_times [wall, score 4 dp, t], mark rewards (6 dp)",
    "game_clock": "board-events.jsonl event holdout_start ts = game wall 0 "
    "(upstream holdout/host.py GameHost._start sets t_start, then emits holdout_start)",
    "wall_clock": "result.json agent_execution.started_at / finished_at (harness agent interval); "
    "started_at / finished_at (whole trial)",
    "world_actions": "agent/agent-N/trajectory.json (ATIF) tool calls: mcp holdout_act calls and "
    "their ok replies (step timestamp); shell commands that name the game; server totals of all "
    "game API calls per seat: details.json calls at each mark and at the final grading",
    "messages": "verifier/messages.jsonl (game channels: wall, seat, channel); board-events.jsonl "
    "event send (harness board, by agent) and notify (harness)",
    "checkpoints": "<job>.checkpoints/<trial>/<n>-<time>/manifest.json and proxy-usage.jsonl",
}

CONVENTIONS = {
    "cut_alignment": "graded marks are cut at the graded game state: holdout_start.ts + "
    "marks/<mark>/details.json wall (score_state_aligned); the nominal label and the harness "
    "elapsed_s are shown beside it; the end cut is agent_execution.finished_at, with the final "
    "verifier score; an early final grade is not substituted for a missing nominal mark",
    "counters": "model USD, tokens and requests count proxy rows with ts <= cut; messages count "
    "game messages with holdout_start.ts + wall <= cut; board sends count board events ts <= cut; "
    "tool calls count at the ATIF step timestamp",
    "seat_metrics": "agent-N denotes a slot, not a persistent identity across trials; seat wall "
    "time is the shared cut, not active time; the verifier supplies no individual scores",
    "score_between_saved_points": "for a task whose grade never decreases, the score at a time is "
    "bounded by the saved points before and after it (lower = max of earlier saved scores minus "
    "half a unit of their rounding, upper = min of later saved scores plus half a unit); a value "
    "is given only at a saved point or when the neighbouring saved scores are equal at the "
    "coarser precision (then it is that rounded score, not the exact grade); for any other task "
    "saved scores are marks only and the value between them is unknown",
    "crossing": "the time an arm 'had spent X' is the ts of the first proxy row whose cumulative "
    "sum reaches X (the same rule for output tokens; for complete action timelines, the first "
    "accepted act call); server game-call totals exist only at marks, so that crossing is a "
    "bracket between 2 marks",
    "aggregation": "cohort = trials of 1 arm with 1 protocol (task, model, harness, timeout, marks, "
    "effort); cohort value = arithmetic mean over the trials that have the cut; a mean is "
    "unknown when a member value is unknown; bounds are means of member bounds",
    "tolerance": "budgets are matched when larger / smaller - 1 <= tolerance (cohort means)",
    "equal_budget_target": "target X = the smaller cohort mean at the cut; every trial of both "
    "cohorts is evaluated at its own first crossing of X; the smaller cohort's mean score at the "
    "mark is shown too, but its trials did not each spend exactly X",
    "world_actions": "act_requests counts accepted mcp holdout_act calls (observable action "
    "requests, not completed operations); it is exact when the seat's accepted mcp game calls "
    "equal the server total of all its game calls (no unobserved call, shell or script); "
    "otherwise it is bounded: lower = accepted mcp act calls, upper = lower + (next server total "
    "- accepted mcp game calls); shell act mentions are textual and unverified",
    "unknown": "a missing value is null and is never 0",
}


def read_json(path: Path) -> dict:
    assert path.is_file(), path
    data = json.loads(path.read_text())
    assert isinstance(data, dict), path
    return data


def read_jsonl(path: Path) -> tuple[list[dict], int]:
    if not path.is_file():
        return [], 0
    lines = path.read_text().splitlines()
    assert len(lines) <= MAX_ROWS, path
    rows: list[dict] = []
    bad = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    return rows, bad


def read_float(path: Path) -> float | None:
    if not path.is_file():
        return None
    text = path.read_text().strip()
    return float(text) if text else None


def epoch(text: str) -> float:
    assert isinstance(text, str) and text, text
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    assert value.tzinfo is not None, text
    return value.timestamp()


def iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="milliseconds")


def rnd(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def half_unit(decimals: int) -> float:
    assert 0 <= decimals <= 12, decimals
    return 0.5 * 10.0 ** (-decimals)


def label_seconds(label: str) -> float | None:
    match = LABEL_SECONDS.match(label)
    if match is None:
        return None
    return float(match.group(2)) * UNIT_SECONDS[match.group(1)]


def resolve_trial(path: Path) -> Path:
    assert path.is_dir(), path
    if (path / "result.json").is_file() and "trial_name" in read_json(path / "result.json"):
        return path
    found = sorted(
        p for p in path.iterdir() if p.is_dir() and (p / "result.json").is_file()
    )
    assert len(found) == 1, f"{path}: expected 1 trial folder, found {len(found)}"
    return found[0]


def infer_arm(env: dict) -> tuple[str, str]:
    n_agents = int(env["n_agents"])
    plugins = [Path(p).name for p in str(env.get("board_plugins") or "").split(",") if p]
    policy = Path(str(env.get("board_policy") or "")).name
    if n_agents == 1:
        return "solo", "config n_agents == 1"
    if policy == "no_messages_policy.py":
        return "silent", "config board_policy no_messages_policy.py"
    if "coord_guard.py" in plugins:
        return "guard", "config board_plugins includes coord_guard.py"
    return "team", f"config n_agents {n_agents}, board_policy {policy}, no coord_guard.py"


def load_identity(trial: Path, result: dict, overrides: dict[str, str]) -> dict:
    config = result["config"]
    env = config["environment"]["kwargs"]
    kwargs = config["agent"]["kwargs"]
    meta = (result.get("agent_result") or {}).get("metadata") or {}
    arm, arm_source = infer_arm(env)
    if str(trial.resolve()) in overrides:
        arm, arm_source = overrides[str(trial.resolve())], "command line --arm"
    protocol = "|".join(str(v) for v in (
        result.get("task_name"), *(kwargs.get(k) for k in
                                   ("model", "harness", "timeout_sec", "grade_at", "effort")),
    ))
    return {
        "trial_dir": str(trial),
        "job": Path(str(config.get("trials_dir") or trial.parent)).name,
        "trial_name": result.get("trial_name"),
        "task_name": result.get("task_name"),
        "arm": arm,
        "arm_source": arm_source,
        "n_agents": int(env["n_agents"]),
        "agent_names": dict(sorted((meta.get("agent_names") or {}).items())),
        "protocol": protocol,
        "model_name": kwargs.get("model"),
        "harness": kwargs.get("harness"),
        "timeout_sec": kwargs.get("timeout_sec"),
        "grade_at": list(meta.get("grade_at") or []),
        "budget_usd": env.get("budget_usd"),
        "agent_budget_usd": env.get("agent_budget_usd"),
        "board_policy": Path(str(env.get("board_policy") or "")).name,
        "board_plugins": [Path(p).name for p in str(env.get("board_plugins") or "").split(",") if p],
        "end_reason": meta.get("end_reason"),
        "timed_out": meta.get("timed_out"),
        "lost_agents": meta.get("lost_agents"),
        "keepalive": meta.get("keepalive") or {},
        "exception": (result.get("exception_info") or {}).get("exception_type")
        if result.get("exception_info") else None,
    }


def load_times(result: dict, board: list[dict]) -> dict:
    execution = result.get("agent_execution") or {}
    agent_started = epoch(execution["started_at"]) if execution.get("started_at") else None
    agent_finished = epoch(execution["finished_at"]) if execution.get("finished_at") else None
    starts = [row for row in board if row.get("event") == "holdout_start"]
    resumes = [row for row in board if row.get("event") in RESUME_EVENTS]
    game_start = float(starts[0]["ts"]) if len(starts) == 1 and not resumes else None
    if agent_started is not None and agent_finished is not None:
        assert agent_finished >= agent_started, "agent_execution ends before it starts"
    return {
        "trial_started": epoch(result["started_at"]) if result.get("started_at") else None,
        "trial_finished": epoch(result["finished_at"]) if result.get("finished_at") else None,
        "agent_started": agent_started,
        "agent_finished": agent_finished,
        "game_start": game_start,
        "holdout_start_events": len(starts),
        "resume_events": len(resumes),
    }


def load_proxy(trial: Path) -> dict:
    path = trial / "proxy-usage.jsonl"
    rows, bad = read_jsonl(path)
    calls = []
    for order, row in enumerate(rows):
        tokens = row.get("tokens")
        if (
            not isinstance(tokens, dict)
            or not all(isinstance(tokens.get(k), int) and tokens[k] >= 0
                       for k in ("input", "cache_read", "output"))
            or not isinstance(row.get("usd"), int | float)
            or row["usd"] < 0
            or not row.get("ts")
        ):
            bad += 1
            continue
        calls.append({
            "ts": row["ts"],
            "epoch": epoch(row["ts"]),
            "order": order,
            "seat": row.get("agent"),
            "status": row.get("status"),
            "path": row.get("path"),
            "model": row.get("model"),
            "usd": float(row["usd"]),
            "input": tokens["input"],
            "cache_read": tokens["cache_read"],
            "cache_write": int(tokens.get("cache_write") or 0) + int(tokens.get("cache_write_1h") or 0),
            "output": tokens["output"],
        })
    calls.sort(key=lambda c: (c["epoch"], c["order"]))
    summary_path = trial / "agent" / "proxy-usage.json"
    summary = read_json(summary_path) if summary_path.is_file() else {}
    total = (summary.get("total") or {}).get("usd")
    return {
        "calls": calls,
        "complete": path.is_file() and bad == 0,
        "bad_lines": bad,
        "summary_usd": None if total is None else float(total),
        "non_200": sum(1 for c in calls if c["status"] != 200),
    }


def load_marks(trial: Path, meta: dict, game_start: float | None) -> list[dict]:
    marks = []
    for mark in meta.get("marks") or []:
        label = str(mark["mark"])
        folder = trial / "marks" / label
        details_path = folder / "details.json"
        details = read_json(details_path) if details_path.is_file() else {}
        wall = details.get("wall")
        cut = None if game_start is None or wall is None else game_start + float(wall)
        reward_file = read_float(folder / "reward.txt")
        reward_meta = mark.get("reward")
        marks.append({
            "label": label,
            "nominal_s": mark.get("mark_s"),
            "harness_elapsed_s": mark.get("elapsed_s"),
            "final": bool(mark.get("final")),
            "ended_early": bool(mark.get("ended_early")),
            "status": mark.get("status"),
            "reward": reward_file if reward_file is not None else reward_meta,
            "reward_source": "reward.txt" if reward_file is not None else "metadata",
            "reward_mismatch": reward_file is not None and reward_meta is not None
            and abs(reward_file - float(reward_meta)) > 1e-9,
            "game_wall_s": wall,
            "frozen": details.get("frozen"),
            "written_at": details.get("written_at"),
            "calls": details.get("calls"),
            "cut_epoch": cut,
        })
    return marks


def load_final(trial: Path, game_start: float | None) -> dict:
    details_path = trial / "verifier" / "details.json"
    details = read_json(details_path) if details_path.is_file() else {}
    frames, bad = read_jsonl(trial / "verifier" / "replay.jsonl")
    frames = [f for f in frames if f.get("type") == "frame" and "wall" in f and "score" in f]
    wall = details.get("wall")
    freeze = None
    if details.get("frozen") and wall is not None and game_start is not None:
        freeze = game_start + float(wall)
    return {
        "reward": read_float(trial / "verifier" / "reward.txt"),
        "details": details,
        "frames": frames,
        "replay_bad_lines": bad,
        "freeze_epoch": freeze,
        "score_times": details.get("score_times") or [],
    }


def reply_ok(content: object) -> bool:
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return False
    if isinstance(content, list) and len(content) == 1:
        content = content[0]
    if isinstance(content, dict) and content.get("type") == "text":
        try:
            content = json.loads(content["text"])
        except (KeyError, TypeError, ValueError):
            return False
    return isinstance(content, dict) and content.get("ok") is True


def step_events(step: dict) -> list[tuple[float, str]]:
    calls = step.get("tool_calls") or []
    if not calls or not step.get("timestamp"):
        return []
    stamp = epoch(step["timestamp"])
    replies = {
        r.get("source_call_id"): r.get("content")
        for r in (step.get("observation") or {}).get("results") or []
    }
    events = []
    for call in calls:
        name = str(call.get("function_name") or "")
        match = GAME_TOOL.match(name)
        if match is not None:
            ok = reply_ok(replies.get(call.get("tool_call_id")))
            events.append((stamp, "mcp_call_ok" if ok else "mcp_call_failed"))
            if match.group(1) == "act":
                events.append((stamp, "mcp_act_ok" if ok else "mcp_act_failed"))
            continue
        text = json.dumps(call.get("arguments"), sort_keys=True)
        if SHELL_GAME.search(text) is None:
            continue
        events.append((stamp, "shell_game_cmd"))
        events.extend((stamp, "shell_act_mention") for _ in SHELL_ACT.finditer(text))
    return events


def load_actions(trial: Path, agent_id: str) -> dict:
    path = trial / "agent" / agent_id / "trajectory.json"
    if not path.is_file():
        return {"present": False, "events": []}
    steps = read_json(path).get("steps") or []
    assert len(steps) <= MAX_STEPS, path
    events = []
    for step in steps:
        events.extend(step_events(step))
    events.sort()
    return {"present": True, "events": events}


def load_messages(trial: Path, game_start: float | None, board: list[dict]) -> dict:
    rows, bad = read_jsonl(trial / "verifier" / "messages.jsonl")
    game = []
    if game_start is not None:
        game = sorted(
            (game_start + float(r["wall"]), str(r.get("seat", r.get("from"))), str(r["channel"]))
            for r in rows if r.get("wall") is not None and r.get("channel") is not None
        )
    sends = sorted((float(r["ts"]), str(r.get("agent"))) for r in board if r.get("event") == "send")
    notifies = sorted(float(r["ts"]) for r in board if r.get("event") == "notify")
    return {
        "game": game,
        "game_present": (trial / "verifier" / "messages.jsonl").is_file() and len(game) == len(rows)
        and bad == 0,
        "board_sends": sends,
        "board_notifies": notifies,
    }


def checkpoint_row(folder: Path, calls: list[dict]) -> dict:
    manifest = read_json(folder / "manifest.json")
    snapshot, _ = read_jsonl(folder / "proxy-usage.jsonl")
    created = epoch(manifest["created"]) if manifest.get("created") else None
    at_created = None
    if created is not None:
        at_created = sum(c["usd"] for c in calls if c["epoch"] <= created)
    snapshot_usd = sum(float(r.get("usd") or 0.0) for r in snapshot)
    return {
        "dir": folder.name,
        "n": manifest.get("n"),
        "reason": manifest.get("reason"),
        "final": manifest.get("final"),
        "mark": manifest.get("mark"),
        "created": manifest.get("created"),
        "elapsed_agent_sec": manifest.get("elapsed_agent_sec"),
        "manifest_spend_usd": manifest.get("spend_usd"),
        "snapshot_rows": len(snapshot),
        "snapshot_usd": rnd(snapshot_usd, 9),
        "final_file_usd_at_created": rnd(at_created, 9),
    }


def load_checkpoints(trial: Path, calls: list[dict]) -> dict:
    root = trial.parent.parent / f"{trial.parent.name}.checkpoints" / trial.name
    if not root.is_dir():
        return {"dir": str(root), "found": False, "rows": [], "mark_links": {}}
    children = sorted(root.iterdir())
    assert len(children) <= MAX_STEPS, root
    rows = []
    links = {}
    for child in children:
        if child.is_symlink():
            links[child.name] = Path(child.readlink()).name
        elif (child / "manifest.json").is_file():
            rows.append(checkpoint_row(child, calls))
    rows.sort(key=lambda r: (r["n"] is None, r["n"] or 0))
    return {"dir": str(root), "found": True, "rows": rows, "mark_links": links}


def build_score_model(task: str | None, game_start: float | None, marks: list[dict],
                      final: dict) -> dict:
    points: list[tuple[float, float, int, str]] = []
    if game_start is not None:
        points.append((game_start, 0.0, EXACT_DECIMALS, "game_start"))
        for frame in final["frames"]:
            points.append((game_start + float(frame["wall"]), float(frame["score"]),
                           FRAME_DECIMALS, "replay_frame"))
        for entry in final["score_times"]:
            points.append((game_start + float(entry[0]), float(entry[1]),
                           FRAME_DECIMALS, "score_times"))
    for mark in marks:
        if mark["cut_epoch"] is not None and mark["reward"] is not None:
            points.append((mark["cut_epoch"], float(mark["reward"]), EXACT_DECIMALS,
                           f"mark:{mark['label']}"))
    points.sort(key=lambda p: (p[0], -p[2], p[3]))
    lows: list[float] = []
    for point in points:
        low = point[1] - half_unit(point[2])
        lows.append(low if not lows else max(lows[-1], low))
    highs: list[float] = [0.0] * len(points)
    for index in range(len(points) - 1, -1, -1):
        high = points[index][1] + half_unit(points[index][2])
        highs[index] = high if index == len(points) - 1 else min(highs[index + 1], high)
    consistent = all(lows[i] <= highs[i] + 1e-12 for i in range(len(points)))
    return {
        "points": points,
        "epochs": [p[0] for p in points],
        "lows": lows,
        "highs": highs,
        "monotone": task in MONOTONE_TASKS and consistent,
        "monotone_basis": MONOTONE_TASKS.get(task or ""),
        "saved_scores_consistent": consistent,
        "game_start": game_start,
        "freeze_epoch": final["freeze_epoch"],
        "final_reward": final["reward"],
    }


def point_ref(point: tuple[float, float, int, str] | None) -> dict | None:
    if point is None:
        return None
    return {"epoch": round(point[0], 3), "score": point[1], "decimals": point[2], "source": point[3]}


def score_value(value: float | None, low: float | None, high: float | None, decimals: int | None,
                source: str, prev: dict | None = None, nxt: dict | None = None) -> dict:
    return {"value": value, "lower": rnd(low, 7), "upper": rnd(high, 7), "decimals": decimals,
            "source": source, "prev_saved": prev, "next_saved": nxt}


def score_at(model: dict, cut: float) -> dict:
    points = model["points"]
    start = model["game_start"]
    if start is None or not points:
        return score_value(None, None, None, None, "no_score_timeline")
    if cut < start:
        return score_value(0.0, 0.0, 0.0, EXACT_DECIMALS, "before_game_start")
    freeze = model["freeze_epoch"]
    if freeze is not None and cut >= freeze and model["final_reward"] is not None:
        final = model["final_reward"]
        return score_value(final, final, final, EXACT_DECIMALS, "frozen_world_final_grade")
    left = bisect_left(model["epochs"], cut)
    right = bisect_right(model["epochs"], cut)
    prev = points[right - 1] if right > 0 else None
    nxt = points[left] if left < len(points) else None
    if right > left:
        best = max(points[left:right], key=lambda p: p[2])
        return score_value(best[1], best[1], best[1], best[2], f"saved_point:{best[3]}",
                           point_ref(best), point_ref(best))
    if not model["monotone"]:
        return score_value(None, None, None, None, "between_saved_marks_unknown",
                           point_ref(prev), point_ref(nxt))
    low = model["lows"][right - 1] if right > 0 else None
    high = model["highs"][left] if left < len(points) else None
    value = None
    decimals = None
    if prev is not None and nxt is not None:
        decimals = min(prev[2], nxt[2])
        if round(prev[1], decimals) == round(nxt[1], decimals):
            value = round(prev[1], decimals)
    source = "equal_neighbours_rounded" if value is not None else "bounded_between_saved_points"
    return score_value(value, low, high, decimals if value is not None else None, source,
                       point_ref(prev), point_ref(nxt))


def load_trial(path: Path, overrides: dict[str, str]) -> dict:
    trial = resolve_trial(path)
    result = read_json(trial / "result.json")
    meta = (result.get("agent_result") or {}).get("metadata") or {}
    board, board_bad = read_jsonl(trial / "board-events.jsonl")
    ident = load_identity(trial, result, overrides)
    times = load_times(result, board)
    proxy = load_proxy(trial)
    marks = load_marks(trial, meta, times["game_start"])
    final = load_final(trial, times["game_start"])
    actions = {a: load_actions(trial, a) for a in ident["agent_names"]}
    messages = load_messages(trial, times["game_start"], board)
    messages["board_present"] = (trial / "board-events.jsonl").is_file() and board_bad == 0
    trial_data = {
        **ident,
        "times": times,
        "proxy": proxy,
        "marks": marks,
        "final": final,
        "actions": actions,
        "messages": messages,
        "board_bad_lines": board_bad,
        "result_cost_usd": (result.get("agent_result") or {}).get("cost_usd"),
        "checkpoints": load_checkpoints(trial, proxy["calls"]),
    }
    trial_data["model"] = build_score_model(ident["task_name"], times["game_start"], marks, final)
    trial_data["server_points"] = server_points(trial_data)
    trial_data["coverage"] = action_coverage(trial_data)
    trial_data["flags"] = trial_flags(trial_data)
    trial_data["exclusion"] = exclusion_reason(trial_data)
    return trial_data


def server_points(trial: dict) -> list[tuple[float, dict, str]]:
    points = []
    start = trial["times"]["game_start"]
    if start is not None:
        points.append((start, dict.fromkeys(trial["agent_names"].values(), 0), "game_start"))
    for mark in trial["marks"]:
        if mark["calls"] is not None and mark["written_at"] is not None:
            stamp = mark["cut_epoch"] if mark["cut_epoch"] is not None else float(mark["written_at"])
            points.append((stamp, mark["calls"], f"mark:{mark['label']}"))
    details = trial["final"]["details"]
    if details.get("calls") is not None and details.get("written_at") is not None:
        points.append((float(details["written_at"]), details["calls"], "final_grading"))
    points.sort(key=lambda p: p[0])
    return points


def count_events(events: list[tuple[float, str]], kind: str, cut: float) -> int:
    return sum(1 for stamp, name in events if name == kind and stamp <= cut)


def action_coverage(trial: dict) -> dict:
    final_calls = trial["final"]["details"].get("calls") or {}
    out = {}
    for agent_id, seat in trial["agent_names"].items():
        acts = trial["actions"][agent_id]
        events = acts["events"]
        mcp_ok = count_events(events, "mcp_call_ok", float("inf"))
        shell = count_events(events, "shell_game_cmd", float("inf"))
        server = final_calls.get(seat)
        complete = acts["present"] and server is not None and mcp_ok == int(server)
        out[agent_id] = {
            "trajectory_present": acts["present"],
            "mcp_game_calls_ok": mcp_ok,
            "shell_game_commands": shell,
            "server_game_calls_final": server,
            "complete": complete,
        }
    return out


def trial_flags(trial: dict) -> list[str]:
    flags = []
    times = trial["times"]
    proxy = trial["proxy"]
    if not proxy["complete"]:
        flags.append("proxy_rows_missing_or_invalid")
    if times["holdout_start_events"] != 1:
        flags.append(f"holdout_start_events={times['holdout_start_events']}")
    if times["resume_events"]:
        flags.append("game_clock_resumed:timeline_not_aligned")
    if proxy["non_200"]:
        flags.append(f"proxy_non_200_rows={proxy['non_200']}")
    usd = sum(c["usd"] for c in proxy["calls"])
    if proxy["summary_usd"] is None:
        flags.append("proxy_summary_missing")
    elif abs(usd - proxy["summary_usd"]) > 1e-6 * max(1, len(proxy["calls"])):
        flags.append(f"proxy_rows_vs_summary_usd_delta={usd - proxy['summary_usd']:.9f}")
    if len(trial["agent_names"]) != trial["n_agents"]:
        flags.append("agent_names_count_differs_from_n_agents")
    for agent_id, acts in trial["actions"].items():
        if not acts["present"]:
            flags.append(f"trajectory_missing:{agent_id}")
    for mark in trial["marks"]:
        if mark["reward_mismatch"]:
            flags.append(f"mark_reward_mismatch:{mark['label']}")
        if mark["ended_early"]:
            flags.append(f"ended_early:{mark['label']}@{mark['harness_elapsed_s']}s")
    if trial["final"]["reward"] is None:
        flags.append("verifier_reward_missing")
    if not trial["final"]["frames"]:
        flags.append("replay_frames_missing")
    if not trial["model"]["saved_scores_consistent"]:
        flags.append("saved_scores_decrease")
    if trial["exception"]:
        flags.append(f"exception:{trial['exception']}")
    if trial["timed_out"]:
        flags.append("timed_out")
    if trial["lost_agents"]:
        flags.append("lost_agents")
    for entry in trial["keepalive"].get("timeline") or []:
        if entry.get("op") == "end" and entry.get("exit") not in (0, None):
            flags.append(f"seat_exit:{entry.get('name')}={entry.get('exit')}@{entry.get('t')}s")
    return flags


def exclusion_reason(trial: dict) -> str | None:
    if trial["final"]["reward"] is None:
        return "no verifier reward: unmeasured"
    graded = [m for m in trial["marks"] if m["status"] == "graded" and not m["ended_early"]]
    nominal = [label_seconds(label) for label in trial["grade_at"]]
    first = min((s for s in nominal if s is not None), default=None)
    final = [m for m in trial["marks"] if m["final"]]
    if not graded and final and first is not None:
        elapsed = final[0]["harness_elapsed_s"]
        if elapsed is not None and float(elapsed) < first:
            return f"aborted: run ended at {elapsed} s, before the first scheduled mark ({first:.0f} s)"
    return None


def proxy_totals(proxy: dict, cut: float, seat: str | None) -> dict:
    keys = ("usd", "input", "cache_read", "cache_write", "output")
    totals = dict.fromkeys(keys, 0)
    requests = 0
    complete = proxy["complete"]
    if seat is not None and any(c["seat"] is None for c in proxy["calls"]):
        complete = False
    if not complete:
        return dict.fromkeys(
            ("usd", "input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens", "requests")
        )
    calls = proxy["calls"]
    for call in calls:
        if call["epoch"] > cut or (seat is not None and call["seat"] != seat):
            continue
        requests += 1
        for key in keys:
            totals[key] += call[key]
    return {
        "usd": round(totals["usd"], 9),
        "input_tokens": int(totals["input"]),
        "cached_tokens": int(totals["cache_read"]),
        "cache_write_tokens": int(totals["cache_write"]),
        "output_tokens": int(totals["output"]),
        "requests": requests,
    }


def next_server_total(trial: dict, cut: float, seat: str | None) -> tuple[int | None, str | None]:
    for stamp, calls, source in trial["server_points"]:
        if stamp >= cut:
            if seat is None:
                return sum(int(v) for v in calls.values()), source
            return (None, source) if calls.get(seat) is None else (int(calls[seat]), source)
    return None, None


def agent_actions(trial: dict, agent_id: str, cut: float, server_at_cut: dict | None) -> dict:
    seat = trial["agent_names"][agent_id]
    events = trial["actions"][agent_id]["events"]
    act_ok = count_events(events, "mcp_act_ok", cut)
    mcp_ok = count_events(events, "mcp_call_ok", cut)
    complete = trial["coverage"][agent_id]["complete"]
    upper_total, upper_source = next_server_total(trial, cut, seat)
    upper = None if upper_total is None else act_ok + max(0, upper_total - mcp_ok)
    server_now = None if server_at_cut is None else server_at_cut.get(seat)
    return {
        "act_requests": act_ok if complete or upper == act_ok else None,
        "act_requests_lower": act_ok,
        "act_requests_upper": act_ok if complete else upper,
        "act_upper_source": None if complete else upper_source,
        "mcp_act_failed": count_events(events, "mcp_act_failed", cut),
        "shell_act_mentions": count_events(events, "shell_act_mention", cut),
        "shell_game_commands": count_events(events, "shell_game_cmd", cut),
        "action_coverage": "complete" if complete else "partial",
        "game_calls": None if server_now is None else int(server_now),
    }


def message_counts(trial: dict, cut: float, seat: str | None) -> dict:
    messages = trial["messages"]
    game = [m for m in messages["game"] if m[0] <= cut and (seat is None or m[1] == seat)]
    channels: dict[str, int] = {}
    for _, _, channel in game:
        channels[channel] = channels.get(channel, 0) + 1
    sends = [s for s in messages["board_sends"] if s[0] <= cut and (seat is None or s[1] == seat)]
    known = messages["game_present"] and trial["times"]["game_start"] is not None
    return {
        "game_messages": len(game) if known else None,
        "game_channels": dict(sorted(channels.items())) if known else None,
        "board_sends": len(sends) if messages["board_present"] else None,
    }


def efficiency(score: dict, usd: float | None, output_tokens: int | None) -> dict:
    def ratio(value: float | None, denominator: float | None) -> float | None:
        return None if value is None or not denominator else round(value / denominator, 6)

    kilo = None if output_tokens is None else output_tokens / 1000.0
    return {
        "score_per_usd": ratio(score["value"], usd),
        "score_per_usd_bounds": [ratio(score["lower"], usd), ratio(score["upper"], usd)],
        "score_per_1k_output_tokens": ratio(score["value"], kilo),
        "score_per_1k_output_tokens_bounds": [ratio(score["lower"], kilo), ratio(score["upper"], kilo)],
    }


def cut_metrics(trial: dict, cut: float, score: dict, server_calls: dict | None) -> dict:
    times = trial["times"]
    start = times["agent_started"]
    elapsed = None if start is None else round(cut - start, 3)
    team = proxy_totals(trial["proxy"], cut, None)
    agents = {}
    for agent_id, seat in trial["agent_names"].items():
        row = proxy_totals(trial["proxy"], cut, seat)
        row.update(agent_actions(trial, agent_id, cut, server_calls))
        row.update(message_counts(trial, cut, seat))
        row["seat"] = seat
        row["wall_s"] = elapsed
        row["score"] = None
        agents[agent_id] = row
    acts = list(agents.values())
    team.update({
        "act_requests": None if not acts or any(a["act_requests"] is None for a in acts)
        else sum(a["act_requests"] for a in acts),
        "act_requests_lower": sum(a["act_requests_lower"] for a in acts) if acts else None,
        "act_requests_upper": None if not acts or any(a["act_requests_upper"] is None for a in acts)
        else sum(a["act_requests_upper"] for a in acts),
        "shell_act_mentions": sum(a["shell_act_mentions"] for a in acts) if acts else None,
        "game_calls": None if server_calls is None else sum(int(v) for v in server_calls.values()),
    })
    team.update(message_counts(trial, cut, None))
    game_start = times["game_start"]
    return {
        "cut_epoch": round(cut, 3),
        "cut_utc": iso(cut),
        "wall_s": elapsed,
        "game_wall_s": None if game_start is None else round(cut - game_start, 3),
        "score": score,
        "team": team,
        "efficiency": efficiency(score, team["usd"], team["output_tokens"]),
        "agents": agents,
    }


def exact_score(value: float, source: str, model: dict, cut: float) -> dict:
    check = score_at(model, cut)
    inside = (check["lower"] is None or value >= check["lower"] - 1e-9) and (
        check["upper"] is None or value <= check["upper"] + 1e-9)
    out = score_value(value, value, value, EXACT_DECIMALS, source)
    out["timeline_check"] = "consistent" if inside else "outside_timeline_bounds"
    return out


def mark_cut(trial: dict, label: str, mark: dict, alignment: str) -> dict:
    times = trial["times"]
    if mark["cut_epoch"] is not None:
        cut = mark["cut_epoch"]
    else:
        if times["agent_started"] is None or mark["harness_elapsed_s"] is None:
            return {"label": label, "status": "missing", "nominal_s": label_seconds(label),
                    "reason": "mark time unavailable", "score": mark["reward"]}
        alignment = "harness_elapsed_aligned"
        cut = times["agent_started"] + float(mark["harness_elapsed_s"])
    score = exact_score(float(mark["reward"]), f"mark:{mark['label']}:{mark['reward_source']}",
                        trial["model"], cut)
    row = cut_metrics(trial, cut, score, mark["calls"])
    row.update({
        "label": label,
        "status": "present",
        "alignment": alignment,
        "graded_mark": mark["label"],
        "nominal_s": label_seconds(label),
        "harness_elapsed_s": mark["harness_elapsed_s"],
        "final": mark["final"],
        "frozen": mark["frozen"],
        "game_calls_source": None if mark["calls"] is None else f"marks/{mark['label']}/details.json",
    })
    return row


def end_cut(trial: dict) -> dict:
    cut = trial["times"]["agent_finished"]
    final = trial["final"]
    if cut is None:
        return {"label": "end", "status": "missing", "nominal_s": None,
                "reason": "execution end time unavailable", "score": final["reward"]}
    details = final["details"]
    if final["reward"] is None:
        score = score_value(None, None, None, None, "verifier_reward_missing")
    else:
        score = exact_score(final["reward"], "verifier/reward.txt", trial["model"], cut)
    row = cut_metrics(trial, cut, score, details.get("calls"))
    row.update({
        "label": "end",
        "status": "present" if final["reward"] is not None else "missing",
        "alignment": "agent_execution_end",
        "graded_mark": None,
        "nominal_s": None,
        "harness_elapsed_s": None,
        "final": True,
        "frozen": details.get("frozen"),
        "game_calls_source": "verifier/details.json (written at final grading)",
    })
    return row


def trial_cuts(trial: dict) -> list[dict]:
    rows = []
    graded = {m["label"]: m for m in trial["marks"] if m["status"] == "graded"}
    for label in trial["grade_at"]:
        nominal = label_seconds(label)
        if label in graded and graded[label]["reward"] is not None:
            rows.append(mark_cut(trial, label, graded[label], "score_state_aligned"))
            continue
        rows.append({"label": label, "status": "missing", "nominal_s": nominal})
    rows.append(end_cut(trial))
    return rows


def mean_or_none(values: list[float | None]) -> float | None:
    if not values or any(v is None for v in values):
        return None
    return fmean(float(v) for v in values if v is not None)


def cohort_agents(rows: list[dict]) -> dict:
    slots = sorted({slot for row in rows for slot in row["agents"]})
    result = {}
    for slot in slots:
        seats = [row["agents"].get(slot, {}) for row in rows]
        result[slot] = {
            "n": sum(bool(seat) for seat in seats),
            **{key: rnd(mean_or_none([seat.get(key) for seat in seats]), 9)
               for key in (*COUNTERS, "wall_s", "score")},
        }
    return result


def cohort_label(rows: list[dict]) -> dict:
    present = [r for r in rows if r.get("status") == "present"]
    teams = [r["team"] for r in present]
    scores = [r["score"] for r in present]
    out = {
        "n": len(present),
        "n_missing": len(rows) - len(present),
        "trials": [r["trial_key"] for r in present],
        "score": rnd(mean_or_none([s["value"] for s in scores]), 7),
        "score_bounds": [rnd(mean_or_none([s["lower"] for s in scores]), 7),
                         rnd(mean_or_none([s["upper"] for s in scores]), 7)],
        "wall_s": rnd(mean_or_none([r["wall_s"] for r in present]), 3),
        "game_wall_s": rnd(mean_or_none([r["game_wall_s"] for r in present]), 3),
    }
    out["agents"] = cohort_agents(present)
    for key in COUNTERS:
        out[key] = rnd(mean_or_none([t[key] for t in teams]), 9)
    usd = out["usd"]
    out_k = None if out["output_tokens"] is None else out["output_tokens"] / 1000.0
    out["score_per_usd_ratio_of_means"] = (
        None if out["score"] is None or not usd else round(out["score"] / usd, 6))
    out["score_per_1k_output_ratio_of_means"] = (
        None if out["score"] is None or not out_k else round(out["score"] / out_k, 6))
    return out


def verdict(a: float | None, b: float | None, tolerance: float) -> dict:
    if a is None or b is None:
        return {"verdict": "unknown", "ratio": None, "reason": "missing evidence"}
    low, high = sorted((float(a), float(b)))
    if high == 0:
        return {"verdict": "matched", "ratio": 1.0, "reason": "both zero"}
    if low <= 0:
        return {"verdict": "unmatched", "ratio": None, "reason": "one side zero"}
    ratio = high / low
    state = "matched" if ratio - 1.0 <= tolerance + 1e-12 else "unmatched"
    return {"verdict": state, "ratio": round(ratio, 6), "reason": "larger / smaller - 1 vs tolerance"}


def usage_series(trial: dict, key: str) -> list[tuple[float, float]]:
    total = 0.0
    series = []
    for call in trial["proxy"]["calls"]:
        total += call[key]
        series.append((call["epoch"], total))
    return series


def act_series(trial: dict) -> list[tuple[float, float]] | None:
    if not trial["coverage"] or not all(c["complete"] for c in trial["coverage"].values()):
        return None
    stamps = sorted(
        stamp for acts in trial["actions"].values()
        for stamp, name in acts["events"] if name == "mcp_act_ok"
    )
    return [(stamp, float(i + 1)) for i, stamp in enumerate(stamps)]


def first_crossing(series: list[tuple[float, float]], target: float) -> tuple[float, float] | None:
    return next(((stamp, total) for stamp, total in series if total >= target - 1e-12), None)


def game_call_bracket(trial: dict, target: float) -> dict:
    points = [(stamp, sum(int(v) for v in calls.values()), source)
              for stamp, calls, source in trial["server_points"]]
    below = [p for p in points if p[1] < target]
    above = [p for p in points if p[1] >= target]
    if not above:
        final = points[-1][1] if points else None
        return {"status": "not_reached", "trial_total": final}
    high_point = above[0]
    low_point = below[-1] if below and below[-1][0] <= high_point[0] else None
    model = trial["model"]
    upper = score_at(model, high_point[0])
    lower = score_at(model, low_point[0]) if low_point else upper
    same = lower["value"] is not None and lower["value"] == upper["value"]
    return {
        "status": "bracketed" if low_point else "at_saved_point",
        "between": [low_point[2] if low_point else None, high_point[2]],
        "score": score_value(upper["value"] if same else None, lower["lower"], upper["upper"],
                             upper["decimals"] if same else None, "bracket_between_server_totals"),
    }


def score_at_budget(trial: dict, key: str, target: float) -> dict:
    if key == "game_calls":
        return game_call_bracket(trial, target)
    if key == "act_requests":
        series = act_series(trial)
        if series is None:
            return {"status": "unknown", "reason": "partial action coverage"}
    else:
        if not trial["proxy"]["complete"]:
            return {"status": "unknown", "reason": "missing proxy evidence"}
        series = usage_series(trial, "usd" if key == "usd" else "output")
    start = trial["times"]["agent_started"]
    crossing = (start, 0.0) if target == 0 and start is not None else first_crossing(series, target)
    if crossing is None:
        return {"status": "not_reached", "trial_total": series[-1][1] if series else 0.0}
    stamp, total = crossing
    return {
        "status": "reached",
        "crossing_utc": iso(stamp),
        "crossing_wall_s": None if start is None else round(stamp - start, 3),
        "crossing_budget_total": round(total, 9),
        "score": score_at(trial["model"], stamp),
    }


def mean_scores(results: list[dict]) -> dict:
    reached = [r for r in results if "score" in r]
    if len(reached) != len(results):
        return {"n_reached": len(reached), "score": None, "score_bounds": [None, None]}
    scores = [r["score"] for r in reached]
    return {
        "n_reached": len(reached),
        "score": rnd(mean_or_none([s["value"] for s in scores]), 7),
        "score_bounds": [rnd(mean_or_none([s["lower"] for s in scores]), 7),
                         rnd(mean_or_none([s["upper"] for s in scores]), 7)],
    }


def score_difference(a: dict, b: dict) -> dict:
    value = None if a["score"] is None or b["score"] is None else round(b["score"] - a["score"], 7)
    lows = (a["score_bounds"][1], b["score_bounds"][0])
    highs = (a["score_bounds"][0], b["score_bounds"][1])
    low = None if None in lows else round(lows[1] - lows[0], 7)
    high = None if None in highs else round(highs[1] - highs[0], 7)
    return {"second_minus_first": value, "bounds": [low, high]}


def equal_budget(cohorts: dict, pair: tuple[str, str], label: str, key: str) -> dict:
    first, second = pair
    means = {name: cohorts[name]["labels"][label][key] for name in pair}
    incomplete = any(cohorts[name]["labels"][label]["n_missing"] for name in pair)
    if incomplete or any(v is None for v in means.values()):
        return {"budget": key, "status": "unknown", "reason": "incomplete cohort evidence",
                "means": means}
    reference = min(pair, key=lambda name: (means[name], name))
    target = means[reference]
    sides = {}
    for name in pair:
        results = {}
        for trial in cohorts[name]["members"]:
            results[trial["key"]] = score_at_budget(trial["data"], key, target)
        sides[name] = {"trials": results, **mean_scores(list(results.values()))}
    return {
        "budget": key,
        "status": "evaluated",
        "target": round(target, 9),
        "reference_cohort": reference,
        "reference_mark_mean_score": cohorts[reference]["labels"][label]["score"],
        "note": "every trial is read at its own first crossing of the target; the reference "
        "cohort's mark mean is not a per-trial matched treatment",
        "cohorts": sides,
        "difference": score_difference(sides[first], sides[second]),
    }


def compare_pair(cohorts: dict, pair: tuple[str, str], tolerance: float) -> dict:
    first, second = pair
    labels = [x for x in cohorts[first]["labels"] if x in cohorts[second]["labels"]]
    out = {"pair": list(pair), "labels": {}}
    for label in labels:
        a = cohorts[first]["labels"][label]
        b = cohorts[second]["labels"][label]
        complete = not (a["n_missing"] or b["n_missing"])
        budgets = {
            key: {"first": a[key], "second": b[key],
                  **verdict(a[key] if complete else None, b[key] if complete else None, tolerance)}
            for key in BUDGETS
        }
        out["labels"][label] = {
            "budget_verdicts": budgets,
            "equal_wall_time": {
                "status": budgets["wall_s"]["verdict"],
                "basis": "named marks; end is each trial's own execution cut",
                "first_score": a["score"], "second_score": b["score"],
                "first_bounds": a["score_bounds"], "second_bounds": b["score_bounds"],
                "n": [a["n"], b["n"]],
                "difference": score_difference(a, b),
            },
            "equal_budgets": [equal_budget(cohorts, pair, label, key) for key in EQUAL_BUDGETS],
        }
    return out


def trial_key(trial: dict) -> str:
    return f"{trial['job']}/{trial['trial_name']}"


def build_cohorts(trials: list[dict]) -> dict:
    included = [t for t in trials if t["data"]["exclusion"] is None]
    protocols = sorted({t["data"]["protocol"] for t in included})
    cohorts: dict[str, dict] = {}
    for trial in included:
        data = trial["data"]
        name = data["arm"]
        if len(protocols) > 1:
            name = f"{data['arm']}#p{protocols.index(data['protocol']) + 1}"
        cohort = cohorts.setdefault(name, {"arm": data["arm"], "protocol": data["protocol"],
                                           "members": []})
        cohort["members"].append(trial)
    for cohort in cohorts.values():
        labels: dict[str, list[dict]] = {}
        for trial in cohort["members"]:
            for row in trial["cuts"]:
                labels.setdefault(row["label"], []).append({**row, "trial_key": trial["key"]})
        cohort["labels"] = {label: cohort_label(rows) for label, rows in labels.items()}
    return {"protocols": protocols, "cohorts": dict(sorted(cohorts.items()))}


def trial_summary(trial: dict) -> dict:
    data = trial["data"]
    times = data["times"]
    keep = ("trial_dir", "job", "trial_name", "task_name", "arm", "arm_source", "n_agents",
            "agent_names", "protocol", "harness", "timeout_sec", "grade_at", "budget_usd",
            "agent_budget_usd", "board_policy", "board_plugins", "end_reason", "exclusion",
            "flags", "coverage", "result_cost_usd")
    out = {key: data[key] for key in keep}
    out["model"] = data["model_name"]
    out["key"] = trial["key"]
    out["times"] = {key: iso(value) if key.endswith(("started", "finished", "start")) else value
                    for key, value in times.items()}
    start, end = times["agent_started"], times["agent_finished"]
    out["agent_execution_s"] = None if start is None or end is None else round(end - start, 3)
    out["freeze_utc"] = iso(data["final"]["freeze_epoch"])
    out["proxy"] = {
        "rows": len(data["proxy"]["calls"]),
        "complete": data["proxy"]["complete"],
        "calls": [{k: v for k, v in call.items() if k not in ("order", "epoch")}
                  for call in data["proxy"]["calls"]],
        "rows_usd": round(sum(c["usd"] for c in data["proxy"]["calls"]), 9),
        "summary_usd": data["proxy"]["summary_usd"],
        "non_200_rows": data["proxy"]["non_200"],
        "bad_lines": data["proxy"]["bad_lines"],
    }
    out["score_timeline"] = {
        "saved_points": len(data["model"]["points"]),
        "replay_frames": len(data["final"]["frames"]),
        "score_times": len(data["final"]["score_times"]),
        "monotone": data["model"]["monotone"],
        "monotone_basis": data["model"]["monotone_basis"],
        "live_mark_alignment_s": mark_alignment(data),
    }
    out["marks"] = [{k: v for k, v in m.items() if k != "calls"} for m in data["marks"]]
    out["checkpoints"] = data["checkpoints"]
    out["cuts"] = trial["cuts"]
    return out


def mark_alignment(data: dict) -> dict:
    start = data["times"]["game_start"]
    gaps = {}
    for mark in data["marks"]:
        if start is None or mark["frozen"] or mark["written_at"] is None or mark["game_wall_s"] is None:
            continue
        gaps[mark["label"]] = round(float(mark["written_at"]) - (start + float(mark["game_wall_s"])), 3)
    return gaps


def analyze(paths: list[Path], overrides: dict[str, str], tolerance: float) -> dict:
    assert 1 <= len(paths) <= MAX_TRIALS, len(paths)
    assert 0.0 <= tolerance < 1.0, tolerance
    trials = []
    for path in paths:
        data = load_trial(path, overrides)
        trial = {"data": data, "key": trial_key(data)}
        trial["cuts"] = trial_cuts(data)
        trials.append(trial)
    keys = [t["key"] for t in trials]
    assert len(set(keys)) == len(keys), "the same trial was given twice"
    trials.sort(key=lambda t: t["key"])
    grouped = build_cohorts(trials)
    cohorts = grouped["cohorts"]
    pairs = [p for p in combinations(sorted(cohorts), 2)
             if cohorts[p[0]]["protocol"] == cohorts[p[1]]["protocol"]]
    return {
        "schema": SCHEMA,
        "tolerance": tolerance,
        "inputs": sorted(str(p) for p in paths),
        "sources": SOURCES,
        "conventions": CONVENTIONS,
        "trials": [trial_summary(t) for t in trials],
        "excluded": {t["key"]: t["data"]["exclusion"] for t in trials if t["data"]["exclusion"]},
        "protocols": grouped["protocols"],
        "cohorts": {
            name: {"arm": c["arm"], "protocol": c["protocol"],
                   "trials": [m["key"] for m in c["members"]], "labels": c["labels"]}
            for name, c in cohorts.items()
        },
        "comparisons": [compare_pair(cohorts, pair, tolerance) for pair in pairs],
    }


def fmt_scalar(value: object, digits: int) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def fmt(value: object, digits: int = 4) -> str:
    if isinstance(value, list | tuple):
        return "[" + ",".join(fmt_scalar(item, digits) for item in value) + "]"
    return fmt_scalar(value, digits)


def fmt_score(score: dict) -> str:
    if score["value"] is not None:
        return f"{score['value']:.6f}"
    if score["lower"] is None and score["upper"] is None:
        return "unknown"
    return f"unknown between saved points [{fmt(score['lower'], 6)},{fmt(score['upper'], 6)}]"


def render_cut(row: dict) -> list[str]:
    if row.get("status") != "present":
        return [f"    {row['label']:<5} unknown: {row.get('reason', 'no graded state')}"]
    team = row["team"]
    acts = team["act_requests"]
    act_text = str(acts) if acts is not None else (
        f"[{team['act_requests_lower']},{fmt(team['act_requests_upper'])}]")
    eff = row["efficiency"]
    lines = [
        f"    {row['label']:<5} {row['alignment']} graded={row['graded_mark']} "
        f"nominal_s={fmt(row['nominal_s'], 0)} harness_elapsed_s={fmt(row['harness_elapsed_s'], 1)} "
        f"wall_s={fmt(row['wall_s'], 1)} game_wall_s={fmt(row['game_wall_s'], 1)}",
        f"          score={fmt_score(row['score'])} ({row['score']['source']}) "
        f"usd={fmt(team['usd'], 6)} in={fmt(team['input_tokens'])} cached={fmt(team['cached_tokens'])} "
        f"cache_write={fmt(team['cache_write_tokens'])} out={fmt(team['output_tokens'])} "
        f"req={fmt(team['requests'])}",
        f"          act_requests={act_text} shell_act_mentions={team['shell_act_mentions']} "
        f"game_calls={fmt(team['game_calls'])} game_msgs={fmt(team['game_messages'])} "
        f"board_sends={fmt(team['board_sends'])} score/usd={fmt(eff['score_per_usd'])} "
        f"score/1k_out={fmt(eff['score_per_1k_output_tokens'])}",
    ]
    for agent_id, agent in row["agents"].items():
        agent_acts = agent["act_requests"]
        agent_act_text = str(agent_acts) if agent_acts is not None else (
            f"[{agent['act_requests_lower']},{fmt(agent['act_requests_upper'])}]")
        lines.append(
            f"          {agent_id} {agent['seat']}: score=unknown wall_s={fmt(agent['wall_s'], 1)} "
            f"usd={fmt(agent['usd'], 6)} in={fmt(agent['input_tokens'])} "
            f"cached={fmt(agent['cached_tokens'])} cache_write={fmt(agent['cache_write_tokens'])} "
            f"out={fmt(agent['output_tokens'])} req={fmt(agent['requests'])} acts={agent_act_text} "
            f"({agent['action_coverage']}) shell_cmds={agent['shell_game_commands']} "
            f"game_calls={fmt(agent['game_calls'])} game_msgs={fmt(agent['game_messages'])} "
            f"board_sends={fmt(agent['board_sends'])}")
    return lines


def render_trial(trial: dict) -> list[str]:
    times = trial["times"]
    lines = [
        f"TRIAL {trial['key']}",
        f"  arm={trial['arm']} ({trial['arm_source']}) n_agents={trial['n_agents']} "
        f"model={trial['model']} timeout_s={trial['timeout_sec']} marks={trial['grade_at']} "
        f"budget_usd={trial['budget_usd']} agent_budget_usd={fmt(trial['agent_budget_usd'])}",
        f"  agent_execution {times['agent_started']} -> {times['agent_finished']} "
        f"({fmt(trial['agent_execution_s'], 1)} s); game start {fmt(times['game_start'])}; "
        f"  freeze {fmt(trial['freeze_utc'])}; end_reason={trial['end_reason']}",
        f"  proxy rows={trial['proxy']['rows']} usd={trial['proxy']['rows_usd']:.6f} "
        f"summary_usd={fmt(trial['proxy']['summary_usd'], 6)} non_200={trial['proxy']['non_200_rows']}; "
        f"score timeline points={trial['score_timeline']['saved_points']} "
        f"monotone={trial['score_timeline']['monotone']} "
        f"live mark write-vs-state gap s={trial['score_timeline']['live_mark_alignment_s']}",
        f"  flags: {', '.join(trial['flags']) or 'none'}",
        f"  excluded: {trial['exclusion'] or 'no'}",
    ]
    for row in trial["cuts"]:
        lines.extend(render_cut(row))
    lines.append("  checkpoints (separate from marks):")
    for ck in trial["checkpoints"]["rows"]:
        lines.append(
            f"    #{ck['n']} {ck['reason']} created={ck['created']} "
            f"elapsed_agent_s={ck['elapsed_agent_sec']} manifest_usd={ck['manifest_spend_usd']} "
            f"snapshot_usd={fmt(ck['snapshot_usd'], 6)} final_rows_usd_at_created="
            f"{fmt(ck['final_file_usd_at_created'], 6)}")
    if not trial["checkpoints"]["found"]:
        lines.append(f"    not found at {trial['checkpoints']['dir']}")
    return lines


def render_cohorts(report: dict) -> list[str]:
    lines = ["COHORTS (arithmetic mean over trials that have the cut)"]
    for name, cohort in report["cohorts"].items():
        lines.append(f"  {name}: arm={cohort['arm']} n_trials={len(cohort['trials'])} "
                     f"protocol={cohort['protocol']}")
        for label, row in cohort["labels"].items():
            score = row["score"] if row["score"] is not None else row["score_bounds"]
            lines.append(
                f"    {label:<5} n={row['n']} missing={row['n_missing']} score={fmt(score, 6)} "
                f"wall_s={fmt(row['wall_s'], 1)} usd={fmt(row['usd'], 6)} "
                f"in={fmt(row['input_tokens'], 1)} cached={fmt(row['cached_tokens'], 1)} "
                f"cache_write={fmt(row['cache_write_tokens'], 1)} out={fmt(row['output_tokens'], 1)} "
                f"acts={fmt(row['act_requests'], 1)} "
                f"[{fmt(row['act_requests_lower'], 1)},{fmt(row['act_requests_upper'], 1)}] "
                f"game_calls={fmt(row['game_calls'], 1)} game_msgs={fmt(row['game_messages'], 1)} "
                f"board_sends={fmt(row['board_sends'], 1)} "
                f"score/usd={fmt(row['score_per_usd_ratio_of_means'])} "
                f"score/1k_out={fmt(row['score_per_1k_output_ratio_of_means'])}")
            for slot, agent in row["agents"].items():
                metrics = " ".join(f"{key}={fmt(agent[key], 3)}" for key in (*COUNTERS, "wall_s"))
                lines.append(f"      {slot} n={agent['n']} score=unknown {metrics}")
    return lines


def render_equal(entry: dict, first: str, second: str) -> list[str]:
    if entry["status"] != "evaluated":
        return [f"      at equal {entry['budget']}: unknown ({entry['reason']})"]
    lines = [f"      at equal {entry['budget']} = {entry['target']} "
             f"(reference {entry['reference_cohort']}, its mark mean score "
             f"{fmt(entry['reference_mark_mean_score'], 6)}):"]
    for name in (first, second):
        side = entry["cohorts"][name]
        shown = side["score"] if side["score"] is not None else side["score_bounds"]
        parts = []
        for key, result in side["trials"].items():
            if "score" in result:
                parts.append(f"{key.split('/')[0]}={fmt_score(result['score'])}")
            else:
                parts.append(f"{key.split('/')[0]}={result['status']}")
        lines.append(f"        {name}: mean={fmt(shown, 6)} n_reached={side['n_reached']} "
                     f"({'; '.join(parts)})")
    diff = entry["difference"]
    lines.append(f"        {second} - {first} = {fmt(diff['second_minus_first'], 6)} "
                 f"bounds={fmt(diff['bounds'], 6)}")
    return lines


def render_comparisons(report: dict) -> list[str]:
    lines = [f"COMPARISONS (tolerance {report['tolerance']:.3f}; descriptive, no test)"]
    for comp in report["comparisons"]:
        first, second = comp["pair"]
        lines.append(f"  {first} vs {second}")
        for label, block in comp["labels"].items():
            lines.append(f"    {label}:")
            verdicts = [f"{key}={v['verdict']}" + (f"(x{v['ratio']:.3f})" if v["ratio"] else "")
                        for key, v in block["budget_verdicts"].items()]
            lines.append(f"      budgets: {' '.join(verdicts)}")
            wall = block["equal_wall_time"]
            lines.append(
                f"      same-mark scores (wall {wall['status']}): {first}={fmt(wall['first_score'], 6)} "
                f"{second}={fmt(wall['second_score'], 6)} n={wall['n']} "
                f"{second}-{first}={fmt(wall['difference']['second_minus_first'], 6)}")
            for entry in block["equal_budgets"]:
                lines.extend(render_equal(entry, first, second))
    return lines


def render_text(report: dict) -> str:
    lines = [f"{SCHEMA}  tolerance={report['tolerance']}  trials={len(report['trials'])}"]
    lines.append("SOURCES")
    lines.extend(f"  {key}: {value}" for key, value in report["sources"].items())
    lines.append("CONVENTIONS")
    lines.extend(f"  {key}: {value}" for key, value in report["conventions"].items())
    for trial in report["trials"]:
        lines.extend(render_trial(trial))
    lines.append("EXCLUDED")
    lines.extend(f"  {key}: {reason}" for key, reason in report["excluded"].items())
    if not report["excluded"]:
        lines.append("  none")
    lines.append(f"PROTOCOLS {report['protocols']}")
    lines.extend(render_cohorts(report))
    lines.extend(render_comparisons(report))
    return "\n".join(lines) + "\n"


def parse_overrides(values: list[str]) -> dict[str, str]:
    out = {}
    for value in values:
        path, sep, label = value.rpartition("=")
        assert sep and path and label, f"--arm wants DIR=LABEL, got {value!r}"
        out[str(resolve_trial(Path(path)).resolve())] = label
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="matched_budget.py")
    parser.add_argument("trials", nargs="+", type=Path)
    parser.add_argument("--tolerance", type=float, default=0.05)
    parser.add_argument("--arm", action="append", default=[], metavar="DIR=LABEL")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = analyze(args.trials, parse_overrides(args.arm), args.tolerance)
    if args.json:
        sys.stdout.write(json.dumps(report, indent=1, sort_keys=True, allow_nan=False) + "\n")
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
