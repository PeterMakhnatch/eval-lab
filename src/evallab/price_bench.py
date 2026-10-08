"""HAR-201: deterministic, local-only evaluation and training price bench.

Expected spend uses a continuous-refill window, not busy GPU time:
 cold + N/C * mean_wall + max(0, p95_wall - mean_wall) + idle.
The inferred cold-start parameter comes from HAR-129's ~$0.23 production
cold start. HAR-168 measured-time upper bounds are references, not invoices.
This module never contacts providers, starts resources, or runs inference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from evallab.schemas import ContractModel

Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
MONTH_HOURS = 730.0
HOST_PREP_S = 600.0
COLD_START_USD = 0.23
GRPO_ROLLOUTS = 16 * 8
HOSTED_BASE = "different base model (Qwen3.5-9B)"


class CatalogEntry(ContractModel):
    id: str = Field(min_length=1)
    provider: str
    kind: Literal[
        "gpu", "cpu", "memory", "sandbox", "host", "token_api", "hosted_training", "storage"
    ]
    sku: str
    rates: dict[str, Nonnegative | None]
    billing: Literal["per_second", "per_minute", "per_hour", "per_hour_rounded", "per_month"]
    specs: dict[str, Nonnegative] = Field(default_factory=dict)
    multipliers: dict[str, Any] = Field(default_factory=dict)
    source: str = Field(min_length=1)
    spec_source: str | None = None
    observed_at: str
    notes: str = ""


class PriceCatalog(ContractModel):
    schema_version: Literal["evallab.price-catalog/v1"] = Field(alias="schema")
    observed_at: str
    currency: Literal["USD"]
    entries: list[CatalogEntry] = Field(min_length=1)

    @field_validator("entries")
    @classmethod
    def unique_entries(cls, values: list[CatalogEntry]) -> list[CatalogEntry]:
        if len({v.id for v in values}) != len(values):
            raise ValueError("catalog IDs must be unique")
        return values


class Stats(ContractModel):
    n: int | None = Field(default=None, ge=0)
    mean: Nonnegative | None = None
    p50: Nonnegative | None = None
    p90: Nonnegative | None = None
    p95: Nonnegative | None = None
    max: Nonnegative | None = None


class Resources(ContractModel):
    cpus: Positive | None = None
    memory_gib: Positive | None = None
    disk_gib: Nonnegative | None = None


class Inflight(ContractModel):
    mean: Nonnegative | None = None
    peak: Nonnegative | None = None


class Decode(ContractModel):
    value: Nonnegative | None = None
    meaning: Literal["aggregate", "per_stream"] | None = None


class Billed(ContractModel):
    server_usd: Nonnegative | None = None
    sandbox_usd: Nonnegative | None = None
    all_in_usd: Nonnegative | None = None
    per_trial_usd: Nonnegative | None = None
    source: str = ""


class Cohort(ContractModel):
    id: str
    card: str
    dates: list[str]
    gpu: str
    concurrency: int = Field(ge=1)
    sandbox_backend: str
    trials: int = Field(ge=1)
    sources: list[str]
    inputs_sha256: str
    wall_s: Stats
    llm_s: Stats
    env_setup_s: Stats = Field(default_factory=Stats)
    agent_s: Stats = Field(default_factory=Stats)
    verifier_s: Stats = Field(default_factory=Stats)
    model_requests: Stats = Field(default_factory=Stats)
    input_tokens: Stats = Field(default_factory=Stats)
    output_tokens: Stats = Field(default_factory=Stats)
    cached_input_tokens: Stats | None = None
    inflight_requests: Inflight = Field(default_factory=Inflight)
    kv_usage_max: Nonnegative | None = None
    decode_tok_s: Decode = Field(default_factory=Decode)
    billed: Billed = Field(default_factory=Billed)
    declared_sandbox: Resources = Field(default_factory=Resources)


class Serving(ContractModel):
    cold_start_s: Nonnegative | None = None
    scaledown_window_s: Nonnegative | None = None
    max_running_requests: int | None = Field(default=None, ge=1)
    cuda_graph_max_bs: int | None = Field(default=None, ge=1)
    weights_gib: Nonnegative | None = None
    kv_bytes_per_token: Nonnegative | None = None
    source: str


class TrainingRun(ContractModel):
    run: str
    gpu: str | None = None
    seq_tokens: Nonnegative | None = None
    trained_tokens: Nonnegative | None = None
    seconds: Nonnegative | None = None
    seq_tok_s: Nonnegative | None = None
    peak_gib: Nonnegative | None = None
    usd: Nonnegative | None = None
    source: str


class Workload(ContractModel):
    model: str
    agent: str
    task_set: str


class WorkloadProfile(ContractModel):
    schema_version: Literal["evallab.workload-profile/v1"] = Field(alias="schema")
    generated_at: str
    workload: Workload
    cohorts: list[Cohort] = Field(min_length=1)
    serving: Serving
    training: list[TrainingRun]
    unknowns: list[str]


class Pack(ContractModel):
    cpu_request_fraction: Positive = 1.0
    memory_request_fraction: Positive = 1.0


class Scenario(ContractModel):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    gpu: str
    concurrency: int = Field(ge=1)
    sandbox: str
    pack: Pack = Field(default_factory=Pack)
    window_trials: int = Field(default=200, ge=1)
    cold_start_s: Nonnegative | None = None
    idle_tail_s: Nonnegative | None = None
    region_multiplier: Positive = 1.0
    prep_s: Nonnegative = HOST_PREP_S


class ScenarioFile(ContractModel):
    schema_version: Literal["evallab.price-scenarios/v1"] = Field(alias="schema")
    reference_cohort: str
    training_token_cohort: str | None = None
    calibration_windows: dict[str, int] = Field(default_factory=dict)
    window_sweep: list[int] = Field(default_factory=lambda: [20, 200, 1000])
    scenarios: list[Scenario] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_windows(self) -> ScenarioFile:
        if any(v <= 0 for v in (*self.window_sweep, *self.calibration_windows.values())):
            raise ValueError("window sizes must be positive")
        if len({s.id for s in self.scenarios}) != len(self.scenarios):
            raise ValueError("scenario IDs must be unique")
        return self


def load_catalog(path: Path) -> PriceCatalog:
    return PriceCatalog.model_validate(yaml.safe_load(path.read_text()))


def load_profile(path: Path) -> WorkloadProfile:
    return WorkloadProfile.model_validate_json(path.read_text())


def load_scenarios(path: Path) -> ScenarioFile:
    return ScenarioFile.model_validate(yaml.safe_load(path.read_text()))


def hourly(entry: CatalogEntry) -> float:
    for unit, factor in (
        ("per_second", 3600),
        ("per_minute", 60),
        ("per_hour", 1),
        ("per_month", 1 / MONTH_HOURS),
    ):
        value = entry.rates.get(unit)
        if value is not None:
            return value * factor
    raise ValueError(f"{entry.id}: no convertible compute rate")


def gpu_for(catalog: PriceCatalog, sku: str) -> CatalogEntry:
    candidates = [e for e in catalog.entries if e.kind == "gpu" and (e.sku == sku or e.id == sku)]
    if not candidates:
        raise ValueError(f"catalog missing measured GPU {sku!r}")
    return min(candidates, key=lambda e: (e.provider != "modal", e.id))


def server_hourly(catalog: PriceCatalog, gpu: CatalogEntry) -> float:
    rate = hourly(gpu)
    if gpu.provider == "modal":
        for kind, unit, seconds_unit, count in (
            ("cpu", "vcpu_hour", "vcpu_second", 4),
            ("memory", "memory_gib_hour", "memory_gib_second", 16),
        ):
            entries = sorted(
                (e for e in catalog.entries if e.provider == "modal" and e.kind == kind),
                key=lambda e: e.id,
            )
            if not entries:
                raise ValueError(f"catalog missing Modal {kind} overhead entry")
            unit_rate = entries[0].rates.get(unit)
            if unit_rate is None:
                second_rate = entries[0].rates.get(seconds_unit)
                if second_rate is None:
                    raise ValueError(f"{entries[0].id}: missing {unit} or {seconds_unit}")
                unit_rate = second_rate * 3600
            rate += count * unit_rate
    return rate


def sandbox_hourly(
    catalog: PriceCatalog, sandbox: CatalogEntry, cpus: float, memory: float, disk: float
) -> tuple[float, str]:
    """Price the admitted shape, preferring the provider's sandbox tariff."""
    if all(sandbox.rates.get(k) is not None for k in ("vcpu_hour", "memory_gib_hour")):
        cpu_rate = float(sandbox.rates["vcpu_hour"] or 0)
        memory_rate = float(sandbox.rates["memory_gib_hour"] or 0)
        sources = [sandbox.id]
    else:
        sources = []
        rates = []
        for kind, hour_unit, second_unit in (
            ("cpu", "vcpu_hour", "vcpu_second"),
            ("memory", "memory_gib_hour", "memory_gib_second"),
        ):
            candidates = [
                e
                for e in catalog.entries
                if e.provider == sandbox.provider
                and (e.kind == kind or (e.kind == "sandbox" and (kind in e.id or kind in e.sku)))
                and e.id != sandbox.id
            ]
            sandbox_tariffs = [e for e in candidates if "sandbox" in e.id or e.kind == "sandbox"]
            candidates = sandbox_tariffs or candidates
            if len(candidates) != 1:
                raise ValueError(f"{sandbox.id}: missing or ambiguous {kind} component tariff")
            component = candidates[0]
            rate = component.rates.get(hour_unit)
            if rate is None:
                seconds = component.rates.get(second_unit)
                rate = seconds * 3600 if seconds is not None else component.rates.get("per_hour")
                if rate is None:
                    rate = hourly(component)
            rates.append(rate)
            sources.append(component.id)
        cpu_rate, memory_rate = rates
    disk_rate = sandbox.rates.get("disk_gib_hour")
    free_disk = sandbox.specs.get("storage_free_gib", 0)
    if disk_rate is None:
        storage = [
            e
            for e in catalog.entries
            if e.provider == sandbox.provider
            and e.kind == "storage"
            and e.rates.get("disk_gib_hour") is not None
        ]
        if len(storage) > 1:
            raise ValueError(f"{sandbox.id}: ambiguous disk tariff")
        if storage:
            disk_rate = storage[0].rates["disk_gib_hour"]
            free_disk = storage[0].specs.get("storage_free_gib", 0)
            sources.append(storage[0].id)
    rate = cpus * cpu_rate + memory * memory_rate
    rate += max(0, disk - free_disk) * (disk_rate or 0)
    return rate, "list: catalog tariffs " + ", ".join(sources)


