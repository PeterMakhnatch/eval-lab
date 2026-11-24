from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from random import Random
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from campaign import cli, keyenv, plan, spend_cli, system
from campaign.runner import EventLog, Runner, drive, expand_first_run
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

SOURCE = "OPENROUTER_KEY_MUSTER"
OTHER = "OPENROUTER_KEY_OTHER"
MISSING = "OPENROUTER_KEY_MISSING"
OWNER = SHELL_PID + 1
NOW = datetime(2026, 11, 24, 15, tzinfo=UTC)
MUTATIONS = {
    "keyenv-shared-fallback": "keyenv-missing-never-execs",
    "keyenv-keep-other": "keyenv-arm-least-privilege",
    "keyenv-print-argv": "keyenv-no-secret-in-argv-or-artifacts",
}


class ExecReplaced(Exception):
    pass


class ArmWorld(World):
    def __init__(self, case: Scenario, root: Path, rng: Random, values: dict[str, str], mutation: str | None) -> None:
        super().__init__(case, root, rng)
        self.live_owners.add(OWNER)
        self.values = values
        self.mutation = mutation
        self.argvs: list[Sequence[str]] = []
        self.child_envs: list[dict[str, str]] = []
        self.exit_status = 0

    def capture(self, argv: list[str], timeout: float, owner: int) -> tuple[int | None, str]:
        self.argvs.append(argv)
        return 0, "1000"

    def _exec(self, _program: str, argv: Sequence[str], env: Mapping[str, str]) -> None:
        self.argvs.append(argv)
        self.child_envs.append(dict(env))
        raise ExecReplaced

    def _mapped(self, name: str, values: Mapping[str, str]) -> dict[str, str] | None:
        mapped = self._map(name, values)
        if self.mutation == "keyenv-shared-fallback" and mapped is None:
            return {keyenv.API_KEY: values[keyenv.API_KEY]}
        if self.mutation == "keyenv-keep-other" and mapped is not None:
            mapped[OTHER] = values[OTHER]
        return mapped

    def spawn(self, argv: list[str], cwd: Path, env: dict[str, str], log: Path, owner: int) -> int:
        assert cwd == self.root and owner == OWNER
        self.argvs.append(argv)
        stream = io.StringIO()
        self._map = keyenv.arm_environment
        fake_os = SimpleNamespace(environ={**env, **self.values}, execvpe=self._exec)
        with (
            contextlib.redirect_stderr(stream),
            patch.object(keyenv, "os", fake_os),
            patch.object(keyenv, "arm_environment", self._mapped),
        ):
            try:
                self.exit_status = keyenv.main(argv[6:])
            except ExecReplaced:
                self.exit_status = 0
        log.write_text(stream.getvalue(), encoding="utf-8")
        return self.next_pid

    def poll(self, pid: int) -> int | None:
        assert pid == self.next_pid
        return self.exit_status


def _plan(case: Scenario, root: Path) -> dict[str, Any]:
    data = plan_data(case, root)
    lane = data["lanes"][0]
    arm = next(arm for arm in data["arms"] if arm["name"] == lane["arms"][0])
    data["lanes"], data["arms"] = [{**lane, "arms": [arm["name"]]}], [{**arm, "pilot": False}]
    data["command"]["argv"][:0] = ["keys", "run", "--"]
    data["money"].update(key_env=SOURCE, floor_usd=0, cap_usd=1000,
                         probe=["fake-balance", SOURCE], key_limit_probe=["fake-limit", SOURCE])
    return data


def _check_cli(path: Path, world: ArmWorld) -> tuple[int, str]:
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream), patch.object(cli, "RealSystem", lambda: world):
        status = cli.main(["check", str(path)])
    return status, stream.getvalue()


