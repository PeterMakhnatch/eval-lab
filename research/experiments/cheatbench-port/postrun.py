#!/usr/bin/env python3
"""Post-run pipeline for one CheatBench-port batch.

detect.jsonl for the batch, optional judge (per family, judge.py with
--family), analyze.py, spend.py, then export.py.

Usage:
  uv run --no-sync python research/experiments/cheatbench-port/postrun.py \\
      --batch 2026-10-11-foo [--judge zai/glm-5.3-flash] [--jobs 'cb-*-foo-*']

--judge is paid (judge tokens; run under `keys run`). Without it, detection,
analysis, spend and export still run; trials simply carry no verdicts yet.
Jobs default to runs/_batches/<batch>/jobs.txt (written by run_batch.sh).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EXP = ROOT / "research/experiments/cheatbench-port"
JUDGE_DIR = EXP / "judge"
sys.path.insert(0, str(EXP))
sys.path.insert(0, str(JUDGE_DIR))
sys.path.insert(0, str(ROOT / "src"))

import analyze as analyzer  # noqa: E402
import detect as detector  # noqa: E402
import locate as metalocate  # noqa: E402

UV = ["uv", "run", "--no-sync"]


def sh(cmd: list[str], *, tail: int = 3) -> None:
    print(f"+ {' '.join(cmd)}", flush=True)
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    out = (p.stdout + p.stderr).strip().splitlines()
    for line in out[-tail:]:
        print(f"  {line}")
    if p.returncode != 0:
        raise SystemExit(f"{cmd[2] if cmd[:2] == UV else cmd[0]} failed (rc={p.returncode})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--batch", required=True)
    ap.add_argument("--judge", default=None,
                    help="paid judge route, e.g. zai/glm-5.3-flash")
    ap.add_argument("--jobs", nargs="*", default=None,
                    help="override jobs (names/globs); default: batch jobs.txt")
    args = ap.parse_args()

    batch = ROOT / "runs" / "_batches" / args.batch
    if args.jobs:
        from export import expand_jobs
        jobs = expand_jobs(args.jobs, ROOT / "runs")
    else:
        jobs_file = batch / "jobs.txt"
        if not jobs_file.is_file():
            raise SystemExit(f"{jobs_file} missing; pass --jobs")
        jobs = [line.strip() for line in jobs_file.read_text().splitlines()
                if line.strip()]
    if not jobs:
        raise SystemExit("no jobs resolved")

    trials: list[tuple[str, Path, str]] = []  # (job, trial_dir, family)
    for job in jobs:
        for trial in sorted((ROOT / "runs" / job).glob("*__*")):
            if not (trial / "result.json").is_file():
                continue
            m = analyzer.parse_job(job)
            if m is None:
                print(f"warn: cannot parse job name {job}; skipped")
                continue
            trials.append((job, trial, m["family"]))
    if not trials:
        raise SystemExit("no scored trials under the batch jobs")
    print(f"batch={args.batch} jobs={len(jobs)} trials={len(trials)}")

    # 1. detect.jsonl for the batch (exit 0 always; findings are data).
    batch.mkdir(parents=True, exist_ok=True)
    detect_path = batch / "detect.jsonl"
    n_sig = 0
    with detect_path.open("w") as f:
        for job, trial, _ in trials:
            meta, _, pkg = metalocate.locate_metadata(trial, None)
            row = detector.detect_trial(trial, meta, pkg)
            row["job"] = job
            f.write(json.dumps(row) + "\n")
            n_sig += 1 if any(row.get("signals", {}).values()) else 0
    print(f"detect: {len(trials)} trials, {n_sig} with-signal -> {detect_path}")

    # 2. Optional judge, one judge.py call per family (each family has its
    # own rubric schema); resume-safe, already-judged trials are skipped.
    judge_paths: list[Path] = []
    if args.judge:
        tag = args.judge.replace("/", "-").replace(".", "-")
        by_fam: dict[str, list[str]] = {}
        for _, trial, fam in trials:
            try:
                rew = (json.loads((trial / "result.json").read_text()
                                  ).get("verifier_result") or {}
                       ).get("rewards", {}).get("reward")
            except ValueError:
                rew = None
            if rew is not None:  # only verifier-scored trials
                by_fam.setdefault(fam, []).append(str(trial.relative_to(ROOT)))
        for fam, dirs in sorted(by_fam.items()):
            out = batch / f"judge-{tag}-{fam}.jsonl"
            judge_paths.append(out)
            sh([*UV, "python", str((JUDGE_DIR / "judge.py").relative_to(ROOT)),
                *dirs, "--family", fam, "--judge", args.judge,
                "--out", str(out.relative_to(ROOT)), "--workers", "4"], tail=4)
    else:
        judge_paths = sorted(batch.glob("judge-*.jsonl"))
        print("judge: skipped (no --judge); reusing "
              f"{len(judge_paths)} existing batch judge file(s)")

    # 3. analyze.py over the batch's jobs with the batch's detect (+ judge) outputs.
    analysis = batch / "analysis"
    cmd = [*UV, "python", str((EXP / "analyze.py").relative_to(ROOT)),
           "--runs", "runs", "--detect", str(detect_path.relative_to(ROOT))]
    for jp in judge_paths:
        cmd += ["--judge", str(jp.relative_to(ROOT))]
    cmd += ["--out", str(analysis.relative_to(ROOT)), "--jobs", *jobs]
    sh(cmd, tail=30)

    # 4. spend.py for the batch's jobs plus the batch's judge calls.
    spend_out = batch / "spend.txt"
    cmd = [*UV, "python", str((EXP / "spend.py").relative_to(ROOT)),
           "--runs", "runs", "--jobs", *jobs]
    for jp in judge_paths:
        cmd += ["--judge-glob", str(jp)]
    print(f"+ {' '.join(cmd)} > {spend_out}", flush=True)
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    spend_out.write_text(p.stdout + p.stderr)
    print(spend_out.read_text().strip())
    if p.returncode != 0:
        raise SystemExit(f"spend.py failed (rc={p.returncode})")

    # 5. export.py (one schema for old and new batches).
    sh([*UV, "python", str((EXP / "export.py").relative_to(ROOT)),
        "--batch", args.batch, "--jobs", *jobs], tail=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
