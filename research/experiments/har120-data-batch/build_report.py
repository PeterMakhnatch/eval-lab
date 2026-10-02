#!/usr/bin/env python3
"""G2 (HAR-120 data batch) report builder for HAR-126.

Reads finished Harbor job dirs from a run worktree, plus capture dirs,
sampler telemetry JSONL and the frozen specs, and writes:

  results.jsonl      one row per in-scope trial (raw reward, counts verdict,
                     proxy-settled tokens, stop reason, loop-break calls,
                     capture-link status, publication paths)
  counted_pass.jsonl counted_pass trials only (job path, trial path, task,
                     attempt) for Traces (HAR-128) and Data (HAR-127, G3)
  RESULTS.md         the G2 write-up: per-task table, infra list, GEPA seed
                     receipt, telemetry section, capture summary
  telemetry-extract-<round>.json
                     `evallab telemetry extract` output per round

Nothing here spends anything: no trial launches, no deploys, no model
calls. The only writes into the run worktree are the two idempotent
maintenance steps the G2 receipt requires, both skipped when fresh:

  * `evallab process-job <job>` where processed/ is missing or predates
    the #600 counts fix (counts schema != evallab.counts/v1) or the
    HAR-131 tokens_proxy fix (tokens_proxy source != proxy_settled_ledger)
  * `evallab capture link <capdir> <job>` where the derived
    job_id=<id>/capture_link.json receipt is missing, names a different
    capture dir, or is older than the capture calls.jsonl. The target is
    wherever the bytes are: r2 jobs -> g2c once it is non-empty, else g2
    (the live r2 traffic is recorded in g2/calls.jsonl).

Re-running after the r2 tick finishes picks up the new job dirs and
rewrites every output from scratch (results.jsonl is rebuilt, not
appended). Use --no-maintenance for a pure rebuild.

Token rule (HAR-131/Cdx-2 defect): input/output tokens always come from
lab-metadata.json provider_usage.totals (the proxy-settled ledger), never
from tokens_proxy without checking its source, and never from the native
agent_result step sums. The builder cross-checks processed
ledger.totals.used against provider_usage.totals and flags any mismatch.

Stop rule (HAR-131 stop-vocabulary gap): the top-level stop_reason is
"unknown" for harness loop-break stops, so the canonical stop is derived
from diagnosis.exception_class first, then token_flow.stop.stop_reason,
then the raw stop_reason field.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import statistics
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from evallab.step_layers import classify_stop_reason, limit_hit_summary, stop_category

SCRIPT_DIR = Path(__file__).resolve().parent

SPEC_RE = re.compile(r"^har120-(\d+)-(a\d+)(-r2)?\.json$")
COUNTS_SCHEMA = "evallab.counts/v1"
PROXY_SOURCE = "proxy_settled_ledger"

# The 10 outcome-blind GEPA minibatch tasks frozen 04:34Z on HAR-135.
GEPA_MINIBATCH = [
    "001647",
    "000803",
    "001870",
    "000341",
    "001897",
    "002938",
    "001710",
    "001399",
    "002680",
    "001661",
]


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def discover_cells(spec_dirs: list[Path]) -> dict[tuple[str, int], dict]:
    """Map (task, attempt) -> {'spec': Path, 'spec_r2': Path|None}."""
    cells: dict[tuple[str, int], dict] = {}
    for d in spec_dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            m = SPEC_RE.match(p.name)
            if not m:
                continue
            task, att, r2 = m.group(1), int(m.group(2)[1:]), m.group(3)
            cell = cells.setdefault((task, att), {})
            cell["spec_r2" if r2 else "spec"] = p
    return cells


def read_job(job_dir: Path, derived_root: Path) -> dict:
    """Read every report-relevant fact out of one finished job dir."""
    row: dict = {"job": job_dir.name, "job_path": str(job_dir)}
    row["round"] = "g2c" if job_dir.name.endswith("-r2") else "g2"
    result = _load_json(job_dir / "result.json") or {}
    row["job_id"] = result.get("id")
    meta = _load_json(job_dir / "lab-metadata.json") or {}
    pu = meta.get("provider_usage") or {}
    totals = pu.get("totals") or {}
    row["ledger_tokens"] = {
        "input": totals.get("input_tokens"),
        "output": totals.get("output_tokens"),
        "requests": totals.get("requests"),
        "total": totals.get("total_tokens"),
    }
    row["attempt_id"] = pu.get("attempt_id")
    pj = _load_json(job_dir / "processed" / "job.json")
    row["processed"] = bool(pj)
    row["results_home"] = None
    row["ledger_used"] = None
    if pj:
        row["results_home"] = pj.get("results_home")
        row["results_home_ok"] = (
            bool(pj.get("results_home")) and Path(str(pj["results_home"])).is_dir()
        )
        row["ledger_used"] = ((pj.get("ledger") or {}).get("totals") or {}).get("used")
    trials = sorted((job_dir / "processed").glob("trial-*.json")) if pj else []
    trial_files = [t for t in trials if t.is_file()]
    row["n_processed_trials"] = len(trial_files)
    trial = _load_json(trial_files[0]) if len(trial_files) == 1 else None
    tdirs = sorted(
        d for d in job_dir.iterdir() if d.is_dir() and d.name.startswith(job_dir.name + "__")
    )
    tdir = tdirs[0] if len(tdirs) == 1 else None
    row["trial_dir"] = str(tdir) if tdir else None
    if trial is None or tdir is None:
        row["finished"] = False
        return row
    row["finished"] = True
    row["trial_name"] = trial.get("trial_name")
    counts = trial.get("counts") or {}
    row["counts_schema"] = counts.get("schema")
    row["raw_reward"] = counts.get("raw_reward")
    row["verdict"] = counts.get("verdict")
    row["reasons"] = counts.get("reasons") or []
    row["counts_task_status"] = (counts.get("task_status") or {}).get("status")
    row["stop_reason_raw"] = trial.get("stop_reason")
    row["exception_class"] = (trial.get("diagnosis") or {}).get("exception_class")
    tf = trial.get("token_flow") or {}
    row["loop_onset_call"] = (tf.get("loop_onset") or {}).get("call_index")
    row["last_edit_call"] = (tf.get("last_useful_edit") or {}).get("call_index")
    row["agent_steps"] = trial.get("agent_steps")
    tp = trial.get("tokens_proxy") or {}
    row["tokens_proxy"] = {
        "input": tp.get("input_tokens"),
        "output": tp.get("output_tokens"),
        "source": tp.get("source"),
    }
    tn = trial.get("tokens_native") or {}
    row["tokens_native"] = {
        "input": tn.get("input_tokens"),
        "output": tn.get("output_tokens"),
    }
    row["trial_wall_hours"] = trial.get("trial_wall_hours")
    row["handshake_confirmed"] = (trial.get("handshake") or {}).get("confirmed")
    tres = _load_json(tdir / "result.json") or {}
    ver = tres.get("verifier_result") or {}
    row["verifier_reward"] = (ver.get("rewards") or {}).get("reward")
    lb = ((tres.get("agent_result") or {}).get("metadata") or {}).get("loop_break") or {}
    row["loop_break"] = {
        "fired": lb.get("fired"),
        "nudge_call": lb.get("nudge_call"),
        "stop_call": lb.get("stop_call"),
        "detector": lb.get("detector"),
    }
    exc_info = tres.get("exception_info") or {}
    row["exception_message"] = exc_info.get("exception_message")
    reason = row["stop_reason_raw"]
    if reason in (None, "unknown"):
        reason, _ = classify_stop_reason(
            agent_metadata=(tres.get("agent_result") or {}).get("metadata"),
            exception_info={
                **exc_info,
                "exception_type": row["exception_class"] or exc_info.get("exception_type"),
            },
        )
    row["stop_reason"] = reason
    row["stop_category"] = stop_category(reason)
    row["task_name"] = trial.get("task_name")
    row["task_package_digest"] = trial.get("task_package_digest")
    # Capture-link receipt under the derived root.
    row["capture_receipt"] = None
    if row["job_id"]:
        rp = derived_root / "parquet" / f"job_id={row['job_id']}" / ("capture_link.json")
        if rp.is_file():
            row["capture_receipt"] = _load_json(rp)
            row["capture_receipt_path"] = str(rp)
    for name in ("config.json", "lock.json", "lab-metadata.json"):
        p = job_dir / name
        if not p.is_file() and tdir:
            p = tdir / name
        row[name.replace(".json", "_path")] = str(p) if p.is_file() else None
    return row


def canonical_stop(row: dict) -> str:
    """One stop label per trial (see module docstring for precedence)."""
    exc = row.get("exception_class")
    tf_stop = row.get("token_flow_stop")
    raw = row.get("stop_reason_raw")
    if exc == "LoopBreakStop" or tf_stop == "loop_break":
        return "LoopBreakStop"
    if raw == "ceiling:input_tokens":
        return "TrialBudgetExhausted (input-token ceiling)"
    if raw == "ceiling:requests":
        return "TrialBudgetExhausted (request ceiling)"
    if raw == "task_complete_confirmed":
        return "confirmed completion"
    if exc == "AgentTimeoutError":
        return "agent timeout"
    if exc == "ServiceUnavailableError":
        return "infra (upstream 503)"
    if exc == "RuntimeError" and "tmux" in str(row.get("exception_message")).lower():
        return "infra (tmux missing)"
    if "infra" in (row.get("reasons") or []):
        return f"infra ({exc or raw})"
    return str(raw or exc or "unknown")


def counted_outcome(row: dict) -> str:
    """Display outcome: counted_pass / counted_fail / excluded:<reasons>."""
    if not row.get("finished"):
        if row.get("status") == "refused":
            return (
                "not_run: quiet_failure_rule refusal (LoopBreakStop missing "
                "from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a)"
            )
        return "pending"
    if "infra" in (row.get("reasons") or []):
        return "infra"
    v = row.get("verdict") or "unknown"
    if v == "excluded":
        return "excluded:" + "+".join(row.get("reasons") or ["?"])
    return v


def needs_process(job: dict) -> str | None:
    """Why a job needs `evallab process-job` (None = fresh)."""
    if not job.get("processed") or not job.get("n_processed_trials"):
        return "processed/ missing"
    if job.get("counts_schema") != COUNTS_SCHEMA:
        return f"counts schema {job.get('counts_schema')}"
    if (job.get("tokens_proxy") or {}).get("source") != PROXY_SOURCE:
        return "tokens_proxy not proxy-settled"
    return None


def run_evallab(evallab: list[str], live_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*evallab, *args],
        cwd=str(live_root),
        capture_output=True,
        text=True,
        timeout=600,
    )


def _refresh(job: dict, live_root: Path) -> None:
    """Re-read a job dir after maintenance, keeping cell keys."""
    keep = {k: job.get(k) for k in ("task", "attempt", "which", "spec", "round")}
    job.update(read_job(Path(job["job_path"]), live_root / "derived"))
    job.update(keep)


def _calls_size(cap: Path | None) -> int:
    p = (cap / "calls.jsonl") if cap else None
    return p.stat().st_size if p and p.is_file() else 0


def link_target(job: dict, capdirs: dict[str, Path]) -> tuple[str, Path | None]:
    """Which capture dir holds this job's calls.

    r2 jobs belong to g2c, but the live r2 traffic is recorded in
    g2/calls.jsonl while g2c/calls.jsonl is empty (proxy still points at
    the g2 capture server). Link wherever the bytes are; prefer g2c for
    r2 jobs once it is non-empty.
    """
    if job["job"].endswith("-r2") and _calls_size(capdirs.get("g2c")) > 0:
        return "g2c", capdirs.get("g2c")
    return "g2", capdirs.get("g2")


def maintenance(
    jobs: list[dict],
    live_root: Path,
    evallab: list[str],
    capdirs: dict[str, Path],
    calls_mtime: dict[str, float],
    log: list[str],
) -> None:
    """Idempotent process-job + capture-link repair. Only the allowed write."""
    for job in jobs:
        jd = Path(job["job_path"])
        reason = needs_process(job)
        if reason and job.get("job_path"):
            r = run_evallab(evallab, live_root, "process-job", str(jd))
            log.append(f"process-job {jd.name}: {reason} -> exit={r.returncode}")
            if r.returncode == 0:
                _refresh(job, live_root)
            else:
                log.append(f"  stderr: {r.stderr.strip()[-500:]}")
        if not job.get("finished"):
            continue
        link_tag, cap = link_target(job, capdirs)
        if not cap or not cap.is_dir():
            log.append(f"link {jd.name}: no {link_tag} capture dir; skipped")
            continue
        rec_path = job.get("capture_receipt_path")
        stale = True
        if rec_path and Path(rec_path).is_file():
            rec = job.get("capture_receipt") or {}
            same_dir = Path(str(rec.get("capture_dir") or "")) == cap
            fresh = Path(rec_path).stat().st_mtime >= calls_mtime.get(link_tag, 0)
            stale = not (same_dir and fresh)
        if not stale:
            continue
        r = run_evallab(
            evallab,
            live_root,
            "capture",
            "link",
            "--derived-root",
            str(live_root / "derived" / "parquet"),
            str(cap),
            str(jd),
            "--json",
        )
        if r.returncode == 0:
            _refresh(job, live_root)
            rec = job.get("capture_receipt") or {}
            log.append(
                f"link {jd.name} -> {link_tag}: "
                f"assigned={rec.get('calls_assigned')} "
                f"unassigned={len(rec.get('calls_unassigned', []))} "
                f"ambiguous={len(rec.get('ambiguous_trials', []))} "
                f"verdict={(rec.get('trials') or [{}])[0].get('verdict')}"
            )
        else:
            log.append(
                f"link {jd.name} -> {link_tag}: exit={r.returncode} "
                f"stderr: {r.stderr.strip()[-300:]}"
            )


def summarize_sampler(paths: list[Path]) -> dict:
    """Peak/mean concurrency, queue depth, throughput from sampler JSONL."""
    running: list[float] = []
    queued: list[float] = []
    gen: list[float] = []
    usage: list[float] = []
    qlab: list[tuple[str, dict]] = []
    n_ok = n_probes = n_503 = 0
    for p in paths:
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            n_probes += 1
            sg = d.get("sglang") or {}
            if sg.get("ok"):
                n_ok += 1
                running.append(float(sg.get("num_running_reqs") or 0))
                queued.append(float(sg.get("num_queue_reqs") or 0))
                gen.append(float(sg.get("gen_throughput_tok_s") or 0))
                usage.append(float(sg.get("token_usage") or 0))
            elif sg.get("metrics_bytes") is None and "503" in str(sg.get("error")):
                n_503 += 1
            q = ((d.get("lab") or {}).get("queue")) or {}
            if q:
                qlab.append((d.get("ts"), q))
    out: dict = {
        "n_probes": n_probes,
        "n_sglang_ok": n_ok,
        "n_sglang_503": n_503,
    }
    if running:
        out["running"] = {
            "peak": max(running),
            "mean": round(statistics.fmean(running), 2),
        }
        out["queue_depth"] = {
            "peak": max(queued),
            "mean": round(statistics.fmean(queued), 2),
        }
        out["gen_tok_s"] = {
            "peak": round(max(gen), 1),
            "mean": round(statistics.fmean(gen), 1),
        }
        out["token_usage"] = {
            "peak": round(max(usage), 3),
            "mean": round(statistics.fmean(usage), 3),
        }
    # Running-requests over time, downsampled to at most ~25 points.
    series = [(ts, q.get("running"), q.get("waiting"), q.get("approved")) for ts, q in qlab if ts]
    step = max(1, len(series) // 25)
    out["queue_series"] = [
        {"ts": ts, "running": r, "waiting": w, "approved": a} for ts, r, w, a in series[::step]
    ]
    return out


def summarize_capture_calls(capdir: Path) -> dict:
    """p50/p90 upstream latency + status mix from a capture calls.jsonl."""
    p = capdir / "calls.jsonl"
    if not p.is_file() or p.stat().st_size == 0:
        return {"calls": 0, "empty": True}
    lat: list[float] = []
    status: dict[str, int] = {}
    errors = 0
    n = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        n += 1
        with contextlib.suppress(TypeError, ValueError):
            lat.append(
                float(d.get("upstream_latency_s") or 0)
                or (_parse_ts(d.get("ended_at")) - _parse_ts(d.get("started_at"))).total_seconds()
            )
        status[str(d.get("response_status"))] = status.get(str(d.get("response_status")), 0) + 1
        if d.get("error"):
            errors += 1
    lat = sorted(lat)

    def q(x):
        return round(lat[min(len(lat) - 1, int(x * len(lat)))], 2) if lat else None

    return {
        "calls": n,
        "latency_s": {"p50": q(0.5), "p90": q(0.9), "max": max(lat) if lat else None},
        "status": status,
        "errors": errors,
    }


def scan_bounds(calls_path: Path) -> dict:
    """Immutable prefix bounds per route token in a frozen calls.jsonl.

    Returns {sha256, size_bytes, lines, tokens: {token: {
    first_line, last_line, first_byte, last_byte, n}}}. Line numbers are
    1-based; byte offsets delimit the job's records within the file, so
    Data (G3) can slice an immutable prefix per trial.
    """
    import hashlib

    tokens: dict[str, dict] = {}
    h = hashlib.sha256()
    offset = 0
    lineno = 0
    with open(calls_path, "rb") as f:
        for raw in f:
            lineno += 1
            h.update(raw)
            try:
                tok = json.loads(raw.decode("utf-8")).get("route_token")
            except (ValueError, UnicodeDecodeError):
                tok = None
            if tok:
                b = tokens.setdefault(
                    tok,
                    {
                        "first_line": lineno,
                        "last_line": lineno,
                        "first_byte": offset,
                        "last_byte": offset + len(raw),
                        "n": 0,
                    },
                )
                b["last_line"] = lineno
                b["last_byte"] = offset + len(raw)
                b["n"] += 1
            offset += len(raw)
    return {
        "sha256": h.hexdigest(),
        "size_bytes": offset,
        "lines": lineno,
        "tokens": tokens,
    }


DAYTONA_USD_PER_HOUR = 0.23094


def daytona_usd(wall_hours) -> float | None:
    if wall_hours is None:
        return None
    try:
        return round(float(wall_hours) * DAYTONA_USD_PER_HOUR, 6)
    except (TypeError, ValueError):
        return None


def _spend(rows, c1_ranges, runs_dir: Path, modal_billing) -> dict:
    """Daytona lifetimes + Modal hourly buckets, attributed to windows.

    Modal hour buckets never align exactly to run windows; the raw buckets
    are reported verbatim and window sums carry explicit caveats. Non-G2
    apps (lora/sft) are excluded from every window sum.
    """
    out: dict = {"daytona_usd_per_hour": DAYTONA_USD_PER_HOUR, "modal": {}, "daytona": {}}
    groups = {
        "wave1": [
            r
            for r in rows
            if r.get("which") == "orig"
            and r.get("finished")
            and r.get("exception_class") != "ServiceUnavailableError"
        ],
        "g2b_failures": [
            r
            for r in rows
            if r.get("which") == "orig"
            and r.get("finished")
            and r.get("exception_class") == "ServiceUnavailableError"
        ],
        "r2": [r for r in rows if r.get("which") == "r2" and r.get("finished")],
    }
    for tag, jobs in groups.items():
        vals = [r.get("daytona_usd") for r in jobs if r.get("daytona_usd") is not None]
        out["daytona"][tag] = {
            "n": len(vals),
            "usd": round(sum(vals), 4),
            "note": "trial lifetimes x $0.23094/h",
        }
    c1_vals = []
    for c in c1_ranges:
        jd = runs_dir / c["job"]
        mts = sorted((jd / "processed").glob("trial-*.json")) if (jd / "processed").is_dir() else []
        wh = None
        if len(mts) == 1:
            wh = (_load_json(mts[0]) or {}).get("trial_wall_hours")
        if wh is None:
            m = _load_json(jd / "lab-metadata.json") or {}
            s, e = _parse_ts(m.get("started_at")), _parse_ts(m.get("finished_at"))
            wh = (e - s).total_seconds() / 3600 if s and e else None
        c["trial_wall_hours"] = wh
        c["daytona_usd"] = daytona_usd(wh)
        if c["daytona_usd"] is not None:
            c1_vals.append(c["daytona_usd"])
    out["daytona"]["c1"] = {
        "n": len(c1_vals),
        "usd": round(sum(c1_vals), 4),
        "note": "trial lifetimes x $0.23094/h; HAR-135 scope, not G2",
    }
    if modal_billing:
        try:
            bill = json.loads(Path(modal_billing).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            bill = []
        mimo = [b for b in bill if b.get("description") == "evallab-mimo-v26-9b"]
        other = [b for b in bill if b.get("description") != "evallab-mimo-v26-9b"]
        by_hour: dict[str, float] = {}
        for b in mimo:
            by_hour[b.get("interval_start", "")[:13]] = by_hour.get(
                b.get("interval_start", "")[:13], 0.0
            ) + float(b.get("cost", 0) or 0)
        out["modal"]["hourly_mimo_usd"] = {k: round(v, 4) for k, v in sorted(by_hour.items())}
        out["modal"]["other_apps_usd"] = round(sum(float(b.get("cost", 0) or 0) for b in other), 4)
        out["modal"]["windows"] = {
            "har116_00h00_02h30": round(
                by_hour.get("2026-10-01T00", 0)
                + by_hour.get("2026-10-01T01", 0)
                + by_hour.get("2026-10-01T02", 0),
                4,
            ),
            "smoke_07h27_07h40": round(by_hour.get("2026-10-01T07", 0), 4),
            "g2_07h57_09h51": round(
                by_hour.get("2026-10-01T07", 0)
                + by_hour.get("2026-10-01T08", 0)
                + by_hour.get("2026-10-01T09", 0),
                4,
            ),
            "c1_09h51_10h15": round(by_hour.get("2026-10-01T10", 0), 4),
        }
        out["modal"]["caveats"] = [
            "hour buckets do not align to windows: the 07h bucket holds the 07:27 smoke and G2's 07:57 launch; the 09h bucket holds G2's redeploys, the r2 tail and C1's 09:51 start",
            "the 10h bucket is partial (covers C1's 10:00-10:15 tail and anything after)",
            "other_apps_usd (lora/sft/smoke apps) is Infra G4 scope, excluded from G2/C1/smoke sums",
        ]
    return out


def build_rows(cells, runs_dir: Path, derived_root: Path) -> list[dict]:
    rows = []
    for task, attempt in sorted(cells):
        spec = cells[(task, attempt)].get("spec")
        spec_r2 = cells[(task, attempt)].get("spec_r2")
        for which in ("orig", "r2"):
            jd = runs_dir / (f"har120-{task}-a{attempt}" + ("-r2" if which == "r2" else ""))
            if not jd.is_dir():
                # No r2 spec and no r2 dir: this cell will never have an
                # r2 job (its original already finished in wave 1).
                if which == "r2" and spec_r2 is None:
                    continue
                # An r2 spec with no job dir means the tick refused it
                # (r2: quiet_failure_rule, a lab defect fixed by #643).
                status = "refused" if which == "r2" else "absent"
                rows.append(
                    {
                        "task": task,
                        "attempt": attempt,
                        "which": which,
                        "job": jd.name,
                        "job_path": str(jd),
                        "finished": False,
                        "status": status,
                        "spec": str(spec_r2 if which == "r2" else spec)
                        if (spec_r2 if which == "r2" else spec)
                        else None,
                    }
                )
                continue
            job = read_job(jd, derived_root)
            job["task"] = task
            job["attempt"] = attempt
            job["which"] = which
            job["spec"] = str(spec_r2 if which == "r2" else spec)
            job["status"] = "finished" if job.get("finished") else "unfinished"
            rows.append(job)
    return rows


def render_results_md(
    ctx: dict,
    rows: list[dict],
    telemetry: dict,
    sampler: dict,
    captures: dict,
    gepa: dict,
    maint_log: list[str],
) -> str:
    L: list[str] = []
    A = L.append
    A("# G2 data batch (HAR-120 x lf2): results")
    A("")
    A(f"Generated {ctx['generated_at']} by `build_report.py` ({ctx['command']}).")
    A(f"Run worktree {ctx['live_root']} at `{ctx['live_rev']}`; harness lf2 `{ctx['lf2_digest']}`.")
    A(
        f"Scope: {ctx['n_tasks']} tasks x 2 attempts = {ctx['n_cells']} cells; "
        f"{ctx['n_finished']} jobs finished, {ctx['n_pending']} pending/absent."
    )
    A("")
    A(
        "Token rule: input/output tokens are PROXY-SETTLED ledger totals "
        "(`lab-metadata.json` provider_usage.totals), never tokens_proxy "
        "unverified and never native step sums (HAR-131/Cdx-2 defect; #609 "
        "merged and verified per trial below)."
    )
    A(
        "Stop rule: canonical stop prefers `diagnosis.exception_class`, then "
        "`token_flow.stop.stop_reason`, then the raw `stop_reason` field "
        "(the top-level field still reads `unknown` for harness loop-break "
        "stops; HAR-131 stop-vocabulary fix pending)."
    )
    A("")
    A("## 1. Wave-1 summary")
    A("")
    s = ctx["wave1"]
    A(
        f"Wave 1 (tick 1, {s['n_jobs']} jobs, {s['window']}): "
        f"{s['counted_pass']} counted_pass, {s['counted_fail']} counted_fail, "
        f"{s['excluded_taint']} excluded (taint), {s['infra']} infra."
    )
    A(
        f"Settled tokens over wave-1 counted scope "
        f"(pass + fail, {s['n_counted']} trials): "
        f"{s['in_tokens']:,} in / {s['out_tokens']:,} out "
        f"in {s['calls']:,} calls."
    )
    A(
        f"Raw verifier pass rate (counted scope): "
        f"{s['counted_pass']}/{s['n_counted']} = "
        f"{s['counted_pass'] / max(1, s['n_counted']):.1%}."
    )
    q = ctx["r2"]
    A(
        f"r2 infra re-runs ({q['n_jobs']} of {ctx['n_r2_specs']} specs finished, {q['window']}): "
        f"{q['counted_pass']} counted_pass, "
        f"{q['counted_fail']} counted_fail, "
        f"{q['excluded_taint']} excluded (taint), "
        f"{q['infra']} infra."
    )
    A(
        f"Settled tokens over r2 counted scope "
        f"(pass + fail, {q['n_counted']} trials): "
        f"{q['in_tokens']:,} in / {q['out_tokens']:,} out "
        f"in {q['calls']:,} calls."
    )
    A(
        f"{len(ctx['refused_r2'])} r2 specs not run (refused, lab defect): "
        f"the tick refused them at 09:30:18–19Z with "
        f"`dispatch_refused / quiet_failure_rule` because LoopBreakStop was "
        f"missing from AGENT_STOP_EXCEPTIONS at ba8d8358 (fixed on main by "
        f"#643). No job dirs exist; nothing is counted. "
        f"Jobs: {', '.join(ctx['refused_r2'])}."
    )
    A(ctx["g2_closed"])
    A("")
    limits = limit_hit_summary(r.get("stop_reason") for r in rows if r.get("finished"))
    share = limits["limit_hit_share"]
    share_text = "n/a" if share is None else f"{share:.1%}"
    A(
        f"Limit-hit share over all finished trials: {limits['limit_hit_trials']}/"
        f"{limits['trials']} ({share_text}); "
        f"setup-limited: `{str(limits['setup_limited']).lower()}`."
        + (
            " Flagged: over 5% of trials hit our limits; treat this batch as setup-limited."
            if limits["setup_limited"]
            else ""
        )
    )
    A("")
    A("## 2. Per-task table")
    A("")
    A(
        "| task | att | job | raw | counted | in / out (settled) | "
        "calls | stop | stop category | nudge -> stop | capture |"
    )
    A("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if r.get("which") == "r2" and r.get("status") == "absent":
            continue
        lt = r.get("ledger_tokens") or {}
        tok = f"{_num(lt.get('input'))} / {_num(lt.get('output'))}"
        lb = r.get("loop_break") or {}
        nudge = _nudge(lb)
        rec = r.get("capture_receipt") or {}
        tr = (rec.get("trials") or [{}])[0] if rec else {}
        if rec:
            cap = (
                f"{tr.get('verdict')} "
                f"{rec.get('calls_assigned')}/"
                f"{r.get('ledger_tokens', {}).get('requests')}"
            )
        elif r.get("status") == "refused":
            cap = "n/a (refused; lab defect, see section 1)"
        elif r.get("status") == "absent":
            cap = "pending (r2)" if r.get("which") == "r2" else "refused/absent"
        else:
            cap = "unlinked"
        A(
            f"| {r['task']} | {r['attempt']} | {r['job']} | "
            f"{r.get('raw_reward', r.get('verifier_reward'))} | "
            f"{counted_outcome(r)} | {tok} | "
            f"{_num((r.get('ledger_tokens') or {}).get('requests'))} | "
            f"{canonical_stop(r) if r.get('finished') else r.get('status')} | "
            f"{r.get('stop_category') if r.get('finished') else 'n/a'} | "
            f"{nudge} | {cap} |"
        )
    A("")
    A("## 3. Infra-failed originals (no counted outcome)")
    A("")
    infra = [r for r in rows if counted_outcome(r) == "infra"]
    if infra:
        for r in infra:
            A(
                f"- {r['job']} ({r['task']} attempt {r['attempt']}): "
                f"{canonical_stop(r)}, raw_reward=null, "
                f"reasons={'+'.join(r.get('reasons') or [])}; "
                f"superseded by `{r['job']}-r2` ({r.get('r2_status')})."
            )
    else:
        A("None.")
    A("")
    A("## 4. counted_pass trials")
    A("")
    passes = [r for r in rows if counted_outcome(r) == "counted_pass"]
    A(f"{len(passes)} counted_pass trials (see `counted_pass.jsonl` for Traces/Data):")
    for r in passes:
        A(
            f"- {r['task']} attempt {r['attempt']}: {r['job_path']} "
            f"trial `{r.get('trial_name')}` reward={r.get('raw_reward')}"
        )
    A("")
    A("## 5. GEPA seed receipt (HAR-135, 04:34Z freeze)")
    A("")
    A(f"lf2 harness: `{gepa['lf2_path']}` digest `{gepa['lf2_digest']}`.")
    A(
        f"Representative stock spec: `{gepa['rep_spec']}` "
        f"sha256:{gepa['rep_spec_sha']} limits: "
        f"{gepa['limits']}."
    )
    A("Per-task attempt-1/attempt-2 job paths (result, config, lock, metadata, processed counts):")
    for t in GEPA_MINIBATCH:
        A(f"- {t} attempt 1: {gepa['seeds'][t]['a1']}")
        A(f"  attempt 2: {gepa['seeds'][t]['a2']}")
    A("")
    A("## 6. Telemetry")
    A("")
    for tag, t in telemetry.items():
        A(
            f"Round {tag}: {t.get('n_trials')} trials, {t.get('n_calls')} calls, "
            f"latency p50={t.get('latency_s', {}).get('p50')}s "
            f"p90={t.get('latency_s', {}).get('p90')}s, "
            f"peak call concurrency={t.get('peak_call_concurrency')}, "
            f"mean={round(t.get('mean_call_concurrency', 0) or 0, 2)}; "
            f"trajectory-vs-ledger matched {t.get('trajectory_vs_ledger_matched')}/{t.get('n_trials')} "
            f"(503-infra trials mismatch trivially: no trajectory; "
            f"full list in telemetry-extract-{tag}.json)."
        )
    for tag, sm in sampler.items():
        if not sm.get("running"):
            A(
                f"Sampler {tag}: {sm.get('n_probes')} probes, "
                f"{sm.get('n_sglang_503')} SGLang-503 (cold upstream); "
                f"no healthy gauges."
            )
            continue
        A(
            f"Sampler {tag}: {sm['n_probes']} probes "
            f"({sm['n_sglang_ok']} healthy): running peak={sm['running']['peak']} "
            f"mean={sm['running']['mean']}; queue peak={sm['queue_depth']['peak']} "
            f"mean={sm['queue_depth']['mean']}; gen peak={sm['gen_tok_s']['peak']} "
            f"mean={sm['gen_tok_s']['mean']} tok/s; "
            f"token usage peak={sm['token_usage']['peak']} "
            f"mean={sm['token_usage']['mean']}."
        )
    for tag, cp in captures.items():
        lat = cp.get("latency_s") or {}
        if cp.get("empty"):
            A(f"Capture {tag}: 0 calls (capture file empty).")
            continue
        A(
            f"Capture {tag}: {cp.get('calls')} calls, "
            f"upstream latency p50={lat.get('p50')}s "
            f"p90={lat.get('p90')}s, "
            f"errors={cp.get('errors')}, status={cp.get('status')}."
        )
    A("")
    A("### Infra note (HAR-129)")
    A("")
    A(
        "- Tick-boundary teardown gap: tick 1's teardown stopped the Modal "
        "app at 09:02Z even though 40 re-estimated specs were approved; the "
        "redeploy's warm smoke returned HTTP 503 while cold-starting, the "
        "round script did not abort, and 22 dispatched specs ended "
        "ServiceUnavailableError with 0 tokens (plus 18 quiet-failure "
        "refusals). Cleared by the Q1 $0 local-Docker nop; r2 redeployed "
        "with a must-pass warm check."
    )
    A(
        "- Wave (non-refill) dispatch: within tick 1, queue `waiting` held "
        "39 refused specs while `running` drained 20 -> 2, so no mid-tick "
        "refill was observable; whether a tick refills freed slots when "
        "dispatchable specs exist is untested [INFERENCE]. SGLang served "
        "~2 running requests at 153-250 tok/s at the tail vs ~400 tok/s "
        "under full 20-way load."
    )
    A("")
    A("## 7. Capture-link status")
    A("")
    linked = unlinked = 0
    for r in rows:
        if not r.get("finished"):
            continue
        rec = r.get("capture_receipt") or {}
        tr = (rec.get("trials") or [{}])[0] if rec else {}
        if rec:
            linked += 1
        else:
            unlinked += 1
        bb = r.get("bounds") or {}
        bound = (
            f" lines {bb.get('first_line')}-{bb.get('last_line')} "
            f"bytes {bb.get('first_byte')}-{bb.get('last_byte')}"
            if bb
            else " bounds=n/a"
        )
        A(
            f"- {r['job']}: "
            f"{
                (
                    'assigned=' + str(rec.get('calls_assigned')) + ' '
                    'unassigned=' + str(len(rec.get('calls_unassigned', []))) + ' '
                    'ambiguous=' + str(len(rec.get('ambiguous_trials', []))) + ' '
                    'verdict=' + str(tr.get('verdict'))
                )
                if rec
                else 'no receipt'
            }.{bound}."
        )
    A("")
    A(f"Linked {linked} finished jobs, {unlinked} without receipt.")
    cf = ctx.get("capture_file") or {}
    A(
        f"Frozen capture file `{cf.get('path')}` "
        f"sha256:{cf.get('sha256')} {cf.get('size_bytes')} bytes, "
        f"{cf.get('lines')} lines, {cf.get('n_tokens')} route tokens. "
        f"Only the g2 server ever bound its port; g2b/g2c/c1 dirs are empty."
    )
    for tag, label in (
        ("wave1", "wave 1"),
        ("g2b_failures", "g2b failures"),
        ("r2", "r2"),
    ):
        rg = (ctx.get("ranges") or {}).get(tag)
        if rg:
            A(
                f"- {label}: lines {rg['first_line']}-{rg['last_line']}, "
                f"bytes {rg['first_byte']}-{rg['last_byte']}, {rg['calls']} calls."
            )
    c1 = ctx.get("c1_exclusion") or []
    if c1:
        A("Non-G2 (C1, HAR-135) ranges in the same file — exclude for G3:")
        for c in c1:
            bb = c.get("bounds") or {}
            A(
                f"- {c['job']}: "
                + (
                    f"lines {bb.get('first_line')}-{bb.get('last_line')}, "
                    f"bytes {bb.get('first_byte')}-{bb.get('last_byte')}, "
                    f"{bb.get('n')} calls."
                    if bb
                    else "no records (not run or unlinked)"
                )
            )
    if maint_log:
        A("")
        A("## 8. Maintenance performed by this run")
        A("")
        for line in maint_log:
            A(f"- {line}")
    sp = ctx.get("spend") or {}
    if sp:
        A("")
        A("## 9. Spend split")
        A("")
        for tag in ("wave1", "g2b_failures", "r2"):
            d = (sp.get("daytona") or {}).get(tag) or {}
            A(f"- Daytona {tag}: ${d.get('usd')} over {d.get('n')} trials ({d.get('note')}).")
        d = (sp.get("daytona") or {}).get("c1") or {}
        A(f"- Daytona C1: ${d.get('usd')} over {d.get('n')} trials ({d.get('note')}).")
        m = sp.get("modal") or {}
        if m.get("hourly_mimo_usd"):
            A(f"- Modal evallab-mimo-v26-9b hourly: {m['hourly_mimo_usd']}.")
            A(f"- Modal windows: {m.get('windows')}.")
            A(f"- Modal non-G2 apps (excluded): ${m.get('other_apps_usd')}.")
            for c in m.get("caveats", []):
                A(f"  Caveat: {c}.")
    A("")
    return "\n".join(L) + "\n"


def _num(v) -> str:
    return "-" if v is None else f"{v:,}" if isinstance(v, int) else str(v)


def _nudge(lb: dict) -> str:
    if not lb.get("fired"):
        return "-"
    if lb.get("stop_call") is not None:
        return f"{lb.get('nudge_call')} -> {lb.get('stop_call')}"
    return f"nudge {lb.get('nudge_call')} (no stop)"


def _sibling_status(rows, r, cells) -> str:
    want = r["job"] + "-r2"
    if any(x.get("job") == want and x.get("finished") for x in rows):
        return "landed"
    if cells.get((r["task"], r["attempt"]), {}).get("spec_r2"):
        return "pending"
    return "no r2 spec"


def git_rev(path: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live-root", required=True, help="run worktree root")
    ap.add_argument("--spec-dir", action="append", default=[], help="spec dir (repeatable)")
    ap.add_argument("--order", action="append", default=[], help="ids order file (optional)")
    ap.add_argument(
        "--capture", action="append", default=[], metavar="TAG=PATH", help="capture dir per round"
    )
    ap.add_argument(
        "--sampler", action="append", default=[], metavar="TAG=PATH", help="sampler telemetry JSONL"
    )
    ap.add_argument("--results-home", default=None)
    ap.add_argument("--modal-billing", default=None, help="modal billing report JSON (hourly rows)")
    ap.add_argument(
        "--c1-glob",
        default="har135-c1-*",
        help="non-G2 job dirs whose capture ranges are listed for exclusion",
    )
    ap.add_argument("--out", default=str(SCRIPT_DIR))
    ap.add_argument("--evallab", default="uv run --no-sync evallab")
    ap.add_argument("--no-maintenance", action="store_true")
    args = ap.parse_args(argv)

    live_root = Path(args.live_root).resolve()
    runs_dir = live_root / "runs"
    derived_root = live_root / "derived"
    exp = live_root / "research" / "experiments" / "har120-data-batch"
    spec_dirs = [Path(d).expanduser() for d in args.spec_dir] or [
        exp / "specs",
        exp / "specs" / "r2",
    ]
    spec_dirs = [d if d.is_absolute() else Path.cwd() / d for d in spec_dirs]
    capdirs = {}
    for kv in args.capture:
        tag, _, p = kv.partition("=")
        cap = Path(p.strip()).expanduser()
        capdirs[tag.strip()] = cap if cap.is_absolute() else Path.cwd() / cap
    samplers: dict[str, list[Path]] = {}
    for kv in args.sampler:
        tag, _, p = kv.partition("=")
        sp = Path(p.strip()).expanduser()
        samplers.setdefault(tag.strip(), []).append(sp if sp.is_absolute() else Path.cwd() / sp)
    out = Path(args.out).expanduser()
    out = out if out.is_absolute() else Path.cwd() / out
    out.mkdir(parents=True, exist_ok=True)
    evallab = args.evallab.split()

    cells = discover_cells(spec_dirs)
    if not cells:
        print("no specs found", file=sys.stderr)
        return 2
    rows = build_rows(cells, runs_dir, derived_root)

    calls_mtime = {
        tag: (cap / "calls.jsonl").stat().st_mtime if (cap / "calls.jsonl").is_file() else 0.0
        for tag, cap in capdirs.items()
    }
    maint_log: list[str] = []
    if not args.no_maintenance:
        present = [r for r in rows if Path(r["job_path"]).is_dir()]
        maintenance(present, live_root, evallab, capdirs, calls_mtime, maint_log)
        rows = build_rows(cells, runs_dir, derived_root)

    # Immutable capture bounds from the frozen calls.jsonl (g2 holds every
    # call: the g2b/g2c/c1 servers never bound their ports).
    frozen = scan_bounds(capdirs["g2"] / "calls.jsonl") if capdirs.get("g2") else None
    tok_bounds = (frozen or {}).get("tokens", {})
    for r in rows:
        r["stop"] = canonical_stop(r) if r.get("finished") else r.get("status")
        r["outcome"] = counted_outcome(r)
        r["r2_status"] = _sibling_status(rows, r, cells)
        aid = r.get("attempt_id")
        r["bounds"] = tok_bounds.get(aid) if aid else None
        r["daytona_usd"] = daytona_usd(r.get("trial_wall_hours"))
    # results.jsonl (one row per in-scope trial slot).
    slim = [
        {
            k: r.get(k)
            for k in (
                "task",
                "attempt",
                "which",
                "job",
                "job_path",
                "trial_name",
                "status",
                "finished",
                "round",
                "outcome",
                "stop",
                "stop_reason",
                "stop_category",
                "raw_reward",
                "verdict",
                "reasons",
                "ledger_tokens",
                "tokens_proxy",
                "tokens_native",
                "agent_steps",
                "loop_break",
                "loop_onset_call",
                "last_edit_call",
                "handshake_confirmed",
                "spec",
                "results_home",
                "results_home_ok",
                "trial_dir",
                "capture_receipt_path",
                "counts_task_status",
                "r2_status",
                "attempt_id",
                "bounds",
                "trial_wall_hours",
                "daytona_usd",
            )
        }
        for r in rows
    ]
    for s, r in zip(slim, rows, strict=True):
        rec = r.get("capture_receipt") or {}
        tr = (rec.get("trials") or [{}])[0] if rec else {}
        s["capture"] = {
            "calls_assigned": rec.get("calls_assigned"),
            "calls_unassigned": len(rec.get("calls_unassigned", [])),
            "ambiguous": len(rec.get("ambiguous_trials", [])),
            "trial_verdict": tr.get("verdict"),
            "captured_calls": tr.get("captured_calls"),
            "linked_at": rec.get("linked_at"),
        }
    (out / "results.jsonl").write_text(
        "".join(json.dumps(s, sort_keys=True) + "\n" for s in slim),
        encoding="utf-8",
    )

    # counted_pass.jsonl for Traces (HAR-128) and Data (HAR-127, G3).
    passes = [r for r in rows if counted_outcome(r) == "counted_pass"]
    (out / "counted_pass.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "job_path": r["job_path"],
                    "trial_path": r.get("trial_dir"),
                    "task": r["task"],
                    "attempt": r["attempt"],
                    "trial_name": r.get("trial_name"),
                    "raw_reward": r.get("raw_reward"),
                    "ledger_tokens": r.get("ledger_tokens"),
                    "attempt_id": r.get("attempt_id"),
                    "bounds": r.get("bounds"),
                },
                sort_keys=True,
            )
            + "\n"
            for r in passes
        ),
        encoding="utf-8",
    )

    # Telemetry: extract per round from finished job dirs, summarize
    # sampler JSONL + capture calls directly.
    telemetry: dict = {}
    for tag in ("g2", "g2c"):
        job_dirs = [
            r["job_path"]
            for r in rows
            if r.get("finished") and (r.get("round") == tag or (tag == "g2" and not r.get("round")))
        ]
        if not job_dirs:
            continue
        dest = out / f"telemetry-extract-{tag}.json"
        if not args.no_maintenance:
            r = run_evallab(
                evallab,
                live_root,
                "telemetry",
                "extract",
                *job_dirs,
                "--out",
                str(dest),
            )
            maint_log.append(
                f"telemetry extract {tag}: "
                f"{r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.returncode}"
            )
        if dest.is_file():
            telemetry[tag] = _load_json(dest) or {}
    sampler = {tag: summarize_sampler(ps) for tag, ps in samplers.items()}
    captures = {tag: summarize_capture_calls(cap) for tag, cap in capdirs.items()}

    # Wave-1 summary: tick-1 originals only. Tick-2's 22 dispatched-against-
    # 503 originals are excluded (listed as infra in section 3; r2 supersedes).
    wave1 = [
        r
        for r in rows
        if r.get("which") == "orig"
        and r.get("finished")
        and r.get("exception_class") != "ServiceUnavailableError"
    ]

    def _summarize(jobs):
        counted = [r for r in jobs if (r.get("verdict") or "").startswith("counted")]
        ledgers = [r.get("ledger_tokens") or {} for r in counted]
        for r in jobs:
            meta = _load_json(Path(r["job_path"]) / "lab-metadata.json") or {}
            r["started"] = meta.get("started_at")
            r["ended"] = meta.get("finished_at")
        starts = [s for s in (_parse_ts(r.get("started")) for r in jobs) if s]
        ends = [e for e in (_parse_ts(r.get("ended")) for r in jobs) if e]
        return {
            "n_jobs": len(jobs),
            "window": (
                f"{min(starts).strftime('%H:%MZ')}–{max(ends).strftime('%H:%MZ')}"
                if starts and ends
                else "n/a"
            ),
            "counted_pass": sum(1 for r in jobs if r.get("verdict") == "counted_pass"),
            "counted_fail": sum(1 for r in jobs if r.get("verdict") == "counted_fail"),
            "excluded_taint": sum(
                1
                for r in jobs
                if r.get("verdict") == "excluded" and "infra" not in (r.get("reasons") or [])
            ),
            "infra": sum(1 for r in jobs if "infra" in (r.get("reasons") or [])),
            "n_counted": len(counted),
            "in_tokens": sum((t.get("input") or 0) for t in ledgers),
            "out_tokens": sum((t.get("output") or 0) for t in ledgers),
            "calls": sum((t.get("requests") or 0) for t in ledgers),
        }

    r2jobs = [r for r in rows if r.get("which") == "r2" and r.get("finished")]
    refused_r2 = sorted(
        r["job"] for r in rows if r.get("which") == "r2" and r.get("status") == "refused"
    )
    g2b_jobs = [
        r
        for r in rows
        if r.get("which") == "orig"
        and r.get("finished")
        and r.get("exception_class") == "ServiceUnavailableError"
    ]

    def _range(jobs):
        bbs = [r.get("bounds") for r in jobs if r.get("bounds")]
        if not bbs:
            return None
        return {
            "first_line": min(b["first_line"] for b in bbs),
            "last_line": max(b["last_line"] for b in bbs),
            "first_byte": min(b["first_byte"] for b in bbs),
            "last_byte": max(b["last_byte"] for b in bbs),
            "calls": sum(b["n"] for b in bbs),
        }

    # Non-G2 (C1) jobs sharing the frozen file: ranges listed for exclusion.
    c1_ranges = []
    for c1d in sorted(runs_dir.glob(args.c1_glob)):
        m = _load_json(c1d / "lab-metadata.json") or {}
        aid = (m.get("provider_usage") or {}).get("attempt_id")
        bb = tok_bounds.get(aid) if aid else None
        c1_ranges.append({"job": c1d.name, "attempt_id": aid, "bounds": bb})

    spend = _spend(rows, c1_ranges, runs_dir, args.modal_billing)
    ctx = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ"),
        "command": " ".join(sys.argv),
        "live_root": str(live_root),
        "live_rev": git_rev(live_root),
        "lf2_digest": _lf2_digest(rows),
        "n_tasks": len({t for t, _ in cells}),
        "n_cells": len(cells),
        "n_finished": sum(1 for r in rows if r.get("finished")),
        "n_pending": sum(1 for r in rows if not r.get("finished")),
        "g2_closed": (
            "G2 closes at 42 valid jobs: 20 wave-1 + 22 r2; "
            "the 18 refused r2 specs are not run and never counted."
        ),
        "wave1": _summarize(wave1),
        "r2": _summarize(r2jobs),
        "n_r2_specs": len(r2jobs) + len(refused_r2),
        "refused_r2": refused_r2,
        "capture_file": {
            "path": str(capdirs["g2"] / "calls.jsonl") if capdirs.get("g2") else None,
            "sha256": (frozen or {}).get("sha256"),
            "size_bytes": (frozen or {}).get("size_bytes"),
            "lines": (frozen or {}).get("lines"),
            "n_tokens": len(tok_bounds),
        },
        "ranges": {
            "wave1": _range(wave1),
            "g2b_failures": _range(g2b_jobs),
            "r2": _range(r2jobs),
        },
        "c1_exclusion": c1_ranges,
        "spend": spend,
    }

    # GEPA seed receipt.
    rep = _representative_spec(cells)
    rep_sha = _sha256_file(rep) if rep else None
    by_cell = {(r["task"], r["attempt"], r["which"]): r for r in rows}
    seeds = {}
    for t in GEPA_MINIBATCH:
        seeds[t] = {
            "a1": _seed_paths(by_cell.get((t, 1, "orig"))),
            "a2": _seed_paths(by_cell.get((t, 2, "orig")))
            or _seed_paths(by_cell.get((t, 2, "r2"))),
        }
        # Prefer the r2 job for attempt 2 once it lands.
        r2 = by_cell.get((t, 2, "r2"))
        if r2 and r2.get("finished"):
            seeds[t]["a2"] = _seed_paths(r2)
    gepa = {
        "lf2_path": "research/experiments/har126-lf2/harness-lf2",
        "lf2_digest": ctx["lf2_digest"],
        "rep_spec": str(rep) if rep else None,
        "rep_spec_sha": rep_sha,
        "limits": _spec_limits(_load_json(rep) if rep else None),
        "seeds": seeds,
    }
    md = render_results_md(
        ctx,
        rows,
        telemetry,
        sampler,
        captures,
        gepa,
        maint_log,
    )
    (out / "RESULTS.md").write_text(md, encoding="utf-8")
    print(
        f"wrote {out / 'results.jsonl'} "
        f"({sum(1 for r in rows if r.get('finished'))} finished / "
        f"{len(rows)} rows)"
    )
    print(f"counted_pass: {len(passes)} -> {out / 'counted_pass.jsonl'}")
    print(f"RESULTS.md <- {len(md.splitlines())} lines")
    return 0


def _lf2_digest(rows) -> str:
    for r in rows:
        spec = _load_json(Path(str(r.get("spec")))) if r.get("spec") else None
        if spec and spec.get("harness_tree_sha256"):
            return str(spec["harness_tree_sha256"])
    return "unknown"


def _representative_spec(cells):
    cell = cells.get(("000803", 1)) or {}
    p = cell.get("spec")
    if p and Path(str(p)).is_file():
        return Path(str(p))
    for (_, _), c in sorted(cells.items()):
        if c.get("spec") and Path(str(c["spec"])).is_file():
            return Path(str(c["spec"]))
    return None


def _spec_limits(spec: dict | None) -> str:
    if not spec:
        return "n/a"
    return (
        f"max_requests={spec.get('max_requests')} "
        f"max_input_tokens={spec.get('max_input_tokens')} "
        f"max_output_tokens={spec.get('max_output_tokens')} "
        f"model={spec.get('model')} env={spec.get('environment')} "
        f"timeout_s={spec.get('timeout_seconds')} concurrency={spec.get('concurrency')}"
    )


def _sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_paths(r: dict | None) -> str:
    if not r or not r.get("finished"):
        status = (r or {}).get("status", "absent")
        return f"PENDING ({status})"
    jd = r["job_path"]
    return (
        f"{jd}/result.json {jd}/config.json {jd}/lock.json "
        f"{jd}/lab-metadata.json {jd}/processed/job.json "
        f"{jd}/processed/trial-{r.get('trial_name')}.json "
        f"[outcome={counted_outcome(r)}]"
    )


if __name__ == "__main__":
    sys.exit(main())
