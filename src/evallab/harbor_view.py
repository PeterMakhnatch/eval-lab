from __future__ import annotations

import argparse
import datetime as _datetime
import errno
import fnmatch
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from evallab.harbor_telemetry import harbor_child_environment

SCHEMA = "harbor_view/v1"
REWARD_DETAILS_SCHEMA = "harbor_view/reward_details/v1"

RULE_COPY_CHECK = "evallab.copy_check"
RULE_UPSTREAM_FETCH = "evallab.upstream_fetch"
RULE_GRADER_TAMPER = "evallab.grader_tamper"
RULE_RED_TEAM = "evallab.declared_red_team"

VIEWER_MIN_VERSION = (0, 24)
VIEWER_PIN = "harbor==0.24.0"


def rule_versions() -> dict[str, str]:
    from evallab.copy_check import RULE as copy_rule

    return {
        RULE_COPY_CHECK: copy_rule,
        RULE_UPSTREAM_FETCH: "confirmed_fetch",
        RULE_GRADER_TAMPER: "v1",
        RULE_RED_TEAM: "v1",
    }


def is_job_dir(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "config.json").is_file()
        and (path / "result.json").is_file()
    )


def discover_jobs(sources: list[Path]) -> tuple[list[Path], list[str]]:
    jobs: list[Path] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for source in sources:
        if is_job_dir(source):
            key = str(source.resolve())
            if key not in seen:
                seen.add(key)
                jobs.append(source.resolve())
            continue
        if source.is_dir():
            children = sorted(
                (child.resolve() for child in source.iterdir() if child.is_dir()),
                key=str,
            )
            found = [child for child in children if is_job_dir(child)]
            for job in found:
                if str(job) not in seen:
                    seen.add(str(job))
                    jobs.append(job)
            for child in children:
                if child not in found:
                    skipped.append(f"{child}: not a job dir")
            if not found:
                skipped.append(f"{source}: no job dirs inside")
            continue
        skipped.append(f"{source}: not a job dir or directory")
    jobs.sort(key=str)
    return jobs, skipped