def _arm_case(run: Checks, case: Scenario, root: Path, rng: Random, values: dict[str, str],
              mutation: str | None, arm_may_exec: bool) -> list[str]:
    harbor = root / "bin" / "harbor"
    harbor.parent.mkdir(parents=True)
    harbor.touch(mode=0o700)
    write_supply(case, root)
    data = _plan(case, root)
    path = root / "plan.toml"
    path.write_text(toml_text(data), encoding="utf-8")
    active = plan.load(path)
    world = ArmWorld(case, root, rng, values, mutation)
    render = plan.render_argv

    def exposed(active: plan.Plan, fields: dict[str, str]) -> list[str]:
        return [*render(active, fields), values[SOURCE]]

    screen = io.StringIO()
    events = EventLog(screen, active.state_root / "campaign.log", world.utc)
    runner = Runner(active, world, OWNER, events, list(active.lanes), expand_first_run)
    with patch.object(plan, "render_argv", exposed if mutation == "keyenv-print-argv" else render):
        code, displayed = _check_cli(path, world)
        exit_code = drive(runner)
    out_cmds = [args for args in world.argvs if args[0] == "keys"]
    run.check(code == 0 and len(out_cmds) == 1 and f"  $ {shlex.join(out_cmds[0])}\n" in displayed,
              "keyenv-check-matches-spawn", "check and runner use the same final argv")
    if arm_may_exec:
        expected = {keyenv.API_KEY: values[SOURCE], "FAKE_ARM": active.arms[0].name, "KEEP": "unrelated"}
        run.check(exit_code == 0 and world.child_envs == [expected], "keyenv-arm-least-privilege",
                  "only the lane key reaches exec under the API key name; unrelated env survives")
    else:
        arm_log = active.output_root / f"{active.arms[0].name}.out"
        diagnostic = arm_log.read_text(encoding="utf-8") if arm_log.is_file() else ""
        run.check(exit_code == 1 and not world.child_envs and "missing or empty; arm command refused" in diagnostic,
                  "keyenv-missing-never-execs", "runner fails without executing an arm command")
    return [displayed, screen.getvalue(), *(shlex.join(args) for args in world.argvs)]


def _refusals(run: Checks, case: Scenario, root: Path, rng: Random) -> list[str]:
    root.mkdir()
    base = _plan(case, root)
    world = ArmWorld(case, root, rng, {}, None)
    edits = [
        ("probe", lambda d: d["money"].update(probe=["sh", "-c", "$OPENROUTER_API_KEY"])),
        ("key_limit_probe", lambda d: d["money"].update(key_limit_probe=["fake", "OPENROUTER_API_KEY"])),
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
    def __init__(self, values: dict[str, str], usage: int) -> None:
        self.values = values
        self.usage = usage
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
        self.correct &= self.key_name in (SOURCE, OTHER)

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
        if self.key_name != SOURCE:
            return json.dumps({"error": self.values[OTHER]}).encode()
        return json.dumps({"data": {"usage": self.usage, "usage_daily": 1.25, "usage_weekly": 2.5,
                                    "usage_monthly": 4, "limit": 40, "limit_remaining": 40 - self.usage,
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
                "limit": "40", "limit_remaining": str(40 - usage), "recorded_usd": "2.5",
                "remainder_usd": str(usage - 2.5)}
    table = f"muster | {SOURCE} | {usage:.6f} | 1.250000 | 2.500000 | 4.000000 | 40.000000 | "
    table += f"{40 - usage:.6f} | 2.500000 | {usage - 2.5:.6f}"
    run.check(table in captures[-1] and all(all(rows.get(lane, {}).get(k) == v for k, v in expected.items())
                                           for lane in ("muster", "replica")),
              "keyenv-spend-counters-match", "text and JSON pair provider counters with recorded OpenRouter cost")
    run.check(lookups == sorted((SOURCE, OTHER, MISSING)) and provider.requests == 2 and provider.correct,
              "keyenv-spend-once-per-name",
              "distinct present names only; fixed HTTPS destination, method, header and timeout")
    run.check(code == 1 and MISSING in captures[-1] and "variable is missing or empty" in captures[-1]
              and "invalid OpenRouter key counters" in captures[-1]
              and all(rows.get(lane, {}).get("usage", "unset") is None for lane in ("absent", "bad-reply")),
              "keyenv-spend-problems-not-zero", "missing key and invalid reply are listed and fail even with --lenient")
    return captures


def _private(run: Checks, root: Path, tokens: tuple[str, ...], captures: list[str]) -> None:
    entries = list(root.rglob("*"))
    assert len(entries) <= 1024
    encoded = tuple(token.encode() for token in tokens)
    private = not any(token in output for token in tokens for output in captures)
    for path in entries:
        private &= not any(token in str(path) for token in tokens)
        if path.is_file():
            payload = path.read_bytes()
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
    captures = _arm_case(run, case, root / "present", rng, values, mutation, True)
    for value, label in ((None, "missing"), ("", "empty")):
        changed = {name: secret for name, secret in values.items() if name != SOURCE}
        if value is not None:
            changed[SOURCE] = value
        captures.extend(_arm_case(run, case, root / label, rng, changed,
                                  mutation if mutation == "keyenv-shared-fallback" else None, False))
    captures.extend(_refusals(run, case, root / "refused", rng))
    captures.extend(_spend(run, root / "spend", values, rng))
    _private(run, root, tokens, captures)
