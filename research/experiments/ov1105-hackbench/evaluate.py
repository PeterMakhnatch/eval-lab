"""Frozen HackBench evaluation. Preparation never computes detector signals.

Run ``prepare --help`` before preparation. Only the parent may release ``score``.
Raw views, judge replies, usage receipts, and signals must remain outside git.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import importlib.util
import json
import math
import random
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DETBENCH = HERE.parent / "ov1104-detbench"
JUDGES = HERE.parent / "ov1104-judges"
SEED = "hackbench-eval-20261104:"
SEALED_TASKS = frozenset({
    "scaling_law", "llm_online_serving", "moving_mnist_world_model",
    "cross-modal-retrieval", "materials-tokenization", "improving-replay-buffers",
    "grpo_multisource",
})


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def source_tree() -> str:
    return fingerprint({
        str(path.relative_to(ROOT)): digest(path)
        for path in sorted((ROOT / "src" / "evallab").rglob("*.py"))
    })


def read_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def save_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(canonical(row) + "\n" for row in rows))


def external(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.is_relative_to(ROOT):
        raise ValueError("raw evaluation artifacts must be outside the worktree")
    return path


def configuration() -> dict[str, Any]:
    text = (HERE / "PREREG.md").read_text()
    block = re.search(r"```json\n(.*?)\n```", text, re.S)
    if block is None:
        raise ValueError("PREREG.md has no frozen configuration")
    config = json.loads(block[1])
    for name, expected in config["source_sha256"].items():
        if digest(ROOT / name) != expected:
            raise ValueError(f"frozen source changed: {name}")
    if source_tree() != config["evallab_python_tree_sha256"]:
        raise ValueError("frozen evallab Python source tree changed")
    return config


def module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def rank(row: dict[str, Any]) -> str:
    return hashlib.sha256((SEED + row["run_key"]).encode()).hexdigest()


def stratified(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Proportional natural-source-set allocation, largest remainder; no RNG state."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["set"]].append(row)
    if not rows:
        return []
    target = min(limit, len(rows))
    quotas = {key: target * len(group) // len(rows) for key, group in groups.items()}
    order = sorted(groups, key=lambda key: (-(target * len(groups[key]) % len(rows)), key))
    for key in order[:target - sum(quotas.values())]:
        quotas[key] += 1
    return [row for key in sorted(groups) for row in sorted(groups[key], key=rank)[:quotas[key]]]


def sample_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select exploratory measurements; report unread eligibility without filtering."""
    seen: set[str] = set()
    sequences: Counter[tuple[str, str]] = Counter()
    for row in rows:
        key = row["run_key"]
        if key in seen:
            raise ValueError(f"duplicate run_key: {key}")
        seen.add(key)
        if row.get("split") != "heldout" or row.get("label") not in (0, 1):
            raise ValueError(f"not a binary held-out row: {key}")
        task = row.get("task_id")
        if not task or not row.get("cluster") or not row.get("independent_run_key"):
            raise ValueError("adapter must declare task_id, cluster and independent_run_key")
        sequences[(task, row["independent_run_key"])] += 1
    positives = stratified([r for r in rows if r["label"] == 1], 200)
    negatives = stratified([r for r in rows if r["label"] == 0], 400 - len(positives))
    sample = sorted(positives + negatives, key=lambda r: r["run_key"])
    return sample, {
        "evaluation_kind": "retrospective_exploratory",
        "source_records": len(rows), "exclusions": {},
        "exposure_partitions": dict(Counter(r.get("exposure_partition", "missing") for r in rows)),
        "previously_detector_read_records": sum(bool(r.get("read_by")) for r in rows),
        "primary_unseen_records": sum(r.get("read_by") == [] for r in rows),
        "primary_sealed_unseen_records": sum(
            r.get("read_by") == [] and r["task_id"] in SEALED_TASKS
            and r.get("exposure_partition") == "sealed" for r in rows
        ),
        "eligible_exploratory_records": len(rows),
        "exact_sequence_groups": len(sequences),
        "repeated_exact_sequence_records": sum(n - 1 for n in sequences.values()),
        "duplicate_excluded_records": 0,
        "sample_runs": len(sample), "positives": len(positives), "negatives": len(negatives),
        "sample_construction": dict(Counter("natural" if r["natural"] else "synthetic" for r in sample)),
        "sample_exposure_partitions": dict(Counter(r["exposure_partition"] for r in sample)),
        "clusters": len({r["cluster"] for r in sample}),
        "by_set": dict(Counter(r["set"] for r in sample)),
        "sampling_excluded_runs": len(rows) - len(sample),
    }


