from __future__ import annotations

import hashlib
import math
import os
import re
import shlex
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from campaign import keyenv

SCHEMA = "campaign-plan/v1"
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
FIELD = re.compile(r"\{([^{}]*)\}")
ARM_FIELDS = frozenset({"tasks", "out", "arm", "attempts", "concurrency", "harbor", "out_root"})
REQUIRED_FIELDS = ("tasks", "out", "attempts", "concurrency")
MAX_ARMS = 256
MAX_LANES = 32
MAX_PLAN_BYTES = 1_000_000
RERUN_TAG = ".r1-m"
MIN_STOP_GRACE_SECONDS = 180
MAX_BALANCE_USD = 100_000.0
DEFAULT_FOREIGN_WINDOW_HOURS = 6.0
DIRECT_COMMANDS = frozenset(("uv", "muster", "python", "python3"))
KEYENV_SCRIPT = Path(keyenv.__file__).resolve()
KEYPROBE_SCRIPT = KEYENV_SCRIPT.with_name("keyprobe.py")


class PlanError(Exception):
    pass


@dataclass(frozen=True)
class Arm:
    name: str
    tasks: Path
    attempts: int
    reserve_usd: float
    rerun_reserve_usd: float
    expected_usd: float
    pilot: bool


@dataclass(frozen=True)
class Lane:
    name: str
    concurrency: int
    arms: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    path: Path
    sha256: str
    id: str
    output_root: Path
    state_root: Path
    max_trials_at_once: int
    argv: tuple[str, ...]
    cwd: Path
    env: tuple[tuple[str, str], ...]
    harbor_program: Path
    harbor_run_args: tuple[str, ...]
    provider: str
    probe: tuple[str, ...]
    key_limit_probe: tuple[str, ...]
    key_env: str | None
    probe_timeout_seconds: float
    floor_usd: float
    cap_usd: float
    cap_roots: tuple[Path, ...]
    unknown_seat_usd: float
    expected_seats: int
    ledger: Path
    foreign_reserve_window_hours: float
    poll_seconds: float
    max_arm_minutes: float
    stop_grace_seconds: float
    stop_file: Path
    no_reward_streak: int
    detector: tuple[str, ...]
    detector_timeout_seconds: float
    arms: tuple[Arm, ...]
    lanes: tuple[Lane, ...]

    def arm(self, name: str) -> Arm:
        found = [arm for arm in self.arms if arm.name == name]
        assert len(found) == 1, name
        return found[0]

    def largest_reserve_usd(self) -> float:
        largest = max(max(arm.reserve_usd, arm.rerun_reserve_usd) for arm in self.arms)
        assert largest > 0
        return largest

    def lane_of(self, arm: str) -> Lane:
        found = [lane for lane in self.lanes if arm in lane.arms]
        assert len(found) == 1, arm
        return found[0]

    def lane(self, name: str) -> Lane:
        found = [lane for lane in self.lanes if lane.name == name]
        if len(found) != 1:
            raise PlanError(f"the plan has no lane {name!r}")
        return found[0]

    def wrapper_text(self) -> str:
        program = shlex.quote(str(self.harbor_program))
        extra = " ".join(shlex.quote(arg) for arg in self.harbor_run_args)
        return (
            "#!/bin/sh\n"
            'if [ "$1" = run ]; then\n'
            f'  exec {program} "$@" {extra}\n'
            "fi\n"
            f'exec {program} "$@"\n'
        )

    def wrapper_path(self) -> Path:
        digest = hashlib.sha256(self.wrapper_text().encode()).hexdigest()[:12]
        return self.state_root / f"harbor-{digest}.sh"

    def lane_dir(self) -> Path:
        return self.state_root / "lanes"

    def fill_dir(self) -> Path:
        return self.state_root / "fill"


def base_arm(out_name: str) -> str:
    return out_name.split(RERUN_TAG, 1)[0]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render(template: str, values: dict[str, str]) -> str:
    unknown = [name for name in FIELD.findall(template) if name not in values]
    assert not unknown, unknown
    return FIELD.sub(lambda match: values[match.group(1)], template)


