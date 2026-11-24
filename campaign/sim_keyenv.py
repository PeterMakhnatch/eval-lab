from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import os
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from random import Random
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from campaign import cli, keyenv, keyprobe, plan, runner, spend_cli, spend_keys, system
from campaign.runner import EventLog, Funds, Runner, drive, expand_first_run
from campaign.sim_fixture import (
    SHELL_PID,
    Scenario,
    World,
    plan_data,
    scenario,
    toml_text,
    write_supply,
)
from campaign.sim_spend import Checks
from campaign.system import System

SOURCE = "OPENROUTER_KEY_MUSTER"
OTHER = "OPENROUTER_KEY_OTHER"
MISSING = "OPENROUTER_KEY_MISSING"
OWNER = SHELL_PID + 1
NOW = datetime(2026, 11, 24, 15, tzinfo=UTC)
MUTATIONS = {
    "keyenv-shared-fallback": "keyenv-missing-never-execs",
    "keyenv-keep-other": "keyenv-arm-least-privilege",
    "keyenv-print-argv": "keyenv-no-secret-in-argv-or-artifacts",
    "keyenv-skip-scrub": "keyenv-inherited-keys-scrubbed",
    "keyenv-skip-fixed-limit": "keyenv-unlimited-never-starts",
}


class ExecReplaced(Exception):
    pass


class ArmWorld(World):
    def __init__(self, case: Scenario, root: Path, rng: Random, values: dict[str, str],
                 mode: str, mutation: str | None) -> None:
        super().__init__(case, root, rng)
        self.live_owners.add(OWNER)
        self.values = values
        self.mode = mode
        self.mutation = mutation
        self.argvs: list[Sequence[str]] = []
        self.real = system.RealSystem()
        self.inherited = {**dict.fromkeys((SOURCE, OTHER, keyenv.API_KEY, keyenv.MANAGEMENT_KEY), values[OTHER]),
                          "KEEP": "unrelated"}
        self.before_keys: list[tuple[str, bool]] = []
        self.probe_envs: list[dict[str, str]] = []
        self.provider = FakeProvider(values, 40 if mode == "hidden" else 5, unlimited=mode == "unlimited")
        self._map = keyenv.arm_environment
        self.child_envs: list[dict[str, str]] = []
        self.exit_status = 0

    def _started(self, argv: list[str], env: dict[str, str] | None, role: str) -> dict[str, str]:
        assert argv[:3] == ["keys", "run", "--"] and argv[4] == "-I"
        self.argvs.append(argv)
        inherited = self.inherited if env is None else env
        self.before_keys.append((role, not any(map(keyenv.protected, inherited))))
        return {**inherited, **self.values}

    def _capture(self, argv: list[str], *, env: dict[str, str] | None, **options: Any) -> SimpleNamespace:
        assert options["capture_output"] and not options["check"]
        fixed = argv[5] == str(plan.KEYPROBE_SCRIPT)
        scoped = self._started(argv, env, "limit" if fixed else "balance")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if fixed:
                with (patch.dict(os.environ, scoped, clear=True),
                      patch.object(system, "HTTPSConnection", self.provider.connect)):
                    code = keyprobe.main(argv[6:])
            else:
                code = self._shim(argv, scoped, "balance")
                if code == 0:
                    print(1000 if self.mode == "hidden" else 900)
        return SimpleNamespace(returncode=code, stdout=out.getvalue().encode(), stderr=err.getvalue().encode())

    def capture(self, argv: list[str], timeout: float, owner: int, *,
                scrub_keys: bool = False) -> tuple[int | None, str]:
        with (patch.dict(os.environ, self.inherited, clear=True),
              patch.object(system.subprocess, "run", self._capture)):
            return self.real.capture(argv, timeout, owner, scrub_keys=scrub_keys)

    def _exec(self, role: str, _program: str, argv: Sequence[str], env: Mapping[str, str]) -> None:
        self.argvs.append(argv)
        captures = self.probe_envs if role == "balance" else self.child_envs
        captures.append(dict(env))
        raise ExecReplaced

    def _mapped(self, name: str, values: Mapping[str, str]) -> dict[str, str] | None:
        mapped = self._map(name, values)
        if self.mutation == "keyenv-shared-fallback" and mapped is None:
            return {keyenv.API_KEY: values[keyenv.API_KEY]}
        if self.mutation == "keyenv-keep-other" and mapped is not None:
            mapped[OTHER] = values[OTHER]
        return mapped

    def _shim(self, argv: list[str], env: dict[str, str], role: str) -> int:
        fake_os = SimpleNamespace(environ=env, execvpe=partial(self._exec, role))
        with patch.object(keyenv, "os", fake_os), patch.object(keyenv, "arm_environment", self._mapped):
            try:
                return keyenv.main(argv[6:])
            except ExecReplaced:
                return 0

    def _spawn(self, argv: list[str], *, env: dict[str, str], **options: Any) -> SimpleNamespace:
        scoped = self._started(argv, env, "arm")
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            self.exit_status = self._shim(argv, scoped, "arm")
        options["stdout"].write(stream.getvalue().encode())
        return SimpleNamespace(pid=self.next_pid)

    def spawn(self, argv: list[str], cwd: Path, env: dict[str, str], log: Path, owner: int, *,
              scrub_keys: bool = False) -> int:
        assert cwd == self.root and owner == OWNER
        with (patch.dict(os.environ, self.inherited, clear=True),
              patch.object(system.subprocess, "Popen", self._spawn)):
            return self.real.spawn(argv, cwd, env, log, owner, scrub_keys=scrub_keys)

    def poll(self, pid: int) -> int | None:
        assert pid == self.next_pid
        return self.exit_status


