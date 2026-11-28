from __future__ import annotations

import fnmatch
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import tomllib
import traceback
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from fractions import Fraction
from importlib.machinery import ModuleSpec
from importlib.util import module_from_spec
from math import comb
from pathlib import Path
from typing import Any, Protocol

import muster
from muster.lab import api, episode, load
from muster.lab.schedule import parse

SCHEMA = "muster-holdout/v1"
REPOSITORY = Path(muster.__file__).resolve().parents[1]
TASKS = Path(muster.__file__).resolve().parent / "tasks"
MAX_COHORTS = 64
MAX_TRIES = 4096
MAX_POLICIES = 512
MAX_INSTANCES = 4096
MAX_FILES = 4096
MAX_FIRST = 1_000_000
MAX_REASON = 400
SEED_TEXT = re.compile(r"[A-Za-z0-9._-]{1,128}")
SUFFIX_TEXT = re.compile(r"[A-Za-z_-]{1,16}")
_PACKAGE = "muster.tasks"
_GLOB = frozenset("*?[")


class UsageError(Exception):
    pass


class Rejected(Exception):
    pass


class Invalid(Exception):
    pass


@dataclass(frozen=True)
class Options:
    policies: tuple[str, ...]
    policy_file: Path | None = None
    conditions: tuple[str, ...] = ()
    cohorts: int = 3
    suffix: str = "h"
    built_over: int = 2
    fraction: Fraction = Fraction(1, 5)
    chance: Fraction | None = None
    measure: str = "auto"
    schedule: str = "round"
    max_tries: int = 96
    first: int = 1

    def __post_init__(self) -> None:
        assert 1 <= len(self.policies) <= MAX_POLICIES
        assert 1 <= self.cohorts <= MAX_COHORTS
        assert self.cohorts <= self.max_tries <= MAX_TRIES
        assert SUFFIX_TEXT.fullmatch(self.suffix) and 1 <= self.first <= MAX_FIRST
        assert self.built_over >= 0 and 0 <= self.fraction < 1
        assert self.chance is None or 0 < self.chance < 1
        assert self.measure


@dataclass(frozen=True)
class Cohort:
    seed: str
    adapter: Any
    instances: dict[str, list[dict]]


class Source(Protocol):
    family: str
    seed: str

    def open(self, seed: str) -> AbstractContextManager[Cohort]: ...

    def provenance(self) -> dict[str, Any]: ...


@dataclass
class Tally:
    passes: int = 0
    total: int = 0
    errors: int = 0
    first_error: str | None = None

    def add(self, passed: bool, error: str | None) -> None:
        assert not (passed and error)
        self.total += 1
        self.passes += passed
        if error is not None:
            self.errors += 1
            self.first_error = self.first_error or error
        assert 0 <= self.passes <= self.total and self.errors <= self.total

    def merge(self, other: Tally) -> None:
        self.passes += other.passes
        self.total += other.total
        self.errors += other.errors
        self.first_error = self.first_error or other.first_error

    def row(self) -> dict[str, Any]:
        return {"passes": self.passes, "total": self.total, "errors": self.errors,
                "first_error": self.first_error}


@dataclass(frozen=True)
class Plan:
    conditions: tuple[str, ...]
    policies: tuple[str, ...]
    origin: dict[str, str]
    skills: dict[str, tuple[str, ...] | None]
    needs: dict[str, tuple[str, ...] | None]
    measure: str
    basis: str


@dataclass
class Played:
    cells: dict[tuple[str, str], Tally] = field(default_factory=dict)
    oracle: Tally = field(default_factory=Tally)


def binomial_tail(n: int, k: int, p: Fraction) -> Fraction:
    assert n >= 0 and k >= 0 and 0 <= p <= 1
    if k > n:
        return Fraction(0)
    q = 1 - p
    total = sum((comb(n, j) * p**j * q ** (n - j) for j in range(k, n + 1)), Fraction(0))
    assert 0 <= total <= 1
    return total


def reason(error: BaseException) -> str:
    frames = traceback.extract_tb(error.__traceback__)
    where = f" at {Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else ""
    message = " ".join(str(error).split()) or (frames[-1].line or "" if frames else "")
    return f"{type(error).__name__}{where}: {message}"[:MAX_REASON]


