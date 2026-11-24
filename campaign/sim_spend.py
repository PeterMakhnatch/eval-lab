from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from random import Random
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from campaign import cli, spend, spend_cli, spend_harbor
from campaign.sim_fixture import toml_text
from campaign.spend_types import Root, Trial

EXAMPLE = "spend-roots.example.toml"
ET = ZoneInfo("America/New_York")
AS_OF = datetime(2026, 11, 24, 15, 24, 28, tzinfo=UTC)
NOW = AS_OF + timedelta(hours=2)
SINCE = date(2026, 11, 1)
UNTIL = date(2026, 11, 24)
INSTANTS = (
    datetime(2026, 10, 30, 12, 0, tzinfo=UTC),
    datetime(2026, 11, 21, 3, 0, tzinfo=UTC),
    datetime(2026, 11, 23, 2, 30, tzinfo=UTC),
    datetime(2026, 11, 23, 23, 59, 59, tzinfo=UTC),
    datetime(2026, 11, 24, 0, 0, tzinfo=UTC),
    datetime(2026, 11, 24, 3, 10, tzinfo=UTC),
    datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
    AS_OF,
    datetime(2026, 11, 24, 16, 0, tzinfo=UTC),
)
WINDOWS = {
    "day": datetime(2026, 11, 24, tzinfo=UTC),
    "week": datetime(2026, 11, 23, tzinfo=UTC),
    "month": datetime(2026, 11, 1, tzinfo=UTC),
    "total": None,
}
OR_MODEL = "deepseek/deepseek-v4.1-flash"
DIRECT_MODEL = "deepseek-v4-flash"
BARE_MODEL = "claude-haiku-5-5"
TOKEN = re.compile(r"(?i)sk-[a-z0-9_-]{12,}")
SEED_SALT = 0x5EED_5BE7D
MAX_LINES = 64
MUTATIONS = {
    "copy-twice": "spend-copy-once",
    "add-fallback": "spend-fallback-not-added",
    "missing-zero": "spend-unknown-not-zero",
    "unknown-all": "spend-scripted-not-unknown",
}
LANES = ("calls", "seats", "settle")
SHAPES = ("scripted", "never-ran", "absent", "tokens", "team-cost", "no-way", "scripted-team")
TRACE_TOKENS = {"trajectory.total_prompt_tokens": 1000, "trajectory.total_completion_tokens": 100,
                "trajectory.total_cached_tokens": 0}
LIST_PRICE = "token-list-price"
REPORTED = "provider-reported"
DELTA = "shared-key-delta"

Money = dict[tuple[str, str], Decimal]
Window = tuple[datetime, datetime] | None


class Checks(Protocol):
    step: str

    def check(self, ok: bool, invariant: str, detail: str) -> None: ...


@dataclass(frozen=True)
class Charge:
    bucket: str
    route: str
    source: str
    usd: Decimal
    time: datetime


@dataclass(frozen=True)
class Seat:
    name: str
    model: str
    provider: str | None
    result: Decimal | None
    trajectory: Decimal | None
    trace: bool


@dataclass(frozen=True)
class TrialSpec:
    ident: str
    name: str
    time: datetime
    seats: tuple[Seat, ...]
    multi: bool


@dataclass
class Truth:
    charges: list[Charge] = field(default_factory=list)
    trials: list[tuple[datetime, str, bool, str]] = field(default_factory=list)
    details: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    tokens: dict[tuple[str, str], dict[str, int]] = field(default_factory=dict)
    calls: list[tuple[datetime, bool]] = field(default_factory=list)
    settles: list[tuple[datetime, int]] = field(default_factory=list)
    distractors: dict[str, tuple[Decimal, ...]] = field(default_factory=dict)
    markers: list[str] = field(default_factory=list)


def _harbor_time(stamp: datetime, rng: Random) -> str:
    text = stamp.strftime("%Y-%m-%dT%H:%M:%S.%f")
    return text + rng.choice(("", "Z", "+00:00"))


def _route(provider: str | None, model: str) -> str:
    if provider == "openrouter" or model.startswith("openrouter/"):
        return "openrouter"
    return "direct" if provider else "unknown"


def _inside(stamp: datetime, window: Window) -> bool:
    return window is None or window[0] <= stamp < window[1]


def _money(charges: list[Charge]) -> Money:
    total: Money = {}
    for charge in charges:
        key = charge.route, charge.source
        total[key] = total.get(key, Decimal(0)) + charge.usd
    return total


def _reported(routes: object) -> Money:
    assert isinstance(routes, dict)
    total: Money = {}
    for route, sources in routes.items():
        assert isinstance(sources, dict)
        for source, value in sources.items():
            total[route, source] = total.get((route, source), Decimal(0)) + Decimal(value)
    return total


def _shown(money: Money) -> str:
    return ", ".join(f"{route}/{source} {format(usd, 'f')}" for (route, source), usd in sorted(money.items())) or "none"