def resources(cohort: Cohort) -> tuple[float, float, float, str]:
    r = cohort.declared_sandbox
    fallback = r.cpus is None or r.memory_gib is None
    # These are task.toml defaults verified by ProfileMiner, not resource downsizing.
    return (
        r.cpus if r.cpus is not None else 2.0,
        r.memory_gib if r.memory_gib is not None else 8.0,
        r.disk_gib if r.disk_gib is not None else 10.0,
        "projected: task.toml 2 CPU / 8 GiB fallback" if fallback else "measured: declared_sandbox",
    )


def host_density(entry: CatalogEntry, cpus: float, memory: float, pack: Pack) -> int:
    try:
        return max(
            0,
            min(
                math.floor((entry.specs["vcpu"] - 2) / (cpus * pack.cpu_request_fraction)),
                math.floor(
                    (entry.specs["memory_gib"] - 2) / (memory * pack.memory_request_fraction)
                ),
            ),
        )
    except KeyError as exc:
        raise ValueError(f"{entry.id}: missing host spec {exc.args[0]}") from exc


def daytona_allowance(policy: dict[str, Any], cpus: float, memory: float, disk: float) -> int:
    safety = policy["safety_fraction"]
    # One additional sandbox reserve mirrors the actual queue admission clamp.
    return min(
        math.floor((policy["quota"][key] * safety - need) / need)
        for key, need in (("cpu", cpus), ("memory_gib", memory), ("disk_gib", disk))
        if need > 0
    )


@dataclass
class Context:
    catalog: PriceCatalog
    profile: WorkloadProfile
    cohort: Cohort
    policy: dict[str, Any]
    base_gpu: CatalogEntry
    cold_s: float
    cold_basis: str


def context(
    catalog: PriceCatalog, profile: WorkloadProfile, reference: str, policy: dict[str, Any]
) -> Context:
    cohort = next((c for c in profile.cohorts if c.id == reference), None)
    if cohort is None:
        raise ValueError(f"profile missing reference cohort {reference!r}")
    gpu = gpu_for(catalog, cohort.gpu)
    if profile.serving.cold_start_s is not None:
        cold, basis = profile.serving.cold_start_s, "measured: profile.serving.cold_start_s"
    else:
        server_rate = server_hourly(catalog, gpu)
        if server_rate <= 0:
            raise ValueError("reference server rate must be positive to infer cold-start seconds")
        cold = COLD_START_USD / server_rate * 3600
        basis = "inferred: HAR-129 ~$0.23 production cold-start / reference server rate"
    return Context(catalog, profile, cohort, policy, gpu, cold, basis)