def _iter_trial_dirs(job_dir: Path) -> list[Path]:
    try:
        children = sorted(job_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    return [c for c in children if c.is_dir() and (c / "result.json").is_file()]


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _copy_hit_from_flag(flag: dict[str, Any]) -> dict[str, Any] | None:
    if flag.get("kind") != "copied_code":
        return None
    return {
        "id": RULE_COPY_CHECK,
        "source": "stored",
        "evidence": {
            "matched_lines": flag.get("matched_lines"),
            "added_lines": flag.get("added_lines"),
            "source_artifact": flag.get("source_artifact") or "agent/trajectory.json",
            "examples": flag.get("examples"),
        },
    }


def _upstream_hit_from_flag(flag: dict[str, Any]) -> dict[str, Any] | None:
    from evallab.upstream_fetch import confirmed_fetch

    if not confirmed_fetch(flag):
        return None
    return {
        "id": RULE_UPSTREAM_FETCH,
        "source": "stored",
        "evidence": {
            "target": flag.get("target"),
            "excerpt": (flag.get("evidence") or flag.get("excerpt")),
        },
    }


def stored_hits(job_dir: Path, trial_name: str) -> tuple[list[dict] | None, str]:
    record = _read_json(job_dir / "processed" / f"trial-{trial_name}.json")
    if record is None:
        return None, "no stored processed record"
    errors = record.get("errors") or {}
    if errors.get("analysis"):
        return None, f"stored analysis failed: {errors.get('analysis')}"
    taint = record.get("taint")
    if not isinstance(taint, list):
        return None, "stored record has no taint list"
    hits: list[dict[str, Any]] = []
    for flag in taint:
        if not isinstance(flag, dict):
            continue
        hit = _copy_hit_from_flag(flag) or _upstream_hit_from_flag(flag)
        if hit is not None:
            hits.append(hit)
    return hits, "stored"


def _raw_agent_steps(trial_dir: Path) -> list[dict[str, Any]]:
    agent_dir = trial_dir / "agent"
    if not agent_dir.is_dir():
        return []
    steps: list[dict[str, Any]] = []
    for name in ("trajectory.json", "trajectory.jsonl"):
        path = agent_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        docs: list[Any] = []
        if name.endswith(".jsonl"):
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    docs.append(json.loads(line))
                except ValueError:
                    continue
        else:
            try:
                payload = json.loads(text)
            except ValueError:
                continue
            docs = payload if isinstance(payload, list) else [payload]
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            inner = doc.get("steps")
            if isinstance(inner, list):
                steps.extend(s for s in inner if isinstance(s, dict))
            elif isinstance(doc.get("tool_calls"), list):
                steps.append(doc)
    return [s for s in steps if s.get("source", "agent") == "agent"]


def compute_hits(
    trial_dir: Path, job_dir: Path
) -> tuple[list[dict[str, Any]], list[str]]:
    from evallab.copy_check import copy_check

    hits: list[dict[str, Any]] = []
    problems: list[str] = []
    try:
        flag = copy_check(trial_dir)
    except Exception as exc:  # noqa: BLE001 -- detectors fail open
        problems.append(f"copy_check: {type(exc).__name__}: {exc}")
    else:
        if flag is not None:
            hit = _copy_hit_from_flag(flag)
            if hit is not None:
                hit["source"] = "computed"
                hits.append(hit)
    try:
        from evallab import probe03
        from evallab.upstream_fetch import assess_upstream_fetch, confirmed_fetch

        analysis = probe03.analyze_trial_core(trial_dir, job_dir)
        flags = assess_upstream_fetch(
            analysis["agent_seq"], analysis["info"], trial_dir=trial_dir
        )
        for flag in flags:
            if isinstance(flag, dict) and confirmed_fetch(flag):
                hit = _upstream_hit_from_flag(flag)
                if hit is not None:
                    hit["source"] = "computed"
                    hits.append(hit)
                    break
    except Exception as exc:  # noqa: BLE001 -- detectors fail open
        problems.append(f"upstream_fetch: {type(exc).__name__}: {exc}")
    tamper_hits, tamper_problems = compute_hits_grader_only(trial_dir)
    hits.extend(tamper_hits)
    problems.extend(tamper_problems)
    return hits, problems


def trial_integrity(
    trial_dir: Path, job_dir: Path
) -> dict[str, Any]:
    stored, note = stored_hits(job_dir, trial_dir.name)
    problems: list[str] = []
    if stored is not None:
        hits, provenance = stored, "stored"
        tamper_hits, tamper_problems = compute_hits_grader_only(trial_dir)
        problems.extend(tamper_problems)
        seen = {hit["id"] for hit in hits}
        if RULE_COPY_CHECK not in seen:
            from evallab.copy_check import copy_check

            try:
                flag = copy_check(trial_dir)
            except Exception as exc:  # noqa: BLE001 -- retain explicit detector failure
                problems.append(f"copy_check: {type(exc).__name__}: {exc}")
            else:
                hit = _copy_hit_from_flag(flag) if flag is not None else None
                if hit is not None:
                    hit["source"] = "computed"
                    hits.append(hit)
                    seen.add(hit["id"])
                    provenance = "stored+computed"
        for hit in tamper_hits:
            if hit["id"] not in seen:
                hits.append(hit)
        if tamper_hits:
            provenance = "stored+computed"
    else:
        hits, compute_problems = compute_hits(trial_dir, job_dir)
        problems.extend(compute_problems)
        provenance = f"computed ({note})"
    from evallab.counts import declared_red_team

    red_team = declared_red_team(trial_dir, _read_json(trial_dir / "result.json") or {})
    if red_team is not None:
        hits.append({"id": RULE_RED_TEAM, "source": "computed", "evidence": red_team})
        if provenance == "stored":
            provenance = "stored+computed"
    fired = sorted({hit["id"] for hit in hits})
    return {
        "integrity": 0 if fired else 1,
        "fired": fired,
        "hits": hits,
        "provenance": provenance,
        "problems": problems,
    }


def compute_hits_grader_only(trial_dir: Path) -> tuple[list[dict], list[str]]:
    try:
        from evallab.tripwires import grader_tamper_hits

        tamper = grader_tamper_hits(_raw_agent_steps(trial_dir))
    except Exception as exc:  # noqa: BLE001 -- detectors fail open
        return [], [f"grader_tamper: {type(exc).__name__}: {exc}"]
    if not tamper:
        return [], []
    first = tamper[0]
    return [
        {
            "id": RULE_GRADER_TAMPER,
            "source": "computed",
            "evidence": {
                "n_steps": len(tamper),
                "paths": first.get("paths"),
                "excerpt": (first.get("excerpt") or "")[:200],
            },
        }
    ], []


def _is_native(rewards: dict[str, Any]) -> bool:
    return isinstance(rewards.get("integrity"), (int, float)) and not isinstance(
        rewards.get("integrity"), bool
    )

def _native_needs_overlay(trial_dir: Path, job_dir: Path) -> bool:
    return bool(trial_integrity(trial_dir, job_dir)["fired"])


def _scored_reward(rewards: Any) -> float | int | None:
    if not isinstance(rewards, dict):
        return None
    reward = rewards.get("reward")
    if isinstance(reward, bool) or not isinstance(reward, (int, float)):
        return None
    return reward


_NO_HARD_LINK = frozenset({errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EMLINK})


def _link(source: Path, dest: Path) -> None:
    if source.is_symlink():
        dest.symlink_to(source.parent / os.readlink(source))
        return
    if source.is_dir():
        dest.mkdir()
        for child in source.iterdir():
            _link(child, dest / child.name)
        return
    try:
        os.link(source, dest, follow_symlinks=False)
    except OSError as exc:
        if exc.errno not in _NO_HARD_LINK:
            raise
        dest.symlink_to(source)


VIEWER_ANALYSIS_MD = "analysis.md"


def _link_run_report(job_dir: Path, trial_dir: Path, dest: Path) -> None:
    report = job_dir / "processed" / f"trial-{trial_dir.name}.md"
    target = dest / VIEWER_ANALYSIS_MD
    if report.is_file() and not target.exists() and not target.is_symlink():
        _link(report, target)


def _unique_job_name(name: str, taken: set[str]) -> str:
    if name not in taken:
        return name
    idx = 2
    while f"{name}-{idx}" in taken:
        idx += 1
    return f"{name}-{idx}"


def _apply_arm(doc: dict[str, Any], arm: str) -> str:
    current = doc.get("source")
    label = arm if current in (None, arm) else f"{current}+{arm}"
    doc["source"] = label
    return label


def _write_source_record(
    dest: Path, *, trial: str, source_job: str, arm: str
) -> None:
    (dest / ".evallab-source.json").write_text(
        json.dumps(
            {
                "schema": "harbor_view/trial_source/v1",
                "trial": trial,
                "source_job": source_job,
                "arm": arm,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def overlay_trial(
    trial_dir: Path,
    job_dir: Path,
    dest: Path,
    *,
    reward: float | int,
    arm: str | None = None,
    source_job: str | None = None,
    trial_name: str | None = None,
) -> dict[str, Any]:
    verdict = trial_integrity(trial_dir, job_dir)
    integrity = verdict["integrity"]
    dest.mkdir(parents=True, exist_ok=False)
    for child in sorted(trial_dir.iterdir(), key=lambda p: p.name):
        if child.name in ("result.json", "reward-details.json", ".evallab-source.json"):
            continue
        _link(child, dest / child.name)
    original = _read_json(trial_dir / "result.json") or {}
    rewritten = json.loads(json.dumps(original))
    if arm is not None:
        _apply_arm(rewritten, arm)
    verifier = rewritten.get("verifier_result")
    if not isinstance(verifier, dict):
        verifier = {}
        rewritten["verifier_result"] = verifier
    rewards = verifier.get("rewards")
    if not isinstance(rewards, dict):
        rewards = {}
        verifier["rewards"] = rewards
    rewards["integrity"] = integrity
    rewards["reward_gated"] = reward * integrity
    (dest / "result.json").write_text(
        json.dumps(rewritten, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    details = {
        "schema": REWARD_DETAILS_SCHEMA,
        "trial": trial_name or trial_dir.name,
        "reward": reward,
        "integrity": integrity,
        "reward_gated": reward * integrity,
        "fired_rules": verdict["fired"],
        "rule_versions": rule_versions(),
        "provenance": verdict["provenance"],
        "hits": verdict["hits"],
        "problems": verdict["problems"],
    }
    original_verifier = original.get("verifier_result") or {}
    original_rewards = original_verifier.get("rewards") if isinstance(original_verifier, dict) else None
    if isinstance(original_rewards, dict) and _is_native(original_rewards):
        details["original_rewards"] = original_rewards
    if arm is not None:
        details["arm"] = arm
    if source_job is not None:
        details["source_job"] = source_job
    (dest / "reward-details.json").write_text(
        json.dumps(details, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return verdict


def mirror_job(
    job_dir: Path, dest: Path, *, trial_dirs: list[Path] | None = None
) -> dict[str, Any]:
    dest.mkdir(parents=True, exist_ok=False)
    counts: dict[str, Any] = {
        "source": str(job_dir),
        "trials": 0,
        "scored": 0,
        "native": 0,
        "overlay": 0,
        "unscored": 0,
        "unreadable": 0,
        "integrity_0": [],
        "rules_fired": {},
    }
    filtered = trial_dirs is not None
    if trial_dirs is None:
        trial_dirs = _iter_trial_dirs(job_dir)
    overlay_names = _plan_overlays(trial_dirs)
    for child in sorted(job_dir.iterdir(), key=lambda p: p.name):
        if child.is_dir() and (child / "result.json").is_file():
            continue
        if filtered and child.name in ("config.json", "result.json", "analysis.json"):
            continue
        _link(child, dest / child.name)
    for trial_dir in trial_dirs:
        counts["trials"] += 1
        trial_dest = dest / trial_dir.name
        plan = overlay_names.get(trial_dir.name)
        if plan is None:
            result = _read_json(trial_dir / "result.json")
            verifier = (result or {}).get("verifier_result") or {}
            rewards = verifier.get("rewards")
            if result is None:
                counts["unreadable"] += 1
            elif _scored_reward(rewards) is None:
                counts["unscored"] += 1
            else:
                counts["native"] += 1
                counts["scored"] += 1
            _link(trial_dir, trial_dest)
            _link_run_report(job_dir, trial_dir, trial_dest)
            continue
        reward, _ = plan
        verdict = overlay_trial(trial_dir, job_dir, trial_dest, reward=reward)
        _link_run_report(job_dir, trial_dir, trial_dest)
        counts["scored"] += 1
        counts["overlay"] += 1
        for rule in verdict["fired"]:
            counts["rules_fired"][rule] = counts["rules_fired"].get(rule, 0) + 1
        if verdict["integrity"] == 0:
            counts["integrity_0"].append(trial_dir.name)
    counts["integrity_0"].sort()
    if filtered:
        config = _read_json(job_dir / "config.json") or {}
        _restrict_config(config, trial_dirs)
        (dest / "config.json").write_text(
            json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        trial_docs = [
            (trial.name, doc)
            for trial in trial_dirs
            if (doc := _read_json(dest / trial.name / "result.json")) is not None
        ]
        (dest / "result.json").write_text(
            json.dumps(build_merged_result(dest.name, trial_docs), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    return counts


def _plan_overlays(
    trial_dirs: list[Path],
) -> dict[str, tuple[float | int, dict[str, Any]]]:
    plan: dict[str, tuple[float | int, dict[str, Any]]] = {}
    for trial_dir in trial_dirs:
        result = _read_json(trial_dir / "result.json")
        if result is None:
            continue
        verifier = result.get("verifier_result") or {}
        rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
        reward = _scored_reward(rewards)
        if reward is None:
            continue
        if isinstance(rewards, dict) and _is_native(rewards) and not _native_needs_overlay(trial_dir, trial_dir.parent):
            continue
        plan[trial_dir.name] = (reward, rewards or {})
    return plan


DEFAULT_ARM_REGEX = r"(?P<arm>stock|tuned|gepa)"


def parse_merge_spec(spec: str) -> tuple[str, str]:
    name, sep, glob = spec.partition("=")
    if not sep or not name.strip() or not glob.strip():
        raise ValueError(f"--merge needs NAME=GLOB, got {spec!r}")
    return name.strip(), glob.strip()


def compile_arm_pattern(source: str | None) -> re.Pattern[str]:
    try:
        pattern = re.compile(source or DEFAULT_ARM_REGEX)
    except re.error as exc:
        raise ValueError(f"bad --arm-regex {source!r}: {exc}") from exc
    if "arm" not in pattern.groupindex:
        raise ValueError("--arm-regex must define a named (?P<arm>...) group")
    return pattern


def derive_arm(job_name: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(job_name)
    if match is not None:
        return match.group("arm")
    return job_name


def _union_by_dump(items: list[Any]) -> list[Any]:
    seen: set[str] = set()
    merged: list[Any] = []
    for item in items:
        key = json.dumps(item, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


def build_merged_config(jobs: list[Path], name: str) -> dict[str, Any]:
    configs = [_read_json(job / "config.json") for job in jobs]
    configs = [cfg for cfg in configs if cfg is not None]
    if not configs:
        return {"job_name": name}
    merged = json.loads(json.dumps(configs[0]))
    merged["job_name"] = name
    for key in ("agents", "datasets", "tasks"):
        pooled: list[Any] = []
        for cfg in configs:
            block = cfg.get(key)
            if isinstance(block, list):
                pooled.extend(block)
        if pooled:
            merged[key] = _union_by_dump(pooled)
    return merged


def _restrict_config(config: dict[str, Any], trials: list[Path]) -> None:
    configs = []
    for trial in trials:
        trial_config = _read_json(trial / "config.json") or {}
        result_config = (_read_json(trial / "result.json") or {}).get("config")
        if isinstance(result_config, dict):
            trial_config = {**result_config, **trial_config}
        configs.append(trial_config)
    config["datasets"] = []
    for plural, singular in (("tasks", "task"), ("agents", "agent")):
        config[plural] = _union_by_dump(
            [cfg[singular] for cfg in configs if isinstance(cfg.get(singular), dict)]
        )


def _trial_stat_parts(doc: dict[str, Any]) -> tuple[str, str | None, str]:
    info = doc.get("agent_info") if isinstance(doc.get("agent_info"), dict) else {}
    model_info = info.get("model_info") if isinstance(info, dict) else {}
    agent = info.get("name") if isinstance(info, dict) else None
    model = model_info.get("name") if isinstance(model_info, dict) else None
    dataset = doc.get("source") or "adhoc"
    return str(agent or "unknown"), model, str(dataset)


def _trial_tokens(doc: dict[str, Any]) -> tuple[int | None, int | None, int | None, float | None]:
    contexts: list[dict[str, Any]] = []
    agent_result = doc.get("agent_result")
    if isinstance(agent_result, dict):
        contexts = [agent_result]
    else:
        for step in doc.get("step_results") or []:
            inner = (step or {}).get("agent_result")
            if isinstance(inner, dict):
                contexts.append(inner)
    totals: list[float | None] = [None, None, None, None]
    keys = ("n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd")
    for ctx in contexts:
        for idx, key in enumerate(keys):
            value = ctx.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            totals[idx] = (totals[idx] or 0) + value
    out: list[int | None] = [None if v is None else int(v) for v in totals[:3]]
    cost = totals[3]
    return out[0], out[1], out[2], cost


def build_merged_result(
    name: str, trials: list[tuple[str, dict[str, Any]]]
) -> dict[str, Any]:
    evals: dict[str, dict[str, Any]] = {}
    n_errors = n_cancelled = n_stopped_by_watch = 0
    tokens: list[float] = [0.0, 0.0, 0.0, 0.0]
    have_tokens = [False, False, False, False]
    started: list[str] = []
    finished: list[str] = []
    for trial_name, doc in trials:
        agent, model, dataset = _trial_stat_parts(doc)
        key = f"{agent}__{model}__{dataset}" if model else f"{agent}__{dataset}"
        stats = evals.setdefault(
            key, {"n_trials": 0, "n_errors": 0, "reward_stats": {}, "exception_stats": {}}
        )
        verifier = doc.get("verifier_result") or {}
        rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
        if isinstance(rewards, dict) and rewards:
            stats["n_trials"] += 1
            for rkey, value in rewards.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                stats["reward_stats"].setdefault(rkey, {}).setdefault(
                    str(value), []
                ).append(trial_name)
        exc = doc.get("exception_info")
        if isinstance(exc, dict) and exc.get("exception_type"):
            etype = str(exc["exception_type"])
            stats["exception_stats"].setdefault(etype, []).append(trial_name)
            stats["n_errors"] += 1
            n_errors += 1
            if etype == "CancelledError":
                n_cancelled += 1
            elif etype == "WatchStopCancelledError":
                n_cancelled += 1
                n_stopped_by_watch += 1
        parts = _trial_tokens(doc)
        for idx, value in enumerate(parts):
            if value is not None:
                tokens[idx] += value
                have_tokens[idx] = True
        if isinstance(doc.get("started_at"), str):
            started.append(doc["started_at"])
        if isinstance(doc.get("finished_at"), str):
            finished.append(doc["finished_at"])
    total = len(trials)
    return {
        "id": str(uuid.uuid4()),
        "started_at": min(started) if started else None,
        "updated_at": max(finished) if finished else None,
        "finished_at": max(finished) if finished else None,
        "n_total_trials": total,
        "stats": {
            "n_completed_trials": total,
            "n_errored_trials": n_errors,
            "n_running_trials": 0,
            "n_pending_trials": 0,
            "n_cancelled_trials": n_cancelled,
            "n_stopped_by_watch": n_stopped_by_watch,
            "n_retries": 0,
            "evals": evals,
            "n_input_tokens": int(tokens[0]) if have_tokens[0] else None,
            "n_cache_tokens": int(tokens[1]) if have_tokens[1] else None,
            "n_output_tokens": int(tokens[2]) if have_tokens[2] else None,
            "cost_usd": tokens[3] if have_tokens[3] else None,
        },
        "trial_results": [],
    }


def write_merged_trial(
    trial_dir: Path,
    job_dir: Path,
    dest: Path,
    *,
    arm: str,
    source_job: str,
    trial_name: str,
) -> dict[str, Any]:
    original = _read_json(trial_dir / "result.json")
    if original is None:
        dest.mkdir(parents=True, exist_ok=False)
        for child in sorted(trial_dir.iterdir(), key=lambda p: p.name):
            if child.name == ".evallab-source.json":
                continue
            _link(child, dest / child.name)
        _write_source_record(
            dest, trial=trial_name, source_job=source_job, arm=arm
        )
        _link_run_report(job_dir, trial_dir, dest)
        return {"status": "unreadable", "verdict": None}
    verifier = original.get("verifier_result") or {}
    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    reward = _scored_reward(rewards)
    if reward is not None and (
        not (isinstance(rewards, dict) and _is_native(rewards))
        or _native_needs_overlay(trial_dir, job_dir)
    ):
        verdict = overlay_trial(
            trial_dir,
            job_dir,
            dest,
            reward=reward,
            arm=arm,
            source_job=source_job,
            trial_name=trial_name,
        )
        status = "scored"
    else:
        dest.mkdir(parents=True, exist_ok=False)
        for child in sorted(trial_dir.iterdir(), key=lambda p: p.name):
            if child.name in (
                "result.json",
                "reward-details.json",
                ".evallab-source.json",
            ):
                continue
            _link(child, dest / child.name)
        rewritten = json.loads(json.dumps(original))
        _apply_arm(rewritten, arm)
        (dest / "result.json").write_text(
            json.dumps(rewritten, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        verdict = None
        status = "scored-native" if reward is not None else "unscored"
    _write_source_record(dest, trial=trial_name, source_job=source_job, arm=arm)
    _link_run_report(job_dir, trial_dir, dest)
    return {"status": status, "verdict": verdict}


def build_merged_job(
    dest: Path,
    name: str,
    jobs: list[Path],
    *,
    arm_pattern: re.Pattern[str],
    selected_trials: dict[Path, list[Path]] | None = None,
) -> dict[str, Any]:
    dest.mkdir(parents=True, exist_ok=False)
    counts: dict[str, Any] = {
        "merged_from": [str(job) for job in jobs],
        "trials": 0,
        "scored": 0,
        "native": 0,
        "overlay": 0,
        "unscored": 0,
        "unreadable": 0,
        "integrity_0": [],
        "rules_fired": {},
        "arms": {},
    }
    trial_docs: list[tuple[str, dict[str, Any]]] = []
    taken: set[str] = set()
    for job in sorted(jobs, key=str):
        arm = derive_arm(job.name, arm_pattern)
        trials = selected_trials[job] if selected_trials is not None else _iter_trial_dirs(job)
        for trial_dir in trials:
            trial_name = trial_dir.name
            if trial_name in taken:
                trial_name = f"{job.name}__{trial_dir.name}"
            trial_name = _unique_job_name(trial_name, taken)
            taken.add(trial_name)
            outcome = write_merged_trial(
                trial_dir,
                job,
                dest / trial_name,
                arm=arm,
                source_job=job.name,
                trial_name=trial_name,
            )
            counts["trials"] += 1
            counts["arms"][trial_name] = arm
            status = outcome["status"]
            if status == "unreadable":
                counts["unreadable"] += 1
                continue
            counts["overlay"] += 1
            doc = _read_json(dest / trial_name / "result.json") or {}
            trial_docs.append((trial_name, doc))
            if status == "unscored":
                counts["unscored"] += 1
            else:
                counts["scored"] += 1
                if status == "scored-native":
                    counts["native"] += 1
                verdict = outcome["verdict"]
                if verdict is not None:
                    for rule in verdict["fired"]:
                        counts["rules_fired"][rule] = (
                            counts["rules_fired"].get(rule, 0) + 1
                        )
                    if verdict["integrity"] == 0:
                        counts["integrity_0"].append(trial_name)
    counts["integrity_0"].sort()
    config = build_merged_config(jobs, name)
    if selected_trials is not None:
        _restrict_config(config, [trial for job in jobs for trial in selected_trials[job]])
    (dest / "config.json").write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (dest / "result.json").write_text(
        json.dumps(build_merged_result(name, trial_docs), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return counts

def build_viewer_root(
    job_dirs: list[Path],
    out: Path,
    *,
    merges: list[tuple[str, str]] | None = None,
    arm_pattern: re.Pattern[str] | None = None,
    health_manifest: Path | None = None,
    tags: list[str] | None = None,
    variant_records_dirs: list[Path] | None = None,
) -> dict[str, Any]:
    selected_trials = None
    selection_report = None
    if health_manifest is not None or tags or variant_records_dirs:
        if health_manifest is None or not tags:
            raise ValueError("--task-health and at least one --tag must be supplied together")
        from evallab.task_health_filter import TaskHealthFilter

        selector = TaskHealthFilter(
            health_manifest, tags, records_dirs=variant_records_dirs or ()
        )
        selected_trials, selection_report = selector.select(
            {job: _iter_trial_dirs(job) for job in job_dirs}
        )
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise FileExistsError(f"viewer root exists and is not empty: {out}")
    pattern = arm_pattern or compile_arm_pattern(None)
    matched: set[str] = set()
    pending_merges: list[tuple[str, list[Path]]] = []
    for name, glob in merges or []:
        selected = [
            job for job in job_dirs if fnmatch.fnmatch(job.name, glob)
        ]
        if not selected:
            raise ValueError(f"--merge {name}={glob}: no job dirs match")
        if selected_trials is not None:
            selected = [job for job in selected if selected_trials[job]]
        if selected:
            pending_merges.append((name, selected))
        matched.update(str(job) for job in selected)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "created_at": _datetime.datetime.now(_datetime.UTC).isoformat(),
        "rule_versions": rule_versions(),
        "arm_regex": pattern.pattern,
        "jobs": {},
        "merged": {},
        "totals": {
            "jobs": 0,
            "trials": 0,
            "scored": 0,
            "native": 0,
            "overlay": 0,
            "unscored": 0,
            "unreadable": 0,
            "integrity_0": 0,
        },
    }
    if selection_report is not None:
        report["task_health"] = selection_report
    taken: set[str] = set()

    def _tally(counts: dict[str, Any]) -> None:
        report["totals"]["jobs"] += 1
        for key in ("trials", "scored", "native", "overlay", "unscored", "unreadable"):
            report["totals"][key] += counts[key]
        report["totals"]["integrity_0"] += len(counts["integrity_0"])

    for job_dir in job_dirs:
        if str(job_dir) in matched:
            continue
        if selected_trials is not None and not selected_trials[job_dir]:
            continue
        name = _unique_job_name(job_dir.name, taken)
        taken.add(name)
        counts = mirror_job(
            job_dir,
            out / name,
            trial_dirs=selected_trials[job_dir] if selected_trials is not None else None,
        )
        report["jobs"][name] = counts
        _tally(counts)
    for name, selected in pending_merges:
        merged_name = _unique_job_name(name, taken)
        taken.add(merged_name)
        counts = build_merged_job(
            out / merged_name, merged_name, selected,
            arm_pattern=pattern, selected_trials=selected_trials,
        )
        report["jobs"][merged_name] = counts
        report["merged"][merged_name] = {
            "sources": sorted(job.name for job in selected),
            "arms": counts["arms"],
        }
        _tally(counts)
    (out / ".evallab-view.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


HARBOR_RUNTIME_REMEDY = (
    "install the locked laminar extra in the interpreter that runs evallab: "
    "`uv sync --locked --extra laminar` (or `uv run --extra laminar evallab ...`); "
    "a `harbor` executable on PATH is not enough because the trial runs in-process"
)


def pinned_harbor_bin_dir() -> Path | None:
    try:
        importlib.metadata.version("harbor")
    except (importlib.metadata.PackageNotFoundError, ValueError):
        return None
    candidate = Path(sys.executable).parent / "harbor"
    return candidate.parent if candidate.is_file() else None


def prepend_pinned_harbor_path(environment: dict[str, str], *, fallback: str | None = None) -> bool:
    pinned = pinned_harbor_bin_dir()
    if pinned is None:
        return False
    current = environment.get("PATH") or fallback or ""
    environment["PATH"] = f"{pinned}{os.pathsep}{current}" if current else str(pinned)
    return True


def harbor_runtime_problem() -> str | None:
    import importlib.util
    import sys

    for module in ("harbor.trial.trial", "harbor.models.trial.config"):
        if module in sys.modules:
            continue
        try:
            if importlib.util.find_spec(module) is None:
                return f"{module} is not importable"
        except (ImportError, ModuleNotFoundError, ValueError) as exc:
            return f"{module}: {type(exc).__name__}: {exc}"
    return None


def require_harbor_runtime(backend: str) -> None:
    problem = harbor_runtime_problem()
    if problem is None:
        return
    raise RuntimeError(
        f"the {backend} backend needs the in-process Harbor runtime ({problem}): "
        f"{HARBOR_RUNTIME_REMEDY}"
    )


def installed_harbor_version() -> tuple[int, ...] | None:
    try:
        version = importlib.metadata.version("harbor")
    except importlib.metadata.PackageNotFoundError:
        version = None
    if version is not None:
        try:
            return tuple(int(p) for p in version.split("+")[0].split(".")[:3])
        except ValueError:
            return None
    which = shutil.which("harbor")
    if which is None:
        return None
    try:
        proc = subprocess.run(
            [which, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            env=harbor_child_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", proc.stdout + proc.stderr)
    if not match:
        return None
    return tuple(int(g) for g in match.groups())


def viewer_command(root: Path, *, port: int, host: str = "127.0.0.1") -> list[str]:
    version = installed_harbor_version()
    base = ["harbor", "view", str(root), "--jobs", "--port", str(port), "--host", host]
    if version is not None and version >= VIEWER_MIN_VERSION:
        return base
    return ["uv", "tool", "run", "--from", VIEWER_PIN, *base]


def _view_command(
    args: argparse.Namespace, root: Path, *, harbor: Any | None = None
) -> int:
    del harbor
    raw_paths: list[Path] = list(getattr(args, "paths", []) or [])
    if not raw_paths:
        print("evallab view: need at least one job dir or results root", file=sys.stderr)
        return 2
    sources = [
        (path if path.is_absolute() else (root / path)).resolve() for path in raw_paths
    ]
    jobs, skipped = discover_jobs([Path(s) for s in sources])
    if not jobs:
        print("evallab view: no job dirs found", file=sys.stderr)
        for note in skipped:
            print(f"  skip: {note}", file=sys.stderr)
        return 2
    try:
        merges = [parse_merge_spec(spec) for spec in (getattr(args, "merge", None) or [])]
        health_manifest = getattr(args, "task_health", None)
        if health_manifest is not None:
            health_manifest = (
                health_manifest
                if health_manifest.is_absolute()
                else (root / health_manifest).resolve()
            )
        from evallab.task_variants import RECORDS_DIRNAME

        variant_records_dirs = [
            path.resolve() if path.is_absolute() else (root / path).resolve()
            for path in (getattr(args, "task_variants", None) or [])
        ]
        canonical_records = root / RECORDS_DIRNAME
        if health_manifest is not None and canonical_records.is_dir():
            variant_records_dirs.insert(0, canonical_records)
        arm_pattern = compile_arm_pattern(getattr(args, "arm_regex", None))
    except ValueError as exc:
        print(f"evallab view: {exc}", file=sys.stderr)
        return 2
    out = getattr(args, "out", None)
    if out is None:
        dest = Path(tempfile.mkdtemp(prefix="evallab-view-"))
    else:
        dest = out if out.is_absolute() else (root / out).resolve()
    try:
        report = build_viewer_root(
            jobs, dest, merges=merges, arm_pattern=arm_pattern,
            health_manifest=health_manifest, tags=getattr(args, "tag", None),
            variant_records_dirs=variant_records_dirs,
        )
    except (OSError, ValueError) as exc:
        print(f"evallab view: {exc}", file=sys.stderr)
        return 2
    totals = report["totals"]
    print(f"viewer root: {dest}")
    print(
        f"jobs={totals['jobs']} trials={totals['trials']} scored={totals['scored']} "
        f"(native={totals['native']} overlay={totals['overlay']}) "
        f"unscored={totals['unscored']} integrity_0={totals['integrity_0']}"
    )
    if "task_health" in report:
        selection = report["task_health"]
        selected_counts = selection["totals"]
        print(
            f"task tags: {' AND '.join(selection['tags'])}; "
            f"included={selected_counts['included']}/{selected_counts['trials']} "
            f"tag_mismatch={selected_counts['tag_mismatch']} "
            f"unbound={selected_counts['unbound']} "
            f"digest_mismatch={selected_counts['digest_mismatch']} "
            f"ambiguous={selected_counts['ambiguous']}"
        )
    for merged_name, merged in report.get("merged", {}).items():
        print(f"  merged {merged_name}: {len(merged['sources'])} jobs")
    for note in skipped:
        print(f"  skip: {note}")
    if getattr(args, "no_launch", False) or totals["trials"] == 0:
        return 0
    port = int(getattr(args, "port", 8080) or 8080)
    host = getattr(args, "host", None) or "127.0.0.1"
    cmd = viewer_command(dest, port=port, host=host)
    url = f"http://{host}:{port}"
    print(f"serving: {url}")
    print(f"running: {' '.join(cmd)}")
    try:
        completed = subprocess.run(cmd, env=harbor_child_environment())
    except KeyboardInterrupt:
        return 0
    return completed.returncode