def _plan(case: Scenario, root: Path) -> dict[str, Any]:
    data = plan_data(case, root)
    lane = data["lanes"][0]
    arm = next(arm for arm in data["arms"] if arm["name"] == lane["arms"][0])
    data["lanes"], data["arms"] = [{**lane, "arms": [arm["name"]]}], [{**arm, "pilot": False}]
    data["command"]["argv"][:0] = ["keys", "run", "--"]
    data["money"].pop("key_limit_probe", None)
    data["money"].update(key_env=SOURCE, floor_usd=0, cap_usd=1000,
                         probe=["keys", "run", "--", "fake-balance", keyenv.API_KEY])
    return data


def _check_cli(path: Path, world: ArmWorld) -> tuple[int, str]:
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream), patch.object(cli, "RealSystem", lambda: world):
        status = cli.main(["check", str(path)])
    return status, stream.getvalue()


def _skip_fixed_limit(world: System, active: plan.Plan, owner: int) -> tuple[Funds | None, str]:
    credits, problem = runner._read_probe(world, active.probe, "balance probe", active.probe_timeout_seconds,
                                          owner, none_ok=False, scrub_keys=True)
    return (Funds(credits, None), "") if credits is not None else (None, problem)


def _missing_store(run: Checks, world: ArmWorld, active: plan.Plan, code: int, exit_code: int) -> None:
    run.check(code == exit_code == 1 and not active.ledger.exists() and not world.probe_envs and not world.child_envs,
              "keyenv-missing-refuses-run", "the account shim stops check and run before reserve or arm")
    arm = active.arms[0]
    argv, env = plan.arm_command(active, arm.name, arm.tasks, arm.attempts, active.lanes[0].concurrency)
    probe_code, _ = world.capture(list(active.key_limit_probe), active.probe_timeout_seconds, OWNER, scrub_keys=True)
    run.check(probe_code == 1 and world.provider.requests == 0, "keyenv-missing-no-request",
              "the fixed key probe stops before HTTPS when the store entry is absent")
    arm_log = world.root / "direct-shim.out"
    world.spawn(argv, active.cwd, env, arm_log, OWNER, scrub_keys=True)
    run.check(world.exit_status == 1 and not world.child_envs
              and "missing or empty; arm command refused" in arm_log.read_text(encoding="utf-8"),
              "keyenv-missing-never-execs", "the arm shim refuses a missing store entry without a fallback")


def _bound_limit(run: Checks, world: ArmWorld, active: plan.Plan, code: int, exit_code: int, screen: str) -> None:
    assert world.mode in ("hidden", "unlimited")
    hidden = world.mode == "hidden"
    reason = "balance-low" if hidden else "balance-unreadable"
    run.check(code == exit_code == 1 and not active.ledger.exists() and not world.child_envs and reason in screen,
              "keyenv-hidden-shared-limit-cannot-start" if hidden else "keyenv-unlimited-never-starts",
              "the account probe cannot override a low or missing limit on the selected key")
    run.check(world.provider.requests == 2 and world.provider.correct and world.provider.key_name == SOURCE,
              "keyenv-fixed-probe-selects-lane", "check and run query only the selected key")