@dataclass
class EvalRow:
    id: str
    gpu: str
    sandbox: str
    concurrency: int
    window_trials: int
    basis: str
    feasible: bool
    flags: list[str]
    wall_mean_s: float
    wall_p50_s: float | None
    wall_p90_s: float | None
    drain_tail_s: float
    window_s: float
    server_usd: float
    sandbox_usd: float
    total_usd: float
    per_1000_usd: float
    server_share: float
    sandbox_share: float
    active_streams: float
    max_active_streams: float
    cmax: int | None
    per_host: int | None
    required_hosts: int | None
    billed_host_hours: float | None
    labels: dict[str, str]
    assumptions: list[str]


def evaluate(
    ctx: Context,
    scenario: Scenario,
    *,
    cohort: Cohort | None = None,
    speed_ratio: float | None = None,
    capacity: int | None = None,
    per_host: int | None = None,
    cap_wall: bool = False,
) -> EvalRow:
    entries = {e.id: e for e in ctx.catalog.entries}
    for key in (scenario.gpu, scenario.sandbox):
        if key not in entries:
            raise ValueError(f"catalog entry {key!r} missing; scenario skipped")
    gpu, sandbox = entries[scenario.gpu], entries[scenario.sandbox]
    if gpu.kind != "gpu" or sandbox.kind not in ("sandbox", "host"):
        raise ValueError("scenario requires a gpu and sandbox/host catalog entry")
    c = cohort or ctx.cohort
    measured_gpu = gpu_for(ctx.catalog, c.gpu)
    if c.wall_s.mean is None or c.llm_s.mean is None:
        raise ValueError(f"{c.id}: mean wall/LLM time unavailable; cannot model expected spend")
    if c.wall_s.mean <= 0:
        raise ValueError(f"{c.id}: mean wall time must be positive")
    for candidate in (gpu, measured_gpu, ctx.base_gpu):
        if candidate.specs["hbm_tbps"] <= 0:
            raise ValueError(f"{candidate.id}: hbm_tbps must be positive")
    ratio = (
        speed_ratio
        if speed_ratio is not None
        else gpu.specs["hbm_tbps"] / measured_gpu.specs["hbm_tbps"]
    )
    if ratio <= 0:
        raise ValueError("GPU speed ratio must be positive")
    llm = c.llm_s.mean / ratio
    wall = max(0.0, c.wall_s.mean - c.llm_s.mean) + llm
    shift = llm - c.llm_s.mean
    p50 = c.wall_s.p50 + shift if c.wall_s.p50 is not None else None
    p90 = c.wall_s.p90 + shift if c.wall_s.p90 is not None else None
    p95 = c.wall_s.p95 + shift if c.wall_s.p95 is not None else None
    flags: list[str] = []
    assumptions: list[str] = []
    if cap_wall and p90 is not None:
        wall = max(wall, p90)
    tail = max(0.0, p95 - wall) if p95 is not None else 0.0
    if p95 is None:
        assumptions.append("projected: unknown p95 drain tail assumed 0; spend may be understated")
    cold = scenario.cold_start_s if scenario.cold_start_s is not None else ctx.cold_s
    idle = (
        scenario.idle_tail_s
        if scenario.idle_tail_s is not None
        else ctx.profile.serving.scaledown_window_s
    )
    if idle is None:
        idle = 300.0
        assumptions.append("projected: scaledown window 300s from serve.py")
    duration = cold + max(1, scenario.window_trials / scenario.concurrency) * wall + tail + idle
    server = (
        duration
        * server_hourly(ctx.catalog, gpu)
        * scenario.region_multiplier
        / 3600
        / scenario.window_trials
    )
    active_fraction = (
        c.inflight_requests.mean / c.concurrency
        if c.inflight_requests.mean is not None
        else c.llm_s.mean / c.wall_s.mean
    )
    active = scenario.concurrency * active_fraction
    max_active = float(ctx.profile.serving.cuda_graph_max_bs or 16)
    if gpu.sku != ctx.base_gpu.sku:
        max_active *= gpu.specs["hbm_tbps"] / ctx.base_gpu.specs["hbm_tbps"]
    if active > max_active:
        flags.append(
            f"saturated: active streams {active:.2f} > {max_active:.2f}; speed model unqualified"
        )
    weights = ctx.profile.serving.weights_gib
    cmax = capacity
    if cmax is None and weights is not None and ctx.cohort.kv_usage_max:
        pool = gpu.specs["gpu_memory_gib"] * 0.85 - weights
        base_pool = ctx.base_gpu.specs["gpu_memory_gib"] * 0.85 - weights
        share = ctx.cohort.kv_usage_max / ctx.cohort.concurrency
        cmax = math.floor(0.9 / (share * base_pool / pool)) if pool > 0 and base_pool > 0 else 0
    feasible = cmax is not None and scenario.concurrency <= cmax
    if cmax is None:
        flags.append("KV feasibility unknown: measured KV share / weights missing")
    elif not feasible:
        flags.append(f"KV infeasible: C{scenario.concurrency} > C_max {cmax}")
    cpus, memory, disk, resource_basis = resources(c)
    labels = {
        "wall_mean_s": "measured" if gpu.id == measured_gpu.id else "projected: HBM ratio",
        "gpu_speed_ratio": "measured" if gpu.id == measured_gpu.id else "projected: HBM ratio",
        "active_fraction": "measured: mean in-flight / measured C"
        if c.inflight_requests.mean is not None
        else "projected: llm_mean / wall_mean",
        "max_active_streams": "measured: CUDA graph batch"
        if gpu.sku == ctx.base_gpu.sku
        else "projected: HBM ratio",
        "cmax": "projected: measured KV share scaled by KV pool",
        "cold_start_s": "projected: scenario override"
        if scenario.cold_start_s is not None
        else ctx.cold_basis,
        "idle_tail_s": "projected: scenario override"
        if scenario.idle_tail_s is not None
        else "configured: serve.py scaledown window",
        "window_trials": "projected: scenario warm-window assumption",
        "drain_tail_s": "projected: reference p95-minus-mean tail held fixed across GPU speeds",
        "sandbox_resources": resource_basis,
        "sandbox_disk": "projected: 10 GiB disk fallback"
        if c.declared_sandbox.disk_gib is None
        else "measured: declared_sandbox.disk_gib",
        "region_multiplier": "projected: scenario assumption",
        "rates": "measured: dated catalog list prices, not invoice",
    }
    density = hosts = None
    host_hours = None
    if sandbox.kind == "sandbox":
        sandbox_rate, tariff_basis = sandbox_hourly(ctx.catalog, sandbox, cpus, memory, disk)
        labels["sandbox_rates"] = tariff_basis
        sandbox_cost = sandbox_rate * wall / 3600
        if sandbox.provider == "daytona":
            if ctx.policy:
                allowance = daytona_allowance(ctx.policy, cpus, memory, disk)
                if scenario.concurrency > allowance:
                    flags.append(
                        f"Daytona quota: C{scenario.concurrency} > allowance {allowance} (one sandbox reserved)"
                    )
            else:
                flags.append("Daytona quota policy unavailable")
    else:
        if sandbox.billing == "per_month":
            raise ValueError(f"{sandbox.id}: monthly commitment not modelled")
        density = (
            per_host if per_host is not None else host_density(sandbox, cpus, memory, scenario.pack)
        )
        hosts = math.ceil(scenario.concurrency / density) if density > 0 else 0
        if density == 0:
            feasible = False
            flags.append("host infeasible: zero trials fit after controller reserve")
        host_hours = (duration + scenario.prep_s) / 3600
        if sandbox.billing == "per_hour_rounded":
            host_hours = math.ceil(host_hours)
        sandbox_cost = hosts * hourly(sandbox) * host_hours / scenario.window_trials
        labels["pack_fractions"] = "projected: reserved-resource fractions, not measured usage"
        labels["per_host"] = "projected: floor resource packing with 2 CPU / 2 GiB reserve"
        labels["required_hosts"] = "projected: ceil(C / per_host)"
        labels["prep_s"] = "projected: 600s host preparation from execution-tiers.md"
        if scenario.pack.cpu_request_fraction < 1 or scenario.pack.memory_request_fraction < 1:
            flags.append(
                "packing projection: reduced reservations are not admitted task resource changes"
            )
    total = server + sandbox_cost
    return EvalRow(
        scenario.id,
        gpu.id,
        sandbox.id,
        scenario.concurrency,
        scenario.window_trials,
        "projected",
        feasible,
        flags,
        wall,
        p50,
        p90,
        tail,
        duration,
        server,
        sandbox_cost,
        total,
        total * 1000,
        server / total if total else 0,
        sandbox_cost / total if total else 0,
        active,
        max_active,
        cmax,
        density,
        hosts,
        host_hours,
        labels,
        assumptions,
    )


