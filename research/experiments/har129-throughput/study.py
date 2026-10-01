#!/usr/bin/env python3
"""HAR-129 item 5: throughput and cost per run for self-hosted MiMo on Modal.

Two subcommands:

  collect  job dirs + SGLang server logs + Modal billing rows
           -> tidy CSVs + summary.json in --out
  report   summary.json -> text report with the cost-optimal concurrency
           model and GPU comparison

Inputs are G2/G5-style telemetry (HAR-116 lab-metadata.json /
result.json / trajectory.json job dirs, `modal app logs` output, and
`modal billing report --resolution h --json` output). Nothing here makes
model calls, launches GPUs, or touches Daytona: it only reads files.

Rates are reused from existing code, never re-derived here:
  evallab.execution_contracts.MIMO_SELFHOSTED_SERVER_USD_PER_HOUR /
  mimo_selfhosted_trial_cost_usd (server side) and
  evallab.task_qualification.estimate_cost_usd / DAYTONA_RATE_CARD
  (Daytona sandbox side).

Anything not measured is marked [INFERENCE] in the report output.
"""

from __future__ import annotations

import argparse
import csv
import glob
import itertools
import json
import math
import re
import statistics
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ----------------------------------------------------------------------------
# Server log parsing (SGLang scheduler lines)


_DECODE_RE = re.compile(
    r"Decode batch, #running-req: (?P<running>\d+), "
    r"#full token: (?P<full>\d+), "
    r"full token usage: (?P<usage>[\d.]+), "
    r"(?:mamba num: (?P<mamba_num>\d+), )?"
    r"(?:mamba usage: (?P<mamba_usage>[\d.]+), )?"
    r"(?:cuda graph: (?P<cuda_graph>\w+), )?"
    r"gen throughput \(token/s\): (?P<gen>[\d.]+), "
    r"#queue-req: (?P<queue>\d+)"
)
_PREFILL_RE = re.compile(
    r"Prefill batch, #new-seq: (?P<new_seq>\d+), "
    r"#new-token: (?P<new_token>\d+), "
    r"#cached-token: (?P<cached>\d+), "
    r"full token usage: (?P<usage>[\d.]+), "
    r"(?:mamba usage: (?P<mamba_usage>[\d.]+), )?"
    r"(?:#running-req: (?P<running>\d+), )?"
    r"(?:#queue-req: (?P<queue>\d+), )?"
    r"(?:#pending-token: (?P<pending>\d+), )?"
    r"(?:cuda graph: (?P<cuda_graph>\w+), )?"
    r"input throughput \(token/s\): (?P<put>[\d.]+)"
)
_TS_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


def parse_server_log(path: Path, app_id: str) -> list[dict]:
    """Parse one `modal app logs` capture into batch rows.

    Timestamps in SGLang container logs carry no zone; the serving
    container runs on UTC (Modal tunnel lines in the same capture use
    +0000), so they are treated as UTC.
    """
    rows: list[dict] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = _TS_RE.match(line)
            if not m:
                continue
            ts = m.group("ts").replace(" ", "T") + "+00:00"
            dm = _DECODE_RE.search(line)
            if dm:
                rows.append(
                    {
                        "app_id": app_id,
                        "ts": ts,
                        "kind": "decode",
                        "running_req": int(dm.group("running")),
                        "full_token": int(dm.group("full")),
                        "full_token_usage": float(dm.group("usage")),
                        "gen_tok_s": float(dm.group("gen")),
                        "input_tok_s": "",
                        "queue_req": int(dm.group("queue")),
                        "new_seq": "",
                        "new_token": "",
                        "cached_token": "",
                    }
                )
                continue
            pm = _PREFILL_RE.search(line)
            if pm:
                rows.append(
                    {
                        "app_id": app_id,
                        "ts": ts,
                        "kind": "prefill",
                        "running_req": int(pm.group("running") or 0),
                        "full_token": "",
                        "full_token_usage": float(pm.group("usage")),
                        "gen_tok_s": "",
                        "input_tok_s": float(pm.group("put")),
                        "queue_req": int(pm.group("queue") or 0),
                        "new_seq": int(pm.group("new_seq")),
                        "new_token": int(pm.group("new_token")),
                        "cached_token": int(pm.group("cached")),
                    }
                )
    return rows


# ----------------------------------------------------------------------------
# Job / trial parsing (HAR-116-style run dirs)