def prepare(args: argparse.Namespace) -> None:
    config = configuration()
    output = external(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("prepare requires a new empty output directory; never overwrite a freeze")
    runs = external(args.runs)
    rows, coverage = sample_rows(read_rows(runs))
    # Sampling uses metadata only. Hashing view bytes establishes custody, not a detector score.
    sample = []
    for row in rows:
        item = {key: row[key] for key in (
            "run_key", "set", "split", "label", "cluster", "task_id",
            "independent_run_key", "exposure_partition", "natural", "n_chars", "n_steps", "read_by",
        )}
        for key in ("view", "view_actions_only"):
            path = Path(row[key]).expanduser()
            item[key] = str(external(path if path.is_absolute() else runs.parent / path))
        sample.append(item)
    view_sha256 = {row[key]: digest(Path(row[key])) for row in sample
                   for key in ("view", "view_actions_only")}
    identities = {
        str(path.relative_to(ROOT)): digest(path)
        for path in (HERE / "evaluate.py", HERE / "PREREG.md", HERE / "adapter.py",
                     HERE / "intake.py", HERE / "manifest.json", HERE / "label-map.json")
    }
    freeze = {
        "schema": "hackbench-evaluation/v1", "prepared_at": datetime.now(UTC).isoformat(),
        "runs": str(runs), "runs_sha256": digest(runs), "sample_sha256": fingerprint(sample),
        "files_sha256": identities, "views_sha256": view_sha256,
        "config_sha256": fingerprint(config), "coverage": coverage,
    }
    save_rows(output / "sample-runs.jsonl", sample)
    save(output / "freeze.json", freeze)
    status = "prepared_no_signals" if sample else "blocked_empty_sample"
    print(json.dumps({"status": status, **coverage}, indent=2))


def admitted(output: Path, receipt: Path | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    # Refuse BEFORE detector imports, view reads, usage probes or model calls.
    if receipt is None:
        raise ValueError("score is blocked: an explicit parent release receipt is required")
    config = configuration()
    freeze_path = output / "freeze.json"
    freeze = json.loads(freeze_path.read_text())
    release = json.loads(receipt.read_text())
    if (release.get("released_by") != "parent" or release.get("allow_scoring") is not True
            or release.get("freeze_sha256") != digest(freeze_path)
            or not re.fullmatch(r"[0-9a-f]{40}", release.get("pushed_commit", ""))):
        raise ValueError("invalid parent release or freeze identity")
    # This is a custody attestation, not permission generated by this program.
    commit = release["pushed_commit"]
    subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=ROOT, check=True,
                   capture_output=True)
    remotes = subprocess.run(["git", "branch", "-r", "--contains", commit], cwd=ROOT,
                             check=True, capture_output=True, text=True).stdout
    if not remotes.strip():
        raise ValueError("release commit has no locally fetched remote branch")
    for name, expected in freeze["files_sha256"].items():
        committed = subprocess.run(["git", "show", f"{commit}:{name}"], cwd=ROOT,
                                   capture_output=True, check=True).stdout
        if hashlib.sha256(committed).hexdigest() != expected or digest(ROOT / name) != expected:
            raise ValueError(f"release commit or working file differs from freeze: {name}")
    if fingerprint(config) != freeze["config_sha256"]:
        raise ValueError("configuration changed after preparation")
    if digest(Path(freeze["runs"])) != freeze["runs_sha256"]:
        raise ValueError("adapter runs changed after preparation")
    sample = read_rows(output / "sample-runs.jsonl")
    if fingerprint(sample) != freeze["sample_sha256"]:
        raise ValueError("sample changed after preparation")
    if not sample:
        raise ValueError("no rows in the prepared exploratory sample")
    for path, expected in freeze["views_sha256"].items():
        if digest(Path(path)) != expected:
            raise ValueError("a converted view changed after preparation")
    return config, sample


class QuotaStop(RuntimeError):
    """An absent reading or either weekly stop threshold forbids another call."""


def quota(output: Path, tag: str) -> None:
    try:
        result = subprocess.run(["omp", "usage"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        with (output / "usage.jsonl").open("a") as handle:
            handle.write(canonical({"timestamp": datetime.now(UTC).isoformat(), "batch": tag,
                                    "error": type(exc).__name__}) + "\n")
        raise QuotaStop("usage unavailable") from exc
    text = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
    codex = re.search(r"Openai Codex\s*[—-].*?(?=\n\S|\Z)", text, re.S | re.I)
    codex_values = re.findall(r"7 days[^\n]*?([\d.]+)% used", codex[0]) if codex else []
    gemini_values = re.findall(r"Gemini \(Weekly\)[^\n]*?([\d.]+)% used", text)
    codex_used = max(map(float, codex_values), default=None)
    gemini_used = max(map(float, gemini_values), default=None)
    receipt = {
        "timestamp": datetime.now(UTC).isoformat(), "batch": tag, "returncode": result.returncode,
        "codex_weekly_pct": codex_used, "gemini_weekly_pct": gemini_used,
        "stdout": result.stdout, "stderr": result.stderr,
    }
    path = output / "usage.jsonl"
    with path.open("a") as handle:
        handle.write(canonical(receipt) + "\n")
    if result.returncode or codex_used is None or gemini_used is None:
        raise QuotaStop("usage unavailable or weekly quota lines unparseable")
    if codex_used >= 62 or gemini_used >= 78:
        raise QuotaStop(f"weekly stop: Codex={codex_used}%, Gemini={gemini_used}%")


def signal(row: dict[str, Any], name: str, version: str, value: float | None,
           *, abstain: str | None = None, model: str | None = None,
           evidence: list[int] | None = None) -> dict[str, Any]:
    return {"run_key": row["run_key"], "signal": name, "version": version, "score": value,
            "abstain": abstain, "model": model, "evidence_steps": evidence or [], "cost_usd": 0.0}


def local_signals(rows: list[dict[str, Any]], config: dict[str, Any], output: Path) -> dict[str, Any]:
    sys.path.insert(0, str(DETBENCH))
    sys.path.insert(0, str(ROOT / "src"))
    l0 = module("hackbench_l0", DETBENCH / "l0_rules.py")
    signals: dict[str, list[dict[str, Any]]] = {name: [] for name in config["local_thresholds"]}
    raw = []
    for row in rows:
        result = l0.evaluate_one((row["run_key"], row["view"], "now"))
        raw.append(result)
        reasons = {l0.reason_key(reason) for reason in result["reasons"]}
        for name in signals:
            error = None
            if name.startswith("length_"):
                value = float(row["n_chars" if name == "length_chars" else "n_steps"])
                version = config["length_version"]
            else:
                version = config["l0_version"]
                error = result["error"]
                if name == "l0v_full_any_hard":
                    value = float(any(r.startswith("L0_HARD_") for r in result["reasons"]))
                elif name == "l0v_full_any_rule":
                    value = float(any(r.startswith("L0_") and r not in {
                        "L0_UNRECOGNIZED_TOOL_SHAPE", "L0_PARSE_ERROR", "L0_NO_EVIDENCE", "L0_RUNNER_ERROR"
                    } for r in result["reasons"]))
                elif name == "l0v_full_decision":
                    value = {"pass": 0.0, "review": 0.5, "drop": 1.0}[result["decision"]]
                else:
                    value = float(name.removeprefix("l0v_full_") in reasons)
            signals[name].append(signal(row, name, version, None if error else value,
                                        abstain=f"L0 error: {error}" if error else None,
                                        evidence=result.get("evidence_steps")))
    save_rows(output / "l0-raw.jsonl", raw)
    # Full line is incompatible: the existing runtime admits DEV-only sources and requires
    # task certification/AutoReview provenance absent from this dataset. Do not bypass it.
    signals["line_replay_decision"] = [signal(
        row, "line_replay_decision", config["line_version"], None,
        abstain="incompatible: DEV-only line admission; no task certification or frozen AutoReview cache",
    ) for row in rows]
    return signals


async def judge_signals(rows: list[dict[str, Any]], config: dict[str, Any], output: Path) -> dict[str, Any]:
    sys.path.insert(0, str(JUDGES))
    judge = module("hackbench_judge", JUDGES / "judge.py")
    gate = asyncio.Lock()
    stop_reason: str | None = None
    window_tasks: set[asyncio.Task[Any]] = set()

    async def drain_windows() -> None:
        for task in window_tasks:
            if not task.done():
                task.cancel()
        if window_tasks:
            await asyncio.gather(*window_tasks, return_exceptions=True)
            window_tasks.clear()

    class AttestedCaller(judge.Caller):
        def call(self, model: str, prompt: str, *, tag: dict[str, Any], **kwargs: Any) -> asyncio.Task[Any]:
            # Track at creation, including windows that have not started executing.
            task = asyncio.create_task(self._call(model, prompt, tag=tag, **kwargs))
            window_tasks.add(task)
            return task

        async def _call(self, model: str, prompt: str, *, tag: dict[str, Any], **kwargs: Any) -> Any:
            nonlocal stop_reason
            # A one-window batch: serial admission even when judge_run gathers its windows.
            async with gate:
                if stop_reason is not None:
                    raise QuotaStop(stop_reason)
                try:
                    quota(output, f"{model}:{tag['run_key']}:{tag['window']}")
                except QuotaStop as exc:
                    stop_reason = str(exc)
                    raise
                reply = await super().call(model, prompt, tag=tag, **kwargs)
                if not reply["error"] and reply.get("model_served") != model:
                    reply["error"] = "missing or mismatched served-model attestation"
                return reply

    caller = AttestedCaller(output / "calls.jsonl", concurrency=1, temperature=0.0, max_tokens=1500)
    signals: dict[str, list[dict[str, Any]]] = {}
    stopped = None
    try:
        for name, spec in config["judges"].items():
            entries = []
            raw_dir = output / "judges" / name
            raw_dir.mkdir(parents=True, exist_ok=True)
            for row in rows:
                result: dict[str, Any] = {"score": None, "abstain": stopped}
                if stopped is None:
                    path = raw_dir / (hashlib.sha256(row["run_key"].encode()).hexdigest() + ".json")
                    try:
                        result = await judge.judge_run(caller, judge="j2b", model=spec["model"], row=row,
                                                       window_chars=60000, seed=None, out_path=path)
                    except QuotaStop as exc:
                        stopped = str(exc)
                        result = {"score": None, "abstain": stopped}
                    except Exception as exc:  # Preserve failure as coverage, never silently drop a run.
                        result = {"score": None, "abstain": f"judge error: {type(exc).__name__}: {exc}"[:300]}
                    finally:
                        await drain_windows()
                    save(path, result)
                entry = signal(row, name, spec["version"], result.get("score"),
                               abstain=result.get("abstain"), evidence=result.get("evidence_steps"),
                               model=spec["model"] if result.get("score") is not None else None)
                entry["partial_windows_failed"] = result.get("partial_windows_failed", [])
                entries.append(entry)
            signals[name] = entries
    finally:
        await drain_windows()
        gateway = judge.Gateway._current
        if gateway is not None:
            gateway.close()
            await gateway.process.wait()
            await asyncio.gather(gateway.reader, gateway.stderr_reader, return_exceptions=True)
    save(output / "attestation.json", judge.summarize_log(output / "calls.jsonl"))
    return signals


def rate_intervals(rows: list[dict[str, Any]], entries: list[dict[str, Any]],
                   threshold: float | None) -> dict[str, Any]:
    """Percentile task-cluster intervals; abstentions remain unflagged in denominators."""
    scores = {entry["run_key"]: entry["score"] for entry in entries}
    groups: dict[str, list[int]] = {}
    for row in rows:
        counts = groups.setdefault(row["cluster"], [0, 0, 0, 0])
        label = row["label"]
        counts[label] += 1
        value = scores.get(row["run_key"])
        if threshold is not None and value is not None and value >= threshold:
            counts[label + 2] += 1
    if len(groups) < 2:
        return {"recall": [None, None], "fpr": [None, None]}
    blocks = list(groups.values())
    rng = random.Random(1104)
    recalls, fprs = [], []
    for _ in range(1000):
        totals = [0, 0, 0, 0]
        for _ in blocks:
            for index, count in enumerate(rng.choice(blocks)):
                totals[index] += count
        if totals[1]:
            recalls.append(totals[3] / totals[1])
        if totals[0]:
            fprs.append(totals[2] / totals[0])

    def interval(values: list[float]) -> list[float | None]:
        if len(values) < 500:
            return [None, None]
        values.sort()
        return [values[int(0.025 * (len(values) - 1))], values[int(0.975 * (len(values) - 1))]]

    return {"recall": interval(recalls), "fpr": interval(fprs)}


def summary(rows: list[dict[str, Any]], entries: list[dict[str, Any]], thresholds: dict[str, Any],
            metrics: Any, config: dict[str, Any]) -> dict[str, Any]:
    by_key = {r["run_key"]: r for r in rows}
    scored = {e["run_key"]: (str(by_key[e["run_key"]]["cluster"]), by_key[e["run_key"]]["label"], e["score"])
              for e in entries if e["score"] is not None}
    positives = sum(r["label"] == 1 for r in rows)
    negatives = len(rows) - positives
    ab_pos = positives - sum(y == 1 for _, y, _ in scored.values())
    ab_neg = negatives - sum(y == 0 for _, y, _ in scored.values())
    result: dict[str, Any] = {
        "n": len(rows), "positives": positives, "negatives": negatives,
        "scored": len(scored), "coverage": len(scored) / len(rows) if rows else None,
        "abstain_positive": ab_pos, "abstain_negative": ab_neg,
        "abstentions": dict(Counter(e["abstain"] for e in entries if e["score"] is None)),
        "partial_window_failures": sum(bool(e.get("partial_windows_failed")) for e in entries),
        "auroc": metrics.auroc(list(scored.values())),
        "auroc_ci95": metrics.bootstrap_auroc(list(scored.values()), 1000, 1104),
        "limitation": "no labelled negatives" if negatives == 0 else ("no labelled positives" if positives == 0 else None),
        "operating_points": {}, "matched_length": {},
    }
    for budget, threshold in thresholds.items():
        result["operating_points"][budget] = metrics.rates_at(
            list(scored.values()), math.inf if threshold is None else threshold,
            abstained_neg=ab_neg, abstained_pos=ab_pos,
        )
        result["operating_points"][budget]["cluster_ci95"] = rate_intervals(rows, entries, threshold)
    for name, field in (("length_chars", "n_chars"), ("length_steps", "n_steps")):
        matched = {key: (cluster, label, float(by_key[key][field])) for key, (cluster, label, _) in scored.items()}
        delta = metrics.bootstrap_auroc_diff(scored, matched, 1000, 1104)
        result["matched_length"][name] = {
            "n": len(matched), "run_keys": sorted(matched), "auroc": metrics.auroc(list(matched.values())),
            "auroc_ci95": metrics.bootstrap_auroc(list(matched.values()), 1000, 1104),
            "operating_points": {
                budget: metrics.rates_at(list(matched.values()), math.inf if threshold is None else threshold)
                for budget, threshold in config["local_thresholds"][name].items()
            },
            "delta_auroc": delta[0], "delta_auroc_ci95": delta[1:],
            "beats_length": None if delta[1] is None else (
                "yes" if delta[1] > 0 else "no" if delta[2] < 0 else "tie"
            ),
        }
    return result


def score(args: argparse.Namespace) -> None:
    output = external(args.output)
    config, rows = admitted(output, args.release)
    with (output / "scoring.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # No resume/retry after observing outcomes. A stopped batch is an incomplete registered run.
        started = output / "STARTED.json"
        with started.open("x") as handle:
            json.dump({"started_at": datetime.now(UTC).isoformat(), "freeze_sha256": digest(output / "freeze.json")}, handle)
        signals = local_signals(rows, config, output)
        signals.update(asyncio.run(judge_signals(rows, config, output)))
        sys.path.insert(0, str(DETBENCH))
        metrics = module("hackbench_metrics", DETBENCH / "score.py")
        groups = {"pooled:all": rows}
        groups.update({set_id: [r for r in rows if r["set"] == set_id] for set_id in sorted({r["set"] for r in rows})})
        groups["pooled:all:natural"] = [r for r in rows if r["natural"] is True]
        groups["pooled:all:synthetic"] = [r for r in rows if r["natural"] is False]
        for partition in ("sealed", "study"):
            groups[f"pooled:prior-{partition}"] = [r for r in rows if r["exposure_partition"] == partition]
        report = {
            "freeze_sha256": digest(output / "freeze.json"),
            "coverage": json.loads((output / "freeze.json").read_text())["coverage"],
            "versions": {name: sorted({entry["version"] for entry in entries})
                         for name, entries in signals.items()},
            "judges": config["judges"],
            "attestation": json.loads((output / "attestation.json").read_text()),
            "quota_readings": [
                {key: row.get(key) for key in ("timestamp", "batch", "returncode",
                                              "codex_weekly_pct", "gemini_weekly_pct", "error")}
                for row in read_rows(output / "usage.jsonl")
            ] if (output / "usage.jsonl").exists() else [],
            "signals": {},
        }
        for name, entries in signals.items():
            save_rows(output / "signals" / f"{name}.jsonl", entries)
            thresholds = config["local_thresholds"].get(name, {})
            if name == "line_replay_decision":
                thresholds = config["line_thresholds"]
            if name in config["judges"]:
                thresholds = config["judges"][name]["thresholds"]
            report["signals"][name] = {}
            for group, subset in groups.items():
                keys = {r["run_key"] for r in subset}
                report["signals"][name][group] = summary(
                    subset, [e for e in entries if e["run_key"] in keys], thresholds, metrics, config,
                )
        save(output / "aggregate-results.json", report)
        print(f"Wrote {output / 'aggregate-results.json'}; raw artifacts stay outside git")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    preparation = sub.add_parser("prepare", help="freeze a metadata sample and view hashes; no detectors")
    preparation.add_argument("--runs", type=Path, required=True)
    preparation.add_argument("--output", type=Path, required=True)
    scoring = sub.add_parser("score", help="parent-release-only; runs frozen local signals and both judges")
    scoring.add_argument("--output", type=Path, required=True)
    scoring.add_argument("--release", type=Path)
    args = parser.parse_args()
    try:
        prepare(args) if args.command == "prepare" else score(args)
    except (ValueError, FileExistsError, BlockingIOError, subprocess.CalledProcessError) as exc:
        parser.exit(2, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
