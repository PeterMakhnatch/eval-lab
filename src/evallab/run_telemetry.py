"""Per-round run telemetry: live sampler + post-hoc extractor (HAR-126).

Two halves, one module:

- :func:`extract_round` reads recorded Harbor job directories (no network)
  and rebuilds per-call latency, concurrency over time, and trial startup
  delay from trajectory step timestamps.
- :func:`run_sampler` polls a live round every ``interval_s`` seconds and
  appends one JSON object per sample to a JSONL file: SGLang ``/metrics``,
  Modal container count, GPU utilization via nvidia-smi if running, and
  lab trial counts (queue/running).

Per-call latency here is the gap between consecutive trajectory step
timestamps ending at an agent (LLM) step. That gap covers LLM inference
plus harness overhead (and any server-side queueing), but also the ~1 s
tool-playback sleeps the Terminus harness inserts between steps, so treat
it as an upper bound on server-side latency, not a server measurement.
The proxy ledger (``provider_usage.calls``) carries token counts but no
timing, so there is nothing more precise in the recorded jobs.
Per-call server queue wait is likewise not derivable post-hoc; only the
live sampler's ``sglang:num_queue_reqs`` gauge sees it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_METRICS_URL = (
    "https://p-makhnatch--evallab-mimo-v26-9b-mimoserver.us-east.modal.direct/metrics"
)
DEFAULT_MODAL_APP = "evallab-mimo-v26-9b"

#: SGLang Prometheus gauges the sampler keeps (full text is not stored).
SGLANG_GAUGES = {
    "sglang:num_running_reqs": "num_running_reqs",
    "sglang:num_queue_reqs": "num_queue_reqs",
    "sglang:gen_throughput": "gen_throughput_tok_s",
    "sglang:token_usage": "token_usage",
    "sglang:cache_hit_rate": "cache_hit_rate",
    "sglang:num_used_tokens": "num_used_tokens",
}

QUEUE_WAIT_NOTE = (
    "per-call server queue wait is not recorded in job dirs "
    "(provider_usage.calls has no timing); use the live sampler's "
    "sglang:num_queue_reqs gauge during a round"
)


def parse_ts(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp; return None when missing/unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def percentile(xs: list[float], q: float) -> float | None:
    """Nearest-rank percentile in [0, 1]; None for empty input."""
    if not xs:
        return None
    ordered = sorted(xs)
    rank = min(len(ordered) - 1, max(0, int(q * len(ordered))))
    return ordered[rank]


@dataclass
class CallRecord:
    trial: str
    seq: int
    start: datetime
    end: datetime
    latency_s: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass
class TrialTelemetry:
    job: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    calls: list[CallRecord] = field(default_factory=list)
    provider_calls: int | None = None
    trajectory_found: bool = False

    @property
    def startup_s(self) -> float | None:
        """Job start to first completed LLM call (setup + first latency)."""
        if self.started_at is None or not self.calls:
            return None
        return (self.calls[0].end - self.started_at).total_seconds()


def _trial_trajectories(job_dir: Path) -> list[Path]:
    return sorted(job_dir.glob("*/agent/trajectory.json"))


def extract_trial(job_dir: Path) -> TrialTelemetry:
    """Extract per-call records for one Harbor job directory."""
    trial = TrialTelemetry(job=job_dir.name)
    try:
        lab = json.loads((job_dir / "lab-metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        lab = {}
    if isinstance(lab, dict):
        trial.started_at = parse_ts(lab.get("started_at"))
        trial.finished_at = parse_ts(lab.get("finished_at"))
        provider = lab.get("provider_usage")
        if isinstance(provider, dict) and isinstance(provider.get("calls"), list):
            trial.provider_calls = len(provider["calls"])
    for traj_path in _trial_trajectories(job_dir):
        try:
            payload = json.loads(traj_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        steps = payload.get("steps") if isinstance(payload, dict) else None
        if not isinstance(steps, list):
            continue
        trial.trajectory_found = True
        previous_ts: datetime | None = None
        seq = 0
        for step in steps:
            if not isinstance(step, dict):
                continue
            stamp = parse_ts(step.get("timestamp"))
            is_call = step.get("source") == "agent" and isinstance(step.get("metrics"), dict)
            if is_call and stamp is not None and previous_ts is not None:
                seq += 1
                metrics = step["metrics"]
                trial.calls.append(
                    CallRecord(
                        trial=job_dir.name,
                        seq=seq,
                        start=previous_ts,
                        end=stamp,
                        latency_s=(stamp - previous_ts).total_seconds(),
                        prompt_tokens=metrics.get("prompt_tokens")
                        if isinstance(metrics.get("prompt_tokens"), int)
                        else None,
                        completion_tokens=metrics.get("completion_tokens")
                        if isinstance(metrics.get("completion_tokens"), int)
                        else None,
                    )
                )
            if stamp is not None:
                previous_ts = stamp
    return trial


def _concurrency_at(calls: list[CallRecord], moment: datetime) -> int:
    return sum(1 for call in calls if call.start <= moment < call.end)


def summarize(trials: list[TrialTelemetry]) -> dict[str, Any]:
    """Build the JSON-serializable round summary."""
    calls = [call for trial in trials for call in trial.calls]
    latencies = [call.latency_s for call in calls]

    peak_call = 0
    weighted = 0.0
    span = 0.0
    if calls:
        events: list[tuple[datetime, int]] = []
        for call in calls:
            events.append((call.start, 1))
            events.append((call.end, -1))
        # Endings sort before beginnings at equal timestamps.
        events.sort(key=lambda item: (item[0], item[1]))
        level = 0
        previous: datetime | None = None
        for moment, delta in events:
            if previous is not None and moment > previous:
                width = (moment - previous).total_seconds()
                span += width
                weighted += level * width
                peak_call = max(peak_call, level)
            level += delta
            previous = moment

    by_level: dict[int, list[float]] = {}
    for call in calls:
        midpoint = call.start + (call.end - call.start) / 2
        level = _concurrency_at(calls, midpoint)
        by_level.setdefault(level, []).append(call.latency_s)
    concurrency_stats = {
        str(level): {
            "n": len(values),
            "p50_s": percentile(values, 0.5),
            "p90_s": percentile(values, 0.9),
        }
        for level, values in sorted(by_level.items())
    }

    windows = [
        (trial.started_at, trial.finished_at)
        for trial in trials
        if trial.started_at is not None and trial.finished_at is not None
    ]
    peak_trial = 0
    if windows:
        markers: list[tuple[datetime, int]] = []
        for start, end in windows:
            markers.append((start, 1))
            markers.append((end, -1))
        markers.sort(key=lambda item: (item[0], item[1]))
        level = 0
        for _, delta in markers:
            level += delta
            peak_trial = max(peak_trial, level)

    startups = [trial.startup_s for trial in trials if trial.startup_s is not None]
    matched = sum(
        1
        for trial in trials
        if trial.provider_calls is not None
        and trial.provider_calls == len(trial.calls)
        and trial.calls
    )
    mismatched = sorted(
        trial.job
        for trial in trials
        if trial.provider_calls is not None and trial.provider_calls != len(trial.calls)
    )
    starts = [t.started_at for t in trials if t.started_at is not None]
    ends = [t.finished_at for t in trials if t.finished_at is not None]
    return {
        "n_trials": len(trials),
        "n_trials_with_trajectory": sum(1 for t in trials if t.trajectory_found),
        "n_calls": len(calls),
        "window": {
            "start": min(starts).isoformat() if starts else None,
            "end": max(ends).isoformat() if ends else None,
        },
        "latency_s": {
            "n": len(latencies),
            "mean": (sum(latencies) / len(latencies)) if latencies else None,
            "min": min(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
            "p50": percentile(latencies, 0.5),
            "p90": percentile(latencies, 0.9),
        },
        "latency_by_call_concurrency": concurrency_stats,
        "peak_call_concurrency": peak_call,
        "mean_call_concurrency": (weighted / span) if span > 0 else None,
        "peak_trial_concurrency": peak_trial,
        "trial_startup_s": {
            "n": len(startups),
            "p50": percentile(startups, 0.5),
            "p90": percentile(startups, 0.9),
        },
        "queue_wait_s": None,
        "queue_wait_note": QUEUE_WAIT_NOTE,
        "trajectory_vs_ledger_matched": matched,
        "trajectory_vs_ledger_mismatched": mismatched,
    }


def iter_job_dirs(paths: list[Path]) -> list[Path]:
    """Expand CLI paths to job dirs (a job dir holds lab-metadata.json)."""
    job_dirs: list[Path] = []
    for path in paths:
        if (path / "lab-metadata.json").is_file():
            job_dirs.append(path)
            continue
        if path.is_dir():
            for child in sorted(path.iterdir()):
                if child.is_dir() and (child / "lab-metadata.json").is_file():
                    job_dirs.append(child)
    return job_dirs


def extract_round(paths: list[Path]) -> tuple[list[TrialTelemetry], dict[str, Any]]:
    """Extract every trial under ``paths`` and summarize the round."""
    job_dirs = iter_job_dirs(paths)
    trials = [extract_trial(job_dir) for job_dir in job_dirs]
    return trials, summarize(trials)


# ---------------------------------------------------------------------------
# Live sampler
# ---------------------------------------------------------------------------


def parse_sglang_metrics(text: str) -> dict[str, float]:
    """Keep the gauges in :data:`SGLANG_GAUGES` from Prometheus text."""
    kept: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition(" ")
        name = name.split("{", 1)[0]
        if name not in SGLANG_GAUGES:
            continue
        try:
            kept[SGLANG_GAUGES[name]] = float(value.strip().split(" ", 1)[0])
        except ValueError:
            continue
    return kept


def fetch_metrics_text(url: str, timeout_s: float = 10.0) -> str:
    """GET an SGLang /metrics endpoint (no API key; it is unauthenticated)."""
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return response.read().decode("utf-8", errors="replace")


def modal_containers(app: str | None = None) -> list[dict[str, Any]]:
    """List live Modal containers via the CLI (read-only)."""
    command = ["modal", "container", "list", "--json"]
    if app:
        command += ["--app-id", app]
    raw = subprocess.run(
        command, capture_output=True, text=True, timeout=60, check=False
    ).stdout.strip()
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"modal container list did not return JSON: {raw[:200]}") from exc
    items = payload.get("containers", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise RuntimeError("unexpected modal container list shape")
    return items


def modal_gpu_stats(container_id: str) -> dict[str, Any]:
    """Query nvidia-smi in a live Modal container via `modal container exec`."""
    command = [
        "modal",
        "container",
        "exec",
        "--no-pty",
        container_id,
        "nvidia-smi",
        "--query-gpu=utilization.gpu,utilization.memory,memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ]
    res = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    if res.returncode != 0:
        return {"ok": False, "error": res.stderr.strip() or f"exit {res.returncode}"}
    lines = res.stdout.strip().splitlines()
    if not lines:
        return {"ok": False, "error": "empty output from nvidia-smi"}
    parts = [p.strip() for p in lines[0].split(",")]
    if len(parts) >= 4:
        try:
            return {
                "ok": True,
                "gpu_util_pct": float(parts[0]),
                "mem_util_pct": float(parts[1]),
                "mem_used_mib": float(parts[2]),
                "mem_total_mib": float(parts[3]),
            }
        except ValueError as exc:
            return {"ok": False, "error": f"unparseable numbers: {parts} ({exc})"}
    return {"ok": False, "error": f"unexpected output: {lines[0]}"}


def lab_status(runs_dir: Path | None = None, queue_dir: Path | None = None) -> dict[str, Any]:
    """Count trials by state from lab-metadata and queue directories."""
    status: dict[str, Any] = {}
    if runs_dir and runs_dir.is_dir():
        running = finished = 0
        for child in runs_dir.iterdir():
            lab_file = child / "lab-metadata.json"
            if not (child.is_dir() and lab_file.is_file()):
                continue
            try:
                lab = json.loads(lab_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(lab, dict) and parse_ts(lab.get("finished_at")) is not None:
                finished += 1
            else:
                running += 1
        status["runs_running"] = running
        status["runs_finished"] = finished

    q = queue_dir
    if q is None and runs_dir is not None:
        candidate = runs_dir.parent / "queue"
        if candidate.is_dir():
            q = candidate
    if q and q.is_dir():
        queue_counts: dict[str, int] = {}
        for state_dir in ("running", "approved", "waiting", "pending"):
            target = q / state_dir
            if target.is_dir():
                queue_counts[state_dir] = len(
                    [f for f in target.iterdir() if not f.name.startswith(".") and f.is_file()]
                )
        status["queue"] = queue_counts
    return status


def sample_once(
    metrics_url: str | None = DEFAULT_METRICS_URL,
    runs_dir: Path | None = None,
    modal_app: str | None = DEFAULT_MODAL_APP,
    queue_dir: Path | None = None,
    *,
    fetch_fn: Callable[[str], str] = fetch_metrics_text,
    modal_containers_fn: Callable[[str | None], list[dict[str, Any]]] = modal_containers,
    modal_gpu_fn: Callable[[str], dict[str, Any]] = modal_gpu_stats,
) -> dict[str, Any]:
    """Take one sample; probe failures are recorded, never raised."""
    sample: dict[str, Any] = {"ts": datetime.now(UTC).isoformat()}
    if metrics_url and metrics_url.lower() != "none":
        try:
            text = fetch_fn(metrics_url)
            sample["sglang"] = {
                "ok": True,
                "metrics_bytes": len(text),
                **parse_sglang_metrics(text),
            }
        except Exception as exc:  # noqa: BLE001 - recorded, not raised
            sample["sglang"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        containers = modal_containers_fn(modal_app)
        sample["modal"] = {
            "ok": True,
            "container_count": len(containers),
            "containers": [c.get("container_id") for c in containers if isinstance(c, dict)],
        }
        if containers:
            first = containers[0]
            first_cid = first.get("container_id") if isinstance(first, dict) else None
            if first_cid:
                sample["gpu"] = modal_gpu_fn(first_cid)
    except Exception as exc:  # noqa: BLE001 - recorded, not raised
        sample["modal"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if runs_dir is not None or queue_dir is not None:
        sample["lab"] = lab_status(runs_dir, queue_dir)
    return sample


def run_sampler(
    out: Path,
    *,
    metrics_url: str | None = DEFAULT_METRICS_URL,
    runs_dir: Path | None = None,
    modal_app: str | None = DEFAULT_MODAL_APP,
    queue_dir: Path | None = None,
    interval_s: float = 15.0,
    samples: int | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    fetch_fn: Callable[[str], str] = fetch_metrics_text,
    modal_containers_fn: Callable[[str | None], list[dict[str, Any]]] = modal_containers,
    modal_gpu_fn: Callable[[str], dict[str, Any]] = modal_gpu_stats,
) -> int:
    """Poll until stopped (Ctrl-C/SIGTERM) or ``samples`` is reached.

    Returns the number of samples written.
    """
    stop = False

    def _handle(_signum: object, _frame: object) -> None:
        nonlocal stop
        stop = True

    try:
        import signal as _signal

        _signal.signal(_signal.SIGINT, _handle)
        _signal.signal(_signal.SIGTERM, _handle)
    except (ValueError, OSError, RuntimeError):
        pass
    written = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as handle:
        while not stop:
            record = sample_once(
                metrics_url,
                runs_dir,
                modal_app,
                queue_dir,
                fetch_fn=fetch_fn,
                modal_containers_fn=modal_containers_fn,
                modal_gpu_fn=modal_gpu_fn,
            )
            record["interval_s"] = interval_s
            handle.write(json.dumps(record) + "\n")
            handle.flush()
            written += 1
            if samples is not None and written >= samples:
                break
            if stop:
                break
            sleep_fn(interval_s)
    return written


# ---------------------------------------------------------------------------
# Command entry points (wired into evallab.cli as `telemetry ...`)
# ---------------------------------------------------------------------------


def extract_command(paths: list[Path], out: Path | None) -> int:
    trials, summary = extract_round(paths)
    if not trials:
        print("No Harbor job directories found.", file=sys.stderr)
        return 1
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out} ({summary['n_calls']} calls, {summary['n_trials']} trials)")
    else:
        print(json.dumps(summary, indent=2))
    latency = summary["latency_s"]
    p50_str = f"{latency['p50']:.2f}s" if latency["p50"] is not None else "None"
    p90_str = f"{latency['p90']:.2f}s" if latency["p90"] is not None else "None"
    print(
        f"calls={summary['n_calls']} p50={p50_str} p90={p90_str} "
        f"peak_call_concurrency={summary['peak_call_concurrency']} "
        f"peak_trial_concurrency={summary['peak_trial_concurrency']}"
    )
    return 0


def build_telemetry_parser(
    subparsers: argparse._SubParsersAction,  # noqa: SLF001 - argparse private type
) -> None:
    telemetry = subparsers.add_parser(
        "telemetry", help="Per-round run telemetry: sample live, extract post-hoc"
    )
    actions = telemetry.add_subparsers(dest="telemetry_command", required=True)
    extract = actions.add_parser(
        "extract", help="Summarize per-call latency/concurrency from job dirs (no network)"
    )
    extract.add_argument("paths", type=Path, nargs="+")
    extract.add_argument("--out", type=Path, default=None)
    extract.set_defaults(func=_telemetry_cli_command)

    sample = actions.add_parser(
        "sample", help="Poll SGLang /metrics + Modal + lab trials into JSONL until stopped"
    )
    sample.add_argument("--out", type=Path, required=True)
    sample.add_argument("--metrics-url", default=DEFAULT_METRICS_URL)
    sample.add_argument("--runs-dir", type=Path, default=None)
    sample.add_argument("--queue-dir", type=Path, default=None)
    sample.add_argument("--modal-app", default=DEFAULT_MODAL_APP)
    sample.add_argument("--interval", type=float, default=15.0)
    sample.set_defaults(func=_telemetry_cli_command)


def _telemetry_cli_command(
    args: argparse.Namespace,
    root: Path,
    *,
    harbor: Any = None,
) -> int:
    del root, harbor
    if args.telemetry_command == "extract":
        return extract_command(args.paths, args.out)
    return run_sampler(
        args.out,
        metrics_url=args.metrics_url,
        runs_dir=args.runs_dir,
        modal_app=args.modal_app,
        queue_dir=args.queue_dir,
        interval_s=args.interval,
    )