def _block_seconds(block: dict | None) -> float | None:
    if not isinstance(block, dict):
        return None
    start = _parse_ts(block.get("started_at"))
    end = _parse_ts(block.get("finished_at"))
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def _job_concurrency(command: list) -> int | None:
    try:
        i = command.index("--n-concurrent")
        return int(command[i + 1])
    except (ValueError, IndexError):
        return None


def collect_trials(job_dirs: list[Path]) -> tuple[list[dict], list[dict]]:
    """Return (trial_rows, call_rows) for the given job directories."""
    trials: list[dict] = []
    calls: list[dict] = []
    for job_dir in sorted(job_dirs):
        meta_path = job_dir / "lab-metadata.json"
        if not meta_path.is_file():
            print(f"skip {job_dir}: no lab-metadata.json", file=sys.stderr)
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"skip {job_dir}: {exc}", file=sys.stderr)
            continue
        command = meta.get("command") or []
        job_conc = _job_concurrency(command) if isinstance(command, list) else None
        job_calls = []
        for call in (meta.get("provider_usage") or {}).get("calls") or []:
            # lab-metadata calls carry no timestamps, so per-call LLM time
            # is not derivable from this file (see trial api_request_times).
            job_calls.append(
                {
                    "job": job_dir.name,
                    "trial": "",
                    "call_id": call.get("call_id"),
                    "input_tokens": call.get("input_tokens"),
                    "output_tokens": call.get("output_tokens"),
                    "status": call.get("status"),
                    "returned_model": call.get("returned_model"),
                }
            )
        trial_dirs = sorted(d for d in job_dir.iterdir() if d.is_dir() and "__" in d.name)
        for trial_dir in trial_dirs:
            result_path = trial_dir / "result.json"
            if not result_path.is_file():
                continue
            try:
                res = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"skip {trial_dir}: {exc}", file=sys.stderr)
                continue
            agent_result = res.get("agent_result") or {}
            timings = agent_result.get("metadata", {}).get("api_request_times_msec") or []
            llm_time_s = sum(timings) / 1000.0 if timings else None
            agent_exec_s = _block_seconds(res.get("agent_execution"))
            tool_time_s = (
                agent_exec_s - llm_time_s
                if agent_exec_s is not None and llm_time_s is not None
                else None
            )
            verifier_reward = ((res.get("verifier_result") or {}).get("rewards") or {}).get(
                "reward"
            )
            exc_info = res.get("exception_info") or {}
            trials.append(
                {
                    "job": job_dir.name,
                    "trial": trial_dir.name,
                    "started_at": res.get("started_at"),
                    "finished_at": res.get("finished_at"),
                    "wall_s": _block_seconds(
                        {"started_at": res.get("started_at"), "finished_at": res.get("finished_at")}
                    ),
                    "env_setup_s": _block_seconds(res.get("environment_setup")),
                    "agent_setup_s": _block_seconds(res.get("agent_setup")),
                    "agent_exec_s": agent_exec_s,
                    "verifier_s": _block_seconds(res.get("verifier")),
                    "n_calls": len(timings),
                    "llm_time_s": llm_time_s,
                    "tool_time_s": tool_time_s,
                    "input_tokens": agent_result.get("n_input_tokens"),
                    "output_tokens": agent_result.get("n_output_tokens"),
                    "reward": verifier_reward,
                    "exception": exc_info.get("exception_type"),
                    "job_n_concurrent": job_conc,
                }
            )
        job_trials = [t["trial"] for t in trials if t["job"] == job_dir.name]
        if len(job_trials) == 1:
            # One trial per job (e.g. --n-tasks 1): the job-level provider
            # calls belong to it. With several trials per job the calls
            # cannot be attributed (no trial id on lab-metadata calls).
            for c in job_calls:
                c["trial"] = job_trials[0]
        calls.extend(job_calls)
    return trials, calls


def concurrency_series(trials: list[dict], step_s: int = 60) -> tuple[list[dict], int, float]:
    """Sample concurrent-trial count over the session.

    Returns (rows, max_concurrent, time_weighted_mean). Rows are per-step
    samples with the count of trials whose [started_at, finished_at)
    covers the sample time.
    """
    intervals = []
    for t in trials:
        s = _parse_ts(t.get("started_at"))
        f = _parse_ts(t.get("finished_at"))
        if s is not None and f is not None and f > s:
            intervals.append((s, f))
    if not intervals:
        return [], 0, 0.0
    start = min(s for s, _ in intervals)
    end = max(f for _, f in intervals)
    rows: list[dict] = []
    t = start
    total = 0.0
    span = (end - start).total_seconds()
    peak = 0
    while t < end:
        n = sum(1 for s, f in intervals if s <= t < f)
        peak = max(peak, n)
        rows.append({"ts": t.isoformat(), "concurrent_trials": n})
        total += n * step_s
        t = t + timedelta(seconds=step_s)
    mean = total / span if span > 0 else 0.0
    return rows, peak, mean


