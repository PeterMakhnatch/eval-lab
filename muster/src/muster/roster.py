"""Rosters and the control matrix (docs/DESIGN.md section 6).

Pure: this module reads task files and builds data. It starts nothing.
"""

from __future__ import annotations

import itertools
import math
import os
import re
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from muster.model import InputError

ORACLE = "oracle"  # Harbor's oracle agent: runs solution/solve.sh in the seat
NOP = "nop"  # Harbor's nop agent: does nothing
SCRIPT_AGENT = "muster.agents.script:ScriptAgent"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
SETUP_SEC = 360.0  # Harbor's default agent setup timeout
NO_AGENT_TIMEOUT_SEC = 3600.0  # stands in for a task without [agent] timeout_sec


def roster_key(assignment: Iterable[tuple[str, str]]) -> str:
    return ",".join(f"{seat}={policy}" for seat, policy in assignment)


@dataclass(frozen=True)
class Roster:
    """One policy for every seat, in seat declaration order."""

    seats: tuple[str, ...]
    policies: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.seats or len(self.seats) != len(self.policies):
            raise InputError("a roster needs one policy for every seat")
        if len(set(self.seats)) != len(self.seats):
            raise InputError(f"a roster names a seat twice: {', '.join(self.seats)}")
        for name in (*self.seats, *self.policies):
            if not NAME.fullmatch(name):
                raise InputError(f"{name!r} is not a seat or policy name (letters, digits, '_', '.', '-')")

    def items(self) -> tuple[tuple[str, str], ...]:
        return tuple(zip(self.seats, self.policies, strict=True))

    def roster_key(self) -> str:
        """ "p1=oracle,p2=nop": safe as a directory name."""
        return roster_key(self.items())


def matrix(seats: Sequence[str], policies: Sequence[str], limit: int = 64) -> list[Roster]:
    """Every roster over the policies. The last seat changes fastest."""
    if not seats or not policies:
        raise InputError("a matrix needs at least one seat and one policy")
    if len(set(policies)) != len(policies):
        raise InputError(f"a policy is listed twice: {', '.join(policies)}")
    count = len(policies) ** len(seats)
    if count > limit:
        raise InputError(
            f"{len(policies)} policies on {len(seats)} seats give {count} rosters, more than the limit "
            f"of {limit}: use fewer policies or raise --limit"
        )
    combinations = itertools.product(policies, repeat=len(seats))
    return [Roster(tuple(seats), combination) for combination in combinations]


def find_policy(name: str, policy_dirs: Sequence[Path]) -> Path:
    """The first existing <dir>/<name>.sh, as an absolute path that keeps the file's name."""
    searched = [Path(directory) / f"{name}.sh" for directory in policy_dirs]
    for path in searched:
        if os.path.isfile(path):
            return Path(os.path.abspath(path))
    raise InputError(f"unknown policy {name!r}: no file {', '.join(map(str, searched))}")


SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def policy_env(items: Sequence[str]) -> dict[str, str]:
    """NAME=VALUE items as a dict, for the scripted policies of a matrix (for example a shorter wait).

    The values are written to the job config, so a name that looks like a secret is refused.
    """
    env: dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or not name.isidentifier():
            raise InputError(f"--policy-env takes NAME=VALUE, not {item!r}")
        if any(word in name.upper() for word in SECRET_WORDS):
            raise InputError(f"--policy-env {name}: a secret must not go into a job config")
        env[name] = value
    return env