def _arm_case(run: Checks, case: Scenario, root: Path, rng: Random, values: dict[str, str],
              mutation: str | None, mode: str) -> list[str]:
    harbor = root / "bin" / "harbor"
    harbor.parent.mkdir(parents=True)
    harbor.touch(mode=0o700)
    write_supply(case, root)
    data = _plan(case, root)
    if mode == "hidden":
        data["money"]["probe"][3:] = ["fake-hidden-balance"]
    path = root / "plan.toml"
    path.write_text(toml_text(data), encoding="utf-8")
    active = plan.load(path)
    world = ArmWorld(case, root, rng, values, mode, mutation)
    render = plan.render_argv

    def exposed(active: plan.Plan, fields: dict[str, str]) -> list[str]:
        return [*render(active, fields), values[SOURCE]]

    screen = io.StringIO()
    events = EventLog(screen, active.state_root / "campaign.log", world.utc)
    campaign = Runner(active, world, OWNER, events, list(active.lanes), expand_first_run)
    scrub = dict if mutation == "keyenv-skip-scrub" else system.scrub_environment
    with (patch.object(plan, "render_argv", exposed if mutation == "keyenv-print-argv" else render),
          patch.object(system, "scrub_environment", scrub),
          patch.object(runner, "read_funds", _skip_fixed_limit if mutation == "keyenv-skip-fixed-limit"
                       else runner.read_funds)):
        code, displayed = _check_cli(path, world)
        exit_code = drive(campaign)
        if mode in ("missing", "empty"):
            _missing_store(run, world, active, code, exit_code)
    if mode == "present":
        command = plan.arm_command(active, active.arms[0].name, active.arms[0].tasks, active.arms[0].attempts,
                                   active.lanes[0].concurrency)[0]
        run.check(code == 0 and command in world.argvs and f"  $ {shlex.join(command)}\n" in displayed,
                  "keyenv-check-matches-spawn", "check and runner use the same arm argv")
        run.check(all(shlex.join(probe) in displayed and world.argvs.count(list(probe)) == 2
                      for probe in (active.probe, active.key_limit_probe)),
                  "keyenv-check-matches-probes", "check shows the exact commands that it and run capture")
        expected = {keyenv.API_KEY: values[SOURCE], "KEEP": "unrelated"}
        run.check(exit_code == 0 and world.child_envs == [{**expected, "FAKE_ARM": active.arms[0].name}],
                  "keyenv-arm-least-privilege", "only the selected key reaches the arm; unrelated env survives")
        run.check(world.probe_envs == [expected, expected] and world.provider.correct,
                  "keyenv-account-probe-maps-key", "only the selected key reaches the account probe")
    elif mode in ("hidden", "unlimited"):
        _bound_limit(run, world, active, code, exit_code, screen.getvalue())
    roles = {"balance", "limit", "arm"} if mode in ("present", "missing", "empty") else {"balance", "limit"}
    run.check(roles == {role for role, _ in world.before_keys} and all(clean for _, clean in world.before_keys),
              "keyenv-inherited-keys-scrubbed", f"{mode}: remove every protected inherited variable before entering keys")
    return [displayed, screen.getvalue(), *(shlex.join(args) for args in world.argvs)]


def _refusals(run: Checks, case: Scenario, root: Path, rng: Random) -> list[str]:
    root.mkdir()
    base = _plan(case, root)
    world = ArmWorld(case, root, rng, {OTHER: "dummy"}, "refused", None)
    edits = [
        ("money.probe prefix", lambda d: d["money"].update(probe=["sh", "-c", "$OPENROUTER_API_KEY"])),
        ("limit probe override", lambda d: d["money"].update(key_limit_probe=["fake-hidden-probe"])),
        ("balance probe lane name", lambda d: d["money"]["probe"].append(SOURCE)),
        ("balance probe management name", lambda d: d["money"]["probe"].append(keyenv.MANAGEMENT_KEY)),
        ("balance probe nested keys", lambda d: d["money"]["probe"].extend(["/bin/keys", "run", "--"])),
        ("balance probe nested keys in shell",
         lambda d: d["money"]["probe"].extend(["-c", "exec keys run -- any"])),
        ("missing keys prefix", lambda d: d["command"].update(argv=base["command"]["argv"][3:])),
        ("nested key reload", lambda d: d["command"]["argv"].__setitem__(slice(3, 3), ["keys", "run", "--"])),
        ("nested key reload through uv",
         lambda d: d["command"]["argv"].__setitem__(slice(3, 3), ["uv", "run", "keys", "run", "--"])),
        ("invalid key name", lambda d: d["money"].update(key_env="OPENROUTER_API_KEY")),
        ("command.env injection", lambda d: d["command"]["env"].update(OPENROUTER_KEY_MUSTER="dummy")),
    ]
    captured = []
    for label, edit in edits:
        data = copy.deepcopy(base)
        edit(data)
        path = root / "plan.toml"
        path.write_text(toml_text(data), encoding="utf-8")
        code, out = _check_cli(path, world)
        run.check(code == 3 and not world.argvs, "keyenv-plan-refuses-before-probe", label)
        captured.append(out)
    return captured