class Fixture:
    def __init__(self, workspace: Path, rng: Random) -> None:
        self.workspace = workspace.resolve()
        assert not self.workspace.exists() or not any(self.workspace.iterdir())
        self.rng = rng
        self.secret = f"sk-or-v1-{rng.getrandbits(160):040x}"
        self.truth = Truth()
        self.roots: list[dict[str, Any]] = []
        self.roots_file = self.path("roots.toml")

    def path(self, *parts: str) -> Path:
        path = self.workspace.joinpath(*parts)
        assert path.resolve().is_relative_to(self.workspace), "fixture path escapes the workspace"
        return path

    def put(self, path: Path, body: str) -> None:
        assert path.resolve().is_relative_to(self.workspace), "fixture path escapes the workspace"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def usd(self) -> Decimal:
        return Decimal(self.rng.randrange(1, 4_000_000)).scaleb(-6)

    def when(self) -> datetime:
        return self.rng.choice(INSTANTS)

    def ident(self) -> str:
        return f"{self.rng.getrandbits(128):032x}"

    def build(self) -> None:
        self._harbor()
        self._settle()
        self._control()
        self._delta()
        self._calls()
        self.rng.shuffle(self.roots)
        self.put(self.roots_file, toml_text({"roots": self.roots}))

    def usage(self, window: Window) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        recorded = {name: sum((c.usd for c in self.recorded(name, window)), Decimal(0)) for name in WINDOWS}
        extra = [Decimal(self.rng.randrange(0, 3_000_000)).scaleb(-6) for _ in range(4)]
        day = recorded["day"] + extra[0]
        week = recorded["week"] + extra[0] + extra[1]
        month = recorded["month"] + extra[0] + extra[1] + extra[2]
        return recorded["total"] + sum(extra, Decimal(0)), day, week, month

    def recorded(self, name: str, window: Window) -> list[Charge]:
        lower = WINDOWS[name]
        return [c for c in self.truth.charges if c.bucket in LANES and c.route == "openrouter"
                and _inside(c.time, window) and c.time <= AS_OF and (lower is None or c.time >= lower)]

    def _seat_record(self, seat: Seat, start: str, finish: str) -> dict[str, Any]:
        result = None
        if seat.result is not None or seat.trace:
            result = {"n_input_tokens": self.rng.randrange(100, 9000), "n_cache_tokens": 0,
                      "n_output_tokens": self.rng.randrange(10, 900), "rollout_details": []}
            if seat.result is not None:
                result["cost_usd"] = float(seat.result)
        return {
            "seat": seat.name, "under_test": True,
            "agent_info": {"name": "terminus-2", "version": "1",
                           "model_info": {"name": seat.model, "provider": seat.provider}},
            "agent_result": result,
            "agent_setup": {"started_at": start, "finished_at": start},
            "agent_execution": {"started_at": start, "finished_at": finish},
            "exception_info": None,
        }

    def _trajectory(self, folder: Path, seat: Seat) -> None:
        if not seat.trace:
            return
        metrics: dict[str, Any] = {"total_prompt_tokens": 1000, "total_completion_tokens": 100,
                                   "total_cached_tokens": 0}
        if seat.trajectory is not None:
            metrics["total_cost_usd"] = float(seat.trajectory)
        body = {"schema_version": "ATIF-v1.2", "session_id": self.ident(),
                "agent": {"name": "terminus-2", "version": "1", "model_name": seat.model},
                "steps": [{"step_id": 1, "source": "user", "message": f"export OPENROUTER_API_KEY={self.secret}"}],
                "final_metrics": metrics}
        self.put(folder / "trajectory.json", json.dumps(body))

    def write_trial(self, folder: Path, spec: TrialSpec) -> None:
        start = _harbor_time(spec.time - timedelta(minutes=20), self.rng)
        finish = _harbor_time(spec.time, self.rng)
        seats = [self._seat_record(seat, start, finish) for seat in spec.seats]
        for seat, record in zip(spec.seats, seats, strict=True):
            counts = {f"result.{name}": count for name, count in (record["agent_result"] or {}).items()
                      if name.startswith("n_")}
            self.truth.tokens[spec.name, seat.name] = counts | (TRACE_TOKENS if seat.trace else {})
        first = spec.seats[0]
        body: dict[str, Any] = {
            "schema_version": 1, "id": spec.ident, "task_name": "sim-task", "trial_name": spec.name,
            "task_checksum": "0" * 16, "source": None,
            "config": {"agent": {"name": "terminus-2", "model_name": None, "model_api_key_name": None, "kwargs": {}}},
            "agent_info": {"name": "terminus-2", "version": "1",
                           "model_info": {"name": first.model, "provider": first.provider}},
            "agent_result": None if spec.multi else seats[0]["agent_result"],
            "exception_info": None, "started_at": start, "finished_at": finish,
            "agent_execution": {"started_at": start, "finished_at": finish},
            "verifier_result": {"rewards": {"reward": 1.0}},
        }
        if spec.multi:
            body["seat_results"] = seats
        self.put(folder / "result.json", json.dumps(body))
        for seat in spec.seats:
            self._trajectory(folder / "seats" / seat.name / "agent" if spec.multi else folder / "agent", seat)

    def _specs(self) -> list[TrialSpec]:
        u, w, rng = self.usd, self.when, self.rng
        specs = [
            TrialSpec(self.ident(), "sim-task__t1", w(), (Seat("p1", OR_MODEL, "openrouter", u(), u(), True),
                                                        Seat("p2", OR_MODEL, "openrouter", u(), u(), True)), True),
            TrialSpec(self.ident(), "sim-task__t2", w(), (Seat("p1", OR_MODEL, "openrouter", None, u(), True),
                                                        Seat("p2", OR_MODEL, "openrouter", u(), u(), True)), True),
            TrialSpec(self.ident(), "sim-task__t3", w(), (Seat("agent", DIRECT_MODEL, "deepseek", u(), None, False),), False),
            TrialSpec(self.ident(), "sim-task__t4", w(),
                      (Seat("agent", OR_MODEL, "openrouter", None, None, rng.random() < 0.5),), False),
        ]
        if rng.random() < 0.5:
            specs.append(TrialSpec(self.ident(), "sim-task__t5", w(),
                                   (Seat("agent", OR_MODEL, "openrouter", Decimal(0), None, False),), False))
        if rng.random() < 0.5:
            specs.append(TrialSpec(self.ident(), "sim-task__t6", w(),
                                   (Seat("agent", BARE_MODEL, None, u(), None, False),), False))
        return specs

    def _record_trial(self, spec: TrialSpec, bucket: str) -> None:
        missing = []
        for seat in spec.seats:
            usd = seat.result if seat.result is not None else seat.trajectory
            if usd is not None:
                self.truth.charges.append(Charge(bucket, _route(seat.provider, seat.model), LIST_PRICE, usd, spec.time))
            else:
                missing.append({"seat": seat.name if spec.multi else None, "route": _route(seat.provider, seat.model),
                                "model": seat.model, "tokens": self.truth.tokens[spec.name, seat.name]})
        known = len(missing) < len(spec.seats)
        self.truth.trials.append((spec.time, "known" if known else "model-use", bucket == "duplicates", spec.name))
        if not known:
            self.truth.details[spec.name] = missing
        if bucket == "seats":
            extra = tuple(s.trajectory for s in spec.seats if s.result is not None and s.trajectory is not None)
            self.truth.distractors[spec.ident] = extra
            self.truth.distractors[spec.name] = extra

    def _shaped(self, folder: Path, shape: str) -> None:
        when, usd, name = self.when(), self.usd(), f"sim-task__s-{shape}"
        start, finish = _harbor_time(when - timedelta(minutes=20), self.rng), _harbor_time(when, self.rng)
        kind, fields, details = self._shape(shape, {"started_at": start, "finished_at": finish}, usd)
        body: dict[str, Any] = {
            "schema_version": 1, "id": self.ident(), "task_name": "sim-task", "trial_name": name, "source": None,
            "config": {"agent": {}}, "agent_info": None, "agent_result": None, "exception_info": None,
            "started_at": start, "finished_at": finish, "verifier_result": {"rewards": {"reward": 0.0}},
        }
        self.put(folder / name / "result.json", json.dumps(body | fields))
        if kind == "known":
            self.truth.charges.append(Charge("seats", "direct", LIST_PRICE, usd, when))
        self.truth.trials.append((when, kind, False, name))
        if kind == "model-use":
            self.truth.details[name] = details

    def _shape(self, shape: str, ran: dict[str, str], usd: Decimal) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        tokens = {"n_input_tokens": self.rng.randrange(1, 9000), "n_cache_tokens": 0,
                  "n_output_tokens": self.rng.randrange(1, 900)}
        zero = dict.fromkeys(tokens, 0)
        oracle = {"name": "oracle", "version": "1", "model_info": None}
        model = {"name": "terminus-2", "version": "1", "model_info": {"name": OR_MODEL, "provider": "openrouter"}}
        scripted = {"seat": "p1", "agent_info": oracle, "agent_result": zero, "agent_execution": ran}
        idle = {"seat": "p2", "agent_info": model, "agent_result": None, "agent_execution": None}
        roster = [{"seat": "p1", "name": "oracle", "model_name": None},
                  {"seat": "p2", "name": "terminus-2", "model_name": OR_MODEL, "model_api_key_name": "OPENROUTER_API_KEY"}]
        unnamed = {"seat": None, "route": "unknown", "model": None}
        if shape == "scripted":
            return "no-model", {"seat_results": [scripted, scripted | {"seat": "p2"}],
                                "config": {"agent": {"name": "oracle"}, "participants": roster[:1]}}, []
        if shape == "never-ran":
            return "no-model", {"seat_results": [scripted, idle], "config": {"agent": {}, "participants": roster}}, []
        if shape == "absent":
            return "model-use", {"seat_results": [scripted], "config": {"agent": {}, "participants": roster}}, [
                {"seat": "p2", "route": "openrouter", "model": OR_MODEL, "tokens": {}}]
        if shape == "tokens":
            return "model-use", {"agent_info": {"name": "terminus-2", "version": "1"}, "agent_result": tokens,
                                 "config": {"agent": {"name": "terminus-2"}}}, [
                unnamed | {"tokens": {f"result.{key}": value for key, value in tokens.items()}}]
        if shape == "team-cost":
            info = {"name": "terminus-2", "version": "1", "model_info": {"name": DIRECT_MODEL, "provider": "deepseek"}}
            return "known", {"agent_info": info, "agent_result": tokens | {"cost_usd": float(usd)},
                             "config": {"agent": {"name": "terminus-2", "model_name": DIRECT_MODEL}}}, []
        if shape == "no-way":
            return "model-use", {}, [unnamed | {"tokens": {}}]
        assert shape == "scripted-team", shape
        return "no-model", {"agent_info": oracle, "agent_result": zero, "config": {"agent": {"name": "oracle"}}}, []

    def _harbor(self) -> None:
        seats, copies = self.path("seats"), self.path("copies")
        specs = self._specs()
        for spec in specs:
            self.write_trial(seats / "job-1" / spec.name, spec)
            self._record_trial(spec, "seats")
        copied = [specs[0]] + [spec for spec in specs[1:] if self.rng.random() < 0.4]
        for spec in copied:
            shutil.copytree(seats / "job-1" / spec.name, copies / "arm-1" / spec.name)
            self._record_trial(spec, "duplicates")
        fake = TrialSpec(self.ident(), "sim-task__fx", self.when(),
                         (Seat("agent", OR_MODEL, "openrouter", self.usd(), None, False),), False)
        self.write_trial(seats / "job-1" / "fixtures" / fake.name, fake)
        self.write_trial(seats / "job-1" / "scratch-t9", replace(fake, ident=self.ident(), name="sim-task__t9"))
        for shape in SHAPES:
            self._shaped(seats / "job-1", shape)
        self.put(seats / "job-1" / "result.json", json.dumps({
            "id": self.ident(), "schema_version": 1, "started_at": _harbor_time(INSTANTS[1], self.rng),
            "finished_at": _harbor_time(INSTANTS[2], self.rng), "updated_at": None, "n_total_trials": len(specs),
            "stats": {"cost_usd": 9.5, "n_completed_trials": len(specs), "n_errored_trials": 0}}))
        self.put(seats / "job-1" / f"{self.secret}__bad" / "result.json", '{"id": "' + self.secret + '", "cost_usd": 9')
        self.truth.markers.append("[redacted]")
        self.roots += [{"label": "seats", "path": str(seats), "kind": "harbor-trials", "exclude": ["*/scratch-*"]},
                       {"label": "seats-copy", "path": str(copies), "kind": "harbor-trials", "copies_of": "seats"}]

    def _settle(self) -> None:
        rows: list[dict[str, Any]] = []
        for number in range(self.rng.randrange(1, 4)):
            arm, stamp = f"arm-{number}", self.when()
            rows.append({"ts_utc": "2026-11-01T00:00:00Z", "event": "reserve", "arm": arm, "provider": "openrouter",
                         "roster": "sim", "tasks": 2, "attempts": 1, "reserved_usd": 5.0})
            usd, trials = self.usd(), self.rng.randrange(1, 9)
            rows.append(self._settle_row(stamp, arm, usd, trials))
            self.truth.charges.append(Charge("settle", "openrouter", LIST_PRICE, usd, stamp))
            self.truth.settles.append((stamp, trials))
            if self.rng.random() < 0.5:
                later, more = stamp + timedelta(seconds=1), self.usd()
                rows.append(self._settle_row(later, arm, usd + more, trials + 1))
                self.truth.charges.append(Charge("settle", "openrouter", LIST_PRICE, usd + more, later))
                self.truth.settles.append((later, trials + 1))
        rows.append({"ts_utc": "2026-11-02T00:00:00Z", "event": "void", "experiment": "sim", "arms": ["arm-9"],
                     "reason": "sim"})
        path = self.path("settle", "spend-ledger.jsonl")
        self.put(path, "".join(json.dumps(row) + "\n" for row in rows))
        self.roots.append({"label": "settle", "path": str(path), "kind": "settle-ledger"})

    def _settle_row(self, stamp: datetime, arm: str, usd: Decimal, trials: int) -> dict[str, Any]:
        actual = {"trials": trials, "seats": trials, "seats_without_cost": 0, "input": 1000, "cache": 0,
                  "output": 100, "recorded_usd": float(usd)}
        return {"ts_utc": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), "event": "settle", "arm": arm, "exit": 0,
                "status": "ended", "actual": actual}

    def _control(self) -> None:
        stamp, usd = self.when(), self.usd()
        common = {"experiment": "sim", "campaign": "sim", "plan_sha256": "0" * 64, "lane": "a", "arm": "solo-1"}
        rows = [{"ts_utc": "2026-11-01T00:00:00Z", "event": "reserve", **common, "provider": "openrouter",
                 "tasks": 1, "attempts": 1, "concurrency": 1, "reserved_usd": 1.0, "balance_usd": 50.0},
                {"ts_utc": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), "event": "settle", **common, "exit": 0,
                 "status": "ended", "actual": {"recorded_usd": float(usd), "trials": 1, "seats": 1,
                                               "seats_without_cost": 0, "unreadable_results": 0}}]
        self.truth.charges.append(Charge("excluded/control", "openrouter", LIST_PRICE, usd, stamp))
        path = self.path("control", "ledger.jsonl")
        self.put(path, "".join(json.dumps(row) + "\n" for row in rows))
        self.roots.append({"label": "control", "path": str(path), "kind": "campaign-ledger", "report_only": True})

    def _delta(self) -> None:
        rows: list[dict[str, Any]] = []
        for number in range(self.rng.randrange(1, 4)):
            stamp, usd, wave = self.when(), self.usd(), f"wave-{number}"
            rows.append({"ts_utc": "2026-11-01T00:00:00Z", "event": "reserve", "wave_id": wave, "provider": "openrouter",
                         "model": OR_MODEL, "n_trials": 4, "reserved_usd": 5.0, "baseline_openrouter_spend": 50.0,
                         "baseline_deepseek_balance": 10.0})
            rows.append({"ts_utc": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), "event": "settle", "wave_id": wave,
                         "provider": "openrouter", "n_trials": 4, "actual_usd": float(usd),
                         "deepseek_balance_after": 10.0})
            self.truth.charges.append(Charge("excluded/delta", "openrouter", DELTA, usd, stamp))
        path = self.path("delta", "spend-ledger.jsonl")
        self.put(path, "".join(json.dumps(row) + "\n" for row in rows))
        self.roots.append({"label": "delta", "path": str(path), "kind": "settle-ledger", "delta_ledger": True})

    def _call_time(self, stamp: datetime) -> str:
        return stamp.astimezone(ET).isoformat() if self.rng.random() < 0.5 else stamp.isoformat()

    def _call(self, stamp: datetime, model: str, usd: Decimal, call_id: str) -> list[dict[str, Any]]:
        reserved = {"ts": "2026-11-01T00:00:00+00:00", "status": "reserved", "call_id": call_id, "cost_usd": None,
                    "model": model, "purpose": "sim", "requested_model": model, "reserved_usd": 0.5,
                    "max_tokens": 100, "estimated_prompt_tokens": 100}
        actual = {"ts": self._call_time(stamp), "status": "actual", "call_id": call_id, "cost_usd": float(usd),
                  "model": model, "provider": "openrouter", "purpose": f"sim {self.secret}", "prompt_tokens": 100,
                  "completion_tokens": 10, "tokens": 110, "requested_model": model, "served_model": model,
                  "raw_usage": {"cost": float(usd), "prompt_tokens": 100, "completion_tokens": 10,
                                "total_tokens": 110, "is_byok": False}}
        return [reserved, actual]

    def _calls(self) -> None:
        lines: list[str] = []
        first: dict[str, Any] = {}
        for number in range(self.rng.randrange(2, 6)):
            stamp, usd = self.when(), self.usd()
            model = self.secret if number == 1 else OR_MODEL
            rows = self._call(stamp, model, usd, f"call-{self.rng.getrandbits(64):016x}")
            first = first or rows[1]
            lines += [json.dumps(row) for row in rows]
            self.truth.charges.append(Charge("calls", "openrouter", REPORTED, usd, stamp))
            self.truth.calls.append((stamp, False))
        lines.append(json.dumps(first))
        dup_time = datetime.fromisoformat(first["ts"]).astimezone(UTC)
        self.truth.charges.append(Charge("duplicates", "openrouter", REPORTED, Decimal(str(first["cost_usd"])), dup_time))
        self.truth.calls.append((dup_time, True))
        legacy, usd = self.when(), self.usd()
        lines.append(json.dumps({"ts": self._call_time(legacy), "model": BARE_MODEL, "cost_usd": float(usd),
                                 "prompt_tokens": 10, "completion_tokens": 1, "tokens": 11, "purpose": "legacy"}))
        self.truth.charges.append(Charge("calls", "unknown", LIST_PRICE, usd, legacy))
        self.truth.calls.append((legacy, False))
        bad = self.rng.randrange(0, len(lines) + 1)
        lines.insert(bad, '{"ts": "2026-11-24T00:00:00Z", "purpose": "' + self.secret + '", "cost_usd": 1.')
        self.truth.markers.append(f"spend.jsonl:{bad + 1}")
        assert len(lines) <= MAX_LINES
        path = self.path("calls", "spend.jsonl")
        self.put(path, "\n".join(lines) + "\n")
        self.roots.append({"label": "calls", "path": str(path), "kind": "call-log", "time_field": "ts",
                           "model_field": "model", "provider_field": "provider", "usd_field": "cost_usd",
                           "id_field": "call_id", "skip_field": "status", "skip_value": "reserved"})


