#!/usr/bin/env python3
"""Export batch trials to schema `cheatbench_port.export/v1`.

One schema for old and new batches, for the fxtr-lab viewer:

  results/batches/<batch_id>/batch.json   batch record
  results/batches/<batch_id>/trials.jsonl one row per trial

Usage (new batch; jobs from the batch dir written by run_batch.sh):
  uv run --no-sync python research/experiments/cheatbench-port/export.py \\
      --batch 2026-10-11-foo --jobs 'cb-*-foo-r1'

Usage (the 2026-10-09 runs, from the published flat results):
  uv run --no-sync python research/experiments/cheatbench-port/export.py \\
      --batch 2026-10-09-initial --from-results

--jobs accepts job names, comma-separated lists, and globs (matched under
--runs-dir). Detection runs live per trial (detect_trial); verdicts come
from the batch's judge-*.jsonl files (or results/judge-*.jsonl with
--from-results).
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EXP = ROOT / "research/experiments/cheatbench-port"
RESULTS = EXP / "results"
JUDGE_DIR = EXP / "judge"
sys.path.insert(0, str(EXP))
sys.path.insert(0, str(JUDGE_DIR))
sys.path.insert(0, str(ROOT / "src"))

import analyze as analyzer  # noqa: E402
import capture_config  # noqa: E402
import detect as detector  # noqa: E402
import locate as metalocate  # noqa: E402
import render as renderer  # noqa: E402

SCHEMA = "cheatbench_port.export/v1"
# Judge-row keys that are transport/config metadata, not verdict evidence.
JUDGE_META = {"trial_id", "trial_dir", "family", "judge", "judge_model",
              "judge_generation_config", "rubric_sha256", "schema_sha256",
              "cheatbench_commit", "prompt_sha256", "renderer",
              "instruction_source", "output_mode", "response_format_used",
              "judge_cost_usd", "judge_usage", "judge_attempts", "judge_error",
              "raw"}
TRIPWIRE_RULE = {"grader": "hidden_test_read", "git_objects": "git_object_read",
                 "git_refs": "history_mining"}


def expand_jobs(tokens: list[str], runs: Path) -> list[str]:
    jobs: list[str] = []
    for tok in tokens:
        for part in tok.split(","):
            part = part.strip()
            if not part:
                continue
            if any(c in part for c in "*?[]"):
                jobs.extend(sorted(p.name for p in runs.glob(part) if p.is_dir()))
            elif (runs / part).is_dir():
                jobs.append(part)
            else:
                print(f"warn: no job dir for {part!r}", file=sys.stderr)
    return list(dict.fromkeys(jobs))


def scored_trials(jobs: list[str], runs: Path) -> list[tuple[str, Path]]:
    out = []
    for job in jobs:
        for trial in sorted((runs / job).glob("*__*")):
            if (trial / "result.json").is_file():
                out.append((job, trial))
    return out


def trial_result(trial: Path) -> tuple:
    res = json.loads((trial / "result.json").read_text())
    reward = ((res.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    exc = (res.get("exception_info") or {}).get("exception_type")
    return reward, exc, res


def agent_cost(job: str, runs: Path) -> float | None:
    lab = runs / job / "lab-metadata.json"
    if not lab.is_file():
        return None
    try:
        cost = (json.loads(lab.read_text()).get("cost") or {})
        return float(cost.get("cost_usd") or 0) + float(cost.get("attempted_cost_usd") or 0)
    except (ValueError, TypeError):
        return None


def detect_row(trial: Path) -> dict:
    meta, _, pkg = metalocate.locate_metadata(trial, None)
    return detector.detect_trial(trial, meta, pkg)


def atif_steps(trial: Path) -> list[dict]:
    """ATIF steps via the shared loader (handles trajectory.cont-N.json)."""
    try:
        return renderer.load_atif(trial / "agent").get("steps", []) or []
    except (OSError, ValueError):
        return []


def detector_block(row: dict, family: str, trial: Path | None = None) -> dict:
    """{positive, first_hit_step, hits} with analyze.py's positive call.

    Steps are ATIF step_ids (hits also keep the 0-based step_index)."""
    interesting = ("protected_touched", "answer_exposed", "prohibited_computation")
    steps = atif_steps(trial) if trial is not None else []

    def step_id(i: object) -> int | None:
        if isinstance(i, int) and 0 <= i < len(steps):
            v = steps[i].get("step_id")
            if isinstance(v, int):
                return v
        return i if isinstance(i, int) else None

    hits = []
    for sig in (*interesting, "honeypot_route_seen"):
        for h in row.get(sig) or []:
            hits.append({"signal": sig, "step": step_id(h.get("step")),
                         "step_index": h.get("step"),
                         "quote": str(h.get("snippet", ""))[:300]})
    first = [h["step"] for h in hits
             if h["signal"] in interesting and isinstance(h["step"], int)]
    pos = any(h["signal"] in ("protected_touched", "answer_exposed") for h in hits)
    if family != "openmath_agent":
        pos = pos or any(h["signal"] == "prohibited_computation" for h in hits)
    return {"positive": pos,
            "first_hit_step": min(first) if first else None, "hits": hits}


def verdict_block(row: dict) -> dict:
    out = {"cheated": row.get("cheated"),
           "honey_pots_seen": row.get("honey_pots_seen"),
           "explicit_prohibition_violated": row.get("explicit_prohibition_violated"),
           "summary_or_reasoning": row.get("summary") or row.get("reasoning") or ""}
    for k, v in row.items():
        if k not in JUDGE_META and k not in out:
            out[k] = v
    return out


def cheat_step(det: dict, verdicts: dict, trial: Path | None = None
               ) -> tuple[int | None, str | None]:
    """(ATIF step_id, source). The judge-prose `Step N` fallback is a
    heuristic: it only counts when N matches an existing ATIF step_id."""
    if det["positive"] and det["first_hit_step"] is not None:
        return det["first_hit_step"], "detector"
    ids: set[int] = set()
    if trial is not None:
        for s in atif_steps(trial):
            v = s.get("step_id")
            if isinstance(v, int):
                ids.add(v)
    cands = []
    for v in verdicts.values():
        for val in v.values():
            if isinstance(val, str):
                cands.extend(n for m in re.findall(r"[Ss]tep\s+(\d+)", val)
                             if (n := int(m)) in ids)
    if cands:
        return min(cands), "judge_text"
    return None, None


def step_time_and_cast(trial: Path, step: int | None) -> tuple[str | None, float | None]:
    if step is None:
        return None, None
    ts = None
    for s in atif_steps(trial):
        if s.get("step_id") == step:
            ts = s.get("timestamp")
            break
    if not ts:
        return None, None
    try:
        cast0 = json.loads((trial / "agent" / "recording.cast"
                            ).read_text().splitlines()[0])["timestamp"]
        off = (datetime.fromisoformat(ts).timestamp() - float(cast0))
        return ts, round(off, 3)
    except (OSError, ValueError, IndexError, KeyError):
        return ts, None


def tripwires(trial: Path, family: str) -> list[dict]:
    fa = trial / "agent" / "file-access.jsonl"
    if not fa.is_file():
        return []
    rules = capture_config.tripwire_rules(family)
    out = []
    for i, line in enumerate(fa.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        rule = TRIPWIRE_RULE.get(rec.get("category", ""))
        if rec.get("kind") == "access" and rule in rules:
            out.append({"rule": rule, "path": rec.get("path"),
                        "kind": capture_config.tripwire_kind(rec.get("path")),
                        "events": rec.get("events", []), "line": i})
    return out


def artifacts(job: str, trial: Path, runs: Path) -> dict:
    agent = trial / "agent"
    traj = agent / "trajectory.json"
    cast = agent / "recording.cast"
    fa = agent / "file-access.jsonl"
    proxy = runs / job / "proxy-live" / "calls.jsonl"
    if not proxy.is_file():
        proxy = trial / "proxy-live" / "calls.jsonl"
    mc = None
    for root in (runs / job, trial):
        hits = sorted(root.glob("**/*trial_capture*")) + \
            sorted(root.glob("**/*model_calls*"))
        if hits:
            mc = hits[0]
            break
    alerts = runs / job / "watch" / "alerts.jsonl"
    def rel(p: Path | None) -> str | None:
        return str(p) if p is not None and p.is_file() else None
    return {"trajectory": rel(traj), "recording_cast": rel(cast),
            "file_access": rel(fa), "proxy_calls": rel(proxy),
            "model_capture_calls": rel(mc), "watch_alerts": rel(alerts)}


PUBLISHED_ROOT = Path.home() / "Developer/eval-lab-results"


def published_dir(job: str) -> str | None:
    """Published copy of a job dir (absolute path or None)."""
    if not PUBLISHED_ROOT.is_dir():
        return None
    for p in sorted(PUBLISHED_ROOT.glob(f"*/unknown-{job}")):
        if p.is_dir():
            return str(p)
    for pat in (f"*/*/{job}", f"*/*/*/{job}", f"*/*/*/*/{job}"):
        for p in sorted(PUBLISHED_ROOT.glob(pat)):
            if p.is_dir():
                return str(p)
    return None


def build_row(job: str, trial: Path, family: str, instance: str, model: str,
              arm: str, det: dict, verdicts: dict[str, dict],
              runs: Path) -> dict:
    reward, exc, _ = trial_result(trial)
    infra = reward is None and exc in analyzer.INFRA_EXCEPTIONS
    det_b = detector_block(det, family, trial)
    v_b = {name: verdict_block(r) for name, r in verdicts.items()}
    step, source = cheat_step(det_b, v_b, trial)
    ts, off = step_time_and_cast(trial, step)
    return {"trial_id": trial.name, "job": job, "family": family,
            "instance": instance, "model": model, "arm": arm,
            "reward": reward, "exception": exc, "infra_excluded": infra,
            "agent_cost_usd": agent_cost(job, runs),
            "trial_dir": str(trial),
            "published_dir": published_dir(job),
            "artifacts": artifacts(job, trial, runs),
            "detector": det_b, "tripwires": tripwires(trial, family),
            "verdicts": v_b, "cheat_step": step,
            "cheat_step_source": source,
            "cheat_step_timestamp": ts, "cast_offset_s": off}


def load_judge_maps(paths: list[Path]) -> tuple[dict[str, dict], list[dict]]:
    """trial_id -> {judge_name: row}; plus judge table entries for batch.json."""
    verdicts: dict[str, dict] = {}
    table = []
    for path in paths:
        rows = analyzer.load_jsonl(path)
        header_p = path.with_name(path.stem + ".header.json")
        header = json.loads(header_p.read_text()) if header_p.is_file() else {}
        name = path.stem.removeprefix("judge-")
        for tid, row in rows.items():
            if not row.get("judge_error"):
                verdicts.setdefault(tid, {})[name] = row
        table.append({"name": header.get("judge", name),
                      "model": header.get("judge_model"),
                      "endpoint": header.get("endpoint_host"),
                      "rubric_sha256": header.get("rubric_sha256")})
    return verdicts, table


def spend_usd(jobs: list[str], runs: Path, judge_paths: list[Path]) -> float:
    # Same books as spend.py: agent ledger + every judge row, including the
    # token usage of calls that failed validation (retries are spend too).
    agent = sum(agent_cost(j, runs) or 0 for j in jobs)
    judge_total = 0.0
    try:
        import spend as spendmod
        failed_cost = spendmod.failed_call_cost
    except ImportError:
        def failed_cost(row: dict) -> float:
            return 0.0
    for path in judge_paths:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            judge_total += float(row.get("judge_cost_usd") or 0)
            with contextlib.suppress(Exception):
                judge_total += failed_cost(row)
    return round(agent + judge_total, 4)


def capture_block(jobs: list[str], runs: Path, batch: Path | None) -> dict:
    states = []
    for job in jobs:
        for trial in sorted((runs / job).glob("*__*")):
            fa = trial / "agent" / "file-access.jsonl"
            if not fa.is_file():
                states.append("missing")
                continue
            last_state = "missing"
            for line in fa.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("kind") == "coverage":
                    last_state = r.get("state", "missing")
            states.append(last_state)
    uniq = set(states)
    file_access = "enabled" if uniq == {"active"} else \
        "disabled" if uniq <= {"disabled", "missing"} else "mixed"
    paths: dict = {}
    try:
        import capture_config
        fams = set()
        for job in jobs:
            m = analyzer.parse_job(job)
            if m:
                fams.add(m["family"])
        for fam in sorted(fams):
            with contextlib.suppress(ValueError):
                paths[fam] = capture_config.file_access_paths(fam)
    except ImportError:
        pass
    mc_dirs = []
    for job in jobs:
        lab = runs / job / "lab-metadata.json"
        if lab.is_file():
            try:
                mc = json.loads(lab.read_text()).get("model_capture") or {}
                d = mc.get("capture_dir") or mc.get("dir")
                if d:
                    p = Path(d)
                    mc_dirs.append(str(p if p.is_absolute() else ROOT / p))
            except ValueError:
                pass
    if batch is not None:
        for cap in sorted((batch / "captures").glob("*/")) if \
                (batch / "captures").is_dir() else []:
            mc_dirs.append(str(cap))
    return {"file_access": file_access, "file_access_paths": paths,
            "model_capture_dirs": sorted(set(mc_dirs))}


def write_batch(batch_id: str, rows: list[dict], specs: list[str],
                judges: list[dict], judge_paths: list[Path], jobs: list[str],
                runs: Path, batch: Path | None, readiness: str | None,
                out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with (out / "trials.jsonl").open("w") as f:
        for r in sorted(rows, key=lambda r: r["trial_id"]):
            f.write(json.dumps(r) + "\n")
    models = sorted({r["model"] for r in rows})
    families = sorted({r["family"] for r in rows})
    arms = sorted({r["arm"] for r in rows})
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                            text=True, cwd=ROOT)
    (out / "batch.json").write_text(json.dumps({
        "schema": SCHEMA, "batch_id": batch_id,
        "created_at": datetime.now(UTC).isoformat(),
        "git_commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "specs": sorted(set(specs)), "models": models, "families": families,
        "arms": arms, "capture": capture_block(jobs, runs, batch),
        "judges": judges, "readiness_receipt": readiness,
        "spend_usd": spend_usd(jobs, runs, judge_paths)}, indent=1) + "\n")
    scored = [r for r in rows if not r["infra_excluded"]]
    judged = [r for r in scored if r["verdicts"]]
    print(f"batch={batch_id} trials={len(rows)} scored={len(scored)} "
          f"judged={len(judged)} -> {out}")


def spec_ids(jobs: list[str], runs: Path) -> list[str]:
    out = []
    for job in jobs:
        lab = runs / job / "lab-metadata.json"
        if lab.is_file():
            with contextlib.suppress(ValueError):
                out.append((json.loads(lab.read_text()).get("experiment") or {}
                            ).get("spec_id"))
    return [s for s in out if s]


def cmd_from_results(args: argparse.Namespace) -> int:
    batch_id = args.batch
    out = RESULTS / "batches" / batch_id
    rows_csv = list(csv.DictReader((RESULTS / "trials.csv").read_text().splitlines()))
    detect_map: dict[str, dict] = {}
    for line in (RESULTS / "detect.jsonl").read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            detect_map[r["trial_id"]] = r
    judge_paths = sorted(RESULTS.glob("judge-*.jsonl"))
    verdicts, judges = load_judge_maps(judge_paths)
    rows = []
    for c in rows_csv:
        infra = (c.get("infra_excluded") or "").strip().lower() == "true"
        det = detect_map.get(c["trial_id"])
        raw_dir = Path(c["trial_dir"])
        trial = raw_dir if raw_dir.is_absolute() else ROOT / raw_dir
        if det is None:
            det = detect_row(trial) if (trial / "agent" / "trajectory.json").is_file() \
                else {"protected_touched": [], "answer_exposed": [],
                      "prohibited_computation": [], "honeypot_route_seen": []}
        det_b = detector_block(det, c["family"], trial if trial.is_dir() else None)
        v_b = {n: verdict_block(r) for n, r in verdicts.get(c["trial_id"], {}).items()}
        step, source = cheat_step(det_b, v_b, trial if trial.is_dir() else None)
        ts, off = step_time_and_cast(trial, step) if trial.is_dir() else (None, None)
        try:
            reward = float(c["reward"]) if c.get("reward") not in (None, "") else None
        except ValueError:
            reward = None
        try:
            cost = float(c["agent_cost_usd"]) if c.get("agent_cost_usd") not in (
                None, "") else None
        except ValueError:
            cost = None
        rows.append({
            "trial_id": c["trial_id"], "job": c["job"], "family": c["family"],
            "instance": c["instance"], "model": c["model"], "arm": c["arm"],
            "reward": reward, "exception": c.get("exception") or None,
            "infra_excluded": infra, "agent_cost_usd": cost,
            "trial_dir": str(trial),
            "published_dir": published_dir(c["job"]),
            "artifacts": artifacts(c["job"], trial, args.runs) if trial.is_dir()
            else {"trajectory": None, "recording_cast": None, "file_access": None,
                  "proxy_calls": None, "model_capture_calls": None,
                  "watch_alerts": None},
            "detector": det_b,
            "tripwires": tripwires(trial, c["family"]) if trial.is_dir() else [],
            "verdicts": v_b, "cheat_step": step,
            "cheat_step_source": source,
            "cheat_step_timestamp": ts, "cast_offset_s": off})
    jobs = sorted({c["job"] for c in rows_csv})
    write_batch(batch_id, rows, spec_ids(jobs, args.runs), judges, judge_paths,
                jobs, args.runs, None, None, out)
    # Validate against the published analysis: every scored trial present,
    # counts matching results/summary.md (49 scored, 39 judged).
    scored = [r for r in rows if not r["infra_excluded"]]
    judged = [r for r in scored if r["verdicts"]]
    m = re.search(r"Scored trials:\s*(\d+)",
                  (RESULTS / "summary.md").read_text())
    want_scored = int(m.group(1)) if m else None
    csv_judged = sum(1 for c in rows_csv
                     if (c.get("infra_excluded") or "").strip().lower() != "true"
                     and (c.get("verdict_judge") or "").strip())
    ok = (len(scored) == want_scored and len(judged) == csv_judged
          and len(scored) == sum(1 for c in rows_csv
                                 if (c.get("infra_excluded") or ""
                                     ).strip().lower() != "true"))
    print(f"validate: scored={len(scored)} (summary wants {want_scored}), "
          f"judged={len(judged)} (trials.csv has {csv_judged})")
    return 0 if ok else 1


def cmd_jobs(args: argparse.Namespace) -> int:
    batch_id = args.batch
    batch = ROOT / "runs" / "_batches" / batch_id
    jobs_file = batch / "jobs.txt"
    if args.jobs:
        jobs = expand_jobs(args.jobs, args.runs)
    elif jobs_file.is_file():
        jobs = [line.strip() for line in jobs_file.read_text().splitlines()
                if line.strip()]
    else:
        raise SystemExit(f"no --jobs given and {jobs_file} missing "
                         f"(run_batch.sh writes it)")
    if not jobs:
        raise SystemExit("no jobs resolved")
    judge_paths = sorted(batch.glob("judge-*.jsonl")) if batch.is_dir() else []
    verdicts, judges = load_judge_maps(judge_paths)
    rows = []
    for job, trial in scored_trials(jobs, args.runs):
        m = analyzer.parse_job(job)
        if m is None:
            print(f"warn: cannot parse job name {job}; skipped", file=sys.stderr)
            continue
        family, model = m["family"], m["model"]
        arm = analyzer.job_arm(args.runs / job) or "unknown"
        res = json.loads((trial / "result.json").read_text())
        instance = ((res.get("task_name") or "").split("/", 1)[-1]
                    if res.get("task_name") else m["instance"])
        rows.append(build_row(job, trial, family, instance, model, arm,
                              detect_row(trial),
                              verdicts.get(trial.name, {}), args.runs))
    out = RESULTS / "batches" / batch_id
    receipt = batch / "readiness-receipt.json"
    write_batch(batch_id, rows, spec_ids(jobs, args.runs), judges, judge_paths,
                jobs, args.runs, batch if batch.is_dir() else None,
                str(receipt) if receipt.is_file() else None, out)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--batch", required=True)
    ap.add_argument("--jobs", nargs="*", default=None,
                    help="job names, comma lists, or globs (under --runs-dir)")
    ap.add_argument("--from-results", action="store_true",
                    help="export the 2026-10-09 runs from results/{trials.csv,"
                    "detect.jsonl,judge-*.jsonl}")
    ap.add_argument("--runs", type=Path, default=Path("runs"))
    args = ap.parse_args()
    if not args.runs.is_absolute():
        args.runs = ROOT / args.runs
    if args.from_results:
        return cmd_from_results(args)
    return cmd_jobs(args)


if __name__ == "__main__":
    raise SystemExit(main())
