from __future__ import annotations

import contextlib
import json
import math
import os
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tidegauge.atif import read_trajectory
from tidegauge.model import PHASES, Arm, Job, Problem, Progress, Running, Seat, Trial

_KEY_JUNK = re.compile(r"[^a-z0-9_-]+")
SEAT_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")
PULL_LIMIT = re.compile(r"429 Too Many Requests|toomanyrequests|pull rate limit", re.IGNORECASE)
BUILD_FAILED = re.compile(r"build failed|failed to build|failed to solve", re.IGNORECASE)

INVALID_REWARD_EXCEPTIONS = frozenset(
    (
        "EnvironmentStartTimeoutError",
        "SandboxBuildFailedError",
        "AgentSetupTimeoutError",
        "HealthcheckError",
        "SandboxLikelyOutOfMemoryError",
        "MemoryLimitExceededError",
        "SSHAccessError",
        "GKEExecStreamClosedError",
        "GKEPodNotFoundError",
        "RuntimeRequestError",
        "AddTestsDirError",
        "DownloadVerifierDirError",
        "ApiError",
        "ApiRateLimitError",
        "ApiInternalServerError",
        "ApiOverloadedError",
        "ApiConnectionClosedError",
        "ApiResponseStalledError",
        "UnknownApiError",
        "NetworkConnectionError",
        "ApiConnectionError",
        "ApiUsageLimitError",
        "RateLimitError",
        "InternalServerError",
        "ServiceUnavailableError",
        "APIConnectionError",
        "APIError",
        "Timeout",
        "UnsupportedModelParametersError",
        "BadRequestError",
        "UnprocessableEntityError",
        "UnsupportedParamsError",
        "APIResponseValidationError",
        "BudgetExceededError",
        "VerifierTimeoutError",
        "RewardFileNotFoundError",
        "RewardFileEmptyError",
        "VerifierOutputParseError",
        "AgentAuthenticationError",
        "ModelNotFoundError",
        "ApiProviderResourceNotFoundError",
        "AuthenticationError",
        "PermissionDeniedError",
        "NotFoundError",
        "ServiceOperationsUnsupportedError",
        "VercelScopeError",
        "DspyImportError",
        "MissingExtraError",
        "RegradeError",
        "CancelledError",
    )
)

_DEFAULTS: dict[str, Any] = {
    "timeout_multiplier": 1.0,
    "agent_timeout_multiplier": None,
    "verifier_timeout_multiplier": None,
    "agent_setup_timeout_multiplier": None,
    "environment_build_timeout_multiplier": None,
    "agent.override_timeout_sec": None,
    "agent.override_setup_timeout_sec": None,
    "agent.max_timeout_sec": None,
    "environment.override_cpus": None,
    "environment.override_memory_mb": None,
    "environment.override_storage_mb": None,
    "environment.override_gpus": None,
    "verifier.override_timeout_sec": None,
    "verifier.max_timeout_sec": None,
    "verifier.disable": False,
}


def task_key(name: str) -> str:
    base = name.strip().rsplit("/", 1)[-1].lower()
    return _KEY_JUNK.sub("-", base).strip("-") or name