def _files(source: Path, skip: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for folder, directories, files in os.walk(source):
        top = Path(folder) == source
        directories[:] = sorted(name for name in directories
                                if name != "__pycache__" and not (top and name in skip))
        found.extend(Path(folder) / name for name in files)
        assert len(found) <= MAX_FILES, f"too many files under {source}"
    return sorted(found)


def _copy(source: Path, target: Path, digest: Any, skip: tuple[str, ...]) -> int:
    files = _files(source, skip)
    for path in files:
        relative = path.relative_to(source)
        data = path.read_bytes()
        digest.update(f"{source.name}/{relative.as_posix()}\0{len(data)}\0".encode())
        digest.update(data)
        (target / relative).parent.mkdir(parents=True, exist_ok=True)
        (target / relative).write_bytes(data)
    return len(files)


def _git(*args: str) -> str | None:
    try:
        completed = subprocess.run(["git", "-C", str(REPOSITORY), *args], capture_output=True,
                                   text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


@contextmanager
def _redirected(tasks: Path) -> Iterator[None]:
    owned = [name for name in sys.modules if name == _PACKAGE or name.startswith(_PACKAGE + ".")]
    saved = {name: sys.modules.pop(name) for name in owned}
    namespace = vars(muster)
    saved_attribute = namespace.get("tasks")
    saved_path, saved_flag, saved_tasks = sys.path[:], sys.dont_write_bytecode, load._TASKS
    spec = ModuleSpec(_PACKAGE, None, is_package=True)
    spec.submodule_search_locations = [str(tasks)]
    package = module_from_spec(spec)
    sys.modules[_PACKAGE] = package
    namespace["tasks"] = package
    sys.dont_write_bytecode = True
    load._TASKS = tasks
    try:
        yield
    finally:
        for name in [name for name in sys.modules if name == _PACKAGE or name.startswith(_PACKAGE + ".")]:
            del sys.modules[name]
        sys.modules.update(saved)
        if saved_attribute is None:
            namespace.pop("tasks", None)
        else:
            namespace["tasks"] = saved_attribute
        sys.path[:] = saved_path
        sys.dont_write_bytecode = saved_flag
        load._TASKS = saved_tasks


class FamilySource:
    def __init__(self, family: str, scratch: Path) -> None:
        if not family or not family.replace("_", "").isalnum() or family.startswith("_"):
            raise UsageError(f"invalid family name: {family!r}")
        folder = TASKS / family
        for name in ("lab.py", "generate.py", "instances.py", "family.toml"):
            if not (folder / name).is_file():
                raise UsageError(f"family {family} has no {name}; holdout needs a lab adapter "
                                 "and a seeded generator")
        assert not scratch.resolve().is_relative_to(REPOSITORY), "scratch inside the repository"
        self.family = family
        self.tasks = scratch / "muster" / "tasks"
        digest = hashlib.sha256()
        kits = sorted(path for path in TASKS.glob("_kit*") if path.is_dir())
        copied = sum(_copy(kit, self.tasks / kit.name, digest, ()) for kit in kits)
        copied += _copy(folder, self.tasks / family, digest, ("build",))
        assert copied > 0
        self.digest = digest.hexdigest()
        self.build = folder / "build"
        self.instances_text = (self.tasks / family / "instances.py").read_text(encoding="utf-8")
        self.toml_text = (self.tasks / family / "family.toml").read_text(encoding="utf-8")
        seed = tomllib.loads(self.toml_text).get("seed")
        if (not isinstance(seed, str) or not SEED_TEXT.fullmatch(seed)
                or self.instances_text.count(f'SEED = "{seed}"') != 1
                or self.toml_text.count(f'seed = "{seed}"') != 1):
            raise UsageError(f"family {family}: expected 1 seed line in instances.py and in "
                             f"family.toml with the same seed, got {seed!r}")
        self.seed = seed

    def provenance(self) -> dict[str, Any]:
        prefix = f"{self.family}-"
        changed = _git("diff", "--name-only", "HEAD", "--", "muster")
        return {"version": self.seed.removeprefix(prefix) if self.seed.startswith(prefix) else None,
                "source_digest": self.digest,
                "muster_revision": {"commit": _git("rev-parse", "HEAD"),
                                    "tracked_changes": None if changed is None else bool(changed)}}

    def _write_seed(self, seed: str) -> None:
        assert SEED_TEXT.fullmatch(seed)
        folder = self.tasks / self.family
        (folder / "instances.py").write_text(
            self.instances_text.replace(f'SEED = "{self.seed}"', f'SEED = "{seed}"'), encoding="utf-8")
        (folder / "family.toml").write_text(
            self.toml_text.replace(f'seed = "{self.seed}"', f'seed = "{seed}"'), encoding="utf-8")

    def _match_build(self, instances: dict[str, list[dict]]) -> None:
        for condition, rows in instances.items():
            for row in rows:
                path = (self.build / condition / f"{self.family}-{row['id']}" / "environment"
                        / "world" / "instance.json")
                if not path.is_file():
                    raise Invalid(f"built instance missing: {condition}/{self.family}-{row['id']}")
                if json.loads(path.read_text(encoding="utf-8")) != json.loads(json.dumps(row)):
                    raise Invalid(f"regenerated instance differs from the build: "
                                  f"{condition}/{self.family}-{row['id']}")

    @contextmanager
    def open(self, seed: str) -> Iterator[Cohort]:
        self._write_seed(seed)
        with _redirected(self.tasks):
            generator = importlib.import_module(f"{_PACKAGE}.{self.family}.generate")
            try:
                generated = generator.tasks()
            except AssertionError as error:
                raise Rejected(reason(error)) from error
            assert isinstance(generated, dict) and generated
            adapter = load.family(self.family)
            instances = {condition: adapter.instances(condition) for condition in adapter.conditions()}
            if seed == self.seed:
                self._match_build(instances)
            yield Cohort(seed, adapter, instances)


def _policy_table(adapter: Any, path: Path | None) -> tuple[dict[str, api.Policy], set[str],
                                                           dict[str, tuple[str, ...]]]:
    zoo = dict(adapter.policies())
    if path is None:
        return zoo, set(), {}
    try:
        module = load.module(path, "holdout_policies")
    except Exception as error:
        raise UsageError(f"cannot load policy file {path}: {reason(error)}") from error
    table = getattr(module, "POLICIES", None)
    if (not isinstance(table, dict) or not 1 <= len(table) <= MAX_POLICIES
            or not all(isinstance(name, str) and name and callable(policy)
                       for name, policy in table.items())):
        raise UsageError(f"{path} must define POLICIES, a dict of name to generator policy")
    skills = getattr(module, "SKILLS", {})
    if (not isinstance(skills, dict) or not set(skills) <= set(table)
            or not all(isinstance(values, list | tuple) and all(isinstance(s, str) for s in values)
                       for values in skills.values())):
        raise UsageError(f"{path}: SKILLS must map names in POLICIES to lists of skill names")
    return {**zoo, **table}, set(table), {name: tuple(values) for name, values in skills.items()}


def _skills_claim(adapter: Any) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    for claim in adapter.claims():
        if claim.get("type") == "skills":
            return ({name: tuple(skills) for name, skills in claim["policies"].items()},
                    {name: tuple(skills) for name, skills in claim["conditions"].items()})
    return {}, {}


def _expand(patterns: tuple[str, ...], available: list[str]) -> tuple[str, ...]:
    names: list[str] = []
    for pattern in patterns:
        if _GLOB & set(pattern):
            matched = [name for name in available if fnmatch.fnmatchcase(name, pattern)]
        else:
            matched = [pattern] if pattern in available else []
        if not matched:
            shown = ", ".join(available[:40]) + (", ..." if len(available) > 40 else "")
            raise UsageError(f"no policy matches {pattern!r}; known: {shown}")
        names.extend(name for name in matched if name not in names)
    if len(names) > MAX_POLICIES:
        raise UsageError(f"{len(names)} policies selected; the limit is {MAX_POLICIES}")
    return tuple(names)


def _conditions(adapter: Any, options: Options, claimed: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    known = list(adapter.conditions())
    chosen = list(options.conditions) or [name for name in known if name in claimed] or known
    unknown = [name for name in chosen if name not in known]
    if unknown:
        raise UsageError(f"unknown conditions: {', '.join(unknown)}")
    return tuple(dict.fromkeys(chosen))


def _episode(adapter: Any, instance: dict, policy: str, zoo: dict[str, api.Policy],
             schedule: str) -> tuple[dict[str, float] | None, str | None]:
    lineup = dict.fromkeys(adapter.seats(instance), policy)
    try:
        result = episode.run(adapter, instance, lineup, parse(schedule), zoo=zoo)
    except Exception as error:
        return None, f"{instance['condition']}/{instance['id']}: {reason(error)}"
    return result.rewards, None


def _measure(cohort: Cohort, condition: str, zoo: dict[str, api.Policy],
             options: Options) -> tuple[str, str]:
    if options.measure != "auto":
        return options.measure, "chosen with --measure"
    rewards, error = _episode(cohort.adapter, cohort.instances[condition][0], "oracle", zoo,
                              options.schedule)
    if rewards is None:
        raise Invalid(f"oracle episode failed: {error}")
    if "focal" in rewards:
        return "focal", "auto: the adapter's rewards define focal"
    return "reward", "auto: the adapter's rewards define no focal"


def _plan(cohort: Cohort, options: Options) -> Plan:
    zoo, from_file, file_skills = _policy_table(cohort.adapter, options.policy_file)
    if "oracle" not in zoo:
        raise UsageError("the family has no oracle policy; holdout needs it as a sanity check")
    names = _expand(options.policies, sorted(zoo))
    claimed_policies, claimed_conditions = _skills_claim(cohort.adapter)
    conditions = _conditions(cohort.adapter, options, claimed_conditions)
    measure, basis = _measure(cohort, conditions[0], zoo, options)
    return Plan(conditions=conditions, policies=names,
                origin={name: "file" if name in from_file else "zoo" for name in names},
                skills={name: file_skills.get(name) if name in from_file
                        else claimed_policies.get(name) for name in names},
                needs={name: claimed_conditions.get(name) for name in conditions},
                measure=measure, basis=basis)


def _judge(rewards: dict[str, float] | None, error: str | None, measure: str,
           instance: dict) -> tuple[bool, str | None]:
    if rewards is None:
        return False, error
    value = rewards.get(measure)
    if type(value) is not float:
        return False, f"{instance['condition']}/{instance['id']}: rewards have no measure {measure!r}"
    return value == 1.0, None


def _play_cohort(cohort: Cohort, plan: Plan, options: Options) -> Played:
    zoo, _, _ = _policy_table(cohort.adapter, options.policy_file)
    missing = [name for name in ("oracle", *plan.policies) if name not in zoo]
    if missing:
        raise Invalid(f"cohort {cohort.seed} lacks policies: {', '.join(missing)}")
    played = Played()
    for condition in plan.conditions:
        rows = cohort.instances.get(condition, [])
        if not 1 <= len(rows) <= MAX_INSTANCES:
            raise Invalid(f"cohort {cohort.seed} has {len(rows)} instances in {condition}")
        for row in rows:
            rewards, error = _episode(cohort.adapter, row, "oracle", zoo, options.schedule)
            full = rewards is not None and rewards.get("reward") == 1.0 and rewards.get(plan.measure) == 1.0
            if rewards is not None and not full:
                error = (f"{condition}/{row['id']}: oracle reward {rewards.get('reward')}, "
                         f"{plan.measure} {rewards.get(plan.measure)}")
            played.oracle.add(full, None if full else error)
            for name in plan.policies:
                rewards, error = _episode(cohort.adapter, row, name, zoo, options.schedule)
                played.cells.setdefault((name, condition), Tally()).add(
                    *_judge(rewards, error, plan.measure, row))
    assert len(played.cells) == len(plan.conditions) * len(plan.policies)
    return played


@dataclass
class Heldout:
    seeds: list[dict[str, Any]] = field(default_factory=list)
    cells: dict[tuple[str, str], Tally] = field(default_factory=dict)
    by_seed: dict[tuple[str, str], list[int]] = field(default_factory=dict)
    oracle: Tally = field(default_factory=Tally)
    accepted: int = 0
    invalid: str | None = None

    def add(self, seed: str, played: Played) -> None:
        self.accepted += 1
        self.oracle.merge(played.oracle)
        full = played.oracle.passes == played.oracle.total and played.oracle.errors == 0
        self.seeds.append({"seed": seed, "status": "accepted" if full else "invalid",
                           "reason": None if full else played.oracle.first_error,
                           "oracle": played.oracle.row()})
        if not full:
            missing = played.oracle.total - played.oracle.passes
            self.invalid = (f"held-out cohort {seed} is invalid: the oracle is below 1.0 on "
                            f"{missing} of {played.oracle.total} instances")
        for key, tally in played.cells.items():
            self.cells.setdefault(key, Tally()).merge(tally)
            self.by_seed.setdefault(key, []).append(tally.passes)


def _heldout(source: Source, plan: Plan, options: Options) -> Heldout:
    result = Heldout()
    for ordinal in range(options.first, options.first + options.max_tries):
        if result.accepted == options.cohorts or result.invalid is not None:
            break
        seed = f"{source.seed}-{options.suffix}{ordinal}"
        try:
            with source.open(seed) as cohort:
                played = _play_cohort(cohort, plan, options)
        except Rejected as error:
            result.seeds.append({"seed": seed, "status": "rejected", "reason": str(error),
                                 "oracle": None})
            continue
        except Exception as error:
            result.seeds.append({"seed": seed, "status": "invalid", "reason": reason(error),
                                 "oracle": None})
            result.invalid = f"held-out cohort {seed} is invalid: {reason(error)}"
            break
        result.add(seed, played)
    assert len(result.seeds) <= options.max_tries
    if result.invalid is None and result.accepted < options.cohorts:
        result.invalid = (f"only {result.accepted} of {options.cohorts} held-out cohorts pass the "
                          f"generator's checks in {options.max_tries} seeds")
    return result


def _cell(name: str, condition: str, plan: Plan, built: Tally, held: Heldout,
          options: Options) -> dict[str, Any]:
    tally = held.cells.get((name, condition), Tally())
    skills, needs = plan.skills[name], plan.needs[condition]
    lacks = None if skills is None or needs is None else sorted(set(needs) - set(skills))
    applies = lacks is None or bool(lacks)
    threshold = options.fraction * tally.total
    meets = built.passes > options.built_over and tally.passes > threshold
    chance = options.chance
    return {"policy": name, "condition": condition, "origin": plan.origin[name],
            "skills": _listed(skills), "needs": _listed(needs), "lacks": lacks, "applies": applies,
            "built": built.row(), "heldout": tally.row(),
            "heldout_by_seed": held.by_seed.get((name, condition), []),
            "heldout_threshold": float(threshold),
            "heldout_expected": None if chance is None else float(chance * tally.total),
            "heldout_tail": None if chance is None else float(
                binomial_tail(tally.total, tally.passes, chance)),
            "meets_rule": meets,
            "decision": "control" if not applies else "replicated" if meets else "not replicated"}


def _listed(values: tuple[str, ...] | None) -> list[str] | None:
    return None if values is None else list(values)


def _header(source: Source, options: Options) -> dict[str, Any]:
    path = options.policy_file
    return {"schema": SCHEMA, "family": source.family, "seed": source.seed, **source.provenance(),
            "policy_file": None if path is None else {
                "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
            "rule": {"built_over": options.built_over,
                     "heldout_over_fraction": str(options.fraction),
                     "chance": None if options.chance is None else str(options.chance),
                     "text": (f"replicated when built passes > {options.built_over} and held-out "
                              f"passes > {options.fraction} x held-out count, in a cell whose "
                              "condition needs a skill that the policy lacks (or whose skills "
                              "are not declared)")},
            "request": {"policies": list(options.policies), "conditions": list(options.conditions),
                        "cohorts": options.cohorts, "seed_suffix": options.suffix,
                        "first_seed": options.first,
                        "max_tries": options.max_tries, "measure": options.measure,
                        "schedule": options.schedule}}


def _invalid(report: dict[str, Any], why: str) -> dict[str, Any]:
    return {**report, "valid": False, "reason": why, "measure": None, "measure_basis": None,
            "conditions": [], "policies": [], "oracle": None, "seeds": [], "cells": [],
            "summary": {"cells": 0, "applicable": 0, "controls": 0, "replicated": 0,
                        "replicated_cells": []}}


def study(source: Source, options: Options) -> dict[str, Any]:
    report = _header(source, options)
    try:
        with source.open(source.seed) as cohort:
            plan = _plan(cohort, options)
            built = _play_cohort(cohort, plan, options)
    except UsageError:
        raise
    except Exception as error:
        return _invalid(report, f"built cohort {source.seed} is invalid: {reason(error)}")
    held = _heldout(source, plan, options)
    reasons = [held.invalid] if held.invalid else []
    if built.oracle.passes != built.oracle.total:
        reasons.insert(0, f"built cohort {source.seed} is invalid: {built.oracle.first_error}")
    cells = [_cell(name, condition, plan, built.cells[(name, condition)], held, options)
             for name in plan.policies for condition in plan.conditions]
    replicated = [f"{cell['policy']}@{cell['condition']}" for cell in cells
                  if cell["decision"] == "replicated"]
    applicable = sum(cell["applies"] for cell in cells)
    return {**report, "valid": not reasons, "reason": "; ".join(reasons) or None,
            "measure": plan.measure, "measure_basis": plan.basis,
            "conditions": [{"name": name, "needs": _listed(plan.needs[name])}
                           for name in plan.conditions],
            "policies": [{"name": name, "origin": plan.origin[name],
                          "skills": _listed(plan.skills[name])} for name in plan.policies],
            "oracle": {"built": built.oracle.row(), "heldout": held.oracle.row()},
            "seeds": [{"seed": source.seed, "status": "built", "reason": None,
                       "oracle": built.oracle.row()}, *held.seeds],
            "cells": cells,
            "summary": {"cells": len(cells), "applicable": applicable,
                        "controls": len(cells) - applicable, "replicated": len(replicated),
                        "replicated_cells": replicated}}


def exit_code(report: dict[str, Any]) -> int:
    if not report["valid"]:
        return 2
    return int(report["summary"]["replicated"] > 0)