def sandbox_for_backend(catalog: PriceCatalog, backend: str) -> CatalogEntry:
    candidates = [e for e in catalog.entries if e.kind == "sandbox" and e.provider == backend]
    for e in candidates:
        if e.rates.get("vcpu_hour") is not None and e.rates.get("memory_gib_hour") is not None:
            return e
    if candidates:
        return candidates[0]
    raise ValueError(f"no catalog sandbox for {backend}")


def calibration(ctx: Context, windows: dict[str, int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cohort in ctx.profile.cohorts:
        billed = cohort.billed
        if billed.server_usd is None and billed.all_in_usd is None and billed.per_trial_usd is None:
            continue
        row: dict[str, Any] = {
            "cohort": cohort.id,
            "trials": cohort.trials,
            "source": billed.source,
            "basis": "measured: billed actual"
            if billed.server_usd is not None
            else "reference-only: measured-time upper bound",
            "notes": [],
        }
        n = windows.get(cohort.id, cohort.trials)
        row["window_trials"] = n
        row["windows"] = math.ceil(cohort.trials / n)
        row["billed_server_per_trial"] = (
            billed.server_usd / cohort.trials if billed.server_usd is not None else None
        )
        # Totals are totals; per_trial_usd is not assumed all-in if only server is billed.
        row["billed_all_in_per_trial"] = (
            billed.all_in_usd / cohort.trials if billed.all_in_usd is not None else None
        )
        if row["billed_all_in_per_trial"] is None and billed.server_usd is None:
            row["billed_all_in_per_trial"] = billed.per_trial_usd
        row["predicted_server_per_trial"] = row["predicted_all_in_per_trial"] = None
        row["server_error_usd"] = row["server_error_pct"] = None
        row["all_in_error_usd"] = row["all_in_error_pct"] = None
        if billed.server_usd is None:
            row["notes"].append(
                "Not used to calibrate: no settled server/sandbox split (HAR-168 upper bounds)."
            )
            rows.append(row)
            continue
        try:
            gpu = gpu_for(ctx.catalog, cohort.gpu)
            sandbox = sandbox_for_backend(ctx.catalog, cohort.sandbox_backend)
            scenario = Scenario(
                id=f"cal-{cohort.id}",
                gpu=gpu.id,
                sandbox=sandbox.id,
                concurrency=cohort.concurrency,
                window_trials=n,
            )
            server_total = all_total = 0.0
            for start in range(0, cohort.trials, n):
                size = min(n, cohort.trials - start)
                prediction = evaluate(
                    ctx, scenario.model_copy(update={"window_trials": size}), cohort=cohort
                )
                server_total += prediction.server_usd * size
                all_total += prediction.total_usd * size
            row["predicted_server_per_trial"] = server_total / cohort.trials
            row["predicted_all_in_per_trial"] = all_total / cohort.trials
            for prefix in ("server", "all_in"):
                actual = row[f"billed_{prefix}_per_trial"]
                if actual is not None:
                    delta = row[f"predicted_{prefix}_per_trial"] - actual
                    row[f"{prefix}_error_usd"] = delta
                    row[f"{prefix}_error_pct"] = delta / actual * 100 if actual else None
            row["notes"].append(
                f"Cold-start free parameter {ctx.cold_s:.1f}s ({ctx.cold_basis}); not fit to this cohort."
            )
            if cohort.id == "g2-wave1":
                row["notes"].append(
                    "G2 billed hours (08h+09h rows) include non-trial window time the profile cannot separate; residual reported as-is, no extra idle fitted."
                )
        except (ValueError, KeyError) as exc:
            row["notes"].append(str(exc))
        rows.append(row)
    return rows


def training(ctx: Context, token_cohort: str | None, best: EvalRow | None) -> dict[str, Any]:
    measured = [r for r in ctx.profile.training if r.seq_tok_s and r.gpu and r.seq_tokens]
    measured.sort(key=lambda r: float(r.seq_tokens or 0), reverse=True)
    reference = measured[0] if measured else None
    measured_costs: dict[str, float | None] = {}
    for r in measured:
        measured_costs[r.run] = (
            float(r.usd) / float(r.seq_tokens or 0) * 1e6
            if r.usd is not None and r.seq_tokens
            else None
        )
    peaks = [float(r.peak_gib) for r in measured if r.peak_gib is not None]
    mem_req = max(peaks) if peaks else None
    rows: list[dict[str, Any]] = []
    for r in measured:
        cost = measured_costs[r.run]
        if r.usd is not None:
            basis = f"measured: {r.run} all-in ${r.usd} / {float(r.seq_tokens or 0):.0f} seq tokens"
        else:
            basis = f"measured: {r.run} throughput (no settled training $)"
        rows.append(
            {
                "id": f"measured:{r.run}",
                "basis": basis,
                "seq_tok_s": r.seq_tok_s,
                "usd_per_1m_sequence_tokens": cost,
                "feasible": cost is not None,
                "fit_reason": "" if cost is not None else "training cost unavailable",
                "note": f"Measured {r.run} on {r.gpu}; LoRA/SFT throughput is a projection for GRPO training, not measured GRPO. {r.source}",
            }
        )
    if reference is not None:
        base = gpu_for(ctx.catalog, str(reference.gpu))
        for gpu in ctx.catalog.entries:
            if gpu.kind != "gpu":
                continue
            try:
                speed = (
                    float(reference.seq_tok_s or 0)
                    * gpu.specs["bf16_dense_tflops"]
                    / base.specs["bf16_dense_tflops"]
                )
                rate = hourly(gpu)
                cost = rate / 3600 / speed * 1e6
            except (ValueError, KeyError, ZeroDivisionError):
                speed, cost = None, None
            memory = gpu.specs.get("gpu_memory_gib")
            fits = memory is not None and mem_req is not None and memory >= mem_req
            single_gpu = gpu.provider != "lambda"
            feasible = fits and single_gpu and cost is not None
            reason = (
                "below measured peak / memory unknown"
                if not fits
                else "multi-GPU minimum unqualified"
                if not single_gpu
                else "price/spec unavailable"
                if cost is None
                else ""
            )
            rows.append(
                {
                    "id": gpu.id,
                    "basis": f"projected: dense BF16 FLOPS ratio from {reference.run}",
                    "seq_tok_s": speed,
                    "usd_per_1m_sequence_tokens": cost,
                    "feasible": feasible,
                    "fit_reason": reason,
                    "note": "LoRA/SFT throughput is a projection for GRPO training; not measured GRPO. "
                    + reason,
                }
            )
    for entry in ctx.catalog.entries:
        if entry.kind == "hosted_training":
            rows.append(
                {
                    "id": entry.id,
                    "basis": f"projected: {HOSTED_BASE}",
                    "seq_tok_s": None,
                    "usd_per_1m_sequence_tokens": entry.rates.get("train_per_1m"),
                    "feasible": False,
                    "fit_reason": "different base model",
                    "note": f"Sampling/prefill are separately billed, not included in this training rate. {entry.notes}",
                }
            )
    rows.sort(
        key=lambda r: (
            not r["feasible"],
            r["usd_per_1m_sequence_tokens"] is None,
            r["usd_per_1m_sequence_tokens"] or float("inf"),
            r["id"],
        )
    )
    proj = [
        (r["usd_per_1m_sequence_tokens"], r["id"])
        for r in rows
        if r["feasible"] and str(r["basis"]).startswith("projected: dense")
    ]
    proj_rate, proj_trainer = min(proj) if proj else (None, None)
    meas_rate = measured_costs.get(reference.run) if reference else None
    meas_trainer = f"measured:{reference.run}" if reference and meas_rate is not None else None
    cohort = (
        ctx.cohort
        if token_cohort is None
        else next((c for c in ctx.profile.cohorts if c.id == token_cohort), None)
    )
    if cohort is None:
        raise ValueError(f"profile missing training_token_cohort {token_cohort!r}")
    inp, requests = cohort.input_tokens.p50, cohort.model_requests.p50
    cases: list[dict[str, Any]] = []
    for name, tokens in (
        (
            "prefix-consistent (one sequence)",
            2 * inp / requests if inp is not None and requests else None,
        ),
        ("per-call (every call separately; expensive case)", inp),
    ):
        rollout = GRPO_ROLLOUTS * best.total_usd if best else None
        n_tokens = GRPO_ROLLOUTS * tokens if tokens is not None else None
        mtrain = (
            GRPO_ROLLOUTS * tokens * meas_rate / 1e6
            if tokens is not None and meas_rate is not None
            else None
        )
        cases.append(
            {
                "case": f"{name} [measured:{reference.run}]" if reference else name,
                "trainer_kind": "measured",
                "trainer": meas_trainer,
                "basis": "measured trainer rate + projected rollout $",
                "tokens_per_rollout": tokens,
                "training_tokens_per_step": n_tokens,
                "rollout_usd_per_step": rollout,
                "train_usd_per_step": mtrain,
                "total_usd_per_step": rollout + mtrain
                if rollout is not None and mtrain is not None
                else None,
            }
        )
        ptrain = (
            GRPO_ROLLOUTS * tokens * proj_rate / 1e6
            if tokens is not None and proj_rate is not None
            else None
        )
        cases.append(
            {
                "case": f"{name} [projected:{proj_trainer}]",
                "trainer_kind": "projected",
                "trainer": proj_trainer,
                "basis": "projected trainer rate + projected rollout $",
                "tokens_per_rollout": tokens,
                "training_tokens_per_step": n_tokens,
                "rollout_usd_per_step": rollout,
                "train_usd_per_step": ptrain,
                "total_usd_per_step": rollout + ptrain
                if rollout is not None and ptrain is not None
                else None,
            }
        )
    return {
        "reference_run": reference.run if reference else None,
        "rows": rows,
        "grpo_rollouts": GRPO_ROLLOUTS,
        "token_cohort": cohort.id,
        "best_eval": best.id if best else None,
        "trainer": meas_trainer,
        "trainer_usd_per_1m": meas_rate,
        "projected_trainer": proj_trainer,
        "projected_trainer_usd_per_1m": proj_rate,
        "memory_req_gib": mem_req,
        "cases": cases,
    }


def sensitivities(
    ctx: Context, scenarios: list[Scenario], ranking: list[EvalRow], flagged: list[EvalRow]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """One-input-at-a-time break-even thresholds; no joint certainty claimed. Only emitted when the threshold crosses within the search range."""
    if not ranking:
        return [], [], 0
    by_id = {s.id: s for s in scenarios}
    top = ranking[0]
    opponent = ranking[1] if len(ranking) > 1 else None
    flips: list[dict[str, Any]] = []
    suppressed = 0
    probes_by_run: dict[tuple[str, int, int], dict[str, Any]] = {}

    def measure_for(parameter: str) -> str:
        if parameter == "cmax":
            return "peak KV utilization"
        if parameter == "gpu_speed_ratio":
            return "LLM time"
        return "peak CPU/RAM + declared-limit fit + wall under one packed wave"

    def add_probe(parameter: str, scenario: Scenario) -> None:
        size = max(20, scenario.concurrency)
        key = (scenario.id, size, scenario.concurrency)
        measure = measure_for(parameter)
        existing = probes_by_run.get(key)
        if existing is None:
            probe = scenario.model_copy(update={"window_trials": size})
            result = evaluate(ctx, probe, cap_wall=True)
            probes_by_run[key] = {
                "inputs": [parameter],
                "scenario": scenario.id,
                "trials": size,
                "concurrency": scenario.concurrency,
                "run": f"{size} trials at C{scenario.concurrency} on {scenario.gpu} + {scenario.sandbox}",
                "cap_usd": math.ceil(result.total_usd * size * 100) / 100,
                "basis": "projected: model window at max(mean,p90), including cold/drain/idle and host prep/rounding",
                "measure": measure,
                "limits": "Requires separate paid approval; screen, not a statistical throughput qualification.",
                "flags": result.flags,
            }
        elif parameter not in existing["inputs"]:
            existing["inputs"].append(parameter)
            if measure not in existing["measure"]:
                existing["measure"] = existing["measure"] + "; " + measure

    for row in ranking:
        scenario = by_id[row.id]
        gpu = next(e for e in ctx.catalog.entries if e.id == row.gpu)
        ratio = gpu.specs["hbm_tbps"] / ctx.base_gpu.specs["hbm_tbps"]
        target = opponent.total_usd if row.id == top.id and opponent else top.total_usd
        if row.id == top.id and opponent is None:
            continue
        # Cheaper or more expensive GPU speeds can flip the leader. The
        # bisection is monotone even when host-hour ceilings make it discontinuous.
        lower, upper = ratio / 64, ratio * 64
        low_cost = evaluate(ctx, scenario, speed_ratio=lower).total_usd
        high_cost = evaluate(ctx, scenario, speed_ratio=upper).total_usd
        threshold = None
        if high_cost <= target <= low_cost:
            for _ in range(60):
                mid = (lower + upper) / 2
                if evaluate(ctx, scenario, speed_ratio=mid).total_usd <= target:
                    upper = mid
                else:
                    lower = mid
            threshold = upper
        if threshold is None:
            suppressed += 1
            continue
        if row.id == top.id:
            effect = f"{row.id} loses #1 if its speed ratio <= {threshold:.4g} (now {ratio:.4g})"
            direction = "below"
        else:
            effect = f"{row.id} becomes #1 if its speed ratio >= {threshold:.4g} (now {ratio:.4g})"
            direction = "above"
        flips.append(
            {
                "input": "gpu_speed_ratio",
                "scenario": row.id,
                "basis": "projected: HBM bandwidth scaling, one parameter at a time",
                "current": ratio,
                "threshold": threshold,
                "direction": direction,
                "effect": effect,
                "note": "Other scenario inputs held constant; saturated speed is unqualified.",
            }
        )
        if row.labels["gpu_speed_ratio"].startswith("projected"):
            add_probe("gpu_speed_ratio", scenario)

    # KV boundary of the leader and any cheaper-but-infeasible challenger.
    flips.append(
        {
            "input": "cmax",
            "scenario": top.id,
            "basis": "projected: KV-pool scaling",
            "current": top.cmax,
            "threshold": top.concurrency,
            "direction": "below",
            "effect": f"{top.id} leaves ranking if C_max < {top.concurrency} (now {top.cmax})",
            "note": "Any C_max below the stated concurrency disqualifies this row.",
        }
    )
    add_probe("cmax", by_id[top.id])
    for row in flagged:
        if row.total_usd < top.total_usd and row.cmax is not None and row.cmax < row.concurrency:
            flips.append(
                {
                    "input": "cmax",
                    "scenario": row.id,
                    "basis": "projected: KV-pool scaling",
                    "current": row.cmax,
                    "threshold": row.concurrency,
                    "direction": "at least",
                    "effect": f"{row.id} becomes feasible and replaces {top.id} if C_max >= {row.concurrency} (now {row.cmax})",
                    "note": "Also resolve any saturation flag before trusting the wall-time projection.",
                }
            )
            add_probe("cmax", by_id[row.id])

    # Density is discrete: scan actual host-count transitions, not a
    # continuous division that incorrectly assumes a partially filled host is free.
    for row in ranking:
        if row.per_host is None or row.required_hosts is None or opponent is None:
            continue
        scenario = by_id[row.id]
        target = opponent.total_usd if row.id == top.id else top.total_usd
        threshold_density = None
        if row.id == top.id:
            candidates = range(row.per_host - 1, 0, -1)
            for count in candidates:
                if evaluate(ctx, scenario, per_host=count).total_usd > target:
                    threshold_density = count
                    break
        else:
            for count in range(row.per_host + 1, scenario.concurrency + 1):
                if evaluate(ctx, scenario, per_host=count).total_usd < target:
                    threshold_density = count
                    break
        if threshold_density is None:
            suppressed += 1
        else:
            if row.id == top.id:
                effect = (
                    f"{row.id} loses #1 if per_host <= {threshold_density} (now {row.per_host})"
                )
                direction = "at most"
            else:
                effect = (
                    f"{row.id} wins top-1 if per_host >= {threshold_density} (now {row.per_host})"
                )
                direction = "at least"
            flips.append(
                {
                    "input": "per_host",
                    "scenario": row.id,
                    "basis": "projected: reserved-resource packing",
                    "current": row.per_host,
                    "threshold": threshold_density,
                    "direction": direction,
                    "effect": effect,
                    "note": "Controller reserve is 2 CPU / 2 GiB on every host.",
                }
            )
            add_probe("per_host", scenario)
        host = next(e for e in ctx.catalog.entries if e.id == row.sandbox)
        cpu, mem, _, _ = resources(ctx.cohort)
        for parameter, need, total, other_count, current_fraction in (
            (
                "cpu_request_fraction",
                cpu,
                host.specs["vcpu"] - 2,
                math.floor(
                    (host.specs["memory_gib"] - 2) / (mem * scenario.pack.memory_request_fraction)
                ),
                scenario.pack.cpu_request_fraction,
            ),
            (
                "memory_request_fraction",
                mem,
                host.specs["memory_gib"] - 2,
                math.floor((host.specs["vcpu"] - 2) / (cpu * scenario.pack.cpu_request_fraction)),
                scenario.pack.memory_request_fraction,
            ),
        ):
            fraction = None
            if threshold_density is not None:
                required = threshold_density + 1 if row.id == top.id else threshold_density
                if row.id == top.id or other_count >= required:
                    fraction = total / (need * required)
            if fraction is None:
                suppressed += 1
                continue
            if row.id == top.id:
                effect = (
                    f"{row.id} loses #1 if {parameter} > {fraction:.4g} (now {current_fraction})"
                )
                direction = "above"
            else:
                effect = (
                    f"{row.id} wins top-1 if {parameter} <= {fraction:.4g} (now {current_fraction})"
                )
                direction = "at most"
            flips.append(
                {
                    "input": parameter,
                    "scenario": row.id,
                    "basis": "projected: packing fraction",
                    "current": current_fraction,
                    "threshold": fraction,
                    "direction": direction,
                    "effect": effect,
                    "note": "Strict boundary for losing a floor slot; preserved task limits are not automatically resized.",
                }
            )
            add_probe(parameter, scenario)
    return flips, list(probes_by_run.values()), suppressed


def build_bench(ctx: Context, config: ScenarioFile) -> dict[str, Any]:
    ranking: list[EvalRow] = []
    flagged: list[EvalRow] = []
    skipped: list[dict[str, str]] = []
    for scenario in config.scenarios:
        try:
            row = evaluate(ctx, scenario)
        except (ValueError, KeyError) as exc:
            skipped.append({"id": scenario.id, "reason": str(exc)})
            continue
        (ranking if row.feasible else flagged).append(row)
    ranking.sort(key=lambda r: (r.total_usd, r.id))
    flips, probes, suppressed = sensitivities(ctx, config.scenarios, ranking, flagged)
    best = ranking[0] if ranking else None
    # Keep the same N for the main ranking. The sweep is separate so an N1000
    # variant does not silently win against an N200 scenario.
    sweep: list[dict[str, Any]] = []
    baseline = config.scenarios[0]
    sweep_sources = [baseline]
    if best and best.id != baseline.id:
        sweep_sources.append(next(s for s in config.scenarios if s.id == best.id))
    for scenario in sweep_sources:
        for size in config.window_sweep:
            try:
                sweep.append(
                    asdict(evaluate(ctx, scenario.model_copy(update={"window_trials": size})))
                )
            except (ValueError, KeyError) as exc:
                skipped.append({"id": f"{scenario.id}-N{size}", "reason": str(exc)})
    return {
        "schema": "evallab.price-bench-output/v1",
        "currency": "USD",
        "catalog_observed_at": ctx.catalog.observed_at,
        "profile_generated_at": ctx.profile.generated_at,
        "reference_cohort": ctx.cohort.id,
        "calibration": calibration(ctx, config.calibration_windows),
        "eval_ranking": [asdict(row) for row in ranking],
        "flagged": [asdict(row) for row in flagged],
        "skipped": skipped,
        "window_sweep": sweep,
        "training": training(ctx, config.training_token_cohort, best),
        "what_flips_the_ranking": flips,
        "probes": probes,
        "suppressed_no_crossing": suppressed,
        "assumptions": [
            "Expected spend uses mean wall: cold + (N/C)*wall_mean + max(0,p95-wall_mean) + idle.",
            f"Cold-start free parameter: {ctx.cold_s:.2f}s; {ctx.cold_basis}. Not fitted to HAR-116/G2.",
            "GPU speed: LLM portion scales by HBM ratio. Other wall time and p95-minus-mean drain remain fixed.",
            "KV pool = 0.85*GPU memory - weights/workspace; C_max=floor(0.9/scaled measured KV share).",
            "Saturation is flagged, not modeled as a free speedup; max_active uses CUDA graph batch 16 and HBM scaling.",
            "Modal serving separately bills 4 CPU core units + 16 GiB; Runpod server CPU/RAM are bundled.",
            "Per-sandbox resources: cohort declarations; otherwise verified HAR-168 task.toml 2 CPU / 8 GiB. Unknown disk assumes 10 GiB.",
            "Host bills ceil(C/per_host) whole windows, plus 600s prep; hourly-rounded hosts round each host up.",
            "Training throughput reference is the largest measured run (har129-g4); memory fit uses max peak GiB across measured runs.",
            "All eval rows are projected costs, not measured completed-valid-trial spend or accepted passes. Calibration bills are measured.",
            "Failed/excluded trials remain in historical billed denominators; no unmeasured valid-completion/retry adjustment.",
            "Catalog prices do not establish deployability, stock, egress qualification, or spend authorization. No paid probe was run.",
            "Disk/storage beyond the priced sandbox envelope, transfers, image/weight preparation, retries, taxes and long-lived storage may add unpriced costs.",
        ],
        "unknowns": ctx.profile.unknowns,
    }


def _number(value: float | None, digits: int = 4, prefix: str = "$") -> str:
    return "n/a" if value is None else f"{prefix}{value:.{digits}f}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [
        max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)
    ]
    return "\n".join(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))
        for row in [headers, ["-" * w for w in widths], *rows]
    )


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "evallab price bench — HAR-201 ($0; local-only; no paid probes run)",
        f"Catalog observed {report['catalog_observed_at']}; profile {report['profile_generated_at']}; timing reference {report['reference_cohort']}",
        "",
        "1. Calibration — $/trial; inferred cold-start free parameter, not fitted to these cohorts",
    ]
    rows = []
    for c in report["calibration"]:
        rows.append(
            [
                c["cohort"],
                str(c["windows"]) + "×" + str(c["window_trials"]),
                _number(c["predicted_server_per_trial"]),
                _number(c["billed_server_per_trial"]),
                _number(c["server_error_usd"]),
                _number(c["server_error_pct"], 1, "") + "%",
                _number(c["predicted_all_in_per_trial"]),
                _number(c["billed_all_in_per_trial"]),
                _number(c["all_in_error_usd"]),
                _number(c["all_in_error_pct"], 1, "") + "%",
            ]
        )
    lines.append(
        _table(
            [
                "cohort",
                "windows",
                "server pred",
                "billed",
                "Δ $",
                "Δ %",
                "all-in pred",
                "billed",
                "Δ $",
                "Δ %",
            ],
            rows,
        )
    )
    for c in report["calibration"]:
        lines.append(f"  {c['cohort']} [{c['basis']}]: " + " ".join(c["notes"]))
    lines += [
        "",
        "2. Eval ranking — expected $/trial at mean wall; p50/p90 displayed, not used as the expected mean",
    ]
    rows = []
    for i, r in enumerate(report["eval_ranking"], 1):
        rows.append(
            [
                str(i),
                r["id"],
                _number(r["total_usd"]),
                _number(r["server_usd"]),
                _number(r["sandbox_usd"]),
                f"{r['server_share']:.0%}/{r['sandbox_share']:.0%}",
                _number(r["per_1000_usd"], 2),
                f"{r['wall_mean_s']:.0f}",
                _number(r["wall_p50_s"], 0, ""),
                _number(r["wall_p90_s"], 0, ""),
                str(r["cmax"]),
                f"{r['per_host'] or '-'}/{r['required_hosts'] or '-'}",
            ]
        )
    lines.append(
        _table(
            [
                "#",
                "scenario [projected]",
                "$/trial",
                "server $",
                "sandbox $",
                "shares",
                "$/1000",
                "mean s",
                "p50 s",
                "p90 s",
                "C_max",
                "slots/hosts",
            ],
            rows,
        )
    )
    for r in report["eval_ranking"]:
        if r["flags"]:
            lines.append(f"  FLAG {r['id']}: " + "; ".join(r["flags"]))
    lines += ["", "3. Infeasible / flagged scenarios — infeasible rows excluded from ranking"]
    for r in report["flagged"]:
        lines.append(f"  {r['id']}: " + "; ".join(r["flags"]))
    for s in report["skipped"]:
        lines.append(f"  {s['id']}: SKIPPED — {s['reason']}")
    if not report["flagged"] and not report["skipped"]:
        lines.append("  None infeasible or skipped (quota/saturation warnings above).")
    lines += [
        "",
        "Window-size sweep — baseline and top configuration; small windows dominate historical spend",
    ]
    lines.append(
        _table(
            ["scenario", "N", "$/trial", "server $", "sandbox $", "window s"],
            [
                [
                    r["id"],
                    str(r["window_trials"]),
                    _number(r["total_usd"]),
                    _number(r["server_usd"]),
                    _number(r["sandbox_usd"]),
                    f"{r['window_s']:.0f}",
                ]
                for r in report["window_sweep"]
            ],
        )
    )
    train = report["training"]
    lines += ["", "4. Training — $/1M sequence tokens (GPU-only trainer rate; no serving CPU/RAM)"]
    lines.append(
        _table(
            ["trainer", "$/1M seq", "seq tok/s", "fit", "basis"],
            [
                [
                    r["id"],
                    _number(r["usd_per_1m_sequence_tokens"], 3),
                    _number(r["seq_tok_s"], 0, ""),
                    "ok" if r["feasible"] else "no: " + r["fit_reason"],
                    r["basis"],
                ]
                for r in train["rows"]
            ],
        )
    )
    for r in train["rows"]:
        if HOSTED_BASE in r["basis"]:
            lines.append(f"  Sampling note ({r['id']}): {r['note']}")
    lines.append(
        f"  Throughput reference {train['reference_run']}; eval={train['best_eval']}; token cohort={train['token_cohort']}; measured trainer={train['trainer']} ({_number(train['trainer_usd_per_1m'], 3)}/M); projected cheapest={train['projected_trainer']} ({_number(train['projected_trainer_usd_per_1m'], 3)}/M, memory fit >= {train['memory_req_gib']} GiB). LoRA/SFT rate is a GRPO projection."
    )
    lines.append(
        _table(
            ["case", "trainer", "tok/rollout", "rollout $/step", "train $/step", "total $/step"],
            [
                [
                    c["case"],
                    str(c["trainer"]),
                    _number(c["tokens_per_rollout"], 0, ""),
                    _number(c["rollout_usd_per_step"], 2),
                    _number(c["train_usd_per_step"], 2),
                    _number(c["total_usd_per_step"], 2),
                ]
                for c in train["cases"]
            ],
        )
    )
    lines += ["", "5. What flips the ranking — one projected input at a time, others held fixed"]
    for f in report["what_flips_the_ranking"]:
        lines.append(
            f"  {f['scenario']} / {f['input']}: now {f['current']}, threshold {f['direction']} {f['threshold']:.4g}; {f['effect']}. {f['note']}"
        )
    lines.append(
        f"  {report.get('suppressed_no_crossing', 0)} further input(s) checked with no crossing within the search range; not listed."
    )
    lines += ["", "6. Probes — proposed paid screens only; approval required"]
    for p in report["probes"]:
        inputs = ", ".join(p.get("inputs", [p.get("input", "?")]))
        lines.append(
            f"  {inputs}: {p['run']}; cap {_number(p['cap_usd'], 2)}. Measures {p['measure']}."
        )
    lines += [
        "",
        "Assumptions / limits",
        *("  - " + a for a in report["assumptions"]),
        "",
        "Profile unknowns",
        *("  - " + u for u in report["unknowns"]),
    ]
    return "\n".join(lines) + "\n"


