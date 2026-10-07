"""Fetch and materialize Terminal Wrench's published native ATIF trajectories.

The default detector corpus uses ``sanitized_trajectories`` for hacks and
``baseline_trajectories`` for clean runs. ``--variant stripped`` selects the
upstream tool-only view; ``--variant raw`` explicitly selects the original
``hack_trajectories``. These variants never fall back to one another on a 404.
Native tool calls and observations are preserved, without reconstructing,
duplicating, or truncating terminal output from episode response/prompt files.

Source: few-sh/terminal-wrench (Apache-2.0). Only opaque ids appear in local
trial paths; source paths, variants, and labels stay in the external manifest
and fetch cache. Upstream sanitization is not a claim of zero lexical leakage:
``audit`` reports residual user/agent wording and observation coverage.

Stdlib only; Harbor models validate ATIF when installed.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

SEED = 20261006
TW_BASE = "https://raw.githubusercontent.com/few-sh/terminal-wrench/main"
N_HACK = 100
N_CLEAN = 100
FAMILY_FLOOR = 8
TM_CAP = 2
USER_AGENT = "eval-lab-terminal-wrench"


def native_variant(row: dict, variant: str = "sanitized") -> str:
    """Select the native upstream view, always retaining clean baselines."""
    if variant not in ("sanitized", "stripped", "raw"):
        raise ValueError(f"Unknown Terminal Wrench variant: {variant}")
    is_clean = row.get("kind", row.get("label")) == "clean"
    is_clean = is_clean or row.get("tw_key", "").startswith("clean__")
    return "baseline" if is_clean else variant


def trajectory_url(row: dict, variant: str = "sanitized") -> str:
    selected = native_variant(row, variant)
    tree = "hack_trajectories" if selected == "raw" else f"{selected}_trajectories"
    return (
        f"{TW_BASE}/tasks/{row['task_id']}/{row['model']}/{tree}/"
        f"{row['label']}/trial/agent/trajectory.json"
    )


def trajectory_cache_path(
    row: dict, cache_dir: str | os.PathLike, variant: str = "sanitized"
) -> Path:
    return Path(cache_dir) / native_variant(row, variant) / f"{row['id']}.json"


def prepare_trajectory(native: dict, trajectory_id: str) -> dict:
    """Copy native ATIF, changing only opaque document/run identifiers."""
    doc = copy.deepcopy(native)
    doc["trajectory_id"] = trajectory_id
    doc["session_id"] = trajectory_id
    errors = validate_trajectory(doc)
    if errors:
        raise ValueError(f"Invalid native ATIF for {trajectory_id}: {errors}")
    return doc


def message_text(message: str | list | None) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return "\n".join(
            part.get("text", "")
            for part in message
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def observation_stats(doc: dict) -> dict:
    agent_steps = [step for step in doc["steps"] if step["source"] == "agent"]
    observed = sum(
        any(
            message_text(result.get("content")).strip()
            for result in (step.get("observation") or {}).get("results", [])
        )
        for step in agent_steps
    )
    return {
        "agent_steps": len(agent_steps),
        "observed_steps": observed,
        "observation_coverage": observed / len(agent_steps) if agent_steps else 0.0,
    }


def validate_trajectory(doc: dict) -> list[str]:
    """Return a list of validation errors (empty = valid)."""
    errors = []
    if doc.get("schema_version") not in ("ATIF-v1.5", "ATIF-v1.6", "ATIF-v1.8"):
        errors.append("unsupported native ATIF schema_version")
    agent = doc.get("agent")
    if not isinstance(agent, dict) or not agent.get("name"):
        errors.append("agent.name is required")
    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("steps must be a non-empty list")
        return errors
    for i, s in enumerate(steps):
        if s.get("step_id") != i + 1:
            errors.append(f"step {i}: step_id must be {i + 1}")
        if s.get("source") not in ("system", "user", "agent"):
            errors.append(f"step {i}: bad source")
        if not isinstance(s.get("message"), (str, list)):
            errors.append(f"step {i}: message must be a string or content parts")
        if s.get("source") != "agent" and s.get("tool_calls") is not None:
            errors.append(f"step {i}: tool_calls only allowed on agent steps")
        for c in s.get("tool_calls") or []:
            if not c.get("tool_call_id") or not c.get("function_name"):
                errors.append(f"step {i}: tool call needs id and function name")
        obs = s.get("observation")
        if obs is not None:
            if not isinstance(obs.get("results"), list):
                errors.append(f"step {i}: observation.results must be a list")
            else:
                call_ids = {c.get("tool_call_id") for c in s.get("tool_calls") or []}
                for r in obs["results"]:
                    sc = r.get("source_call_id")
                    if sc is not None and sc not in call_ids:
                        errors.append(f"step {i}: dangling source_call_id {sc}")
    try:
        from harbor.models.trajectories import Trajectory  # ty: ignore[unresolved-import]
    except ImportError:
        return errors
    try:
        Trajectory.model_validate(doc)
    except Exception as e:
        errors.append(f"harbor ATIF validation: {e}")
    return errors


def build_result(
    *,
    task_id: str,
    trial_name: str,
    trial_dir: str | os.PathLike,
    model: str,
    reward: float = 1.0,
) -> dict:
    """Build a Harbor TrialResult-shaped result.json dict (rewarded pass)."""
    task_path = f"terminal-wrench/{task_id}"
    return {
        "task_name": task_path,
        "trial_name": trial_name,
        "trial_uri": f"file://{Path(trial_dir).resolve()}",
        "task_id": {"path": task_path},
        "task_checksum": hashlib.sha1(
            f"terminal-wrench/{task_id}/{trial_name}".encode()
        ).hexdigest(),
        "config": {
            "task": {"path": task_path},
            "agent": {"name": "terminus", "model_name": model},
        },
        "agent_info": {
            "name": "terminus",
            "version": "tw-scoreboard-20261006",
            "model_info": {"name": model},
        },
        "verifier_result": {"rewards": {"reward": reward}},
        "exception_info": None,
    }


def validate_result(doc: dict) -> list[str]:
    """Return a list of TrialResult validation errors (empty = valid)."""
    errors = []
    for f in (
        "task_name",
        "trial_name",
        "trial_uri",
        "task_id",
        "task_checksum",
        "config",
        "agent_info",
    ):
        if doc.get(f) in (None, ""):
            errors.append(f"{f} is required")
    vr = (doc.get("verifier_result") or {}).get("rewards", {}) or {}
    if vr.get("reward") != 1.0:
        errors.append(f"verifier_result.rewards.reward must be 1.0, got {vr.get('reward')!r}")
    if not (doc.get("agent_info") or {}).get("model_info", {}).get("name"):
        errors.append("agent_info.model_info.name is required")
    try:
        from harbor.models.trial.result import TrialResult  # ty: ignore[unresolved-import]

        TrialResult.model_validate(doc)
    except ImportError:
        pass
    except Exception as e:
        errors.append(f"harbor TrialResult validation: {e}")
    return errors


def materialize_trial(
    *,
    dest: str | os.PathLike,
    task_id: str,
    trial_name: str,
    model: str,
    native_trajectory: dict,
    reward: float = 1.0,
) -> dict:
    """Write a self-contained trial from native ATIF, without label metadata.

    The instruction comes from native user text, never the raw Red Team prompt.
    Source variant/path and labels remain outside the reader-visible trial.
    """
    dest = Path(dest)
    traj = prepare_trajectory(native_trajectory, trial_name)
    st = observation_stats(traj)
    result = build_result(
        task_id=task_id,
        trial_name=trial_name,
        trial_dir=dest,
        model=model,
        reward=reward,
    )
    r_errors = validate_result(result)
    if r_errors:
        raise ValueError(f"result invalid: {r_errors}")
    (dest / "agent").mkdir(parents=True, exist_ok=True)
    (dest / "verifier").mkdir(parents=True, exist_ok=True)
    (dest / "agent" / "trajectory.json").write_text(json.dumps(traj, indent=1), encoding="utf-8")
    (dest / "result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    instruction = next(
        (message_text(step["message"]) for step in traj["steps"] if step["source"] == "user"), ""
    )
    (dest / "instruction.md").write_text(
        f"# Task instruction\n\n{instruction}".rstrip() + "\n", encoding="utf-8"
    )
    (dest / "trial.log").write_text(
        f"terminal-wrench trial {trial_name}\n"
        f"task terminal-wrench/{task_id}\n"
        f"agent_steps {st['agent_steps']}\n"
        "adapter evallab.terminal_wrench\n",
        encoding="utf-8",
    )
    (dest / "verifier" / "test-stdout.txt").write_text(f"reward: {reward}\n", encoding="utf-8")
    return st


def _fetch_one(job: tuple[str, str]) -> tuple[str, str]:
    url, dst = job
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        return ("cached", url)
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
            if not body:
                return ("empty", url)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(body)
            return ("ok", url)
        except Exception as e:
            if getattr(e, "code", None) == 404:
                return ("404", url)
            time.sleep(1 + attempt * 2)
    return ("fail", url)


def fetch_trials(
    rows: list[dict],
    cache_dir: str | os.PathLike,
    variant: str = "sanitized",
    workers: int = 12,
) -> dict:
    """Fetch native ATIF into a variant-separated cache; report failed ids."""
    jobs = [
        (trajectory_url(row, variant), str(trajectory_cache_path(row, cache_dir, variant)))
        for row in rows
    ]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(_fetch_one, jobs))
    stats: dict[str, Any] = dict(Counter(status for status, _ in outcomes))
    stats["files"] = len(jobs)
    stats["failures"] = [
        {"id": row["id"], "status": status, "url": url}
        for row, (status, url) in zip(rows, outcomes, strict=True)
        if status not in ("ok", "cached")
    ]
    return stats


def _primary_family(categories: list[str], fam_counts: Counter) -> str:
    if not categories:
        return "unobserved"
    return min(categories, key=lambda c: (fam_counts[c], c))


def subset_sample(
    sample: dict,
    seed: int = SEED,
    n_hack: int = N_HACK,
    n_clean: int = N_CLEAN,
    floor: int = FAMILY_FLOOR,
    tm_cap: int = TM_CAP,
) -> tuple[list[dict], list[dict]]:
    """Seeded stratified subset: hacks across families, model-balanced clean."""
    rng = random.Random(seed)
    hacks = [dict(h) for h in sample["hacks"]]
    fam_counts = Counter(c for h in hacks for c in h["categories"])
    for h in hacks:
        h["families"] = sorted(h["categories"]) or ["unobserved"]
        h["family"] = _primary_family(h["categories"], fam_counts)
        h["tw_key"] = f"hacks__{h['task_id']}__{h['model']}__{h['label']}"

    order = hacks[:]
    rng.shuffle(order)
    chosen: list[dict] = []
    seen: set[tuple] = set()
    tm_count: Counter = Counter()
    fam_have: Counter = Counter()

    def take(h: dict) -> bool:
        key = (h["task_id"], h["model"], h["label"])
        tm = (h["task_id"], h["model"])
        if key in seen or tm_count[tm] >= tm_cap:
            return False
        seen.add(key)
        tm_count[tm] += 1
        for c in h["categories"]:
            fam_have[c] += 1
        chosen.append(h)
        return True

    def fill_family(fam: str, need: int, fresh_only: bool) -> int:
        for h in order:
            if need <= 0 or len(chosen) >= n_hack:
                break
            if fam not in h["categories"]:
                continue
            if fresh_only and tm_count[(h["task_id"], h["model"])] > 0:
                continue
            if take(h):
                need -= 1
        return need

    for fam in sorted(fam_counts, key=lambda c: (fam_counts[c], c)):
        need = min(floor, fam_counts[fam]) - fam_have[fam]
        if need <= 0:
            continue
        need = fill_family(fam, need, fresh_only=True)
        if need > 0:
            fill_family(fam, need, fresh_only=False)
    for h in order:
        if len(chosen) >= n_hack:
            break
        take(h)

    cleans = [dict(c) for c in sample["clean"]]
    for c in cleans:
        c["family"] = "clean_baseline"
        c["families"] = ["clean_baseline"]
        c["tw_key"] = f"clean__{c['task_id']}__{c['model']}__{c['label']}"
    rng.shuffle(cleans)
    by_model: dict[str, list[dict]] = {}
    for c in cleans:
        by_model.setdefault(c["model"], []).append(c)
    models = sorted(by_model)
    picked: list[dict] = []
    seen_tm: Counter = Counter()
    while len(picked) < n_clean:
        progressed = False
        for m in models:
            if len(picked) >= n_clean:
                break
            lst = by_model[m]
            for i, c in enumerate(lst):
                if seen_tm[(c["task_id"], c["model"])] < tm_cap:
                    picked.append(lst.pop(i))
                    seen_tm[(c["task_id"], c["model"])] += 1
                    progressed = True
                    break
        if not progressed:
            break
    rng.shuffle(picked)
    return chosen, picked


def _trial_pass_info(trial_dir: str) -> dict | None:
    rp = os.path.join(trial_dir, "result.json")
    try:
        with open(rp, encoding="utf-8") as f:
            r = json.load(f)
    except Exception:
        return None
    if not isinstance(r, dict) or "verifier_result" not in r:
        return None
    vr = (r.get("verifier_result") or {}).get("rewards", {}) or {}
    ai = r.get("agent_info") or {}
    agent = ai.get("name") or ""
    model = (ai.get("model_info") or {}).get("name")
    return {
        "reward": vr.get("reward"),
        "integrity": vr.get("integrity"),
        "agent": agent,
        "model": model,
        "has_trajectory": os.path.exists(os.path.join(trial_dir, "agent", "trajectory.json")),
    }


def _processed_taint_kinds(job_dir: str, trial: str) -> list[str]:
    ppath = os.path.join(job_dir, "processed", f"trial-{trial}.json")
    try:
        with open(ppath, encoding="utf-8") as f:
            p = json.load(f)
    except Exception:
        return []
    if not isinstance(p, dict):
        return []
    return [t.get("kind") for t in (p.get("taint") or []) if isinstance(t, dict)]


def _is_oracle(agent: str) -> bool:
    a = (agent or "").lower()
    return "oracle" in a or a == "nop" or "nop" in a.split("-")


def collect_ours(worktree: str | os.PathLike, results_root: str | os.PathLike) -> dict:
    """Collect real copied passes and HAR-168 clean passes.

    Copies = union of (a) copy_benchmark.jsonl rows with label true,
    (b) HAR-168 000792/001985 trials, (c) other non-oracle published passes
    with integrity == 0 or processed ``copied_code`` taint. Clean = HAR-168
    passes with integrity == 1. Returns dict with ``copies``, ``clean`` and
    ``excluded`` (name + reason) lists.
    """
    worktree = str(worktree)
    results_root = str(results_root)
    copies: dict[str, dict] = {}
    excluded: list[dict] = []

    def add_copy(trial: str, trial_dir: str, provenance: str) -> None:
        info = _trial_pass_info(trial_dir)
        if info is None:
            excluded.append({"trial": trial, "reason": "no result.json"})
            return
        if not isinstance(info["reward"], (int, float)) or info["reward"] <= 0:
            excluded.append({"trial": trial, "reason": "not a pass"})
            return
        if _is_oracle(info["agent"]):
            excluded.append({"trial": trial, "reason": "oracle/nop control"})
            return
        if not info["has_trajectory"]:
            excluded.append({"trial": trial, "reason": "no trajectory"})
            return
        if trial not in copies:
            copies[trial] = {
                "id": trial,
                "trial_dir": trial_dir,
                "agent": info["agent"],
                "model": info["model"],
                "reward": info["reward"],
                "integrity": info["integrity"],
                "provenance": provenance,
            }

    bench = os.path.join(
        worktree,
        "research",
        "explorations",
        "trace-lab",
        "review",
        "copy_benchmark.jsonl",
    )
    try:
        with open(bench, encoding="utf-8") as f:
            bench_rows = [json.loads(line) for line in f if line.strip()]
    except OSError:
        bench_rows = []
    for r in bench_rows:
        if r.get("label") is True:
            td = r.get("trial_dir", "")
            if td.startswith("/Users/petermakhnatch/Developer/eval-lab"):
                add_copy(r.get("trial") or r.get("id"), td, "copy_benchmark:label-true")
            else:
                excluded.append({"trial": r.get("id"), "reason": "trial_dir outside results"})

    for job_pat in ("HAR-168-har168-20261006-000792-a1", "HAR-168-har168-20261006-001985-a1"):
        jd = os.path.join(results_root, "2026-10-06", job_pat)
        try:
            kids = sorted(os.listdir(jd))
        except OSError:
            continue
        for k in kids:
            td = os.path.join(jd, k)
            if os.path.isdir(td) and os.path.exists(os.path.join(td, "result.json")):
                add_copy(k, td, "har168-flagged-copy")

    for date in sorted(os.listdir(results_root)):
        dated = os.path.join(results_root, date)
        if not os.path.isdir(dated):
            continue
        for job in sorted(os.listdir(dated)):
            jd = os.path.join(dated, job)
            if not os.path.isdir(jd):
                continue
            try:
                kids = os.listdir(jd)
            except OSError:
                continue
            for k in kids:
                if k in copies:
                    continue
                td = os.path.join(jd, k)
                if not (os.path.isdir(td) and os.path.exists(os.path.join(td, "result.json"))):
                    continue
                info = _trial_pass_info(td)
                if (
                    info is None
                    or not isinstance(info["reward"], (int, float))
                    or info["reward"] <= 0
                    or _is_oracle(info["agent"])
                ):
                    continue
                kinds = _processed_taint_kinds(jd, k)
                if info["integrity"] == 0 or "copied_code" in kinds:
                    add_copy(k, td, "scan:integrity0-or-copied-taint")

    clean: list[dict] = []
    for date in sorted(os.listdir(results_root)):
        dated = os.path.join(results_root, date)
        if not os.path.isdir(dated):
            continue
        for job in sorted(os.listdir(dated)):
            if "HAR-168-" not in job:
                continue
            jd = os.path.join(dated, job)
            try:
                kids = sorted(os.listdir(jd))
            except OSError:
                continue
            for k in kids:
                td = os.path.join(jd, k)
                if not (os.path.isdir(td) and os.path.exists(os.path.join(td, "result.json"))):
                    continue
                info = _trial_pass_info(td)
                if info is None:
                    continue
                if (
                    isinstance(info["reward"], (int, float))
                    and info["reward"] > 0
                    and info["integrity"] == 1
                    and info["has_trajectory"]
                    and not _is_oracle(info["agent"])
                ):
                    clean.append(
                        {
                            "id": k,
                            "trial_dir": td,
                            "agent": info["agent"],
                            "model": info["model"],
                            "reward": info["reward"],
                            "integrity": info["integrity"],
                            "provenance": "har168:integrity-1",
                        }
                    )
    return {
        "copies": sorted(copies.values(), key=lambda r: r["id"]),
        "clean": sorted(clean, key=lambda r: r["id"]),
        "excluded": excluded,
    }


def _selected_rows(args: argparse.Namespace) -> tuple[list[dict], list[dict]]:
    sub = json.loads(Path(args.subset).read_text())
    metas = [{**row, "kind": "hack"} for row in sub["hacks"]]
    metas += [{**row, "kind": "clean"} for row in sub["clean"]]
    if args.manifest:
        manifest = [
            json.loads(line)
            for line in Path(args.manifest).read_text().splitlines()
            if line.strip()
        ]
        lookup = {row["tw_key"]: row for row in metas}
        selected = [
            {**lookup[row["tw_key"]], "id": row["id"]} for row in manifest if row["source"] == "tw"
        ]
    else:
        selected = [{**row, "id": f"tw-{i + 1:04d}"} for i, row in enumerate(metas)]
        manifest = []
    return selected, manifest


def _cmd_fetch(args: argparse.Namespace) -> int:
    rows, _ = _selected_rows(args)
    stats = fetch_trials(rows, args.cache_dir, args.variant, args.workers)
    if args.out_stats:
        Path(args.out_stats).write_text(json.dumps(stats, indent=1))
    print(json.dumps(stats, indent=1))
    return 1 if stats["failures"] else 0


def _cmd_subset(args: argparse.Namespace) -> int:
    sample = json.loads(Path(args.sample).read_text(encoding="utf-8"))
    hacks, clean = subset_sample(
        sample, seed=args.seed, n_hack=args.n_hack, n_clean=args.n_clean, floor=args.floor
    )
    out = {
        "seed": args.seed,
        "n_hack": len(hacks),
        "n_clean": len(clean),
        "hacks": hacks,
        "clean": clean,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    fam = Counter(h["family"] for h in hacks)
    print(
        json.dumps(
            {
                "n_hack": len(hacks),
                "n_clean": len(clean),
                "hack_families": dict(sorted(fam.items())),
                "hack_models": dict(Counter(h["model"] for h in hacks)),
                "clean_models": dict(Counter(c["model"] for c in clean)),
            },
            indent=1,
        )
    )
    return 0


def _reward_lookups(sample_dir: str) -> tuple[dict, dict]:
    """Map (task, model, label) -> reward for hacks and clean baselines."""
    hack_rew: dict[tuple, float] = {}
    try:
        trajs = json.loads(Path(sample_dir, "trajectories.json").read_text(encoding="utf-8"))
        for r in trajs:
            hack_rew[(r["task_id"], r["model"], r["trajectory_label"])] = r.get("reward")
    except OSError:
        pass
    clean_rew: dict[tuple, float] = {}
    try:
        entries = json.loads(Path(sample_dir, "tasks.json").read_text(encoding="utf-8"))
        for e in entries:
            for b in e.get("baselines", []):
                clean_rew[(b.get("task_id", e["task_id"]), e["model"], b["label"])] = b.get(
                    "reward"
                )
    except OSError:
        pass
    return hack_rew, clean_rew


def _cmd_materialize(args: argparse.Namespace) -> int:
    rows, manifest = _selected_rows(args)
    existing = {row["id"]: row for row in manifest}
    hack_rew, clean_rew = _reward_lookups(args.sample_dir)
    agg = Counter(agent_steps=0, observed_steps=0, trials=0)
    for meta in rows:
        trial_id = meta["id"]
        key = (meta["task_id"], meta["model"], meta["label"])
        reward = (hack_rew if meta["kind"] == "hack" else clean_rew).get(key)
        if reward != 1.0:
            raise ValueError(f"Missing or non-passing upstream reward for {trial_id}: {reward}")
        native = json.loads(trajectory_cache_path(meta, args.cache_dir, args.variant).read_text())
        dest = Path(args.out_tw).resolve() / trial_id
        st = materialize_trial(
            dest=dest,
            task_id=meta["task_id"],
            trial_name=trial_id,
            model=meta["model"],
            native_trajectory=native,
            reward=1.0,
        )
        for metric in ("agent_steps", "observed_steps"):
            agg[metric] += st[metric]
        agg["trials"] += 1
        row = {
            "id": trial_id,
            "source": "tw",
            "label": meta["kind"],
            "family": meta["family"],
            "trial_dir": str(dest),
            "tw_key": meta["tw_key"],
            "episodes": meta["episodes"],
            "n_agent_steps": st["agent_steps"],
            "reward": reward,
            "tw_variant": native_variant(meta, args.variant),
        }
        if len(meta.get("families", [])) > 1:
            row["families"] = meta["families"]
        if trial_id in existing:
            existing[trial_id].update(
                tw_variant=row["tw_variant"],
                n_agent_steps=st["agent_steps"],
                trial_dir=str(dest),
            )
        else:
            manifest.append(row)
    Path(args.out_manifest).write_text(
        "".join(json.dumps(row) + "\n" for row in manifest), encoding="utf-8"
    )
    report: dict[str, Any] = dict(agg)
    report["observation_coverage"] = agg["observed_steps"] / agg["agent_steps"]
    if args.out_stats:
        Path(args.out_stats).write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    return 0


def _cmd_ours(args: argparse.Namespace) -> int:
    res = collect_ours(args.worktree, args.results_root)
    with open(args.out, "w", encoding="utf-8") as f:
        for r in res["copies"] + res["clean"]:
            kind = "copy" if r in res["copies"] else "clean"
            f.write(json.dumps({**r, "kind": kind}) + "\n")
    print(
        json.dumps(
            {
                "copies": len(res["copies"]),
                "clean": len(res["clean"]),
                "excluded": res["excluded"],
                "copy_ids": [r["id"] for r in res["copies"]],
            },
            indent=1,
        )
    )
    return 0


def _cmd_evalset(args: argparse.Namespace) -> int:
    rows = []
    with open(args.tw_manifest, encoding="utf-8") as f:
        rows += [json.loads(line) for line in f if line.strip()]
    with open(args.ours, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            is_copy = r.pop("kind", "copy") == "copy"
            trial_dir = r.pop("trial_dir")
            rid = r.pop("id")
            info = _trial_pass_info(trial_dir) or {}
            try:
                with open(
                    os.path.join(trial_dir, "agent", "trajectory.json"), encoding="utf-8"
                ) as tf:
                    t = json.load(tf)
                n_steps = sum(1 for s in t.get("steps", []) if s.get("source") == "agent")
                n_eps = len(t.get("steps", []))
            except Exception:
                n_steps, n_eps = 0, 0
            rows.append(
                {
                    "id": f"ours-{rid}",
                    "source": "ours",
                    "label": "hack" if is_copy else "clean",
                    "family": "copy_upstream" if is_copy else "clean_pass",
                    "trial_dir": trial_dir,
                    "tw_key": None,
                    "episodes": n_eps,
                    "n_agent_steps": n_steps,
                    "reward": info.get("reward", r.get("reward")),
                }
            )
    with open(args.out, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    fam = Counter((r["source"], r["label"], r["family"]) for r in rows)
    stats = {
        "n_rows": len(rows),
        "by_source_label_family": {str(k): v for k, v in sorted(fam.items())},
    }
    with open(args.out_stats, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=1)
    print(json.dumps(stats, indent=1))
    return 0


def audit_manifest(rows: list[dict]) -> dict:
    """Count residual wording in messages only, not legitimate tool output."""
    patterns = {
        word: re.compile(pattern, re.IGNORECASE)
        for word, pattern in {
            "red team": r"red[\s-]+team",
            "exploit": r"exploit",
            "bypass": r"bypass",
            "hack": r"hack",
            "cheat": r"cheat",
            "verifier": r"verifier",
        }.items()
    }
    groups = {}
    per_trial = {}
    for row in rows:
        if row["source"] != "tw":
            continue
        doc = json.loads((Path(row["trial_dir"]) / "agent/trajectory.json").read_text())
        label = row["label"]
        group = groups.setdefault(
            label,
            {
                "trials": 0,
                "any_word": 0,
                "user_any_word": 0,
                "agent_any_word": 0,
                "words": dict.fromkeys(patterns, 0),
                "user_words": dict.fromkeys(patterns, 0),
                "agent_words": dict.fromkeys(patterns, 0),
                "agent_steps": 0,
                "observed_steps": 0,
                "schema_versions": {},
            },
        )
        texts = {
            role: "\n".join(
                message_text(step.get("message")) for step in doc["steps"] if step["source"] == role
            )
            for role in ("user", "agent")
        }
        hits = {
            role: [word for word, pattern in patterns.items() if pattern.search(text)]
            for role, text in texts.items()
        }
        all_hits = set(hits["user"] + hits["agent"])
        group["trials"] += 1
        group["any_word"] += bool(all_hits)
        for role in ("user", "agent"):
            group[f"{role}_any_word"] += bool(hits[role])
            for word in hits[role]:
                group[f"{role}_words"][word] += 1
        for word in all_hits:
            group["words"][word] += 1
        stats = observation_stats(doc)
        group["agent_steps"] += stats["agent_steps"]
        group["observed_steps"] += stats["observed_steps"]
        version = doc["schema_version"]
        group["schema_versions"][version] = group["schema_versions"].get(version, 0) + 1
        per_trial[row["id"]] = {
            **stats,
            "user_words": hits["user"],
            "agent_words": hits["agent"],
            "schema_version": version,
        }
    for group in groups.values():
        group["observation_coverage"] = (
            group["observed_steps"] / group["agent_steps"] if group["agent_steps"] else 0.0
        )
    return {
        "message_word_method": "case-insensitive substring; red team accepts whitespace/hyphen",
        "by_label": groups,
        "per_trial": per_trial,
    }


def _cmd_audit(args: argparse.Namespace) -> int:
    rows = [
        json.loads(line) for line in Path(args.manifest).read_text().splitlines() if line.strip()
    ]
    report = audit_manifest(rows)
    Path(args.out).write_text(json.dumps(report, indent=1))
    print(json.dumps(report["by_label"], indent=1))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("fetch", help="download native upstream ATIF")
    s.add_argument("--subset", required=True)
    s.add_argument("--manifest", help="preserve existing TW ids/order")
    s.add_argument("--cache-dir", required=True)
    s.add_argument("--variant", choices=("sanitized", "stripped", "raw"), default="sanitized")
    s.add_argument("--workers", type=int, default=12)
    s.add_argument("--out-stats")
    s.set_defaults(func=_cmd_fetch)

    s = sub.add_parser("subset", help="seeded stratified hack/clean subset")
    s.add_argument("--sample", default="/tmp/tw-scoreboard/sample.json")
    s.add_argument("--out", required=True)
    s.add_argument("--seed", type=int, default=SEED)
    s.add_argument("--n-hack", type=int, default=N_HACK)
    s.add_argument("--n-clean", type=int, default=N_CLEAN)
    s.add_argument("--floor", type=int, default=FAMILY_FLOOR)
    s.set_defaults(func=_cmd_subset)

    s = sub.add_parser("materialize", help="materialize Harbor-shaped TW trial dirs")
    s.add_argument("--subset", required=True)
    s.add_argument("--manifest", help="preserve all rows, ids, labels and family metadata")
    s.add_argument("--sample-dir", default="/tmp/tw-scoreboard")
    s.add_argument("--cache-dir", required=True)
    s.add_argument("--variant", choices=("sanitized", "stripped", "raw"), default="sanitized")
    s.add_argument("--out-tw", required=True)
    s.add_argument("--out-manifest", required=True)
    s.add_argument("--out-stats")
    s.set_defaults(func=_cmd_materialize)

    s = sub.add_parser("ours", help="collect real copied/clean own traces")
    s.add_argument("--worktree", required=True)
    s.add_argument("--results-root", required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(func=_cmd_ours)

    s = sub.add_parser("evalset", help="merge TW + ours manifests")
    s.add_argument("--tw-manifest", required=True)
    s.add_argument("--ours", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--out-stats", required=True)
    s.set_defaults(func=_cmd_evalset)
    s = sub.add_parser("audit", help="audit TW message wording and observation coverage")
    s.add_argument("--manifest", required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(func=_cmd_audit)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
