from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import threading
import time
import tomllib
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from muster import roster
from muster.lab import api, hostjob, load, parity
from muster.load import SEAT_NAME, read_json
from muster.model import InputError

PLACEHOLDER = re.compile(r"\{(instruction|home|seat|seatdir|trial|task|world|token)\}")
VALUES = frozenset({"instruction", "home", "seat", "seatdir", "trial", "task", "world", "token"})
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
BASE_ENV = ("PATH", "HOME", "USER", "LANG", "TMPDIR")
RESERVED_ENV = frozenset({"WORLD", "SEAT", "SEAT_TOKEN"})
LOCAL_HOSTS = "127.0.0.1,localhost,::1,[::1]"
AGENT_KEYS = frozenset({"about", "argv", "env", "pass_env"})
MASK = "***"
MAX_ARGV = 256
MAX_INSTRUCTION_BYTES = 128 * 1024
MAX_TRIALS = 10_000
MAX_CONCURRENCY = 16
MAX_PARKED = 1000
STOP_GRACE_SEC = 10.0
POLL_SEC = 0.05
METADATA = ("family", "condition", "facet", "arm", "instance")
INCOMPLETE = "_incomplete"
LOADING = threading.Lock()


class HostError(Exception):
    pass


@dataclass(frozen=True)
class Agent:
    path: Path
    name: str
    about: str
    argv: tuple[str, ...]
    env: dict[str, str]
    pass_env: tuple[str, ...]


@dataclass(frozen=True)
class Trial:
    task: Path
    task_name: str
    name: str
    attempt: int
    seats: tuple[str, ...]
    under_test: tuple[bool, ...]
    timeout_sec: float
    metadata: dict[str, str | None]


def _names(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(name, str) and ENV_NAME.fullmatch(name) for name in value)


def read_agent(path: Path) -> Agent:
    data, error = read_json(Path(path))
    if error:
        raise InputError(f"{path}: {error}")
    if not isinstance(data, dict) or not set(data) <= AGENT_KEYS or "argv" not in data:
        raise InputError(f"{path}: the agent file is an object with argv and optional about, env, pass_env")
    argv, env, pass_env, about = data["argv"], data.get("env", {}), data.get("pass_env", []), data.get("about", "")
    if not isinstance(argv, list) or not 0 < len(argv) <= MAX_ARGV or not all(
        isinstance(item, str) and item for item in argv
    ):
        raise InputError(f"{path}: argv is a list of 1 to {MAX_ARGV} non-empty strings")
    if not isinstance(env, dict) or not _names(list(env)) or not all(isinstance(v, str) for v in env.values()):
        raise InputError(f"{path}: env maps variable names to strings")
    if not _names(pass_env) or not isinstance(about, str):
        raise InputError(f"{path}: pass_env is a list of variable names and about is a string")
    reserved = RESERVED_ENV & (set(env) | set(pass_env))
    if reserved:
        raise InputError(f"{path}: muster sets {', '.join(sorted(reserved))}; remove it from env and pass_env")
    name = Path(path).stem
    if not SEAT_NAME.fullmatch(name):
        raise InputError(f"{path}: the file name is the agent name; use letters, digits, '.', '_' and '-'")
    return Agent(Path(os.path.abspath(path)), name, about, tuple(argv), dict(env), tuple(pass_env))


def _metadata(task: Path) -> dict[str, str | None]:
    data = tomllib.loads((task / "task.toml").read_text(encoding="utf-8"))
    table = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    assert isinstance(table, dict)
    return {key: value if isinstance(value := table.get(key), str) else None for key in METADATA}