def _rows(result: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    rows = result.get(key)
    assert isinstance(rows, list)
    return {row["lane"]: row for row in rows}


def _check_lanes(run: Checks, fixture: Fixture, result: dict[str, Any], window: Window) -> None:
    lanes = _rows(result, "lanes")
    run.check(set(lanes) == set(LANES), "spend-lane-set", f"lanes {sorted(lanes)}, want {list(LANES)}")
    for lane in LANES:
        want = _money([c for c in fixture.truth.charges if c.bucket == lane and _inside(c.time, window)])
        got = _reported(lanes[lane]["routes"]) if lane in lanes else {}
        run.check(got == want, "spend-lane-sum", f"{lane}: {_shown(got)}; want {_shown(want)}")
        run.check(DELTA not in {source for _, source in got}, "spend-delta-not-added", f"{lane} has a delta source")
    seats = lanes.get("seats", {})
    owned = [(t, kind) for t, kind, dup, _ in fixture.truth.trials if not dup and _inside(t, window)]
    copies = [t for t, _, dup, _ in fixture.truth.trials if dup and _inside(t, window)]
    duplicates = result["duplicates"]
    want_dup = _money([c for c in fixture.truth.charges if c.bucket == "duplicates" and _inside(c.time, window)])
    run.check(seats.get("trials") == len(owned) and duplicates["trials"] == len(copies)
              and _reported(duplicates["routes"]) == want_dup, "spend-copy-once",
              f"seats trials {seats.get('trials')} want {len(owned)}; duplicate trials {duplicates['trials']} "
              f"want {len(copies)}; duplicate USD {_shown(_reported(duplicates['routes']))} want {_shown(want_dup)}")
    got_total = sum(_reported(seats.get("routes", {})).values(), Decimal(0))
    want_total = sum((c.usd for c in fixture.truth.charges if c.bucket == "seats" and _inside(c.time, window)),
                     Decimal(0))
    run.check(got_total == want_total, "spend-fallback-not-added",
              f"seats total {format(got_total, 'f')}, want {format(want_total, 'f')} "
              "(a trajectory total is a fallback, never a second charge)")
    _check_unknown(run, fixture, result, window)
    _check_counts(run, fixture, lanes, duplicates, window)


def _split(rows: list[tuple[datetime, str, bool, str]], dup: bool, window: Window) -> dict[str, set[str]]:
    kinds: dict[str, set[str]] = {"known": set(), "no-model": set(), "model-use": set()}
    for time, kind, copied, name in rows:
        if copied == dup and _inside(time, window):
            kinds[kind].add(name)
    return kinds


def _seat_view(seats: list[dict[str, Any]]) -> list[tuple[str, str, str, str]]:
    return sorted((str(s["seat"]), str(s["route"]), str(s["model"]), json.dumps(s["tokens"], sort_keys=True))
                  for s in seats)


def _check_unknown(run: Checks, fixture: Fixture, result: dict[str, Any], window: Window) -> None:
    seats, duplicates = _rows(result, "lanes").get("seats", {}), result["duplicates"]
    owned, copies = _split(fixture.truth.trials, False, window), _split(fixture.truth.trials, True, window)
    lanes = _rows(result, "lanes").values()
    totals = all(result[key] == sum(row[key] for row in lanes)
                 for key in ("trials", "no_model_trials", "model_use_without_cost_trials"))
    for label, row, kinds in (("seats", seats, owned), ("duplicates", duplicates, copies)):
        old = len(kinds["no-model"]) + len(kinds["model-use"])
        got = (row.get("no_model_trials"), row.get("model_use_without_cost_trials"))
        run.check(got[0] is not None and got[1] is not None and got[0] + got[1] == old and totals,
                  "spend-unknown-not-zero", f"{label}: no model {got[0]} + model use {got[1]}; want sum {old}")
    groups = {lane["lane"]: lane for lane in result["model_use_without_cost"]}
    records = {Path(record["path"]).name: record for record in groups.get("seats", {}).get("records", [])}
    leaked = sorted(owned["no-model"] & set(records))
    run.check(not leaked and seats.get("no_model_trials") == len(owned["no-model"]), "spend-scripted-not-unknown",
              f"no-model trials listed as unknown {leaked}; no model {seats.get('no_model_trials')} "
              f"want {len(owned['no-model'])}")
    lost = sorted(owned["model-use"] - set(records))
    run.check(not lost and seats.get("model_use_without_cost_trials") == len(records), "spend-model-not-scripted",
              f"model-use trials missing from the list {lost}; count {seats.get('model_use_without_cost_trials')} "
              f"listed {len(records)}")
    wrong = sorted(name for name in owned["model-use"] & set(records)
                   if _seat_view(records[name]["seats"]) != _seat_view(fixture.truth.details[name]))
    run.check(not wrong and set(groups) <= {"seats"}, "spend-missing-details",
              f"seat, model or token counts differ for {wrong}; lanes {sorted(groups)}")
    shown = spend.text(result)
    run.check("Model use without a cost record" in shown and all(
        f"{lane}: {group['trials']} trials" in shown for lane, group in groups.items()), "spend-text-unknown",
        f"lanes {sorted(groups)}")


def _check_counts(run: Checks, fixture: Fixture, lanes: dict[str, dict[str, Any]], duplicates: dict[str, Any],
                  window: Window) -> None:
    calls = sum(not dup for t, dup in fixture.truth.calls if _inside(t, window))
    dup_calls = sum(dup for t, dup in fixture.truth.calls if _inside(t, window))
    got = lanes.get("calls", {}).get("calls")
    run.check(got == calls and duplicates["calls"] == dup_calls, "spend-calls-once",
              f"calls {got} want {calls}; duplicate calls {duplicates['calls']} want {dup_calls}")
    settles = [units for t, units in fixture.truth.settles if _inside(t, window)]
    row = lanes.get("settle", {})
    run.check(row.get("settlements") == len(settles) and row.get("settled_trials") == sum(settles),
              "spend-settles-counted",
              f"settlements {row.get('settlements')} trials {row.get('settled_trials')}; "
              f"want {len(settles)} {sum(settles)}")


def _check_excluded(run: Checks, fixture: Fixture, result: dict[str, Any], window: Window) -> None:
    rows = result["excluded"]
    assert isinstance(rows, list)
    for label, reason in (("delta", DELTA), ("control", "overlapping")):
        found = [row for row in rows if row["lane"] == label]
        want = _money([c for c in fixture.truth.charges if c.bucket == f"excluded/{label}" and _inside(c.time, window)])
        got = _reported(found[0]["routes"]) if len(found) == 1 else {}
        ok = len(found) == 1 and found[0]["reason"] == reason and got == want
        invariant = "spend-delta-not-added" if label == "delta" else "spend-overlap-not-added"
        run.check(ok, invariant, f"{label}: {len(found)} rows, reason {found[0]['reason'] if found else '-'}, "
                                 f"{_shown(got)}; want {reason} {_shown(want)}")


def _check_reconciliation(run: Checks, fixture: Fixture, result: dict[str, Any], window: Window,
                          usage: tuple[Decimal, ...]) -> None:
    rows = {row["window"]: row for row in result["reconciliation"]}
    counters = dict(zip(("total", "day", "week", "month"), usage, strict=True))
    run.check(set(rows) == set(WINDOWS), "spend-reconciliation", f"windows {sorted(rows)}")
    for name in WINDOWS:
        if name not in rows:
            continue
        row = rows[name]
        want = sum((c.usd for c in fixture.recorded(name, window)), Decimal(0))
        recorded, counter, remainder = (Decimal(row[key]) for key in ("recorded_usd", "counter_usd", "remainder_usd"))
        ok = recorded == want and counter == counters[name] and remainder == counter - recorded
        run.check(ok, "spend-reconciliation",
                  f"{name}: recorded {row['recorded_usd']} want {format(want, 'f')}, counter {row['counter_usd']}, "
                  f"remainder {row['remainder_usd']}")


def _check_days(run: Checks, fixture: Fixture, result: dict[str, Any], window: Window) -> None:
    for zone, timezone in (("UTC", UTC), ("ET", ET)):
        got: dict[tuple[str, str, str, str], Decimal] = {}
        for row in result["days"][zone]:
            for (route, source), usd in _reported(row["routes"]).items():
                key = row["date"], row["lane"], route, source
                got[key] = got.get(key, Decimal(0)) + usd
        want: dict[tuple[str, str, str, str], Decimal] = {}
        for c in fixture.truth.charges:
            if c.bucket in LANES and _inside(c.time, window):
                key = c.time.astimezone(timezone).date().isoformat(), c.bucket, c.route, c.source
                want[key] = want.get(key, Decimal(0)) + c.usd
        diff = sorted(f"{k[0]} {k[1]} {k[2]}/{k[3]}" for k in set(got) | set(want) if got.get(k) != want.get(k))
        run.check(not diff, "spend-day-zones", f"{zone}: {diff[:4]}")


def _check_private(run: Checks, fixture: Fixture, label: str, text: str) -> None:
    run.check(fixture.secret not in text and not TOKEN.search(text), "spend-output-private", label)


def _check_result(run: Checks, fixture: Fixture, result: dict[str, Any], window: Window,
                  usage: tuple[Decimal, ...]) -> None:
    _check_lanes(run, fixture, result, window)
    _check_excluded(run, fixture, result, window)
    _check_reconciliation(run, fixture, result, window, usage)
    _check_days(run, fixture, result, window)
    problems = result["problems"]
    found = [marker for marker in fixture.truth.markers if any(marker in problem for problem in problems)]
    stray = [n for n, problem in enumerate(problems) if not any(m in problem for m in fixture.truth.markers)]
    run.check(len(found) == len(fixture.truth.markers) and not stray, "spend-problems-listed",
              f"{len(found)} of {len(fixture.truth.markers)} planted defects listed; unexpected problems {stray}")
    body = spend.text(result)
    used = {source for row in result["lanes"] for sources in row["routes"].values() for source in sources}
    run.check(all(source in body for source in used), "spend-text-sources", f"sources {sorted(used)}")
    _check_private(run, fixture, "report json", json.dumps(result))
    _check_private(run, fixture, "report text", body)


def _cli(argv: list[str], capture: spend_cli.Capture | None = None) -> tuple[int | None, str]:
    try:
        args = cli.parser().parse_args(["spend", *argv])
    except SystemExit:
        return None, ""
    assert isinstance(args, argparse.Namespace)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = spend_cli.run(args, now=NOW, capture=capture)
    return code, out.getvalue() + err.getvalue()


def _usage_text(usage: tuple[Decimal, ...]) -> str:
    return ",".join(format(value, "f") for value in usage)


def _check_cli(run: Checks, fixture: Fixture, result: dict[str, Any], usage: tuple[Decimal, ...]) -> None:
    run.step = "spend/cli"
    roots, out = str(fixture.roots_file), fixture.path("out", "spend.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    base = [roots, "--as-of", AS_OF.isoformat(), "--key-usage", _usage_text(usage)]
    strict, strict_text = _cli([*base, "--json", str(out)])
    lenient, lenient_text = _cli([*base, "--lenient"])
    run.check(strict == 1 and lenient == 0, "spend-cli-strict-lenient", f"strict exit {strict}, lenient exit {lenient}")
    written = out.read_bytes() if out.exists() else b""
    same = bool(written) and json.loads(written) == json.loads(json.dumps(result))
    run.check(same, "spend-cli-json", f"{len(written)} bytes match the report: {same}")
    again, again_text = _cli([*base, "--json", str(out), "--lenient"])
    kept = out.read_bytes() if out.exists() else b""
    run.check(again not in (0, None) and kept == written, "spend-cli-json-exclusive", f"exit {again}")
    counters = _usage_text(usage)
    probe_out = fixture.path("out", "probe.json")
    probed, probed_text = _cli([roots, "--key-probe", '["key-probe"]', "--lenient", "--json", str(probe_out)],
                               lambda argv, timeout, owner: (0, counters + "\n"))
    rows = json.loads(probe_out.read_bytes())["reconciliation"] if probe_out.exists() else []
    shown = {row["window"]: Decimal(row["counter_usd"]) for row in rows}
    want = dict(zip(("total", "day", "week", "month"), usage, strict=True))
    run.check(probed == 0 and shown == want and all(
        Decimal(row["remainder_usd"]) == Decimal(row["counter_usd"]) - Decimal(row["recorded_usd"]) for row in rows),
        "spend-cli-probe", f"exit {probed}, {len(rows)} windows")
    leak = f'{{"data":{{"label":"{fixture.secret}","usage":1}}}}\n'
    leaked, leaked_text = _cli([roots, "--key-probe", '["key-probe"]', "--lenient"],
                               lambda argv, timeout, owner: (0, leak))
    run.check(leaked == 1, "spend-cli-probe-fails-closed", f"exit {leaked}")
    for label, text in (("cli strict", strict_text), ("cli lenient", lenient_text), ("cli again", again_text),
                        ("cli probe", probed_text), ("cli bad probe", leaked_text),
                        ("cli json", written.decode("utf-8", "replace"))):
        _check_private(run, fixture, label, text)


def _mutated(name: str, trial: Trial, distractors: dict[str, tuple[Decimal, ...]]) -> Trial:
    if name == "missing-zero":
        return replace(trial, entries=tuple(replace(e, usd=Decimal(0)) if e.usd is None else e for e in trial.entries))
    assert name == "add-fallback"
    extra = distractors.get(trial.identity[0], ())
    results = [e for e in trial.entries if e.channel == "result" and e.usd is not None]
    added = tuple(replace(e, usd=usd, channel="trajectory") for e, usd in zip(results, extra, strict=False))
    return replace(trial, entries=trial.entries + added)


@contextlib.contextmanager
def _swapped(owner: Any, name: str, value: Any) -> Iterator[None]:
    original = getattr(owner, name)
    setattr(owner, name, value)
    try:
        yield
    finally:
        setattr(owner, name, original)
        assert getattr(owner, name) is original, name


@contextlib.contextmanager
def inject(name: str | None, distractors: dict[str, tuple[Decimal, ...]]) -> Iterator[None]:
    assert name is None or name in MUTATIONS
    if name is None:
        yield
        return
    if name == "unknown-all":
        with _swapped(spend, "_model_use", lambda uses: True):
            yield
        return
    if name == "copy-twice":
        add_trial = spend.Summary.add_trial

        def counted(self: spend.Summary, label: str, trial: Trial, *, duplicate: bool) -> None:
            add_trial(self, label, trial, duplicate=False)

        with _swapped(spend.Summary, "add_trial", counted):
            yield
        return
    reader: Callable[[Root, list[str]], Iterator[Trial]] = spend_harbor.trials

    def trials(root: Root, problems: list[str]) -> Iterator[Trial]:
        for trial in reader(root, problems):
            yield _mutated(name, trial, distractors)

    with _swapped(spend_harbor, "trials", trials):
        yield


def example(run: Checks, path: Path) -> None:
    problems: list[str] = []
    roots = spend._roots(path, problems)
    run.check(bool(roots) and not problems, "example-spend-roots-valid", f"{path.name}: {problems[:3]}")


def scenario(run: Checks, seed: int, workspace: Path, mutation: str | None = None) -> None:
    run.step = "spend/fixture"
    fixture = Fixture(workspace, Random(seed ^ SEED_SALT))
    fixture.build()
    window = (datetime.combine(SINCE, datetime.min.time(), UTC), datetime.combine(UNTIL, datetime.min.time(), UTC))
    try:
        with inject(mutation, fixture.truth.distractors):
            usage = fixture.usage(None)
            run.step = "spend/report"
            whole = spend.report(fixture.roots_file, as_of=AS_OF, usage=usage)
            _check_result(run, fixture, whole, None, usage)
            run.step = "spend/window"
            part_usage = fixture.usage(window)
            part = spend.report(fixture.roots_file, since=SINCE, until=UNTIL, as_of=AS_OF, usage=part_usage)
            _check_result(run, fixture, part, window, part_usage)
            _check_cli(run, fixture, whole, usage)
    except Exception as error:
        run.check(False, "spend-no-crash", type(error).__name__)