class FakeProvider:
    def __init__(self, values: dict[str, str], usage: int, *, unlimited: bool = False) -> None:
        self.values = values
        self.usage = usage
        self.unlimited = unlimited
        self.requests = 0
        self.correct = True
        self.key_name: str | None = None

    def connect(self, host: str, timeout: float) -> FakeProvider:
        self.correct &= host == "openrouter.ai" and timeout == 10.0
        return self

    def request(self, method: str, path: str, headers: dict[str, str]) -> None:
        self.requests += 1
        self.correct &= method == "GET" and path == "/api/v1/key" and set(headers) == {"Authorization"}
        self.key_name = next((name for name, value in self.values.items()
                              if headers.get("Authorization") == f"Bearer {value}"), None)
        self.correct &= self.key_name in (SOURCE, OTHER, keyenv.API_KEY)

    def getresponse(self) -> FakeProvider:
        return self

    def __enter__(self) -> FakeProvider:
        return self

    def __exit__(self, *unused: object) -> None:
        pass

    def close(self) -> None:
        pass

    status = 200

    def read(self, size: int) -> bytes:
        self.correct &= size == 65_537
        if self.key_name not in (SOURCE, keyenv.API_KEY):
            return json.dumps({"error": self.values[OTHER]}).encode()
        usage = 0 if self.key_name == keyenv.API_KEY else self.usage
        return json.dumps({"data": {"usage": usage, "usage_daily": 1.25, "usage_weekly": 2.5,
                                    "usage_monthly": 4, "limit": None if self.unlimited else 40,
                                    "limit_remaining": None if self.unlimited else 40 - usage,
                                    "ignored": self.values[SOURCE]}}).encode()


def _spend_roots(root: Path) -> Path:
    root.mkdir()
    path = root / "roots.toml"
    records = root / "calls.jsonl"
    records.write_text("\n".join(json.dumps({"ts": "2026-11-23T00:00:00Z", "usd": cost, "model": model,
                                             "provider": provider}) for cost, model, provider in (
        (2.5, "lab/model", "openrouter"), (3.5, "direct-model", "vendor"),
    )) + "\n", encoding="utf-8")
    rows = [{"label": lane, "kind": "call-log", "path": records.name, "key_env": name,
             "time_field": "ts", "usd_field": "usd", "model_field": "model", "provider_field": "provider"}
            for lane, name in (("muster", SOURCE), ("replica", SOURCE), ("absent", MISSING), ("bad-reply", OTHER))]
    path.write_text(toml_text({"roots": rows}), encoding="utf-8")
    return path