def _task_trials(directory: Path, attempts: int, timeout_sec: float | None) -> list[Trial]:
    task = roster.read_task(Path(os.path.realpath(directory)))
    try:
        tokens = parity.seat_tokens(task.path)
        metadata = _metadata(task.path)
    except (OSError, ValueError, load.LabError) as error:
        raise InputError(f"{task.path}: not a packaged muster task: {error}") from None
    if set(tokens) != set(task.seats):
        raise InputError(f"{task.path}: the seat tokens do not name the seats of task.toml")
    for seat in task.seats:
        if not (task.path / "instructions" / f"{seat}.md").is_file():
            raise InputError(f"{task.path}: no instructions/{seat}.md")
    limit = timeout_sec if timeout_sec is not None else task.agent_timeout_sec
    limit = roster.NO_AGENT_TIMEOUT_SEC if limit is None else limit
    assert limit > 0
    return [
        Trial(task.path, task.name, f"{task.path.parent.name}.{task.path.name}__a{attempt}", attempt,
              task.seats, task.under_test, float(limit), metadata)
        for attempt in range(1, attempts + 1)
    ]


def plan(path: Path, attempts: int, timeout_sec: float | None) -> list[Trial]:
    assert attempts >= 1
    assert timeout_sec is None or timeout_sec > 0
    trials: list[Trial] = []
    for directory in roster.task_dirs(path):
        trials.extend(_task_trials(directory, attempts, timeout_sec))
        if len(trials) > MAX_TRIALS:
            raise InputError(f"more than {MAX_TRIALS} trials")
    if len({trial.name for trial in trials}) != len(trials):
        raise InputError("two tasks have the same directory name")
    assert trials
    return trials


def render(agent: Agent, values: dict[str, str]) -> list[str]:
    assert set(values) == VALUES
    return [PLACEHOLDER.sub(lambda match: values[match[1]], item) for item in agent.argv]


def shown(agent: Agent, values: dict[str, str], tokens: list[str]) -> list[str]:
    argv = render(agent, {**values, "token": MASK, "instruction": "{instruction}"})
    assert not any(token and token in item for token in tokens for item in argv)
    return argv


def _environment(agent: Agent, url: str, seat: str, token: str) -> dict[str, str]:
    env = {name: os.environ[name] for name in (*BASE_ENV, *agent.pass_env) if name in os.environ}
    env |= {"WORLD": url, "SEAT": seat, "SEAT_TOKEN": token, "NO_PROXY": LOCAL_HOSTS, "no_proxy": LOCAL_HOSTS}
    env |= agent.env
    assert env["SEAT_TOKEN"] == token and env["SEAT"] == seat and env["WORLD"] == url
    return env


def _values(trial: Trial, seat: str, seat_dir: Path, home: Path, url: str, token: str) -> dict[str, str]:
    raw = (seat_dir / "instruction.md").read_bytes()
    if len(raw) > MAX_INSTRUCTION_BYTES:
        raise HostError(f"{seat}: the instruction is larger than {MAX_INSTRUCTION_BYTES} bytes")
    return {"instruction": raw.decode("utf-8"), "home": str(home), "seat": seat, "seatdir": str(seat_dir),
            "trial": str(seat_dir.parent), "task": str(trial.task), "world": url, "token": token}


def _signal(process: subprocess.Popen, number: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, number)


def _stop(processes: dict[str, subprocess.Popen]) -> None:
    alive = [process for process in processes.values() if process.poll() is None]
    for process in alive:
        _signal(process, signal.SIGTERM)
    deadline = time.monotonic() + STOP_GRACE_SEC
    for process in alive:
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
    for process in processes.values():
        _signal(process, signal.SIGKILL)
    for process in processes.values():
        process.wait(timeout=STOP_GRACE_SEC)
    assert all(process.returncode is not None for process in processes.values())


def _wait(processes: dict[str, subprocess.Popen], deadline: float, stop: threading.Event,
          ended: dict[str, str]) -> None:
    polls = int(max(0.0, deadline - time.monotonic()) / POLL_SEC) + 2
    for _ in range(polls):
        for seat, process in processes.items():
            if seat not in ended and process.poll() is not None:
                ended[seat] = hostjob.now()
        if len(ended) == len(processes) or stop.is_set() or time.monotonic() >= deadline:
            return
        time.sleep(POLL_SEC)


