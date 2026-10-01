"""HAR-129 item 5: log parser and cost-per-run arithmetic for the study tool."""

import importlib.util
import json
import sys
from pathlib import Path

EXP = Path("research/experiments/har129-throughput/study.py")


def _load():
    spec = importlib.util.spec_from_file_location("har129_study", EXP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DECODE_LINE = (
    "[2026-10-01 01:49:09] Decode batch, #running-req: 3, #full token: 160, "
    "full token usage: 0.06, mamba num: 4, mamba usage: 0.01, cuda graph: True, "
    "gen throughput (token/s): 224.10, #queue-req: 0"
)
PREFILL_LINE = (
    "[2026-10-01 01:49:08] Prefill batch, #new-seq: 1, #new-token: 88, "
    "#cached-token: 0, full token usage: 0.00, mamba usage: 0.01, "
    "#running-req: 0, #queue-req: 0, #pending-token: 0, cuda graph: True, "
    "input throughput (token/s): 144.99"
)
NOISE_LINES = [
    '[2026-10-01 01:49:08] INFO:     172.20.0.1:43532 - "POST '
    '/v1/chat/completions HTTP/1.1" 200 OK',
    "[2026-10-01 01:47:02] Attention backend not specified. Use flashinfer backend by default.",
    "Multi-thread loading shards: 100% Completed | 4/4",
]


def test_parse_decode_line(tmp_path):
    study = _load()
    log = tmp_path / "server.log"
    log.write_text(DECODE_LINE + "\n", encoding="utf-8")
    rows = study.parse_server_log(log, "ap-test")
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "decode"
    assert row["running_req"] == 3
    assert row["gen_tok_s"] == 224.10
    assert row["queue_req"] == 0
    assert row["full_token_usage"] == 0.06
    assert row["ts"] == "2026-10-01T01:49:09+00:00"


def test_parse_prefill_line(tmp_path):
    study = _load()
    log = tmp_path / "server.log"
    log.write_text(PREFILL_LINE + "\n", encoding="utf-8")
    (row,) = study.parse_server_log(log, "ap-test")
    assert row["kind"] == "prefill"
    assert row["new_token"] == 88
    assert row["cached_token"] == 0
    assert row["input_tok_s"] == 144.99


def test_batch_tok_s_regime_switch_at_16():
    study = _load()
    a, b = 0.01, 0.001
    # Thin eager evidence: batch means at bs=18 only.
    gen_fit = {
        "1": {"mean_gen_tok_s": 90.0},
        "16": {"mean_gen_tok_s": 16 * 44.6},
        "18": {"mean_gen_tok_s": 525.2},
    }
    g8, reg8 = study.batch_tok_s(8, a, b, gen_fit)
    assert reg8 == "graph"
    assert g8 == 8 / (a + b * 8)
    g16, reg16 = study.batch_tok_s(16, a, b, gen_fit)
    assert reg16 == "graph"
    # Eager: batch interpolation between fit-at-16 and observed bs=18.
    g17, reg17 = study.batch_tok_s(17, a, b, gen_fit)
    assert reg17 == "eager[INFERENCE]"
    assert 525.2 < g17 < g16
    g18, reg18 = study.batch_tok_s(18, a, b, gen_fit)
    assert g18 == 525.2
    # Past the largest observed batch: batch tok/s held flat (server
    # saturated); still [INFERENCE].
    g32, reg32 = study.batch_tok_s(32, a, b, gen_fit)
    assert reg32 == "eager-held[INFERENCE]"
    assert g32 == 525.2


def test_bs_mapping_and_poisson_tail():
    study = _load()
    kv = {
        "running_req_per_trial_slope": 0.39,
        "running_req_per_trial_intercept": 0.17,
    }
    assert study.bs_mean(20, kv) == 0.17 + 0.39 * 20
    assert study.bs_mean(20, {}) is None
    # Tail is negligible at the observed mean batch, dominant far above it.
    assert study.poisson_tail_gt(7.9, 16) < 0.01
    assert study.poisson_tail_gt(30.0, 16) > 0.9
    assert study.poisson_tail_gt(0.0, 16) == 0.0
    # Size-biased per-request rate at tiny lam converges to bs=1 fit value.
    a, b = 0.01, 0.001
    gen_fit = {"1": {"mean_gen_tok_s": 90.0}, "2": {"mean_gen_tok_s": 160.0}}
    r = study.expected_per_request_tok_s(0.1, a, b, gen_fit)
    assert r is not None and abs(r - 1.0 / (a + b)) < 5.0
    assert study.expected_per_request_tok_s(0.0, a, b, gen_fit) is None
    empty = study.kv_scaling([], [])
    assert empty["kv_capacity_full_tokens"] is None
    assert empty["tail_bs_gt16"]["share"] is None


def test_parse_skips_non_batch_lines(tmp_path):
    study = _load()
    log = tmp_path / "server.log"
    log.write_text("\n".join(NOISE_LINES) + "\n", encoding="utf-8")
    assert study.parse_server_log(log, "ap-test") == []


def test_fit_needs_two_batch_sizes():
    study = _load()
    assert study._fit_saturating({"1": {"mean_gen_tok_s": 80.0}}) == (None, None)
    # bs/G linear in bs (a=0.01, b=0.001): fit recovers the coefficients.
    gen_fit = {str(bs): {"mean_gen_tok_s": bs / (0.01 + 0.001 * bs)} for bs in (1, 4, 8)}
    a, b = study._fit_saturating(gen_fit)
    assert a is not None and b is not None
    assert a == abs(a) and abs(a - 0.01) < 1e-9
    assert abs(b - 0.001) < 1e-9


def _write_job(job_dir: Path, trial: str, start: str, end: str) -> None:
    job_dir.mkdir(parents=True)
    meta = {
        "command": ["harbor", "run", "--n-concurrent", "1"],
        "started_at": start,
        "finished_at": end,
        "provider_usage": {
            "calls": [
                {
                    "call_id": 1,
                    "input_tokens": 1000,
                    "output_tokens": 50,
                    "status": 200,
                    "returned_model": "m",
                }
            ]
        },
    }
    (job_dir / "lab-metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    trial_dir = job_dir / trial
    trial_dir.mkdir()
    result = {
        "started_at": start,
        "finished_at": end,
        "environment_setup": {"started_at": start, "finished_at": start},
        "agent_setup": {"started_at": start, "finished_at": start},
        "agent_execution": {"started_at": start, "finished_at": end},
        "verifier": {"started_at": end, "finished_at": end},
        "agent_result": {
            "n_input_tokens": 1000,
            "n_output_tokens": 50,
            "metadata": {"api_request_times_msec": [1000.0, 2000.0]},
        },
        "verifier_result": {"rewards": {"reward": 1.0}},
        "exception_info": {},
    }
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")


def test_collect_cost_per_run_arithmetic(tmp_path):
    study = _load()
    runs = tmp_path / "runs"
    _write_job(
        runs / "job-a",
        "job-a__t1",
        "2026-10-01T00:06:00Z",
        "2026-10-01T00:16:00Z",
    )
    _write_job(
        runs / "job-b",
        "job-b__t2",
        "2026-10-01T00:10:00Z",
        "2026-10-01T00:20:00Z",
    )
    log = tmp_path / "server.log"
    log.write_text(DECODE_LINE + "\n" + PREFILL_LINE + "\n", encoding="utf-8")
    billing = tmp_path / "billing.json"
    billing.write_text(
        json.dumps(
            [
                {
                    "object_id": "ap-test",
                    "description": "d",
                    "interval_start": "2026-10-01T00:00:00",
                    "cost": "1.50",
                },
                {
                    "object_id": "ap-other",
                    "description": "d",
                    "interval_start": "2026-10-01T00:00:00",
                    "cost": "99.00",
                },
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    rc = study.main(
        [
            "collect",
            "--job",
            str(runs / "job-*"),
            "--server-log",
            f"ap-test={log}",
            "--billing",
            str(billing),
            "--app",
            "ap-test",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["n_trials"] == 2
    # Only the attributed app counts; cost/run splits billed $ over runs.
    assert summary["billing"]["billed_usd_total"] == 1.50
    assert summary["billing"]["cost_per_run_usd"] == 0.75
    # Overlapping 00:10-00:16 window: peak concurrency is 2.
    assert summary["concurrency"]["peak_concurrent_trials"] == 2
    assert summary["server"]["n_decode_batches"] == 1
    assert summary["server"]["n_prefill_batches"] == 1
    assert summary["trials"]["llm_time_s"]["mean"] == 3.0