def _spend(run: Checks, root: Path, values: dict[str, str], rng: Random) -> list[str]:
    path = _spend_roots(root)
    usage = rng.randrange(6, 40)
    provider = FakeProvider(values, usage)
    seam = system.RealSystem()
    out_file = root / "spend.json"
    lookups: list[str] = []

    def lookup(name: str) -> str | None:
        lookups.append(name)
        return values.get(name)

    captures = []
    for flags in ([], ["--probe-keys", "--json", str(out_file), "--lenient"]):
        args = cli.parser().parse_args(["spend", str(path), *flags])
        assert isinstance(args, argparse.Namespace)
        screen = io.StringIO()
        with (
            contextlib.redirect_stdout(screen),
            contextlib.redirect_stderr(screen),
            patch.object(seam, "key_env_value", lookup),
            patch.object(system, "HTTPSConnection", provider.connect),
        ):
            code = spend_cli.run(args, now=NOW, key_system=seam)
        captures.append(screen.getvalue())
        if not flags:
            run.check(not lookups and code == 0 and provider.requests == 0 and "Live OpenRouter" not in captures[-1],
                      "keyenv-spend-offline-default", "no transport call or key table without opt-in")
    report = json.loads(out_file.read_bytes()) if out_file.is_file() else {}
    rows = {row["lane"]: row for row in report.get("key_reconciliation", [])}
    expected = {"usage": str(usage), "usage_daily": "1.25", "usage_weekly": "2.5", "usage_monthly": "4",
                "limit": "40", "limit_remaining": str(40 - usage), "recorded_usd": "5.0",
                "combined_remainder_usd": str(usage - 5) + ".0"}
    table = f"All lanes for key | {SOURCE} | shared | {usage:.6f} | 1.250000 | 2.500000 | 4.000000 | 40.000000 | "
    table += f"{40 - usage:.6f} | 5.000000 | - | {usage - 5:.6f}"
    shared = rows.get(None, {})
    run.check(captures[-1].count(table) == 1 and shared.get("shared") is True
              and all(shared.get(k) == v for k, v in expected.items()),
              "keyenv-spend-shared-counters-once", "one shared row holds the provider counters and the combined remainder")
    run.check(all(rows.get(lane, {}).get("shared") is True and rows[lane].get("recorded_usd") == "2.5"
                  and all(field in rows[lane] and rows[lane][field] is None for field in (
                      *spend_keys.COUNTERS, "remainder_usd", "combined_remainder_usd"))
                  for lane in ("muster", "replica")),
              "keyenv-spend-shared-no-lane-remainder", "per-lane rows show cost, but no provider figures or remainder")
    run.check(lookups == sorted((SOURCE, OTHER, MISSING)) and provider.requests == 2 and provider.correct,
              "keyenv-spend-once-per-name",
              "distinct present names only; fixed HTTPS destination, method, header and timeout")
    run.check(code == 1 and MISSING in captures[-1] and "variable is missing or empty" in captures[-1]
              and "invalid OpenRouter key counters" in captures[-1]
              and all(rows.get(lane, {}).get("usage", "unset") is None for lane in ("absent", "bad-reply")),
              "keyenv-spend-problems-not-zero", "missing key and invalid reply are listed and fail even with --lenient")
    return captures


def _private(run: Checks, root: Path, tokens: tuple[str, ...], captures: list[str]) -> None:
    encoded = tuple(token.encode() for token in tokens)
    private = not any(token in output for token in tokens for output in captures)
    pending = [root]
    count = 0
    for _ in range(1025):
        if not pending:
            break
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                count += 1
                if count > 1024:
                    run.check(False, "keyenv-artifact-walk-bounded", "scratch tree has more than 1024 entries")
                    return
                private &= not any(token in entry.path for token in tokens)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file():
                    payload = Path(entry.path).read_bytes()
                    private &= not any(token in payload for token in encoded)
    run.check(private, "keyenv-no-secret-in-argv-or-artifacts", "all argv, output streams, file names and file bytes")


def replay(run: Checks, seed: int, root: Path, mutation: str | None = None) -> None:
    run.step = "keyenv/arm-and-spend"
    assert mutation is None or mutation in MUTATIONS
    rng = Random(seed ^ 0xAB97EA)
    values = {name: f"sk-sim-{rng.getrandbits(128):032x}" for name in (SOURCE, OTHER, keyenv.API_KEY, keyenv.MANAGEMENT_KEY)}
    tokens = tuple(values.values())
    values["KEEP"] = "unrelated"
    case = scenario(seed)
    case = replace(case, ps_fail_rate=0, preexisting_micro=0, foreign_lines="", churn=False, twin=False)
    captures = []
    modes = {
        "keyenv-print-argv": ("present",),
        "keyenv-keep-other": ("present",),
        "keyenv-shared-fallback": ("missing",),
        "keyenv-skip-scrub": ("present", "missing"),
        "keyenv-skip-fixed-limit": ("hidden", "unlimited"),
    }.get(mutation or "", ())
    for mode in ("present", "missing", "empty", "hidden", "unlimited"):
        scoped = dict(values)
        if mode == "missing":
            del scoped[SOURCE]
        if mode == "empty":
            scoped[SOURCE] = ""
        captures.extend(_arm_case(run, case, root / mode, rng, scoped, mutation if mode in modes else None, mode))
    captures.extend(_refusals(run, case, root / "refused", rng))
    captures.extend(_spend(run, root / "spend", values, rng))
    _private(run, root, tokens, captures)