def arm_values(plan: Plan, out_name: str, tasks: Path, attempts: int, concurrency: int) -> dict[str, str]:
    assert attempts >= 1 and concurrency >= 1
    return {
        "tasks": str(tasks),
        "out": str(plan.output_root / out_name),
        "arm": out_name,
        "attempts": str(attempts),
        "concurrency": str(concurrency),
        "harbor": str(plan.wrapper_path()),
        "out_root": str(plan.output_root),
    }


def _wrap_keys(argv: tuple[str, ...], source: str) -> tuple[str, ...]:
    return (*argv[:3], sys.executable, "-I", str(KEYENV_SCRIPT), source, "--", *argv[3:])


def render_argv(plan: Plan, values: dict[str, str]) -> list[str]:
    argv = tuple(render(arg, values) for arg in plan.argv)
    return list(_wrap_keys(argv, plan.key_env) if plan.key_env is not None else argv)


def arm_command(
    plan: Plan, out_name: str, tasks: Path, attempts: int, concurrency: int
) -> tuple[list[str], dict[str, str]]:
    values = arm_values(plan, out_name, tasks, attempts, concurrency)
    argv = render_argv(plan, values)
    env = {key: render(value, values) for key, value in plan.env}
    return argv, env


def _where(where: str, key: str) -> str:
    return f"{where}.{key}" if where else key