def _start(agent: Agent, trial: Trial, seat: str, seat_dir: Path, home: Path, url: str,
           tokens: dict[str, str], record: dict[str, Any]) -> subprocess.Popen:
    values = _values(trial, seat, seat_dir, home, url, tokens[seat])
    record["agent"]["argv"][seat] = shown(agent, values, list(tokens.values()))
    with (seat_dir / "agent.stdout").open("wb") as out, (seat_dir / "agent.stderr").open("wb") as err:
        try:
            return subprocess.Popen(render(agent, values), cwd=home,
                                    env=_environment(agent, url, seat, tokens[seat]),
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
        except OSError as error:
            raise HostError(f"{seat}: the agent cannot start: {error}") from error


def _play(agent: Agent, trial: Trial, directory: Path, url: str, tokens: dict[str, str],
          homes: dict[str, Path], stop: threading.Event, record: dict[str, Any]) -> None:
    processes: dict[str, subprocess.Popen] = {}
    ended: dict[str, str] = {}
    started = time.monotonic()
    record["play_started_at"] = hostjob.now()
    try:
        for seat in trial.seats:
            processes[seat] = _start(agent, trial, seat, directory / seat, homes[seat], url, tokens, record)
        _wait(processes, started + trial.timeout_sec, stop, ended)
    finally:
        running = {seat for seat, process in processes.items() if process.poll() is None}
        _stop(processes)
        finished = hostjob.now()
        record["play_finished_at"] = finished
        record["wall_sec"] = round(time.monotonic() - started, 3)
        record["exit_codes"] = {seat: processes[seat].returncode if seat in processes else None
                                for seat in trial.seats}
        record["timed_out"] = {seat: seat in running and not stop.is_set() for seat in trial.seats}
        record["seat_finished_at"] = {seat: ended.get(seat, finished) for seat in trial.seats}
    if stop.is_set():
        raise HostError("interrupted")


def _remote(world: Any) -> Callable[..., tuple[int, dict]]:
    handle = world.handle

    def remote(method: str, path: str, token: str | None, body: object, client: str) -> tuple[int, dict]:
        return handle(method, path, token, body, api.REMOTE)

    world.handle = remote
    return handle


def _requests(records: list[dict], seats: tuple[str, ...]) -> dict[str, dict[str, int]]:
    counts: dict[str, Counter] = {seat: Counter() for seat in seats}
    for event in records:
        if event.get("seat") in counts:
            counts[event["seat"]][str(event.get("status"))] += 1
    return {seat: dict(sorted(count.items())) for seat, count in counts.items()}


def _grade(trial: Trial, directory: Path, events: Path, world: Any, kit: Any, verify: Any,
           record: dict[str, Any]) -> dict[str, float]:
    records = kit.load_events(events)
    if not parity.closed_log(records, world):
        raise HostError("the event log does not end with a successful world close")
    record["requests"] = _requests(records, trial.seats)
    with LOADING:
        rewards = verify.score(records, parity.packaged_instance(trial.task))
    kit.write_rewards(rewards, directory / "verifier/reward.json", events)
    written, error = read_json(directory / "verifier/reward.json")
    if error or not isinstance(written, dict) or "reward" not in written:
        raise HostError(f"the verifier gave no reward: {error}")
    return written


def _episode(agent: Agent, trial: Trial, directory: Path, home_root: Path, tokens: dict[str, str],
             stop: threading.Event, record: dict[str, Any]) -> dict[str, float]:
    events = directory / "artifacts/logs/world/events.jsonl"
    with LOADING:
        world, kit, verify = parity.packaged_world(trial.task, tokens, events)
    handle = _remote(world)
    homes: dict[str, Path] = {}
    try:
        hostjob.prepare_seats(trial, directory, home_root, homes, record)
        with parity.serve_world(world, kit) as url:
            _play(agent, trial, directory, url, tokens, homes, stop, record)
            parity.health(url, time.monotonic() + parity.START_TIMEOUT_SEC)
            status, _ = handle("POST", "/admin/close", None, {}, api.LOOPBACK)
            if status != 200:
                raise HostError(f"the world close returned {status}")
    finally:
        hostjob.collect_homes(directory, homes, list(tokens.values()), record)
    return _grade(trial, directory, events, world, kit, verify, record)


def run_trial(trial: Trial, agent: Agent, out: Path, home_root: Path, stop: threading.Event,
              revision: dict[str, Any]) -> dict[str, Any]:
    directory = out / trial.name
    directory.mkdir()
    record = hostjob.base_record(trial, agent, revision)
    hostjob.write_config(trial, agent, directory)
    tokens = parity.seat_tokens(trial.task)
    rewards = None
    try:
        rewards = _episode(agent, trial, directory, home_root, tokens, stop, record)
    except AssertionError:
        raise
    except Exception as error:
        record["error"] = f"{type(error).__name__}: {error}"
        rewards = None
    record["finished_at"] = hostjob.now()
    record["rewards"] = rewards
    record["reward"] = None if rewards is None else rewards["reward"]
    hostjob.mask_logs(trial, directory, list(tokens.values()))
    hostjob.write_json(directory / "host.json", record)
    if rewards is not None:
        hostjob.write_json(directory / "result.json", hostjob.result(trial, agent, record))
    assert (record["reward"] is None) == (record["error"] is not None)
    return record


def finished(directory: Path) -> dict[str, Any] | None:
    record, error = read_json(directory / "host.json")
    if error or not isinstance(record, dict) or record.get("schema") != hostjob.SCHEMA:
        return None
    if record.get("error") is not None or record.get("reward") is None:
        return None
    return record if (directory / "result.json").is_file() else None


def _park(out: Path, directory: Path) -> Path:
    parked = out / INCOMPLETE
    parked.mkdir(exist_ok=True)
    stamp = hostjob.now().replace(":", "").replace(".", "")
    for index in range(MAX_PARKED):
        target = parked / f"{directory.name}.{stamp}.{index}"
        if not target.exists():
            os.rename(directory, target)
            return target
    raise InputError(f"{parked}: more than {MAX_PARKED} parked copies of {directory.name}")


def sort_out(out: Path, trials: list[Trial]) -> tuple[list[Trial], list[dict[str, Any]], list[Path]]:
    pending: list[Trial] = []
    done: list[dict[str, Any]] = []
    parked: list[Path] = []
    for trial in trials:
        directory = out / trial.name
        if not os.path.lexists(directory):
            pending.append(trial)
            continue
        record = finished(directory)
        if record is not None:
            done.append(record)
            continue
        parked.append(_park(out, directory))
        pending.append(trial)
    assert len(pending) + len(done) == len(trials)
    return pending, done, parked


def run_job(trials: list[Trial], agent: Agent, out: Path, home_root: Path, concurrency: int,
            report: Callable[[dict[str, Any]], None]) -> list[dict[str, Any]]:
    assert 1 <= concurrency <= MAX_CONCURRENCY
    revision = hostjob.revision()
    stop = threading.Event()
    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="muster-host") as pool:
        futures = [pool.submit(run_trial, trial, agent, out, home_root, stop, revision) for trial in trials]
        try:
            for future in as_completed(futures):
                record = future.result()
                records.append(record)
                report(record)
        except BaseException:
            stop.set()
            for future in futures:
                future.cancel()
            raise
    assert len(records) == len(trials)
    return records


def dry_argv(trial: Trial, agent: Agent, out: Path) -> dict[str, list[str]]:
    tokens = parity.seat_tokens(trial.task)
    argv = {}
    for seat in trial.seats:
        seat_dir = out / trial.name / seat
        values = {"instruction": "", "home": "<home>", "seat": seat, "seatdir": str(seat_dir),
                  "trial": str(seat_dir.parent), "task": str(trial.task), "world": "<world>",
                  "token": tokens[seat]}
        argv[seat] = shown(agent, values, list(tokens.values()))
    return argv
