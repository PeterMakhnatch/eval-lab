"""Copied-pass benchmark from frozen blind labels, and the current counts verdict scored against it.

A labelled pass is a run where either rater filled `pass_copied`. The benchmark label is the two
raters' agreed value (`disagree` rows are kept but not scored). Each trial is re-processed with the
checked-out `evallab process-job` on a private copy, so published results are never touched.

    uv run python research/explorations/trace-lab/review/copy_benchmark.py \
        --results ~/Developer/eval-lab-results --work /private/tmp/copybench \
        --out research/explorations/trace-lab/review/copy_benchmark.jsonl
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

HAR128 = Path(__file__).resolve().parents[1] / "har128"
LABEL_SETS = {
    "labels_har116": HAR128 / "labels_har116",
    "labels_g2_a1": HAR128 / "labels_g2_a1",
    "labels_g2_r2": HAR128 / "labels_g2_r2",
    "labels_g2_tail": HAR128 / "labels_g2_tail",
    "g6": HAR128 / "g6" / "labels",
}
G6_ARM_MAP = HAR128 / "g6" / "arm_map.json"


def labelled_passes(results: Path) -> list[dict]:
    arm_map = json.loads(G6_ARM_MAP.read_text())
    rows = []
    for name, labels in LABEL_SETS.items():
        for path in sorted((labels / "rater_a").glob("*.json")):
            a = json.loads(path.read_text())
            b = json.loads((labels / "rater_b" / path.name).read_text())
            if a["pass_copied"] is None and b["pass_copied"] is None:
                continue
            trial = arm_map[path.stem]["trial"] if name == "g6" else path.stem
            (trial_dir,) = results.glob(f"*/*/{trial}")
            label = a["pass_copied"] if a["pass_copied"] == b["pass_copied"] else "disagree"
            rows.append(
                {
                    "set": name,
                    "id": path.stem,
                    "trial": trial,
                    "trial_dir": str(trial_dir),
                    "label": label,
                }
            )
    return rows


def verdict_now(trial_dir: Path, work: Path) -> dict:
    """Re-run process-job on a one-trial copy of the job; returns counts verdict and reasons."""
    job = trial_dir.parent
    dst = work / trial_dir.name
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for f in job.iterdir():
        if f.is_file():
            shutil.copy2(f, dst / f.name)
    shutil.copytree(trial_dir, dst / trial_dir.name)
    out = dst / "processed_now"
    cmd = [
        sys.executable,
        "-W",
        "ignore",
        "-m",
        "evallab.cli",
        "process-job",
        "--no-ingest",
        "--no-publish",
    ]
    subprocess.run(
        [*cmd, "--output-dir", str(out), str(dst)], check=True, capture_output=True, text=True
    )
    counts = json.loads(next(out.glob("trial-*.json")).read_text())["counts"]
    return {"verdict": counts["verdict"], "reasons": counts.get("reasons") or []}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    rows = labelled_passes(args.results.expanduser())
    hits = scored = 0
    for row in rows:
        now = verdict_now(Path(row["trial_dir"]), args.work)
        row["counts_now"] = now
        published = next(
            Path(row["trial_dir"]).parent.glob(f"processed/trial-{row['trial']}.json"), None
        )
        row["counts_published"] = (
            json.loads(published.read_text())["counts"]["verdict"] if published else None
        )
        if row["label"] in (True, False):
            scored += 1
            flagged = now["verdict"] == "excluded" and "copied_fix" in now["reasons"]
            hits += flagged == row["label"]
    args.out.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    print(f"{len(rows)} labelled passes; counts agrees with the raters on {hits}/{scored}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
