from __future__ import annotations

import json
import os
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
from queue import Empty, Queue
from tempfile import TemporaryDirectory
from threading import Thread
from types import ModuleType
from typing import Any

from muster.lab import episode, load
from muster.lab.schedule import RoundRobin, Scripted

TASKS = Path(__file__).resolve().parents[1] / "tasks"
START_TIMEOUT_SEC = 10
SCRIPT_TIMEOUT_SEC = 120
CONCURRENT_TIMEOUT_SEC = 180
STOP_TIMEOUT_SEC = 5
HEALTH_POLLS = 100
MAX_SEATS = 128
MAX_CASES = 10_000
TRANSPORT_FIELDS = frozenset({"t", "port", "address", "client_address", "client"})


@dataclass(frozen=True)
class Case:
    task: Path
    instance: dict
    seats: tuple[str, ...]
    tokens: dict[str, str]
    script: Path


@dataclass(frozen=True)
class Result:
    condition: str
    instance: str
    policy: str
    passed: bool
    detail: str


class ParityError(Exception):
    pass


def seat_tokens(task: Path) -> dict[str, str]:
    prefix = "      SEAT_TOKENS: "
    matches = [line[len(prefix):] for line in (task / "environment/docker-compose.yaml")
               .read_text().splitlines() if line.startswith(prefix)]
    if len(matches) != 1:
        raise load.LabError(f"cannot read seat tokens from {task}")
    by_token = json.loads(json.loads(matches[0]))
    if not isinstance(by_token, dict) or not all(
        isinstance(token, str) and isinstance(seat, str) for token, seat in by_token.items()
    ):
        raise load.LabError(f"invalid seat tokens in {task}")
    tokens = {seat: token for token, seat in by_token.items()}
    if len(tokens) != len(by_token):
        raise load.LabError(f"duplicate seats in tokens of {task}")
    return tokens


def _case(adapter: Any, task: Path, instance: dict, policy: str) -> Case:
    if not (task / "task.toml").is_file():
        raise load.LabError(f"no built task directory: {task}")
    meta = tomllib.loads((task / "task.toml").read_text())
    seats = tuple(sorted(adapter.seats(instance)))
    if not seats or len(set(seats)) != len(seats) or len(seats) > MAX_SEATS:
        raise load.LabError(f"invalid adapter seats for {task}")
    if set(meta["agents"]) != set(seats):
        raise load.LabError(f"packaged seats differ from lab: {task}")
    packaged = json.loads((task / "environment/world/instance.json").read_text())
    if packaged != json.loads(json.dumps(instance)):
        raise load.LabError(f"packaged instance differs from lab: {task}")
    tokens = seat_tokens(task)
    if set(tokens) != set(seats):
        raise load.LabError(f"packaged token seats differ from lab: {task}")
    script = task / "policies" / f"{policy}.sh"
    if not script.is_file():
        raise load.LabError(f"no packaged policy: {script}")
    return Case(task, instance, seats, tokens, script)


def _cases(adapter: Any, root: Path, condition: str | None, count: int, policy: str) -> list[Case]:
    assert count > 0
    conditions = adapter.conditions()
    if not conditions:
        raise load.LabError(f"no conditions for family: {adapter.NAME}")
    if condition is not None and condition not in conditions:
        raise load.LabError(f"unknown condition: {condition}")
    if policy not in adapter.policies() or not policy.replace("_", "").replace("-", "").isalnum():
        raise load.LabError(f"unknown packaged policy: {policy}")
    cases = []
    for cell in [condition] if condition is not None else conditions:
        instances = adapter.instances(cell)[:count]
        if not instances:
            raise load.LabError(f"no instances for condition: {cell}")
        for instance in instances:
            if len(cases) >= MAX_CASES:
                raise load.LabError("parity case limit exceeded")
            task = root / cell / f"{adapter.NAME}-{instance['id']}"
            cases.append(_case(adapter, task, instance, policy))
    return cases