def price_bench_command(args: argparse.Namespace, root: Path, *, harbor: Any = None) -> int:
    del harbor
    paths = [
        p if p.is_absolute() else root / p for p in (args.catalog, args.profile, args.scenarios)
    ]
    try:
        catalog, profile, scenarios = (
            load_catalog(paths[0]),
            load_profile(paths[1]),
            load_scenarios(paths[2]),
        )
        policy_path = root / "policy/daytona-limits.yaml"
        policy = yaml.safe_load(policy_path.read_text()) if policy_path.is_file() else {}
        ctx = context(catalog, profile, scenarios.reference_cohort, policy)
        report = build_bench(ctx, scenarios)
        report["inputs"] = {
            name: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for name, path in zip(("catalog", "profile", "scenarios"), paths, strict=True)
        }
    except (OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        print(f"price bench: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
        if args.json
        else render_text(report),
        end="\n" if args.json else "",
    )
    return 0


def build_price_parser(subparsers: argparse._SubParsersAction) -> None:
    price = subparsers.add_parser(
        "price", help="Read-only local price comparison (no provider calls)"
    )
    actions = price.add_subparsers(dest="price_command", required=True)
    bench = actions.add_parser(
        "bench", help="Calibrate and rank eval/training execution costs ($0)"
    )
    for option, filename in (
        ("catalog", "prices.yaml"),
        ("profile", "profile-mimo9b.json"),
        ("scenarios", "scenarios.yaml"),
    ):
        bench.add_argument(
            f"--{option}", type=Path, default=Path("research/price-bench") / filename
        )
    bench.add_argument(
        "--json", action="store_true", help="Emit reproducible machine-readable JSON"
    )
    bench.set_defaults(func=price_bench_command)
