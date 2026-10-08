"""Window billing, packing boundaries, KV exclusion and GRPO accounting."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError

from evallab.price_bench import (
    Billed,
    CatalogEntry,
    Cohort,
    Decode,
    Inflight,
    Pack,
    PriceCatalog,
    Resources,
    Scenario,
    ScenarioFile,
    Serving,
    Stats,
    TrainingRun,
    Workload,
    WorkloadProfile,
    build_bench,
    calibration,
    context,
    daytona_allowance,
    evaluate,
    host_density,
    sensitivities,
    training,
)


def entry(
    id: str,
    kind: str,
    *,
    rates: dict | None = None,
    specs: dict | None = None,
    billing: str = "per_second",
) -> CatalogEntry:
    return CatalogEntry.model_validate(
        {
            "id": id,
            "provider": id.split(".")[0],
            "kind": kind,
            "sku": id.split(".")[-1],
            "rates": rates or {},
            "specs": specs or {},
            "billing": billing,
            "source": "https://example.org/prices",
            "observed_at": "2026-10-08",
        }
    )


@pytest.fixture
def ctx():
    prices = PriceCatalog.model_validate(
        {
            "schema": "evallab.price-catalog/v1",
            "observed_at": "2026-10-08",
            "currency": "USD",
            "entries": [
                entry(
                    "modal.gpu.A100",
                    "gpu",
                    rates={"per_second": 0.001},
                    specs={"gpu_memory_gib": 80, "hbm_tbps": 2, "bf16_dense_tflops": 312},
                ),
                entry(
                    "modal.gpu.FAST",
                    "gpu",
                    rates={"per_second": 0.002},
                    specs={"gpu_memory_gib": 160, "hbm_tbps": 4, "bf16_dense_tflops": 624},
                ),
                entry("modal.cpu.core", "cpu", rates={"vcpu_hour": 0}),
                entry("modal.memory.gib", "memory", rates={"memory_gib_hour": 0}),
                entry(
                    "daytona.sandbox.standard",
                    "sandbox",
                    rates={"vcpu_hour": 0.05, "memory_gib_hour": 0.016, "disk_gib_hour": 0.0001},
                ),
                entry(
                    "hetzner.host.CCX63",
                    "host",
                    rates={"per_hour": 1.6},
                    specs={"vcpu": 48, "memory_gib": 192, "disk_gib": 960},
                    billing="per_hour_rounded",
                ),
                entry(
                    "tinker.hosted_training.qwen", "hosted_training", rates={"train_per_1m": 1.463}
                ),
            ],
        }
    )
    cohort = Cohort(
        id="reference",
        card="HAR-X",
        dates=["2026-10-01"],
        gpu="A100",
        concurrency=20,
        sandbox_backend="daytona",
        trials=40,
        sources=["fixture"],
        inputs_sha256="0" * 64,
        wall_s=Stats(n=40, mean=600, p50=500, p90=900, p95=1200, max=1500),
        llm_s=Stats(n=40, mean=200),
        input_tokens=Stats(n=40, p50=1_000_000),
        model_requests=Stats(n=40, p50=50),
        inflight_requests=Inflight(mean=4.5, peak=19),
        kv_usage_max=0.42,
        decode_tok_s=Decode(value=236, meaning="aggregate"),
        billed=Billed(server_usd=3.24, all_in_usd=6, source="invoice fixture"),
        declared_sandbox=Resources(cpus=2, memory_gib=8, disk_gib=10),
    )
    profile = WorkloadProfile.model_validate(
        {
            "schema": "evallab.workload-profile/v1",
            "generated_at": "2026-10-08T00:00:00Z",
            "workload": Workload(model="mimo9b", agent="mimoagent", task_set="swe"),
            "cohorts": [cohort],
            "serving": Serving(
                cold_start_s=100,
                scaledown_window_s=300,
                cuda_graph_max_bs=16,
                max_running_requests=20,
                weights_gib=17.5,
                kv_bytes_per_token=32768,
                source="fixture",
            ),
            "training": [
                TrainingRun(
                    run="dryrun",
                    gpu="A100",
                    seq_tokens=143829,
                    seconds=256.9,
                    seq_tok_s=500,
                    peak_gib=52.66,
                    usd=0.28,
                    source="fixture",
                ),
                TrainingRun(
                    run="g4",
                    gpu="A100",
                    seq_tokens=1569955,
                    seconds=1330.3,
                    seq_tok_s=1180,
                    peak_gib=38.84,
                    usd=1.2696,
                    source="fixture",
                ),
            ],
            "unknowns": [],
        }
    )
    policy = {"safety_fraction": 0.8, "quota": {"cpu": 100, "memory_gib": 200, "disk_gib": 300}}
    return context(prices, profile, "reference", policy)


def scenario(
    *,
    gpu: str = "modal.gpu.A100",
    concurrency: int = 20,
    sandbox: str = "daytona.sandbox.standard",
    n: int = 20,
    pack: Pack | None = None,
) -> Scenario:
    return Scenario(
        id="case",
        gpu=gpu,
        concurrency=concurrency,
        sandbox=sandbox,
        window_trials=n,
        pack=pack or Pack(),
    )


def test_expected_window_uses_mean_plus_drain_not_median_or_busy_time(ctx):
    row = evaluate(ctx, scenario())
    assert row.window_s == 100 + 600 + (1200 - 600) + 300
    assert row.server_usd == pytest.approx(0.08)
    assert row.sandbox_usd == pytest.approx(0.229 * 600 / 3600)
    assert row.total_usd == pytest.approx(0.1181666666667)
    assert row.wall_p50_s == 500
    assert row.wall_p90_s == 900
    warm = evaluate(ctx, scenario(n=200))
    assert warm.window_s == 7000
    assert warm.server_usd == pytest.approx(0.035)


def test_calibration_billed_totals_and_two_independent_windows(ctx):
    row = calibration(ctx, {"reference": 20})[0]
    assert row["windows"] == 2
    assert row["billed_server_per_trial"] == pytest.approx(0.081)
    assert row["predicted_server_per_trial"] == pytest.approx(0.08)
    assert row["server_error_usd"] == pytest.approx(-0.001)
    assert row["server_error_pct"] == pytest.approx(-100 / 81)
    assert row["all_in_error_usd"] == pytest.approx(0.1181666666667 - 0.15)


def test_reference_upper_bounds_are_not_server_calibration(ctx):
    upper = ctx.cohort.model_copy(
        update={"id": "har168", "billed": Billed(all_in_usd=15.7, per_trial_usd=0.654)}
    )
    profile = ctx.profile.model_copy(update={"cohorts": [upper]})
    row = calibration(replace(ctx, profile=profile), {})[0]
    assert row["predicted_server_per_trial"] is None
    assert row["server_error_pct"] is None
    assert row["billed_all_in_per_trial"] == pytest.approx(15.7 / 40)
    assert row["basis"].startswith("reference-only")


def test_kv_capacity_boundary_excludes_only_infeasible_rows(ctx):
    assert evaluate(ctx, scenario(concurrency=42)).feasible
    assert not evaluate(ctx, scenario(concurrency=43)).feasible
    config = ScenarioFile.model_validate(
        {
            "schema": "evallab.price-scenarios/v1",
            "reference_cohort": "reference",
            "scenarios": [
                scenario(concurrency=42).model_copy(update={"id": "fits"}),
                scenario(concurrency=43).model_copy(update={"id": "too-big"}),
            ],
        }
    )
    report = build_bench(ctx, config)
    assert [r["id"] for r in report["eval_ranking"]] == ["fits"]
    assert [r["id"] for r in report["flagged"]] == ["too-big"]


def test_active_streams_uses_measured_fraction_and_flags_saturation(ctx):
    observed = ctx.cohort.model_copy(update={"inflight_requests": Inflight(mean=10)})
    row = evaluate(replace(ctx, cohort=observed), scenario(concurrency=40))
    assert row.active_streams == 20
    assert row.max_active_streams == 16
    assert any(flag.startswith("saturated") for flag in row.flags)
    fast = evaluate(ctx, scenario(gpu="modal.gpu.FAST", concurrency=40))
    assert fast.wall_mean_s == 500  # only 200s LLM portion halved
    assert fast.max_active_streams == 32
    fallback = ctx.cohort.model_copy(update={"inflight_requests": Inflight()})
    assert evaluate(replace(ctx, cohort=fallback), scenario()).active_streams == pytest.approx(
        20 / 3
    )


def test_pack_density_reserves_controller_and_bills_whole_hosts(ctx):
    host = next(e for e in ctx.catalog.entries if e.kind == "host")
    assert host_density(host, 2, 8, Pack()) == 23
    packed = Pack(cpu_request_fraction=0.25, memory_request_fraction=0.4)
    assert host_density(host, 2, 8, packed) == 59
    full = evaluate(ctx, scenario(concurrency=40, n=200, sandbox=host.id))
    assert full.required_hosts == 2
    assert full.billed_host_hours == 2  # window4000s + prep600s rounds to2h
    assert full.sandbox_usd == pytest.approx(0.032)
    fit = evaluate(ctx, scenario(concurrency=40, n=200, sandbox=host.id, pack=packed))
    assert fit.required_hosts == 1
    assert fit.sandbox_usd == pytest.approx(0.016)


def test_hour_rounding_changes_at_boundary(ctx):
    # C40,N200: wall window4000s. Select prep/cold to straddle 3600s.
    base = scenario(concurrency=40, n=200, sandbox="hetzner.host.CCX63")
    a = evaluate(ctx, base.model_copy(update={"cold_start_s": 0, "idle_tail_s": 0, "prep_s": 0}))
    b = evaluate(
        ctx, base.model_copy(update={"cold_start_s": 0, "idle_tail_s": 0, "prep_s": 0.001})
    )
    assert a.window_s == 3600
    assert a.billed_host_hours == 1
    assert b.billed_host_hours == 2


def test_daytona_quota_matches_19_wide_reserved_clamp(ctx):
    assert daytona_allowance(ctx.policy, 2, 8, 10) == 19
    assert any("allowance 19" in f for f in evaluate(ctx, scenario()).flags)


def test_missing_catalog_scenario_skipped_without_assumed_price(ctx):
    config = ScenarioFile.model_validate(
        {
            "schema": "evallab.price-scenarios/v1",
            "reference_cohort": "reference",
            "scenarios": [scenario().model_copy(update={"gpu": "missing.gpu.unknown"})],
        }
    )
    report = build_bench(ctx, config)
    assert not report["eval_ranking"]
    assert "missing.gpu.unknown" in report["skipped"][0]["reason"]


def test_grpo_uses_measured_trainer_with_projected_second_line(ctx):
    best = evaluate(ctx, scenario())
    result = training(ctx, "reference", best)
    assert result["reference_run"] == "g4"
    assert result["memory_req_gib"] == pytest.approx(52.66)
    assert result["trainer"] == "measured:g4"
    assert result["trainer_usd_per_1m"] == pytest.approx(1.2696 / 1569955 * 1e6)
    assert result["projected_trainer"] == "modal.gpu.A100"
    assert result["projected_trainer_usd_per_1m"] == pytest.approx(1000 / 1180)
    measured_ids = [r["id"] for r in result["rows"] if str(r["basis"]).startswith("measured:")]
    assert "measured:g4" in measured_ids and "measured:dryrun" in measured_ids
    g4 = next(r for r in result["rows"] if r["id"] == "measured:g4")
    assert g4["seq_tok_s"] == pytest.approx(1180)
    assert g4["usd_per_1m_sequence_tokens"] == pytest.approx(1.2696 / 1569955 * 1e6)
    by_case = {(c["case"], c["trainer_kind"]): c for c in result["cases"]}
    prefix_m = by_case[("prefix-consistent (one sequence) [measured:g4]", "measured")]
    prefix_p = by_case[("prefix-consistent (one sequence) [projected:modal.gpu.A100]", "projected")]
    assert prefix_m["tokens_per_rollout"] == 40_000
    assert prefix_m["training_tokens_per_step"] == 128 * 40_000
    assert prefix_m["train_usd_per_step"] == pytest.approx(128 * 40_000 * 1.2696 / 1569955)
    assert prefix_p["train_usd_per_step"] == pytest.approx(128 * 40_000 * (1000 / 1180) / 1e6)
    assert prefix_m["rollout_usd_per_step"] == pytest.approx(128 * best.total_usd)
    percall_m = by_case[
        ("per-call (every call separately; expensive case) [measured:g4]", "measured")
    ]
    assert percall_m["train_usd_per_step"] == pytest.approx(128 * 1.2696 / 1569955 * 1e6)
    assert percall_m["total_usd_per_step"] == pytest.approx(
        percall_m["rollout_usd_per_step"] + percall_m["train_usd_per_step"]
    )


def test_unknown_token_counts_remain_null(ctx):
    cohort = ctx.cohort.model_copy(update={"input_tokens": Stats(), "model_requests": Stats()})
    profile = ctx.profile.model_copy(update={"cohorts": [cohort]})
    result = training(
        replace(ctx, profile=profile, cohort=cohort), "reference", evaluate(ctx, scenario())
    )
    assert all(c["train_usd_per_step"] is None for c in result["cases"])
    assert all(c["total_usd_per_step"] is None for c in result["cases"])


def test_flip_suppression_counts_inputs_with_no_crossing(ctx):
    cheap = scenario()
    pricey = Scenario(
        id="pricey",
        gpu="modal.gpu.A100",
        concurrency=20,
        sandbox="daytona.sandbox.standard",
        window_trials=20,
        region_multiplier=1000.0,
    )
    rows = sorted([evaluate(ctx, cheap), evaluate(ctx, pricey)], key=lambda r: (r.total_usd, r.id))
    flips, _probes, suppressed = sensitivities(ctx, [cheap, pricey], rows, [])
    assert all(f["threshold"] is not None for f in flips)
    assert suppressed == 2
    assert [f["input"] for f in flips] == ["cmax"]


def test_probes_merge_inputs_sharing_one_run(ctx):
    h1 = Scenario(
        id="h1",
        gpu="modal.gpu.A100",
        concurrency=40,
        sandbox="hetzner.host.CCX63",
        window_trials=200,
    )
    h2 = Scenario(
        id="h2",
        gpu="modal.gpu.A100",
        concurrency=40,
        sandbox="hetzner.host.CCX63",
        window_trials=200,
    )
    rows = sorted([evaluate(ctx, h1), evaluate(ctx, h2)], key=lambda r: (r.total_usd, r.id))
    _flips, probes, _suppressed = sensitivities(ctx, [h1, h2], rows, [])
    assert len(probes) == 2
    keys = [(p["scenario"], p["trials"], p["concurrency"]) for p in probes]
    assert len(set(keys)) == len(keys)
    by_scenario = {p["scenario"]: p for p in probes}
    assert "cmax" in by_scenario["h1"]["inputs"] and "per_host" in by_scenario["h1"]["inputs"]
    assert sum(len(p["inputs"]) for p in probes) > len(probes)


def test_catalog_rejects_duplicate_ids_and_negative_rates(ctx):
    with pytest.raises(ValidationError, match="unique"):
        PriceCatalog.model_validate(
            {
                "schema": "evallab.price-catalog/v1",
                "observed_at": "2026-10-08",
                "currency": "USD",
                "entries": [ctx.catalog.entries[0]] * 2,
            }
        )
    with pytest.raises(ValidationError):
        entry("broken.gpu.rate", "gpu", rates={"per_hour": -1})