def packaged_world(task: Path, tokens: dict[str, str],
                   events: Path) -> tuple[Any, ModuleType, ModuleType]:
    folder = task / "environment/world"
    try:
        mod = load.module(folder / "world.py", "parity_world")
        kit = mod.worldkit
        classes = {value for value in vars(mod).values() if isinstance(value, type)
                   and value.__module__ == mod.__name__ and issubclass(value, kit.World)}
        if len(classes) != 1:
            raise load.LabError(f"expected one world class in {folder / 'world.py'}")
        instance = json.loads((folder / "instance.json").read_text())
        world_class, = classes
        world = world_class(instance, {token: seat for seat, token in tokens.items()}, events)
        kit.serve = getattr(mod, "serve", kit.serve)
        verify = load.module(task / "tests/verify.py", "parity_verify", siblings=(folder,))
        if not callable(verify.score):
            raise load.LabError(f"no verifier score in {task}")
        return world, kit, verify
    except Exception as error:
        message = f"cannot load packaged world or verifier in {task}: {error}"
        raise load.LabError(message) from error


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def health(url: str, deadline: float) -> None:
    opener = _opener()
    for _ in range(HEALTH_POLLS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            with opener.open(url + "/health", timeout=min(remaining, 0.2)) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    raise ParityError("world did not answer /health within 10 s")


@contextmanager
def serve_world(world: Any, kit: ModuleType) -> Iterator[str]:
    messages: Queue = Queue(maxsize=2)
    serve_fn = kit.serve
    namespace = serve_fn.__globals__
    original = namespace["ThreadingHTTPServer"]

    class LocalServer(original):
        def __init__(self, address, handler):
            host = "::1" if ":" in address[0] else "127.0.0.1"
            super().__init__((host, 0), handler)
            messages.put_nowait(self)

    def serve() -> None:
        try:
            serve_fn(world, port=0)
        except Exception as error:
            messages.put_nowait(error)

    namespace["ThreadingHTTPServer"] = LocalServer
    thread = Thread(target=serve, daemon=True)
    deadline = time.monotonic() + START_TIMEOUT_SEC
    server = None
    thread.start()
    try:
        try:
            server = messages.get(timeout=START_TIMEOUT_SEC)
        except Empty as error:
            raise ParityError("world did not bind within 10 s") from error
        if isinstance(server, Exception):
            raise ParityError(f"world server failed: {server}") from server
        host, port = server.server_address[:2]
        url = f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
        health(url, deadline)
        yield url
    finally:
        namespace["ThreadingHTTPServer"] = original
        if server is not None and not isinstance(server, Exception):
            stop = Thread(target=server.shutdown, daemon=True)
            stop.start()
            stop.join(timeout=STOP_TIMEOUT_SEC)
            thread.join(timeout=STOP_TIMEOUT_SEC)
            if stop.is_alive() or thread.is_alive():
                raise ParityError("world server did not stop")
            server.server_close()


def _script(case: Case, seat: str, url: str, directory: Path) -> None:
    env = {"PATH": os.environ.get("PATH", os.defpath), "WORLD": url, "SEAT": seat,
           "SEAT_TOKEN": case.tokens[seat], "NO_PROXY": "*"}
    try:
        result = subprocess.run(
            ["bash", str(case.script)], cwd=directory, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=SCRIPT_TIMEOUT_SEC, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ParityError(f"{seat}: script exceeded {SCRIPT_TIMEOUT_SEC} s") from error
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-5:]
        raise ParityError(f"{seat}: script exited {result.returncode}: {' | '.join(tail)}")


def _concurrent_scripts(case: Case, url: str, directory: Path) -> None:
    deadline = time.monotonic() + CONCURRENT_TIMEOUT_SEC
    processes: dict[str, subprocess.Popen] = {}
    try:
        for seat in case.seats:
            env = {"PATH": os.environ.get("PATH", os.defpath), "WORLD": url, "SEAT": seat,
                   "SEAT_TOKEN": case.tokens[seat], "NO_PROXY": "*"}
            with (directory / f"{seat}.out").open("w") as log:
                processes[seat] = subprocess.Popen(
                    ["bash", str(case.script)], cwd=directory, env=env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT,
                )
        for seat, process in processes.items():
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                raise ParityError(f"{seat}: concurrent scripts exceeded {CONCURRENT_TIMEOUT_SEC} s") from error
        for seat, process in processes.items():
            if process.returncode != 0:
                tail = (directory / f"{seat}.out").read_text().strip().splitlines()[-5:]
                raise ParityError(f"{seat}: script exited {process.returncode}: {' | '.join(tail)}")
    finally:
        for process in processes.values():
            if process.poll() is None:
                process.kill()
        for process in processes.values():
            process.wait(timeout=STOP_TIMEOUT_SEC)


def close_world(url: str) -> None:
    request = urllib.request.Request(url + "/admin/close", data=b"{}", method="POST")
    with _opener().open(request, timeout=START_TIMEOUT_SEC) as response:
        if response.status != 200:
            raise ParityError(f"world close returned {response.status}")
        response.read()


def closed_log(records: list[dict], world: Any) -> bool:
    return bool(records) and records[-1]["kind"] == "admin.close" and bool(records[-1]["ok"]) \
        and bool(world.closed)


def packaged_instance(task: Path) -> dict:
    return json.loads((task / "environment/world/instance.json").read_text())


def _http_episode(case: Case, *, concurrent: bool = False) -> tuple[list[dict], dict[str, float]]:
    with TemporaryDirectory(prefix="muster-parity-") as tmp:
        directory = Path(tmp)
        events = directory / "events.jsonl"
        world, kit, verify = packaged_world(case.task, case.tokens, events)
        with serve_world(world, kit) as url:
            if concurrent:
                _concurrent_scripts(case, url, directory)
            else:
                for seat in case.seats:
                    _script(case, seat, url, directory)
            close_world(url)
        records = kit.load_events(events)
        if concurrent and not closed_log(records, world):
            raise ParityError("event log does not end with a successful world close")
        return records, verify.score(records, packaged_instance(case.task))


def _normalized(event: dict | None) -> dict | None:
    return None if event is None else {key: value for key, value in event.items()
                                       if key not in TRANSPORT_FIELDS}


def _difference(actual: list[dict], expected: tuple[dict, ...],
                rewards: dict[str, float], reference: dict[str, float]) -> str:
    for index, (left, right) in enumerate(zip_longest(actual, expected), 1):
        lhs, rhs = _normalized(left), _normalized(right)
        if lhs != rhs:
            if lhs is None or rhs is None:
                lengths = f"HTTP {len(actual)}, lab {len(expected)}"
                return f"event {index}: log lengths differ ({lengths})"
            key = next(k for k in sorted(set(lhs) | set(rhs))
                       if k not in lhs or k not in rhs or lhs[k] != rhs[k])
            if key not in lhs or key not in rhs:
                missing = "HTTP" if key not in lhs else "lab"
                return f"event {index}: field {key} missing from {missing}"
            return f"event {index} field {key}: HTTP {lhs[key]!r} != lab {rhs[key]!r}"
    for key in sorted(set(rewards) | set(reference)):
        if key not in rewards or key not in reference or rewards[key] != reference[key]:
            return f"score {key}: HTTP {rewards.get(key)!r} != lab {reference.get(key)!r}"
    return ""


def _concurrent_difference(rewards: dict[str, float], reference: dict[str, float],
                           policy: str) -> str:
    if policy == "oracle" and reference.get("reward") != 1.0:
        return f"lab oracle reward: {reference.get('reward')!r} != 1.0"
    for key in ("reward", "focal", "register_sufficient"):
        if (key == "reward" or key in rewards or key in reference) and (
            key not in rewards or key not in reference or rewards[key] != reference[key]
        ):
            return f"score {key}: HTTP {rewards.get(key)!r} != lab {reference.get(key)!r}"
    return ""


def _check(adapter: Any, case: Case, policy: str, *, concurrent: bool = False) -> Result:
    try:
        actual, rewards = _http_episode(case, concurrent=concurrent)
        lineup = dict.fromkeys(case.seats, policy)
        expected = episode.run(adapter, case.instance, lineup, RoundRobin() if concurrent else Scripted(()))
        if expected.ended != "all_done":
            raise ParityError(f"lab ended with {expected.ended}")
        if concurrent:
            detail = _concurrent_difference(rewards, expected.rewards, policy)
        else:
            detail = _difference(actual, expected.events, rewards, expected.rewards)
    except Exception as error:
        if type(error) is load.LabError:
            raise
        detail = f"{type(error).__name__}: {error}"
    return Result(case.instance["condition"], case.instance["id"], policy, not detail, detail)


def run(family: str, *, condition: str | None, instances: int, policy: str,
        concurrent: bool = False) -> list[Result]:
    root = TASKS / family / "build"
    if family == "toy" or family.startswith("toy:"):
        raise load.LabError("toy is a lab fixture and has no built task directory")
    adapter = load.family(family)
    if not root.is_dir():
        raise load.LabError(f"no built task directory for {family}: {root}")
    try:
        cases = _cases(adapter, root, condition, instances, policy)
    except (OSError, ValueError, KeyError, AssertionError) as error:
        raise load.LabError(f"cannot load tasks for {family}: {error}") from error
    return [_check(adapter, case, policy, concurrent=concurrent) for case in cases]