def rekey(arm: Arm, pattern: re.Pattern[str]) -> Arm:
    trials = []
    for trial in arm.trials:
        found = pattern.search(trial.task_name)
        part = found.group(1) if found else None
        trials.append(replace(trial, task=task_key(part) if part else trial.task, key_matched=bool(part)))
    return replace(arm, trials=tuple(trials))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _count(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 0 else None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _get(data: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(data, Mapping):
            return None
        data = data.get(part)
    return data


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rewards(result: Mapping[str, Any]) -> dict[str, float]:
    verifier = _mapping(result.get("verifier_result"))
    raw = verifier.get("rewards")
    if not isinstance(raw, Mapping):
        raw = {"reward": verifier.get("reward")}
    return {str(k): n for k, v in raw.items() if (n := _number(v)) is not None}


def _primary(rewards: Mapping[str, float], reward_key: str | None) -> float | None:
    if reward_key is not None:
        return rewards.get(reward_key)
    if "reward" in rewards:
        return rewards["reward"]
    if len(rewards) == 1:
        return next(iter(rewards.values()))
    return None


def _exceptions(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    found = [result.get("exception_info")]
    for key in ("step_results", "seat_results"):
        records = result.get(key)
        if isinstance(records, list):
            found += [_mapping(record).get("exception_info") for record in records]
    return [e for e in found if isinstance(e, Mapping)]


def _valid(result: Mapping[str, Any], rewards: Mapping[str, float]) -> bool:
    stored = result.get("reward_valid")
    if isinstance(stored, bool):
        return stored
    if not rewards:
        return False
    for exception in _exceptions(result):
        policy = _get(exception, "policy.has_valid_reward")
        if policy is False:
            return False
        if policy is None and exception.get("exception_type") in INVALID_REWARD_EXCEPTIONS:
            return False
    return True


USAGE = ("tokens_in", "tokens_cache", "tokens_out", "cost_usd")


def _usage_of(context: Any) -> list[float | None]:
    keys = ("n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd")
    values = [_number(_mapping(context).get(key)) for key in keys]
    return [v if v is not None and v >= 0 else None for v in values]


def _sum(values: Iterable[float | None]) -> float | None:
    total: float | None = None
    for value in values:
        if value is not None:
            total = (total or 0) + value
    return total


def _usage_fields(totals: Sequence[float | None]) -> dict[str, Any]:
    assert len(totals) == len(USAGE)
    pairs = zip(USAGE, totals, strict=True)
    return {key: v if v is None or key == "cost_usd" else int(v) for key, v in pairs}


def _usage(result: Mapping[str, Any]) -> list[float | None]:
    contexts = [result.get("agent_result")]
    if not isinstance(contexts[0], Mapping) and isinstance(result.get("step_results"), list):
        contexts = [_mapping(step).get("agent_result") for step in result["step_results"]]
    rows = [_usage_of(context) for context in contexts]
    return [_sum(row[index] for row in rows) for index in range(len(USAGE))]


def _identity(info: Mapping[str, Any], config: Mapping[str, Any]) -> tuple[Any, str | None, Any]:
    version = info.get("version")
    return (
        info.get("name") or config.get("name") or config.get("import_path"),
        version if isinstance(version, str) else None,
        config.get("model_name") or _get(info, "model_info.name"),
    )


def _participants(config: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [p for p in config.get("participants") or [] if isinstance(p, Mapping)]


def _exit_code(seat_dir: Path) -> int | None:
    try:
        with open(seat_dir / "agent" / "exit-code.txt", encoding="utf-8") as handle:
            text = handle.read(32).strip()
    except (OSError, ValueError):
        return None
    return int(text) if re.fullmatch(r"-?\d{1,6}", text) else None


def _seat_run(path: Path, record: Mapping[str, Any], config: Mapping[str, Any], model: bool) -> Seat:
    name = record["seat"]
    context = _mapping(record.get("agent_result"))
    metadata = _mapping(context.get("metadata"))
    requests = metadata.get("api_request_times_msec")
    calls = len(requests) if isinstance(requests, list) else None
    if calls is None and model and _number(context.get("n_input_tokens")) == 0:
        calls = 0
    exception = _get(record, "exception_info.exception_type")
    seat = Seat(
        name=name,
        model=model,
        exception=str(exception) if exception else None,
        started_at=_time(_get(record, "agent_execution.started_at")),
        finished_at=_time(_get(record, "agent_execution.finished_at")),
        turns=_count(metadata.get("n_episodes")),
        max_turns=_count(_get(config, "kwargs.max_turns")),
        calls=calls,
    )
    if not SEAT_NAME.fullmatch(name) or name in (".", ".."):
        return seat
    seat_dir = path / "seats" / name
    seat = replace(seat, exit_code=_exit_code(seat_dir))
    if seat.exception != "AgentTimeoutError" and not (model and calls is None):
        return seat
    trajectory = read_trajectory(seat_dir)
    if trajectory is None or trajectory.problem:
        return seat
    return replace(
        seat,
        calls=seat.calls if seat.calls is not None else trajectory.agent_steps,
        agent_steps=trajectory.agent_steps,
        last_step_at=trajectory.last_agent_at or trajectory.last_step_at,
    )


def _team(path: Path, result: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    records = result.get("seat_results")
    records = records if isinstance(records, list) else []
    records = [r for r in records if isinstance(r, Mapping) and isinstance(r.get("seat"), str)]
    if not records:
        return {}
    names = tuple(r["seat"] for r in records)
    configs = {p.get("seat"): p for p in _participants(config)}
    who = [_identity(_mapping(r.get("agent_info")), _mapping(configs.get(r["seat"]))) for r in records]
    usage = [_usage_of(r.get("agent_result")) for r in records]
    used = [w[2] is not None or any(v is not None for v in u) for w, u in zip(who, usage, strict=True)]
    totals: list[float | None] = []
    partial = []
    for index, key in enumerate(USAGE):
        total = _sum(row[index] for row in usage)
        complete = all(row[index] is not None for row, model in zip(usage, used, strict=True) if model)
        totals.append(total if complete else None)
        if total is not None and not complete:
            partial.append(key)

    def roster(index: int) -> str:
        return ",".join(f"{name}={item[index] or '-'}" for name, item in zip(names, who, strict=True))

    errors = {r["seat"]: str(e) for r in records if (e := _get(r, "exception_info.exception_type"))}
    return {
        "agent": roster(0),
        "agent_version": roster(1) if any(w[1] not in (None, "unknown") for w in who) else "unknown",
        "model": roster(2),
        **_usage_fields(totals),
        "seats": names,
        "model_seats": tuple(name for name, model in zip(names, used, strict=True) if model),
        "seat_exceptions": errors,
        "partial_usage": tuple(partial),
        "seat_runs": tuple(
            _seat_run(path, r, _mapping(configs.get(r["seat"])), model)
            for r, model in zip(records, used, strict=True)
        ),
    }


def _conditions(config: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for dotted, default in _DEFAULTS.items():
        value = _get(config, dotted)
        if value is not None and value != default:
            out[dotted] = value
    kwargs = _get(config, "agent.kwargs")
    if seats := _participants(config):
        kwargs = {str(p.get("seat")): p["kwargs"] for p in seats if p.get("kwargs")}
    if isinstance(kwargs, Mapping) and kwargs:
        out["agent.kwargs"] = json.dumps(kwargs, sort_keys=True, default=str)
    return out


def _not_started(result: Mapping[str, Any]) -> str | None:
    if _time(_get(result, "agent_execution.started_at")) is not None:
        return None
    kind = _get(result, "exception_info.exception_type")
    if not kind:
        return None
    message = str(_get(result, "exception_info.exception_message") or "")
    if PULL_LIMIT.search(message):
        return "pull-limit"
    if kind == "SandboxBuildFailedError" or BUILD_FAILED.search(message):
        return "build-failed"
    return f"setup-error {kind}"


def parse_trial(
    path: Path, result: Mapping[str, Any], reward_key: str | None = None, harbor_version: str | None = None
) -> Trial:
    config = _mapping(result.get("config"))
    agent, version, model = _identity(_mapping(result.get("agent_info")), _mapping(config.get("agent")))
    fields: dict[str, Any] = {
        "agent": agent,
        "agent_version": version,
        "model": model,
        **_usage_fields(_usage(result)),
    }
    fields.update(_team(path, result, config))
    task_name = str(
        result.get("task_name") or _get(config, "task.name") or _get(config, "task.path") or path.name
    )
    rewards = _rewards(result)
    phases: dict[str, float] = {}
    for phase in PHASES:
        start, end = _time(_get(result, f"{phase}.started_at")), _time(_get(result, f"{phase}.finished_at"))
        if start and end:
            phases[phase] = (end - start).total_seconds()
    exceptions = _exceptions(result)
    checksum = result.get("task_checksum")
    return Trial(
        path=path,
        trial_name=str(result.get("trial_name") or path.name),
        task=task_key(task_name),
        task_name=task_name,
        task_checksum=checksum if isinstance(checksum, str) else None,
        source=result.get("source") if isinstance(result.get("source"), str) else None,
        environment=_get(config, "environment.type") or _get(config, "environment.import_path"),
        reward=_primary(rewards, reward_key),
        valid=_valid(result, rewards),
        rewards=rewards,
        exception=str(exceptions[0].get("exception_type")) if exceptions else None,
        started_at=_time(result.get("started_at")),
        finished_at=_time(result.get("finished_at")),
        phase_seconds=phases,
        harbor_version=harbor_version,
        conditions=_conditions(config),
        agent_started_at=_time(_get(result, "agent_execution.started_at")),
        agent_finished_at=_time(_get(result, "agent_execution.finished_at")),
        not_started=_not_started(result),
        **fields,
    )


def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _is_trial_result(data: Any) -> bool:
    return isinstance(data, Mapping) and "trial_name" in data and ("task_name" in data or "task_id" in data)


def _is_trial_config(data: Any) -> bool:
    return isinstance(data, Mapping) and "trial_name" in data and "task" in data


def _job(here: Path, result: Any) -> Job | None:
    if not isinstance(result, Mapping) or "n_total_trials" not in result:
        return None
    version = None
    with contextlib.suppress(OSError, ValueError):
        version = _get(_read_json(here / "lock.json"), "harbor.version")
    return Job(
        path=here,
        harbor_version=version if isinstance(version, str) else None,
        n_total_trials=_count(result.get("n_total_trials")),
        n_retries=_count(_get(result, "stats.n_retries")),
    )


def walk(root: Path, reward_key: str | None = None) -> Iterator[Trial | Problem | Job]:
    versions: dict[Path, str | None] = {}
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = sorted(d for d in subdirs if not d.startswith("."))
        here = Path(directory)
        if "result.json" in files:
            try:
                data = _read_json(here / "result.json")
            except (OSError, ValueError, RecursionError) as error:
                yield Problem(here / "result.json", f"unreadable: {type(error).__name__}")
                continue
            if _is_trial_result(data):
                subdirs[:] = []
                try:
                    yield parse_trial(here, data, reward_key, versions.get(here.parent))
                except (TypeError, ValueError, AttributeError) as error:
                    yield Problem(here / "result.json", f"unexpected shape: {error}")
            elif (job := _job(here, data)) is not None:
                versions[here] = job.harbor_version
                yield job
        elif "config.json" in files:
            try:
                data = _read_json(here / "config.json")
            except (OSError, ValueError, RecursionError):
                continue
            if _is_trial_config(data):
                subdirs[:] = []
                yield Problem(here, "trial has no result.json: it did not finish")


def load_arm(path: str | os.PathLike[str], name: str | None = None, reward_key: str | None = None) -> Arm:
    root = Path(path)
    if not root.is_dir():
        raise FileNotFoundError(f"not a directory: {root}")
    trials: list[Trial] = []
    problems: list[Problem] = []
    jobs: list[Job] = []
    for item in walk(root, reward_key):
        if isinstance(item, Trial):
            trials.append(item)
        elif isinstance(item, Job):
            jobs.append(item)
        else:
            problems.append(item)
    return Arm(name or root.resolve().name, tuple(trials), tuple(problems), tuple(jobs))


def parse_arm_spec(spec: str) -> tuple[str | None, str]:
    if "=" in spec and not os.path.exists(spec):
        name, _, path = spec.partition("=")
        return name, path
    return None, spec


def load_arms(specs: list[str], reward_key: str | None = None) -> list[Arm]:
    arms = []
    for spec in specs:
        name, path = parse_arm_spec(spec)
        arms.append(load_arm(path, name, reward_key))
    names = [arm.name for arm in arms]
    if len(set(names)) != len(names):
        arms = [
            Arm(parse_arm_spec(spec)[1], arm.trials, arm.problems, arm.jobs)
            for spec, arm in zip(specs, arms, strict=True)
        ]
    return arms


def split_arms(path: str | os.PathLike[str], reward_key: str | None = None) -> list[Arm]:
    root = Path(path)
    if not root.is_dir():
        raise FileNotFoundError(f"not a directory: {root}")
    arms = []
    for child in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        arm = load_arm(child, reward_key=reward_key)
        if arm.trials:
            arms.append(arm)
    return arms


MAX_SEATS = 1024


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _progress(seat: str, directory: Path) -> Progress:
    trajectory = read_trajectory(directory)
    exited = _exit_code(directory) is not None
    if trajectory is None:
        return Progress(seat, "exited" if exited else "no-trajectory")
    if trajectory.problem:
        return Progress(seat, "exited" if exited else "unreadable")
    last = trajectory.last_agent_at or trajectory.last_step_at
    state = "exited" if exited else "done" if trajectory.marked_complete else "playing"
    return Progress(seat, state, trajectory.agent_steps, last)


def _seat_names(path: Path, config: Mapping[str, Any]) -> list[str]:
    raw = config.get("participants")
    names = [p.get("seat") for p in raw if isinstance(p, Mapping)] if isinstance(raw, list) else []
    if not names and (path / "seats").is_dir():
        with contextlib.suppress(OSError):
            names = sorted(p.name for p in (path / "seats").iterdir() if p.is_dir())
    return [n for n in names if isinstance(n, str) and SEAT_NAME.fullmatch(n) and n not in (".", "..")]


def read_running(path: Path) -> Running:
    config: Mapping[str, Any] = {}
    with contextlib.suppress(OSError, ValueError, RecursionError):
        config = _mapping(_read_json(path / "config.json"))
    stamps = [s for name in ("config.json", "lock.json") if (s := _mtime(path / name)) is not None]
    started = min(stamps) if stamps else _mtime(path)
    names = _seat_names(path, config)[:MAX_SEATS]
    seats = [_progress(name, path / "seats" / name) for name in names] or [_progress("agent", path)]
    assert 1 <= len(seats) <= MAX_SEATS
    return Running(path, started, tuple(seats))