def _keys(data: Any, where: str, required: set[str], optional: set[str]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise PlanError(f"{where or 'the plan'} must be a table")
    unknown = sorted(set(data) - required - optional)
    if unknown:
        raise PlanError(f"{where or 'the plan'} has unknown keys: {', '.join(unknown)}")
    missing = sorted(required - set(data))
    if missing:
        raise PlanError(f"{where or 'the plan'} misses keys: {', '.join(missing)}")
    return data


def _text(data: dict[str, Any], key: str, where: str) -> str:
    value = data[key]
    if not isinstance(value, str) or not value or value != value.strip():
        raise PlanError(f"{_where(where, key)} must be a non-empty string without outer spaces")
    return value


def _name(data: dict[str, Any], key: str, where: str) -> str:
    value = _text(data, key, where)
    if not NAME.fullmatch(value):
        raise PlanError(f"{_where(where, key)} {value!r} must match {NAME.pattern}")
    return value


def _int(data: dict[str, Any], key: str, where: str, low: int, high: int) -> int:
    value = data[key]
    if type(value) is not int or not low <= value <= high:
        raise PlanError(f"{_where(where, key)} must be an integer from {low} to {high}")
    return value


def _number(data: dict[str, Any], key: str, where: str, low: float, high: float) -> float:
    value = data[key]
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise PlanError(f"{_where(where, key)} must be a number from {low} to {high}")
    return float(value)


def _bool(data: dict[str, Any], key: str, where: str) -> bool:
    value = data[key]
    if type(value) is not bool:
        raise PlanError(f"{_where(where, key)} must be true or false")
    return value


def _expand(value: str) -> str:
    return os.path.expanduser(value) if value.startswith("~/") else value


def _path_value(value: Any, where: str) -> Path:
    if not isinstance(value, str) or not (value.startswith("/") or value.startswith("~/")):
        raise PlanError(f"{where} must be an absolute path or start with ~/")
    return Path(os.path.normpath(_expand(value)))


def _path(data: dict[str, Any], key: str, where: str) -> Path:
    return _path_value(data[key], _where(where, key))


def _argv(data: dict[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = data[key]
    if not isinstance(value, list) or not 1 <= len(value) <= 256:
        raise PlanError(f"{_where(where, key)} must be a list of 1 to 256 strings")
    if not all(isinstance(arg, str) and arg for arg in value):
        raise PlanError(f"{_where(where, key)} must hold non-empty strings")
    return tuple(_expand(arg) for arg in value)


def _fields(texts: tuple[str, ...]) -> set[str]:
    found: set[str] = set()
    for text in texts:
        found.update(FIELD.findall(text))
    return found


def _command(data: Any) -> tuple[tuple[str, ...], Path, tuple[tuple[str, str], ...]]:
    table = _keys(data, "command", {"argv", "cwd"}, {"env"})
    argv = _argv(table, "argv", "command")
    env_table = table.get("env", {})
    if not isinstance(env_table, dict) or len(env_table) > 64:
        raise PlanError("command.env must be a table of at most 64 strings")
    env: list[tuple[str, str]] = []
    for key, value in sorted(env_table.items()):
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) or not isinstance(value, str):
            raise PlanError(f"command.env.{key} must be an upper-case name with a string value")
        env.append((key, _expand(value)))
    texts = argv + tuple(value for _, value in env)
    unknown = sorted(_fields(texts) - ARM_FIELDS)
    if unknown:
        raise PlanError(f"command has unknown fields: {', '.join(unknown)}")
    missing = [name for name in REQUIRED_FIELDS if name not in _fields(argv)]
    if missing:
        raise PlanError(f"command.argv misses fields: {', '.join(missing)}")
    if ("--out", "{out}") not in zip(argv, argv[1:], strict=False) and "--out={out}" not in argv:
        raise PlanError('command.argv must pass the arm folder as "--out", "{out}" or "--out={out}", so that '
                        "the runner can find the processes of an arm by their command line")
    if "harbor" not in _fields(texts):
        raise PlanError("command must pass {harbor} in argv or env, so that the plan fixes the backend")
    return argv, _path(table, "cwd", "command"), tuple(env)


def _harbor(data: Any) -> tuple[Path, tuple[str, ...]]:
    table = _keys(data, "harbor", {"program", "run_args"}, set())
    run_args = table["run_args"]
    if not isinstance(run_args, list) or not all(isinstance(arg, str) and arg for arg in run_args):
        raise PlanError("harbor.run_args must be a list of non-empty strings")
    if len(run_args) > 64 or _fields(tuple(run_args)):
        raise PlanError("harbor.run_args must hold at most 64 fixed strings")
    return _path(table, "program", "harbor"), tuple(run_args)


def _money(data: Any) -> dict[str, Any]:
    required = {"provider", "probe", "probe_timeout_seconds", "floor_usd", "cap_usd", "cap_roots",
                "unknown_seat_usd", "ledger"}
    optional = {"foreign_reserve_window_hours", "key_limit_probe", "expected_seats", "key_env"}
    table = _keys(data, "money", required, optional)
    key_env = table.get("key_env")
    if "key_env" in table and not keyenv.valid_name(key_env):
        raise PlanError("money.key_env must be a name such as OPENROUTER_KEY_MUSTER")
    if key_env is not None and "key_limit_probe" in table:
        raise PlanError("money.key_env forbids money.key_limit_probe; campaign builds the fixed probe")
    roots = table["cap_roots"]
    if not isinstance(roots, list) or not 1 <= len(roots) <= 64:
        raise PlanError("money.cap_roots must be a list of 1 to 64 paths")
    return {
        "provider": _name(table, "provider", "money"),
        "probe": _argv(table, "probe", "money"),
        "key_limit_probe": _argv(table, "key_limit_probe", "money") if "key_limit_probe" in table else (),
        "key_env": key_env,
        "probe_timeout_seconds": _number(table, "probe_timeout_seconds", "money", 1, 600),
        "floor_usd": _number(table, "floor_usd", "money", 0, 1_000_000),
        "cap_usd": _number(table, "cap_usd", "money", 0.01, 1_000_000),
        "cap_roots": tuple(_path_value(root, "money.cap_roots") for root in roots),
        "unknown_seat_usd": _number(table, "unknown_seat_usd", "money", 0.0001, 1_000),
        "expected_seats": _int(table, "expected_seats", "money", 1, 4096) if "expected_seats" in table else 1,
        "ledger": _path(table, "ledger", "money"),
        "foreign_reserve_window_hours": (
            _number(table, "foreign_reserve_window_hours", "money", 0.1, 720)
            if "foreign_reserve_window_hours" in table else DEFAULT_FOREIGN_WINDOW_HOURS
        ),
    }


def _watch(data: Any) -> dict[str, Any]:
    required = {"poll_seconds", "max_arm_minutes", "stop_grace_seconds", "stop_file", "no_reward_streak"}
    table = _keys(data, "watch", required, set())
    return {
        "poll_seconds": _number(table, "poll_seconds", "watch", 1, 600),
        "max_arm_minutes": _number(table, "max_arm_minutes", "watch", 1, 1440),
        "stop_grace_seconds": _number(table, "stop_grace_seconds", "watch", MIN_STOP_GRACE_SECONDS, 1800),
        "stop_file": _path(table, "stop_file", "watch"),
        "no_reward_streak": _int(table, "no_reward_streak", "watch", 1, 100),
    }


def _rerun(data: Any) -> dict[str, Any]:
    table = _keys(data, "rerun", set(), {"detector", "detector_timeout_seconds"})
    if ("detector" in table) != ("detector_timeout_seconds" in table):
        raise PlanError("rerun.detector and rerun.detector_timeout_seconds go together")
    if "detector" not in table:
        return {"detector": (), "detector_timeout_seconds": 0.0}
    detector = _argv(table, "detector", "rerun")
    if _fields(detector) != {"arm_dir"}:
        raise PlanError("rerun.detector must use the field {arm_dir} and no other field")
    return {
        "detector": detector,
        "detector_timeout_seconds": _number(table, "detector_timeout_seconds", "rerun", 1, 3600),
    }


def _arm(data: Any, where: str) -> Arm:
    required = {"name", "tasks", "attempts", "reserve_usd", "expected_usd"}
    table = _keys(data, where, required, {"rerun_reserve_usd", "pilot"})
    reserve = _number(table, "reserve_usd", where, 0.01, 10_000)
    rerun = _number(table, "rerun_reserve_usd", where, 0.01, 10_000) if "rerun_reserve_usd" in table else reserve
    expected = _number(table, "expected_usd", where, 0, 10_000)
    if expected > reserve:
        raise PlanError(f"{where}.expected_usd {expected} is above its reserve_usd {reserve}")
    return Arm(
        name=_name(table, "name", where),
        tasks=_path(table, "tasks", where),
        attempts=_int(table, "attempts", where, 1, 100),
        reserve_usd=reserve,
        rerun_reserve_usd=rerun,
        expected_usd=expected,
        pilot=_bool(table, "pilot", where) if "pilot" in table else False,
    )


def _lane(data: Any, where: str) -> Lane:
    table = _keys(data, where, {"name", "concurrency", "arms"}, set())
    arms = table["arms"]
    if not isinstance(arms, list) or not 1 <= len(arms) <= MAX_ARMS:
        raise PlanError(f"{where}.arms must be a list of 1 to {MAX_ARMS} arm names")
    if not all(isinstance(arm, str) for arm in arms):
        raise PlanError(f"{where}.arms must hold arm names")
    return Lane(_name(table, "name", where), _int(table, "concurrency", where, 1, 256), tuple(arms))


def _tables(data: dict[str, Any], key: str, limit: int) -> list[Any]:
    value = data[key]
    if not isinstance(value, list) or not 1 <= len(value) <= limit:
        raise PlanError(f"{key} must be an array of 1 to {limit} tables")
    return value


def _inside(child: Path, parent: Path) -> bool:
    child, parent = Path(os.path.realpath(child)), Path(os.path.realpath(parent))
    return child == parent or parent in child.parents


def _check_arms_and_lanes(arms: tuple[Arm, ...], lanes: tuple[Lane, ...], total: int) -> None:
    names = [arm.name for arm in arms]
    if len(set(names)) != len(names):
        raise PlanError("arm names must be unique")
    if len({lane.name for lane in lanes}) != len(lanes):
        raise PlanError("lane names must be unique")
    placed = [arm for lane in lanes for arm in lane.arms]
    unknown = sorted(set(placed) - set(names))
    if unknown:
        raise PlanError(f"lanes name unknown arms: {', '.join(unknown)}")
    if len(set(placed)) != len(placed):
        raise PlanError("an arm must be in exactly 1 lane, once")
    if set(placed) != set(names):
        raise PlanError(f"arms in no lane: {', '.join(sorted(set(names) - set(placed)))}")
    at_once = sum(lane.concurrency for lane in lanes)
    if at_once > total:
        raise PlanError(f"the lanes run {at_once} trials at once; max_trials_at_once is {total}")


def _check_places(fields: dict[str, Any]) -> None:
    output, state = fields["output_root"], fields["state_root"]
    if _inside(output, state) or _inside(state, output):
        raise PlanError("output_root and state_root must not contain each other")
    if _inside(fields["ledger"], output) or _inside(fields["ledger"], state):
        raise PlanError("money.ledger must be outside output_root and state_root")
    if not any(_inside(output, root) for root in fields["cap_roots"]):
        raise PlanError("money.cap_roots must cover output_root, or the cap misses this campaign")


def _check_key_env(fields: dict[str, Any], argv: tuple[str, ...], env: tuple[tuple[str, str], ...]) -> None:
    source = fields["key_env"]
    if source is None:
        return
    probe = fields["probe"]
    if len(probe) < 4 or Path(probe[0]).name != "keys" or probe[1:3] != ("run", "--"):
        raise PlanError("money.key_env requires money.probe to start keys run --")
    probe_text = " ".join(probe[3:])
    if keyenv.LANE_PREFIX in probe_text or keyenv.MANAGEMENT_KEY in probe_text:
        raise PlanError("money.probe with money.key_env must use OPENROUTER_API_KEY, not lane or management variables")
    if re.search(r"\bkeys\s+run\b", probe_text):
        raise PlanError("money.key_env forbids another keys run inside money.probe")
    if (len(argv) < 4 or Path(argv[0]).name != "keys" or argv[1:3] != ("run", "--")
            or Path(argv[3]).name not in DIRECT_COMMANDS):
        raise PlanError("money.key_env requires command.argv to start keys run -- followed by uv, muster, "
                        "python or python3 so the keyenv shim runs inside keys")
    if any(Path(arg).name == "keys" and sub == "run" and separator == "--"
           for arg, sub, separator in zip(argv[3:], argv[4:], argv[5:], strict=False)):
        raise PlanError("money.key_env forbids another keys run -- inside the arm command")
    if any(keyenv.protected(name) for name, _ in env):
        raise PlanError("money.key_env requires command.env to omit OpenRouter key settings")
    fields["probe"] = _wrap_keys(probe, source)
    fields["key_limit_probe"] = ("keys", "run", "--", sys.executable, "-I", str(KEYPROBE_SCRIPT), source)


def parse(raw: bytes, path: Path) -> Plan:
    if len(raw) > MAX_PLAN_BYTES:
        raise PlanError(f"{path} is larger than {MAX_PLAN_BYTES} bytes")
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PlanError(f"{path}: {error}") from error
    top = {"schema", "id", "output_root", "state_root", "max_trials_at_once", "command", "harbor",
           "money", "watch", "arms", "lanes"}
    _keys(data, "", top, {"rerun"})
    if data["schema"] != SCHEMA:
        raise PlanError(f"schema must be {SCHEMA!r}")
    argv, cwd, env = _command(data["command"])
    program, run_args = _harbor(data["harbor"])
    fields: dict[str, Any] = {
        "output_root": _path(data, "output_root", ""),
        "state_root": _path(data, "state_root", ""),
        **_money(data["money"]),
        **_watch(data["watch"]),
        **_rerun(data.get("rerun", {})),
    }
    _check_key_env(fields, argv, env)
    _check_places(fields)
    arms = tuple(_arm(item, f"arms[{i}]") for i, item in enumerate(_tables(data, "arms", MAX_ARMS)))
    lanes = tuple(_lane(item, f"lanes[{i}]") for i, item in enumerate(_tables(data, "lanes", MAX_LANES)))
    total = _int(data, "max_trials_at_once", "", 1, 4096)
    _check_arms_and_lanes(arms, lanes, total)
    return Plan(
        path=path, sha256=hashlib.sha256(raw).hexdigest(), id=_name(data, "id", ""),
        max_trials_at_once=total, argv=argv, cwd=cwd, env=env, harbor_program=program,
        harbor_run_args=run_args, arms=arms, lanes=lanes, **fields,
    )


def load(path: Path) -> Plan:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise PlanError(f"cannot read the plan {path}: {error.strerror}") from error
    return parse(raw, path.absolute())
