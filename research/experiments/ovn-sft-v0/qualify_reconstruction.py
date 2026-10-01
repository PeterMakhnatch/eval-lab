#!/usr/bin/env python3
"""Qualify ATIF reconstruction against captured bodies before admitting
historical passes to G3 (HAR-127; Research-Harbor ruling 04:45Z).

The question: does ``sft_terminus --per-turn-stride 1`` rebuild every call a
trial made, byte for byte, from its ATIF trajectory alone? Answered on G2
trials whose calls were captured by ``evallab capture serve``:

1. ``g3_capture.split_capture`` links the capture to every job's trials
   (route token) and writes each trial's calls to its own file. Pass every
   job that used the capture: a call no job claims is ``unassigned``;
2. every trial of the jobs (passes and fails: ``--reward-threshold 0``,
   whole trajectories, no cut) is exported at stride 1;
3. ``fidelity.py --require-capture --gate-target`` checks every row against
   its own trial's delivered calls (status 200, no error, not truncated):
   history and target byte-identical, prompt and completion tokens exact,
   and the matched call's ``seq`` recorded;
4. per trial, ``g3_capture.witness``: rows, identical rows, one call per row,
   delivered calls no row stands at, undelivered calls, link ambiguity.

A trial is **complete** when every row is identical to its own call, no call
backs two rows, every delivered call is reproduced by a row, and its link is
not ambiguous. ``admit_reconstructed`` needs at least ``MIN_TRIALS`` complete
trials, no failing row anywhere and no unassigned call. ``qualification.json``
records the verdict, the coverage, the producer digests (exporter, checker,
linker, tokenizer files, library versions; ``freeze_sft.py`` refuses
``reconstructed_validated`` trials when they differ) and the sha256 of every
input and output.

    uv run python research/experiments/ovn-sft-v0/qualify_reconstruction.py \\
        --capture-dir CAPTURE_DIR --job JOB_DIR ... --tokenizer TOKDIR --out OUT
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from g3_capture import check_fidelity, producer, sha256, split_capture, witness  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SPLIT = ROOT / "research/experiments/har81-mimo-sft/split.json"
MIN_TRIALS = 3


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--job", type=Path, action="append", required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True)

    capture = split_capture(args.capture_dir, args.job, args.out)

    export = args.out / "export"
    command = [sys.executable, "-m", "evallab.sft_terminus", "export"]
    command += ["--split-manifest", str(SPLIT), "--reward-threshold", "0"]
    command += ["--per-turn-stride", "1", "--out", str(export)]
    for job in args.job:
        command += ["--root", f"{job.name}={job}"]
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f"export failed: {done.stdout}{done.stderr}")
    manifest = json.loads((export / "manifest.json").read_text())
    checked = check_fidelity(export, args.tokenizer, capture, require_capture=True)

    trial_of = {c["conversation_id"]: Path(c["trial"]).name for c in manifest["conversations"]}
    results_by_trial = defaultdict(list)
    for info, result in zip(manifest["rows"], checked["rows"], strict=True):
        results_by_trial[trial_of[info["conversation_id"]]].append(result)
    excluded = {
        Path(t["trial"]).name: t.get("reasons") or t.get("disposition")
        for t in manifest["trials"]
        if t.get("disposition") != "selected"
    }
    coverage = []
    for trial in sorted(set(capture["trials"]) | set(results_by_trial)):
        results = results_by_trial.get(trial, [])
        entry = capture["trials"].get(trial)
        cover = witness(results, entry)
        failing = [
            {
                "row_id": r["row_id"],
                "capture": r.get("capture", "missing"),
                "prompt_ok": r["prompt_ok"],
                "target_delta": r["target_delta"],
            }
            for r in results
            if not (r.get("capture") == "identical" and r["prompt_ok"] and r["target_delta"] == 0)
        ]
        complete = (
            entry is not None
            and cover["rows"] > 0
            and not failing
            and cover["one_call_per_row"]
            and not cover["delivered_without_row"]
            and not cover["link_ambiguous"]
        )
        coverage.append(
            {
                "trial": trial,
                **cover,
                "failing_rows": failing,
                "export_exclusion": excluded.get(trial),
                "complete": complete,
                "capture_file": sha256(entry["file"]) if entry else None,
            }
        )

    failing_total = sum(len(c["failing_rows"]) for c in coverage)
    complete_total = sum(c["complete"] for c in coverage)
    qualification = {
        "schema": "evallab.ovn_reconstruction_qualification/2",
        "rule": (
            f"admit when >= {MIN_TRIALS} trials are complete (every row identical to its own "
            "delivered call, one call per row, every delivered call reproduced, link not "
            "ambiguous), no exported row fails and no captured call is unassigned"
        ),
        "admit_reconstructed": complete_total >= MIN_TRIALS
        and failing_total == 0
        and not capture["unassigned"],
        "trials_complete": complete_total,
        "rows_exported": len(checked["rows"]),
        "rows_failing": failing_total,
        "calls_captured": len(capture["calls"]),
        "calls_unassigned": capture["unassigned"],
        "producer": producer(args.tokenizer),
        "links": capture["links"],
        "coverage": coverage,
        "sha256": {
            "calls.jsonl": capture["calls_sha256"],
            "export/conversations.jsonl": sha256(export / "conversations.jsonl"),
            "export/manifest.json": sha256(export / "manifest.json"),
            "export/fidelity.json": sha256(export / "fidelity.json"),
            "split.json": sha256(SPLIT),
        },
    }
    (args.out / "qualification.json").write_text(json.dumps(qualification, indent=1) + "\n")
    print(json.dumps({k: qualification[k] for k in list(qualification)[2:9]}, indent=1))


if __name__ == "__main__":
    main()