# ----------------------------------------------------------------------------
# collect


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def _median(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def cmd_collect(args: argparse.Namespace) -> int:
    from evallab.execution_contracts import MIMO_SELFHOSTED_SERVER_USD_PER_HOUR

    job_dirs: list[Path] = []
    for pattern in args.job:
        job_dirs.extend(Path(p) for p in glob.glob(pattern))
    job_dirs = sorted({p for p in job_dirs if p.is_dir()})
    if not job_dirs:
        print("no job dirs matched", file=sys.stderr)
        return 2

    trials, calls = collect_trials(job_dirs)

    batch_rows: list[dict] = []
    for spec in args.server_log or []:
        if "=" in spec:
            app_id, log_path = spec.split("=", 1)
        else:
            log_path = spec
            app_id = Path(spec).stem
        batch_rows.extend(parse_server_log(Path(log_path), app_id))
    batch_rows.sort(key=lambda r: r["ts"])

    billing_rows: list[dict] = []
    if args.billing:
        billing_rows = json.loads(Path(args.billing).read_text(encoding="utf-8"))
        if isinstance(billing_rows, dict):
            billing_rows = billing_rows.get("rows", billing_rows)
    billed_by_app: dict[str, float] = {}
    for row in billing_rows or []:
        if args.app and row.get("object_id") not in args.app:
            continue
        billed_by_app[row.get("object_id", "?")] = billed_by_app.get(
            row.get("object_id", "?"), 0.0
        ) + float(row.get("cost", 0.0) or 0.0)
    billed_total = sum(billed_by_app.values())

    conc_rows, peak, conc_mean = concurrency_series(trials)

    decodes = [r for r in batch_rows if r["kind"] == "decode"]
    prefills = [r for r in batch_rows if r["kind"] == "prefill"]
    gen_by_bs: dict[int, list[float]] = {}
    for r in decodes:
        gen_by_bs.setdefault(r["running_req"], []).append(r["gen_tok_s"])
    gen_fit = {
        str(bs): {
            "n": len(v),
            "mean_gen_tok_s": statistics.mean(v),
            "median_gen_tok_s": statistics.median(v),
        }
        for bs, v in sorted(gen_by_bs.items())
    }
    queue_batches = sum(1 for r in decodes if (r["queue_req"] or 0) > 0)

    walls = [t["wall_s"] for t in trials]
    llms = [t["llm_time_s"] for t in trials]
    tools = [t["tool_time_s"] for t in trials]
    exc_groups: dict[str, list[dict]] = {}
    for t in trials:
        exc_groups.setdefault(t["exception"] or "none", []).append(t)
    # Experienced concurrency per trial: mean concurrent-trial count over
    # the trial's own span (differs from the session time-weighted mean,
    # which includes idle gaps between waves).
    conc_samples = [(_parse_ts(c["ts"]), c["concurrent_trials"]) for c in conc_rows]
    for t in trials:
        s = _parse_ts(t.get("started_at"))
        f = _parse_ts(t.get("finished_at"))
        vals = [n for ts, n in conc_samples if ts is not None and s is not None and s <= ts < f]
        # conc_rows holds ints in memory (CSV strings only on disk).
        t["experienced_concurrency"] = (sum(vals) / len(vals)) if vals else None
    by_exc = {
        exc: {
            "n": len(group),
            "wall_s_mean": _mean([g["wall_s"] for g in group]),
            "wall_s_median": _median([g["wall_s"] for g in group]),
            "llm_time_s_mean": _mean([g["llm_time_s"] for g in group]),
            "tool_time_s_mean": _mean([g["tool_time_s"] for g in group]),
            "mean_output_tokens": _mean([float(g["output_tokens"] or 0) for g in group]),
            "experienced_concurrency_mean": _mean([g["experienced_concurrency"] for g in group]),
        }
        for exc, group in sorted(exc_groups.items())
    }

    summary = {
        "n_job_dirs": len(job_dirs),
        "n_trials": len(trials),
        "n_provider_calls": len(calls),
        "session_window": {
            "start": min((t["started_at"] for t in trials if t["started_at"]), default=None),
            "end": max((t["finished_at"] for t in trials if t["finished_at"]), default=None),
        },
        "job_n_concurrent_values": sorted(
            {t["job_n_concurrent"] for t in trials if t["job_n_concurrent"] is not None}
        ),
        "concurrency": {
            "peak_concurrent_trials": peak,
            "time_weighted_mean_concurrent_trials": conc_mean,
        },
        "trials": {
            "wall_s": {
                "mean": _mean(walls),
                "median": _median(walls),
                "max": max(walls) if walls else None,
            },
            "llm_time_s": {"mean": _mean(llms), "median": _median(llms)},
            "tool_time_s": {"mean": _mean(tools), "median": _median(tools)},
            "input_tokens_total": sum(t["input_tokens"] or 0 for t in trials),
            "output_tokens_total": sum(t["output_tokens"] or 0 for t in trials),
            "mean_output_tokens": _mean([float(t["output_tokens"] or 0) for t in trials]),
            "by_exception": by_exc,
        },
        "server": {
            "n_decode_batches": len(decodes),
            "n_prefill_batches": len(prefills),
            "gen_tok_s_by_batch_size": gen_fit,
            "gen_tok_s_overall_mean": statistics.mean([r["gen_tok_s"] for r in decodes])
            if decodes
            else None,
            "input_tok_s_prefill_median": statistics.median([r["input_tok_s"] for r in prefills])
            if prefills
            else None,
            "queue_req_gt0_batches": queue_batches,
            "queue_req_gt0_share": (queue_batches / len(decodes)) if decodes else None,
            "full_token_usage_max": max(
                [r["full_token_usage"] for r in batch_rows if r["full_token_usage"] != ""],
                default=None,
            ),
            "kv_scaling": kv_scaling(batch_rows, conc_rows),
        },
        "billing": {
            "billed_usd_by_app": billed_by_app,
            "billed_usd_total": billed_total,
            "cost_per_run_usd": (billed_total / len(trials)) if trials else None,
            "server_rate_usd_per_h": MIMO_SELFHOSTED_SERVER_USD_PER_HOUR,
        },
    }

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    _write_csv(
        out / "trials.csv",
        trials,
        [
            "job",
            "trial",
            "started_at",
            "finished_at",
            "wall_s",
            "env_setup_s",
            "agent_setup_s",
            "agent_exec_s",
            "verifier_s",
            "n_calls",
            "llm_time_s",
            "tool_time_s",
            "input_tokens",
            "output_tokens",
            "reward",
            "exception",
            "job_n_concurrent",
            "experienced_concurrency",
        ],
    )
    _write_csv(
        out / "calls.csv",
        calls,
        ["job", "trial", "call_id", "input_tokens", "output_tokens", "status", "returned_model"],
    )
    _write_csv(
        out / "server_batches.csv",
        batch_rows,
        [
            "app_id",
            "ts",
            "kind",
            "running_req",
            "full_token",
            "full_token_usage",
            "gen_tok_s",
            "input_tok_s",
            "queue_req",
            "new_seq",
            "new_token",
            "cached_token",
        ],
    )
    _write_csv(out / "concurrency.csv", conc_rows, ["ts", "concurrent_trials"])
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(
        f"jobs={len(job_dirs)} trials={len(trials)} "
        f"calls={len(calls)} batches={len(batch_rows)} "
        f"peak_concurrent={peak} billed=${billed_total:.4f}"
    )
    print(f"wrote {out}/trials.csv calls.csv server_batches.csv concurrency.csv summary.json")
    return 0


# ----------------------------------------------------------------------------
# report + model

GRAPH_BS = 16  # --cuda-graph-max-bs-decode: the largest batch SGLang captures
# a CUDA graph for. Above it SGLang still batches every running request but
# decodes in eager mode, so per-request rate follows a separate regime.


def _fit_saturating(gen_fit: dict) -> tuple[float | None, float | None]:
    """Least-squares fit of batch throughput G(bs) = bs / (a + b*bs).

    Only CUDA-graph batches (bs <= 16) enter the fit: eager batches above
    the graph capture size follow a different regime. Regress bs/G on bs
    (linear: y = a + b*x); returns (a, b) in s/token units, or (None, None)
    when fewer than 2 graph-regime batch sizes were observed.
    """
    xs: list[float] = []
    ys: list[float] = []
    for bs_s, agg in gen_fit.items():
        bs = float(bs_s)
        g = agg["mean_gen_tok_s"]
        if g and g > 0 and 0 < bs <= GRAPH_BS:
            xs.append(bs)
            ys.append(bs / g)
    if len(xs) < 2:
        return None, None
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None, None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    a = my - b * mx
    return a, b


def batch_tok_s(bs: float, a: float, b: float, gen_fit: dict) -> tuple[float, str]:
    """Batch decode throughput at batch size bs, with a regime label.

    bs <= 16: saturating fit bs/(a+b*bs) (CUDA-graph regime). Above 16:
    linear interpolation between the fit value at 16 and observed mean
    batch throughputs, held flat past the largest observed batch size.
    Observed eager points are usually thin and the flat hold is optimistic,
    so every bs > 16 rate is labelled [INFERENCE].
    """
    if bs <= GRAPH_BS:
        return bs / (a + b * bs), "graph"
    obs = {
        int(k): v["mean_gen_tok_s"]
        for k, v in gen_fit.items()
        if int(k) > GRAPH_BS and v["mean_gen_tok_s"] > 0
    }
    if not obs:
        return bs / (a + b * bs), "graph-extrapolated[INFERENCE]"
    pts = [(GRAPH_BS, GRAPH_BS / (a + b * GRAPH_BS)), *sorted(obs.items())]
    for (b0, g0), (b1, g1) in itertools.pairwise(pts):
        if bs <= b1:
            t = (bs - b0) / (b1 - b0)
            return g0 + t * (g1 - g0), "eager[INFERENCE]"
    return pts[-1][1], "eager-held[INFERENCE]"


def bs_mean(c: int, kv: dict) -> float | None:
    """Mean decode batch size at C concurrent trials, from the measured
    running-req regression (trials decode a fraction of wall time)."""
    slope = kv.get("running_req_per_trial_slope")
    intercept = kv.get("running_req_per_trial_intercept")
    if slope is None or intercept is None:
        return None
    return intercept + slope * c


def poisson_tail_gt(lam: float, k: int) -> float:
    """P(X > k) for X ~ Poisson(lam), stdlib only."""
    if lam <= 0:
        return 0.0
    term = math.exp(-lam)
    cdf = term  # P(X == 0)
    for i in range(1, k + 1):
        term *= lam / i
        cdf += term
    return max(0.0, 1.0 - cdf)


def expected_per_request_tok_s(
    lam: float, a: float, b: float, gen_fit: dict, kmax: int = 200
) -> float | None:
    """Mean per-request decode tok/s when batches follow Poisson(lam).

    A decoding request sees a size-biased batch (P(B=b) ∝ b·Pois(b; lam)),
    so r_req = E[G(B)/B] = E[G(X)·1{X≥1}]/lam for X ~ Poisson(lam).
    """
    if lam <= 0:
        return None
    term = math.exp(-lam)  # P(X == 0)
    total = 0.0
    for k in range(1, kmax + 1):
        term *= lam / k  # P(X == k)
        total += term * batch_tok_s(k, a, b, gen_fit)[0]
    return total / lam


def kv_scaling(batch_rows: list[dict], conc_rows: list[dict]) -> dict:
    """Relate KV full-token usage to concurrent trials (all measured).

    Returns KV capacity, the running-req regression slope/intercept, mean
    full-tokens per running request, and the empirical share of decode
    batches above the graph batch size in peak-concurrency minutes.
    """
    decodes = [r for r in batch_rows if r["kind"] == "decode"]
    ratios = [
        r["full_token"] / r["full_token_usage"]
        for r in decodes
        if r["full_token_usage"] not in ("", 0, 0.0) and r["full_token"]
    ]
    conc = {c["ts"][:16]: c["concurrent_trials"] for c in conc_rows}
    per_min: dict[str, list[int]] = {}
    for r in decodes:
        if r["ts"][:16] in conc and conc[r["ts"][:16]] > 0:
            per_min.setdefault(r["ts"][:16], []).append(r["running_req"])
    xs = [conc[m] for m in per_min]
    ys = [statistics.mean(v) for v in per_min.values()]
    slope = intercept = None
    if len(xs) >= 2:
        mx = sum(xs) / len(xs)
        my = sum(ys) / len(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx > 0:
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
            intercept = my - slope * mx
    full = sum(r["full_token"] for r in decodes if r["full_token"] != "")
    running = sum(r["running_req"] for r in decodes)
    peak_band = max(conc.values(), default=0) - 2
    band = [b for m, v in per_min.items() if conc[m] >= peak_band for b in v]
    return {
        "kv_capacity_full_tokens": statistics.median(ratios) if ratios else None,
        "running_req_per_trial_slope": slope,
        "running_req_per_trial_intercept": intercept,
        "full_token_per_running_req_mean": (full / running) if running else None,
        "tail_bs_gt16": {
            "min_concurrent_trials": peak_band,
            "minutes": sum(1 for m in per_min if conc[m] >= peak_band),
            "batches": len(band),
            "share": (sum(1 for b in band if b > GRAPH_BS) / len(band)) if band else None,
        },
    }


def cmd_report(args: argparse.Namespace) -> int:
    from evallab.execution_contracts import (
        MIMO_SELFHOSTED_SERVER_USD_PER_HOUR,
        mimo_selfhosted_trial_cost_usd,
    )
    from evallab.task_qualification import DAYTONA_RATE_CARD, estimate_cost_usd

    data = Path(args.data)
    summary = json.loads((data / "summary.json").read_text(encoding="utf-8"))
    trials_s = summary["trials"]
    server = summary["server"]
    billing = summary["billing"]
    conc = summary["concurrency"]

    rate = MIMO_SELFHOSTED_SERVER_USD_PER_HOUR
    n = summary["n_trials"]
    cost_per_run = billing["cost_per_run_usd"]
    wall_mean = trials_s["wall_s"]["mean"]
    llm_mean = trials_s["llm_time_s"]["mean"]
    tool_mean = trials_s["tool_time_s"]["mean"]
    mean_out = trials_s["mean_output_tokens"]

    gen_fit = server["gen_tok_s_by_batch_size"]
    a, b = _fit_saturating(gen_fit)

    lines: list[str] = []
    lines.append("HAR-129 item 5: throughput and cost per run")
    lines.append(
        f"trials={n} window={summary['session_window']['start']}.."
        f"{summary['session_window']['end']}"
    )
    lines.append(
        f"job --n-concurrent values: "
        f"{summary['job_n_concurrent_values'] or 'unknown'}; "
        f"observed peak concurrent trials={conc['peak_concurrent_trials']}, "
        f"time-weighted mean="
        f"{conc['time_weighted_mean_concurrent_trials']:.1f}"
    )
    lines.append(
        f"trial wall s: mean={wall_mean:.0f} "
        f"median={trials_s['wall_s']['median']:.0f} "
        f"max={trials_s['wall_s']['max']:.0f}"
    )
    for exc, g in (trials_s.get("by_exception") or {}).items():
        llm_g = g["llm_time_s_mean"]
        lines.append(
            f"  outcome {exc}: n={g['n']} wall mean={g['wall_s_mean']:.0f} "
            f"median={g['wall_s_median']:.0f} "
            f"llm mean={'n/a' if llm_g is None else f'{llm_g:.0f}'} "
            f"mean_out_tok={g['mean_output_tokens']:.0f}"
        )
    lines.append(
        f"LLM time s: mean={llm_mean:.0f}; "
        f"tool/sandbox time s: mean={tool_mean:.0f} "
        f"(agent_exec minus sum(api_request_times))"
    )
    lines.append(
        f"decode batches={server['n_decode_batches']} "
        f"prefill={server['n_prefill_batches']}; "
        f"gen tok/s overall mean="
        f"{server['gen_tok_s_overall_mean']:.0f}; "
        f"queue>0 batch share="
        f"{server['queue_req_gt0_share']:.4f}; "
        f"KV full-token-usage max={server['full_token_usage_max']:.2f}"
    )
    lines.append(
        "gen tok/s by batch size (mean): "
        + ", ".join(
            f"bs={bs}: {agg['mean_gen_tok_s']:.0f} (n={agg['n']})" for bs, agg in gen_fit.items()
        )
    )
    if a is not None and b is not None and b > 0:
        lines.append(
            f"fit G(bs)=bs/(a+b*bs) over CUDA-graph batches (bs<=16): a={a:.4f}s b={b:.5f}s "
            f"-> per-request {1 / (a + b):.0f} tok/s at bs=1; eager batches excluded"
        )
    else:
        lines.append("fit: <2 graph-regime batch sizes observed; no fit [INFERENCE above bs=16]")
    lines.append(
        f"Modal billed=${billing['billed_usd_total']:.4f} "
        f"-> server cost/run=${cost_per_run:.4f} "
        f"({n} runs, rate ${rate}/h)"
    )

    # Daytona per-trial estimate. Sandbox shape is not recorded in the
    # trial dir (overrides only pin storage); CPU/RAM scenarios below are
    # [INFERENCE]. Storage floor uses the recorded 10240 MB override.
    sandbox_hours = (wall_mean or 0) / 3600.0
    storage_floor = estimate_cost_usd(
        backend="daytona",
        sandbox_seconds=wall_mean or 0,
        cpus=0,
        memory_mb=0,
        storage_mb=10240,
    )
    lines.append(
        f"Daytona sandbox: storage floor "
        f"(10 GiB override, 5 GiB billable @ "
        f"${DAYTONA_RATE_CARD['storage_usd_per_gib_hour']}/GiB-h, "
        f"{sandbox_hours:.2f} h wall)=${storage_floor:.4f}/trial; "
        f"CPU/RAM unknown from telemetry [INFERENCE needed]"
    )
    for cpus, mem in ((2, 4096), (4, 8192)):
        est = estimate_cost_usd(
            backend="daytona",
            sandbox_seconds=wall_mean or 0,
            cpus=cpus,
            memory_mb=mem,
            storage_mb=10240,
        )
        lines.append(
            f"  [INFERENCE] {cpus}vCPU/{mem // 1024}GiB sandbox ~${est:.4f}/trial at mean wall"
        )
    # Cost-optimal concurrency model. Trials decode a fraction of wall time,
    # so the mean decode batch is bs_mean(C) = intercept + slope*C from the
    # measured running-req regression -- NOT C itself. A decoding request
    # sees a size-biased batch, so r_req(C) = E[G(B)/B] over
    # Poisson(bs_mean(C)), and W(C) = tool + out/r_req(C).
    lines.append(
        "cost-optimal concurrency model: W(C) = tool + out/r_req(C), size-biased Poisson(bs_mean(C))"
    )
    kv = server.get("kv_scaling") or {}
    cap = kv.get("kv_capacity_full_tokens")
    slope = kv.get("running_req_per_trial_slope")
    icept = kv.get("running_req_per_trial_intercept")
    tok_per_req = kv.get("full_token_per_running_req_mean")
    tail = kv.get("tail_bs_gt16") or {}
    kv_ceiling = None
    if cap and slope and icept is not None and tok_per_req and slope > 0:
        # usage(C) ~= (icept + slope*C) * tok/req / capacity; solve usage = 0.9.
        kv_ceiling = (0.9 * cap / tok_per_req - icept) / slope
        lines.append(
            f"KV ceiling: usage(C) ~= ({icept:.2f}+{slope:.2f}*C)*{tok_per_req:.0f}tok/{cap:.0f}tok"
            f" -> usage 0.9 at C~{kv_ceiling:.0f} [INFERENCE: linear running-req(C), same mix]"
        )
    else:
        lines.append("KV ceiling: insufficient server/concurrency overlap to scale usage")
    if tail.get("share") is not None and slope and icept is not None:
        peak = conc["peak_concurrent_trials"] or 0
        ref_lam = icept + slope * peak
        model_p = poisson_tail_gt(ref_lam, GRAPH_BS) / (1.0 - math.exp(-ref_lam))
        lines.append(
            f"tail calibration: empirical P(bs>16|conc>={tail['min_concurrent_trials']}) = "
            f"{tail['share']:.4f} ({tail['batches']} batches, {tail['minutes']} min); "
            f"Poisson(bs_mean({peak})={ref_lam:.1f}) predicts {model_p:.4f} [INFERENCE above C={peak}]"
        )
    if (
        a is not None
        and b is not None
        and b > 0
        and mean_out
        and tool_mean
        and slope
        and icept is not None
    ):
        lines.append("C  bs_mean  P(bs>16)  r_req  W(s)  server$/run  kv_use  regime/binding")
        best = None
        rec = None  # largest grid C with P(bs>16) < 0.01 (always set: C=1 qualifies)
        for c in (1, 4, 8, 12, 16, 20, 24, 32, 40, 48):
            lam = icept + slope * c
            p = poisson_tail_gt(lam, GRAPH_BS) / (1.0 - math.exp(-lam))
            r = expected_per_request_tok_s(lam, a, b, gen_fit)
            w = tool_mean + mean_out / r
            cpu = mimo_selfhosted_trial_cost_usd(
                trial_hours=w / 3600.0, concurrency=c, sandbox_usd=0.0
            )
            use = (icept + slope * c) * tok_per_req / cap if (cap and tok_per_req) else None
            if p < 0.01:
                regime = "graph"
            elif p < 0.5:
                regime = "graph+eager-mix[INFERENCE]"
            else:
                regime = "eager[INFERENCE]"
            binding = []
            if regime != "graph":
                binding.append(regime)
            if use is not None and use > 0.9:
                binding.append("kv")
            if c > 84:
                binding.append("max-running-84")
            if best is None or cpu < best[1]:
                best = (c, cpu, regime != "graph")
            if p < 0.01:
                rec = (c, cpu, w, p, use)
            lines.append(
                f"{c:<3d}{lam:<8.1f}{p:<10.4f}{r:<9.0f}{w:<7.0f}{cpu:<13.4f}"
                f"{(f'{use:.2f}' if use is not None else '-'):<8s}{'+'.join(binding) or '-'}"
            )
        lines.append(
            f"grid minimum server $/run at C={best[0]} of this grid"
            + (" [INFERENCE: eager-mix regime]" if best[2] else "")
            + "; G2 at C=20 calibrates bs_mean and P(bs>16) directly"
        )
        lines.append(
            f"recommendation (provisional): default C={rec[0]} (largest grid C with P(bs>16)<0.01: "
            f"server ${rec[1]:.4f}/run, W={rec[2]:.0f}s, P(bs>16)={rec[3]:.4f}); cost keeps falling to "
            f"C={best[0]} but that rides the eager-mix [INFERENCE] tail with W ballooning; revisit upward "
            f"once G2 confirms the regression at C=20. Raising --cuda-graph-max-bs-decode is a serving "
            f"change to production's launch command, out of scope"
        )
        # Sanity gates (measured, not modelled).
        meas_bs1 = gen_fit.get("1", {}).get("mean_gen_tok_s")
        lam1 = icept + slope * 1
        r1 = expected_per_request_tok_s(lam1, a, b, gen_fit)
        if meas_bs1 and r1:
            ratio1 = r1 / meas_bs1
            lines.append(
                f"sanity r_req(1)={r1:.0f} vs measured bs=1 per-request {meas_bs1:.0f} "
                f"(ratio {ratio1:.2f}; PASS within ±10%)"
                if 0.9 <= ratio1 <= 1.1
                else f"sanity r_req(1)={r1:.0f} vs measured bs=1 per-request {meas_bs1:.0f} "
                f"(ratio {ratio1:.2f}; FAIL outside ±10%)"
            )
        by_exc = trials_s.get("by_exception") or {}
        if by_exc and slope and icept is not None:
            for exc, grp in sorted(by_exc.items()):
                cexp = grp.get("experienced_concurrency_mean")
                wall = grp.get("wall_s_mean")
                tout = grp.get("tool_time_s_mean")
                gout = grp.get("mean_output_tokens")
                if not cexp or not wall or tout is None or not gout:
                    continue
                rg = expected_per_request_tok_s(icept + slope * cexp, a, b, gen_fit)
                if not rg:
                    continue
                wpred = tout + gout / rg
                ratio = wpred / wall
                lines.append(
                    f"sanity outcome {exc}: W({cexp:.1f})={wpred:.0f}s vs wall mean {wall:.0f}s "
                    f"(ratio {ratio:.2f}"
                    + ("; PASS within ±15%)" if 0.85 <= ratio <= 1.15 else "; FAIL outside ±15%)")
                )

    # GPU comparison at Modal list prices (modal.com/pricing, 2026-10-01):
    # throughput scaling across GPU types is [INFERENCE] unless measured.
    gpu_h = {
        "A100-80GB": 0.000694 * 3600,
        "A100-40GB": 0.000583 * 3600,
        "H100": 0.001097 * 3600,
        "L40S": 0.000542 * 3600,
    }
    lines.append(
        "GPU comparison (Modal list $/s 2026-10-01; server total "
        "adds 4 CPU + 16 GiB; throughput scale [INFERENCE]):"
    )
    for gpu, g in gpu_h.items():
        total = g + 4 * 0.0000131 * 3600 + 16 * 0.00000222 * 3600
        lines.append(f"  {gpu}: gpu ${g:.4f}/h -> server ${total:.4f}/h")
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="build the tidy dataset")
    c.add_argument("--job", action="append", required=True, help="job dir glob (repeatable)")
    c.add_argument(
        "--server-log",
        action="append",
        default=[],
        help="APPID=path to a `modal app logs` capture (repeatable)",
    )
    c.add_argument(
        "--billing", default=None, help="modal billing report --resolution h --json output"
    )
    c.add_argument(
        "--app", action="append", default=[], help="attribute billing to this app id (repeatable)"
    )
    c.add_argument("--out", required=True, help="output directory")
    c.set_defaults(func=cmd_collect)
    r = sub.add_parser("report", help="model + text report from a dataset")
    r.add_argument("--data", required=True, help="collect --out directory")
    r.add_argument("--out", default=None, help="write report text to file")
    r.set_defaults(func=cmd_report)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
