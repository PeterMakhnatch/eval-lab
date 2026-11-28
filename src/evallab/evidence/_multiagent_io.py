from __future__ import annotations

import json
import math
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

TrialFormat = Literal["olam_seats", "bespoke_team", "single_agent"]
CostStatus = Literal["recorded", "zero_no_model", "missing", "partial"]
TrajectoryStatus = Literal["present", "missing", "invalid"]

MEMBER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
ATIF_SCHEMA_RE = re.compile(r"^ATIF-v1\.\d+$")
VALID_ATIF_SOURCES = frozenset({"system", "user", "agent", "tool"})
_MAX_JSON_BYTES = 64 * 1024 * 1024
_MAX_JSONL_BYTES = 128 * 1024 * 1024
_MAX_JSONL_LINES = 500_000
_MAX_ATIF_STEPS = 100_000
_MAX_SANITIZE_NODES = 100_000
ATIF_USAGE_PAIRS = (
    ("n_input_tokens", "total_prompt_tokens"),
    ("n_cache_tokens", "total_cached_tokens"),
    ("n_output_tokens", "total_completion_tokens"),
    ("cost_usd", "total_cost_usd"),
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NormalizedToolCall(_StrictModel):
    tool_call_id: str | None = None
    function_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    arguments_text: str = ""


class NormalizedObservation(_StrictModel):
    source_call_id: str | None = None
    content: str = ""
    exit_code: int | None = None


class NormalizedAtifStep(_StrictModel):
    member_id: str
    step_id: int
    source: str
    timestamp: str | None = None
    ts_epoch: float | None = None
    message: str = ""
    reasoning_content: str | None = None
    tool_calls: list[NormalizedToolCall] = Field(default_factory=list)
    observations: list[NormalizedObservation] = Field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    cost_usd: float | None = None


class MemberUsage(_StrictModel):
    n_input_tokens: int | None = None
    n_cache_tokens: int | None = None
    n_output_tokens: int | None = None
    cost_usd: float | None = None
    cost_status: CostStatus
    cost_source: str | None = None
    n_model_requests: int | None = None
    n_proxy_rate_limited: int = 0
    n_proxy_errors: int = 0
    usage_mismatches: list[str] = Field(default_factory=list)


class MemberRecord(_StrictModel):
    member_id: str
    display_name: str | None = None
    role: str | None = None
    under_test: bool | None = None
    agent_name: str | None = None
    harness: str | None = None
    model_name: str | None = None
    model_provider: str | None = None
    ran_model: bool = False
    logs_downloaded: bool | None = None
    trajectory_path: str | None = None
    trajectory_status: TrajectoryStatus = "missing"
    trajectory_issues: list[str] = Field(default_factory=list)
    atif_schema_version: str | None = None
    n_steps: int | None = None
    n_tool_calls: int | None = None
    oracle_log_present: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    exception_type: str | None = None
    exception_message: str | None = None
    exit_code: int | None = None
    stopped: bool | None = None
    lost: bool = False
    usage: MemberUsage


class GradeMarkRecord(_StrictModel):
    mark: str
    mark_s: float | None = None
    elapsed_s: float | None = None
    commit: str | None = None
    reward: float | None = None
    verifier_s: float | None = None
    status: str | None = None
    final: bool = False
    ended_early: bool = False
    mark_dir_present: bool = False


def safe_finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        num = float(value)
    except OverflowError:
        return None
    return num if math.isfinite(num) else None


def safe_fsum(values: list[float]) -> float | None:
    try:
        total = math.fsum(values)
    except OverflowError:
        return None
    return total if math.isfinite(total) else None


def parse_timestamp_epoch(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return safe_finite_float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def epoch_to_iso_utc(value: object) -> tuple[str | None, float | None]:
    epoch = parse_timestamp_epoch(value)
    if epoch is None or not (-62_135_596_800.0 <= epoch <= 253_402_300_799.0):
        return None, None
    try:
        iso = datetime.fromtimestamp(epoch, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None, None
    return iso, epoch


def sanitize_utf8_text(value: str) -> str:
    if value.isascii():
        return value
    try:
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        return value.encode("utf-8", errors="replace").decode("utf-8")


_MAX_SANITIZE_DEPTH = 64


def sanitize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_utf8_text(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if not isinstance(value, (dict, list, tuple)):
        return value
    root: Any = {} if isinstance(value, dict) else []
    stack: list[tuple[Any, Any, int]] = [(value, root, 0)]
    visited = 0

    def clean_item(v: Any, depth: int) -> Any:
        if isinstance(v, str):
            return sanitize_utf8_text(v)
        if isinstance(v, float):
            return v if math.isfinite(v) else None
        if isinstance(v, (dict, list, tuple)):
            if depth + 1 >= _MAX_SANITIZE_DEPTH:
                return None
            child: Any = {} if isinstance(v, dict) else []
            stack.append((v, child, depth + 1))
            return child
        return v

    while stack and visited < _MAX_SANITIZE_NODES:
        src, dst, depth = stack.pop()
        visited += 1
        if isinstance(src, dict):
            for k, v in src.items():
                dst[sanitize_utf8_text(str(k))] = clean_item(v, depth)
        elif isinstance(src, (list, tuple)):
            for v in src:
                dst.append(clean_item(v, depth))
    return root


def as_dict(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def as_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return sanitize_utf8_text(value.strip())
    return None


def as_nonneg_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def as_nonneg_float(value: object) -> float | None:
    num = safe_finite_float(value)
    return num if num is not None and num >= 0.0 else None


def valid_member_name(name: object) -> str | None:
    if isinstance(name, str) and MEMBER_NAME_RE.fullmatch(name):
        return name
    return None


def _reject_constant(_name: str) -> object:
    raise ValueError("non-finite JSON constant")


def parse_finite_json_float(token: str) -> float:
    val = float(token)
    if not math.isfinite(val):
        raise ValueError("non-finite JSON float")
    return val


def safe_regular_file(root: Path, *parts: str) -> Path | None:
    if not parts:
        return None
    for part in parts:
        if not part or part in (".", "..") or "/" in part or "\\" in part or "\x00" in part:
            return None
    cur = root
    try:
        for part in parts[:-1]:
            cur = cur / part
            if not stat.S_ISDIR(cur.lstat().st_mode):
                return None
        cur = cur / parts[-1]
        if stat.S_ISREG(cur.lstat().st_mode) and os.access(cur, os.R_OK):
            return cur
    except (OSError, ValueError):
        return None
    return None


def read_json_object(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if path is None:
        return None, "missing"
    try:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode):
            return None, "not_regular"
        if st.st_size > _MAX_JSON_BYTES:
            return None, "too_large"
        raw = path.read_bytes()
    except OSError:
        return None, "unreadable"
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        return None, "invalid_json"
    if not isinstance(parsed, dict):
        return None, "not_an_object"
    return cast(dict[str, Any], parsed), None


def read_jsonl_records(path: Path | None) -> tuple[list[dict[str, Any]], int]:
    if path is None:
        return [], 0
    rows: list[dict[str, Any]] = []
    errors = 0
    try:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_JSONL_BYTES:
            return [], 1
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh):
                if line_no >= _MAX_JSONL_LINES:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except (ValueError, RecursionError):
                    errors += 1
                    continue
                if isinstance(obj, dict):
                    rows.append(cast(dict[str, Any], obj))
                else:
                    errors += 1
    except OSError:
        return [], 1
    return rows, errors


def _clean_reward_map(raw: object, prefix: str, problems: list[str]) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    cleaned: dict[str, float] = {}
    for k, v in sorted(raw.items()):
        key = sanitize_utf8_text(str(k))
        if isinstance(v, bool):
            problems.append(f"{prefix}:reward_bool:{key}")
        elif not isinstance(v, (int, float)):
            problems.append(f"{prefix}:reward_non_numeric:{key}")
        elif (fv := safe_finite_float(v)) is None:
            problems.append(f"{prefix}:reward_non_finite:{key}")
        elif fv == -1.0:
            problems.append(f"{prefix}:reward_unscored_sentinel:{key}")
        else:
            cleaned[key] = fv
    return cleaned or None


def extract_rewards(
    root: Path, result: dict[str, Any] | None
) -> tuple[dict[str, float] | None, bool | None, str | None, bool, list[str]]:
    problems: list[str] = []
    file_rewards: dict[str, float] | None = None
    reward_json_path = safe_regular_file(root, "verifier", "reward.json")
    reward_txt_path = safe_regular_file(root, "verifier", "reward.txt")
    details_path = safe_regular_file(root, "verifier", "details.json")
    if reward_json_path is not None:
        raw_file, err = read_json_object(reward_json_path)
        if err is not None:
            problems.append(f"reward_file_unreadable:{err}")
        else:
            file_rewards = _clean_reward_map(raw_file, "file", problems)
    elif reward_txt_path is not None:
        try:
            if reward_txt_path.lstat().st_size > 65_536:
                problems.append("reward_file_unreadable:too_large")
            else:
                val = float(reward_txt_path.read_text(encoding="utf-8").strip())
                file_rewards = _clean_reward_map({"reward": val}, "file", problems)
        except (OSError, ValueError, OverflowError):
            problems.append("reward_file_unreadable:reward_txt")
    elif details_path is not None:
        det, derr = read_json_object(details_path)
        if derr is None and det is not None and "reward" in det:
            file_rewards = _clean_reward_map({"reward": det["reward"]}, "details", problems)

    if result is not None:
        rv = result.get("reward_valid")
        reward_valid: bool | None = rv if isinstance(rv, bool) else None
        vr = as_dict(result.get("verifier_result"))
        recorded = (
            _clean_reward_map(vr.get("rewards"), "result", problems)
            if vr.get("rewards") is not None
            else None
        )
        mismatch = file_rewards is not None and recorded is not None and file_rewards != recorded
        if recorded is not None:
            return recorded, reward_valid, "result_json", mismatch, problems
        if file_rewards is not None:
            return file_rewards, reward_valid, "verifier_file", False, problems
        return None, reward_valid, None, False, problems
    if file_rewards is not None:
        return file_rewards, None, "verifier_file", False, problems
    return None, None, None, False, problems


def _extract_message_text(raw: object) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return sanitize_utf8_text(raw)
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                text = item["text"] if item.get("text") is not None else item.get("content")
                parts.append(str(text) if text is not None else json.dumps(item, sort_keys=True))
            else:
                parts.append(str(item))
        return sanitize_utf8_text("\n".join(parts))
    if isinstance(raw, dict):
        return sanitize_utf8_text(json.dumps(raw, sort_keys=True))
    return sanitize_utf8_text(str(raw))


AtifParseResult = tuple[
    TrajectoryStatus,
    str | None,
    list[str],
    str | None,
    list[NormalizedAtifStep],
    dict[str, Any],
    str | None,
]


def _parse_atif_tool_calls(raw_tcs: object, issues: list[str]) -> list[NormalizedToolCall]:
    tool_calls: list[NormalizedToolCall] = []
    if isinstance(raw_tcs, list):
        for tc in raw_tcs:
            if not isinstance(tc, dict):
                issues.append("invalid_tool_call")
                continue
            fn = as_str(tc.get("function_name")) or as_str(tc.get("name")) or "tool"
            raw_args = tc.get("arguments")
            args_dict: dict[str, Any] = {}
            args_text = ""
            if isinstance(raw_args, dict):
                args_dict = cast(dict[str, Any], sanitize_json_value(raw_args))
                args_text = json.dumps(
                    args_dict, sort_keys=True, ensure_ascii=False, allow_nan=False
                )
            elif isinstance(raw_args, str):
                args_text = sanitize_utf8_text(raw_args)
                try:
                    parsed = json.loads(
                        raw_args,
                        parse_constant=_reject_constant,
                        parse_float=parse_finite_json_float,
                    )
                    if isinstance(parsed, dict):
                        args_dict = cast(dict[str, Any], sanitize_json_value(parsed))
                except (ValueError, RecursionError):
                    pass
            tc_id = as_str(tc.get("tool_call_id")) or as_str(tc.get("id"))
            tool_calls.append(
                NormalizedToolCall(
                    tool_call_id=tc_id,
                    function_name=fn,
                    arguments=args_dict,
                    arguments_text=args_text,
                )
            )
    elif raw_tcs is not None:
        issues.append("invalid_tool_calls")
    return tool_calls


def _parse_atif_observations(raw_obs: object) -> list[NormalizedObservation]:
    observations: list[NormalizedObservation] = []
    if isinstance(raw_obs, dict):
        raw_results = raw_obs.get("results")
        if isinstance(raw_results, list):
            for res in raw_results:
                if not isinstance(res, dict):
                    continue
                raw_ec = as_dict(res.get("extra")).get("exit_code")
                observations.append(
                    NormalizedObservation(
                        source_call_id=as_str(res.get("source_call_id")),
                        content=_extract_message_text(res.get("content")),
                        exit_code=raw_ec if type(raw_ec) is int else None,
                    )
                )
        elif "content" in raw_obs or "text" in raw_obs:
            val = raw_obs["content"] if "content" in raw_obs else raw_obs["text"]
            observations.append(
                NormalizedObservation(
                    source_call_id=as_str(raw_obs.get("source_call_id")),
                    content=_extract_message_text(val),
                )
            )
    elif isinstance(raw_obs, list) and (txt := _extract_message_text(raw_obs)):
        observations.append(NormalizedObservation(content=txt))
    elif isinstance(raw_obs, str):
        observations.append(NormalizedObservation(content=sanitize_utf8_text(raw_obs)))
    return observations


def _parse_atif_step(
    member_id: str, idx: int, item: dict[str, Any], prev_id: int | None, issues: list[str]
) -> tuple[NormalizedAtifStep, int]:
    raw_sid = item.get("step_id")
    if type(raw_sid) is int and raw_sid >= 0:
        step_id = raw_sid
    else:
        issues.append("invalid_step_id")
        step_id = idx
    if prev_id is not None and step_id <= prev_id:
        issues.append("non_monotonic_step_id")
    source = item.get("source")
    if not isinstance(source, str) or source not in VALID_ATIF_SOURCES:
        issues.append("invalid_step_source")
        source_str = sanitize_utf8_text(str(source)) if source is not None else "unknown"
    else:
        source_str = source
    metrics = as_dict(item.get("metrics"))
    ts_raw = as_str(item.get("timestamp"))
    step = NormalizedAtifStep(
        member_id=member_id,
        step_id=step_id,
        source=source_str,
        timestamp=ts_raw,
        ts_epoch=parse_timestamp_epoch(ts_raw),
        message=_extract_message_text(item.get("message")),
        reasoning_content=as_str(item.get("reasoning_content")),
        tool_calls=_parse_atif_tool_calls(item.get("tool_calls"), issues),
        observations=_parse_atif_observations(item.get("observation")),
        prompt_tokens=as_nonneg_int(metrics.get("prompt_tokens")),
        completion_tokens=as_nonneg_int(metrics.get("completion_tokens")),
        cached_tokens=as_nonneg_int(metrics.get("cached_tokens")),
        cost_usd=as_nonneg_float(metrics.get("cost_usd")),
    )
    return step, step_id


def parse_atif_file(member_id: str, path: Path | None, root: Path) -> AtifParseResult:
    if path is None:
        return "missing", None, [], None, [], {}, None
    rel = path.relative_to(root).as_posix()
    doc, err = read_json_object(path)
    if err is not None or doc is None:
        return "invalid", rel, [f"trajectory_{err or 'unreadable'}"], None, [], {}, None
    issues: list[str] = []
    version = doc.get("schema_version")
    schema_version = (
        version if isinstance(version, str) and ATIF_SCHEMA_RE.fullmatch(version) else None
    )
    if schema_version is None:
        issues.append("invalid_schema_version")
    if not as_str(doc.get("session_id")):
        issues.append("missing_session_id")
    agent = as_dict(doc.get("agent"))
    if not as_str(agent.get("name")):
        issues.append("invalid_agent")
    traj_model = as_str(agent.get("model_name"))
    raw_steps = doc.get("steps")
    if not isinstance(raw_steps, list):
        bad_issues = sorted({*issues, "invalid_steps"})
        return "invalid", rel, bad_issues, schema_version, [], {}, traj_model

    steps: list[NormalizedAtifStep] = []
    prev_id: int | None = None
    for idx, item in enumerate(raw_steps[:_MAX_ATIF_STEPS], start=1):
        if not isinstance(item, dict):
            issues.append("invalid_step_object")
            continue
        step, prev_id = _parse_atif_step(member_id, idx, item, prev_id, issues)
        steps.append(step)

    final_metrics = as_dict(doc.get("final_metrics"))
    unique_issues = sorted(set(issues))
    fatal = {"invalid_schema_version", "invalid_steps", "invalid_step_object"}
    status: TrajectoryStatus = "invalid" if (fatal & set(unique_issues)) else "present"
    return status, rel, unique_issues, schema_version, steps, final_metrics, traj_model


def _exact_int(v: object) -> int | None:
    if type(v) is int:
        return v
    return int(v) if type(v) is float and math.isfinite(v) and v.is_integer() else None


def usage_mismatches(recorded: dict[str, Any], final_metrics: dict[str, Any]) -> list[str]:
    if not final_metrics:
        return []
    out: list[str] = []
    for k, ak in ATIF_USAGE_PAIRS:
        raw_exp, raw_act = recorded.get(k), final_metrics.get(ak)
        if raw_act is None:
            continue
        if k != "cost_usd":
            ai, ei = _exact_int(raw_act), _exact_int(raw_exp)
            if ai is None or ai < 0 or (raw_exp is not None and (ei is None or ei < 0 or ei != ai)):
                out.append(f"usage_mismatch:{k}")
            continue
        af, ef = as_nonneg_float(raw_act), as_nonneg_float(raw_exp)
        if af is None or (
            raw_exp is not None
            and (ef is None or not math.isclose(ef, af, rel_tol=1e-6, abs_tol=1e-9))
        ):
            out.append(f"usage_mismatch:{k}")
    return out