def participants(
    roster: Roster, policy_dirs: Sequence[Path], env: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """The Harbor `participants` list of a roster. `env` goes to every oracle and scripted seat."""
    listed: list[dict[str, Any]] = []
    for seat, policy in roster.items():
        if policy in (ORACLE, NOP):
            entry: dict[str, Any] = {"seat": seat, "name": policy}
        else:
            script = find_policy(policy, policy_dirs)
            entry = {"seat": seat, "import_path": SCRIPT_AGENT, "kwargs": {"script": str(script)}}
        if env and policy != NOP:
            entry["env"] = dict(env)
        listed.append(entry)
    return listed


def job_config(
    *,
    name: str,
    jobs_dir: Path,
    tasks: Sequence[Path],
    participants: Sequence[dict[str, Any]],
    attempts: int,
    concurrency: int,
    timeout_multiplier: float | None,
) -> dict[str, Any]:
    """A Harbor job config: every task with the same participants, on local Docker, removed after use.

    `timeout_multiplier` scales the agent timeout only; verifier and build timeouts keep their values.
    """
    config: dict[str, Any] = {
        "job_name": name,
        "jobs_dir": str(jobs_dir),
        "n_attempts": attempts,
        "n_concurrent_trials": concurrency,
        "quiet": True,
        "environment": {"type": "docker", "delete": True},
        "participants": list(participants),
        "tasks": [{"path": str(path)} for path in tasks],
    }
    if timeout_multiplier is not None:
        config["agent_timeout_multiplier"] = timeout_multiplier
    return config


@dataclass(frozen=True)
class Task:
    """What muster needs from a task's task.toml."""

    path: Path
    name: str  # [task] name, else the directory name
    seats: tuple[str, ...]  # [agents.<seat>] tables, in file order
    under_test: tuple[bool, ...]
    oracle_reward: float | None  # [metadata] oracle_reward
    exploits: tuple[str, ...]  # [metadata] exploits
    agent_timeout_sec: float | None
    verifier_timeout_sec: float
    build_timeout_sec: float
    expected_findings: tuple[str, ...] = ()  # [metadata] expected_findings: codes that the design produces

    def trial_budget_sec(self, timeout_multiplier: float | None) -> float:
        """An upper limit for one trial: build, setup, play, verifier and 300 s of slack."""
        play = NO_AGENT_TIMEOUT_SEC if self.agent_timeout_sec is None else self.agent_timeout_sec
        play *= 1.0 if timeout_multiplier is None else timeout_multiplier
        return self.build_timeout_sec + SETUP_SEC + play + self.verifier_timeout_sec + 300.0


def read_task(path: Path) -> Task:
    """The seats and metadata of a seat task directory."""
    path = Path(os.path.abspath(path))
    try:
        data = tomllib.loads((path / "task.toml").read_text(encoding="utf-8"))
    except OSError as error:
        raise InputError(
            f"{path} is not a task directory: cannot read task.toml ({error.strerror})"
        ) from None
    except ValueError as error:
        raise InputError(f"{path / 'task.toml'} is not valid TOML: {error}") from None
    agents = data.get("agents")
    if not isinstance(agents, dict) or not agents:
        raise InputError(f"{path} has no [agents.<seat>] table: it is not a seat task")
    under_test = tuple(_table(table).get("under_test", True) for table in agents.values())
    metadata = _table(data.get("metadata"))
    oracle_reward, exploits = metadata.get("oracle_reward"), metadata.get("exploits", [])
    if not all(isinstance(flag, bool) for flag in under_test):
        raise InputError(f"{path / 'task.toml'}: under_test is true or false")
    if oracle_reward is not None and not _is_number(oracle_reward):
        raise InputError(f"{path / 'task.toml'}: [metadata] oracle_reward is a number")
    if not isinstance(exploits, list) or not all(isinstance(name, str) for name in exploits):
        raise InputError(f"{path / 'task.toml'}: [metadata] exploits is a list of policy names")
    expected_findings = metadata.get("expected_findings", [])
    if not isinstance(expected_findings, list) or not all(
        isinstance(code, str) for code in expected_findings
    ):
        raise InputError(f"{path / 'task.toml'}: [metadata] expected_findings is a list of finding codes")
    name = _table(data.get("task")).get("name")
    return Task(
        path=path,
        name=name if isinstance(name, str) and name else path.name,
        seats=tuple(agents),
        under_test=under_test,
        oracle_reward=None if oracle_reward is None else float(oracle_reward),
        exploits=tuple(exploits),
        agent_timeout_sec=_seconds(_table(data.get("agent")).get("timeout_sec"), None),
        verifier_timeout_sec=_seconds(_table(data.get("verifier")).get("timeout_sec"), 600.0),
        build_timeout_sec=_seconds(_table(data.get("environment")).get("build_timeout_sec"), 600.0),
        expected_findings=tuple(expected_findings),
    )


def job_timeout_sec(
    tasks: Sequence[Task], attempts: int, concurrency: int, multiplier: float | None
) -> float:
    """An upper limit for one Harbor job: the trial budgets, spread over the concurrent trials."""
    budgets = [task.trial_budget_sec(multiplier) for task in tasks]
    return sum(budgets) * attempts / concurrency + max(budgets) + 120.0


def task_dirs(path: Path) -> list[Path]:
    """One task directory, or the task directories directly inside a directory."""
    path = Path(path)
    if os.path.isfile(path / "task.toml"):
        return [path]
    try:
        found = sorted(child for child in path.iterdir() if os.path.isfile(child / "task.toml"))
    except OSError as error:
        raise InputError(f"cannot read the directory {path}: {error.strerror}") from None
    if not found:
        raise InputError(f"{path} is not a task directory and holds no task directory")
    return found


def _table(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _seconds(value: Any, default: float | None) -> float | None:
    return float(value) if _is_number(value) else default
