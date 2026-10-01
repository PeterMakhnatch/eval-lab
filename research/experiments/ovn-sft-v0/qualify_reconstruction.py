#!/usr/bin/env python3
"""Qualify ATIF reconstruction against captured bodies before admitting
historical passes to G3 (HAR-127; Research-Harbor ruling 04:45Z).

The question: does ``sft_terminus --per-turn-stride 1`` rebuild every call a
trial made, byte for byte, from its ATIF trajectory alone? Answered on G2
trials whose calls were captured by ``evallab capture serve``:

1. ``evallab.model_capture.link_capture`` attributes each captured call to its
   job's trial (route token); unassigned or ambiguous calls are listed and
   are not admissible;
2. the calls of each trial go to ``captures/<trial>.jsonl``, in ``seq`` order;
3. every trial of the jobs (passes and fails: ``--reward-threshold 0``,
   whole trajectories, no cut) is exported at stride 1;
4. ``fidelity.py --require-capture --gate-target`` checks every exported row
   against its own trial's capture: history and target byte-identical, prompt
   and completion token counts exact;
5. coverage per trial: captured calls, exported rows, identical rows, failing
   rows with their verdict, and captured calls no row accounts for (by message
   count; summarization subagent calls are expected there).

``admit_reconstructed`` is true only when at least ``MIN_TRIALS`` trials are
complete (every captured main-chat call reproduced, every row identical) and
no exported row anywhere fails. ``qualification.json`` records the verdict,
the coverage and the sha256 of every input and output.

    uv run python research/experiments/ovn-sft-v0/qualify_reconstruction.py \\
        --capture-dir CAPTURE_DIR --job JOB_DIR ... --tokenizer TOKDIR --out OUT
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SPLIT = ROOT / "research/experiments/har81-mimo-sft/split.json"
MIN_TRIALS = 3


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    import pyarrow.parquet as pq

    from evallab.model_capture import link_capture

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--job", type=Path, action="append", required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        shutil.rmtree(args.out)
    (args.out / "captures").mkdir(parents=True)

    calls_path = args.capture_dir / "calls.jsonl"
    by_seq = {
        int(call["seq"]): call
        for call in (json.loads(line) for line in calls_path.read_text().splitlines() if line)
    }
    trial_seqs: dict[str, list[int]] = defaultdict(list)
    attribution: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    links = []
    for job in args.job:
        receipt = link_capture(args.capture_dir, job, derived_root=args.out / "link")
        table = pq.read_table(Path(receipt["parquet_dir"]) / "model_calls.parquet").to_pylist()
        for row in table:
            if row["trial_name"]:
                trial_seqs[row["trial_name"]].append(int(row["seq"]))
                attribution[row["trial_name"]][row["attribution"]] += 1
        links.append(
            {
                "job": job.name,
                "calls_assigned": receipt["calls_assigned"],
                "calls_unassigned_in_shared_file": len(receipt["calls_unassigned"]),
                "ambiguous_trials": receipt["ambiguous_trials"],
                "receipt": sha256(Path(receipt["parquet_dir"]) / "capture_link.json"),
            }
        )
    capture_files = {}
    for trial, seqs in sorted(trial_seqs.items()):
        path = args.out / "captures" / f"{trial}.jsonl"
        path.write_text(
            "".join(json.dumps(by_seq[s], ensure_ascii=False) + "\n" for s in sorted(seqs))
        )
        capture_files[trial] = path

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

    fidelity = ["uv", "run", "--no-project", "--with", "transformers", "--with", "jinja2"]
    fidelity += [
        "python",
        str(HERE / "fidelity.py"),
        str(export),
        "--tokenizer",
        str(args.tokenizer),
    ]
    fidelity += ["--require-capture", "--gate-target"]
    for trial, path in capture_files.items():
        fidelity += ["--capture", f"{trial}={path}"]
    subprocess.run(fidelity, check=False, capture_output=True)
    checked = json.loads((export / "fidelity.json").read_text())

    conversation_trial = {
        c["conversation_id"]: Path(c["trial"]).name for c in manifest["conversations"]
    }
    rows = [json.loads(line) for line in (export / "conversations.jsonl").open()]
    per_trial: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"rows": 0, "identical": 0, "failing": [], "history_lengths": set()}
    )
    for row, info, result in zip(rows, manifest["rows"], checked["rows"], strict=True):
        entry = per_trial[conversation_trial[info["conversation_id"]]]
        entry["rows"] += 1
        entry["history_lengths"].add(len(row["messages"]) - 1)
        if (
            result.get("capture") == "identical"
            and result["prompt_ok"]
            and result["target_delta"] == 0
        ):
            entry["identical"] += 1
        else:
            entry["failing"].append(
                {
                    "row_id": info["row_id"],
                    "capture": result.get("capture", "missing"),
                    "prompt_ok": result["prompt_ok"],
                    "target_delta": result["target_delta"],
                }
            )

    excluded = {
        Path(t["trial"]).name: t.get("reasons") or t.get("disposition")
        for t in manifest["trials"]
        if t.get("disposition") != "selected"
    }
    coverage = []
    for trial in sorted(set(trial_seqs) | set(per_trial)):
        entry = per_trial.get(
            trial, {"rows": 0, "identical": 0, "failing": [], "history_lengths": set()}
        )
        seqs = sorted(trial_seqs.get(trial, []))
        delivered = [
            s
            for s in seqs
            if by_seq[s].get("error") is None and by_seq[s].get("response_status") == 200
        ]
        # A delivered call no exported row stands at (e.g. a summarization
        # subagent call) leaves the trial incomplete: reconstruction is proven
        # only for trials where every delivered call is reproduced.
        unaccounted = [
            {"seq": s, "messages": len((by_seq[s].get("request_body") or {}).get("messages") or [])}
            for s in delivered
            if len((by_seq[s].get("request_body") or {}).get("messages") or [])
            not in entry["history_lengths"]
        ]
        complete = (
            trial in capture_files
            and entry["rows"] > 0
            and entry["identical"] == entry["rows"]
            and not unaccounted
        )
        coverage.append(
            {
                "trial": trial,
                "captured_calls": len(seqs),
                "undelivered_calls": [s for s in seqs if s not in delivered],
                "exported_rows": entry["rows"],
                "identical_rows": entry["identical"],
                "failing_rows": entry["failing"],
                "captured_calls_without_row": unaccounted,
                "export_exclusion": excluded.get(trial),
                "attribution": dict(attribution.get(trial, {})),
                "complete": complete,
                "capture_file": sha256(capture_files[trial]) if trial in capture_files else None,
            }
        )

    failing = sum(len(c["failing_rows"]) for c in coverage)
    complete = sum(c["complete"] for c in coverage)
    qualification = {
        "schema": "evallab.ovn_reconstruction_qualification/1",
        "rule": (
            f"admit when >= {MIN_TRIALS} trials are complete (every delivered captured call "
            "reproduced by an exported row identical to it, bytes and token counts) and no "
            "exported row fails"
        ),
        "admit_reconstructed": complete >= MIN_TRIALS and failing == 0,
        "trials_complete": complete,
        "rows_exported": len(rows),
        "rows_identical": sum(c["identical_rows"] for c in coverage),
        "rows_failing": failing,
        "tokenizer_revision": checked["summary"]["tokenizer_revision"],
        "calls_captured": len(by_seq),
        "calls_assigned_to_no_job": sorted(
            set(by_seq) - {s for seqs in trial_seqs.values() for s in seqs}
        ),
        "links": links,
        "coverage": coverage,
        "sha256": {
            "calls.jsonl": sha256(calls_path),
            "export/conversations.jsonl": sha256(export / "conversations.jsonl"),
            "export/manifest.json": sha256(export / "manifest.json"),
            "export/fidelity.json": sha256(export / "fidelity.json"),
            "split.json": sha256(SPLIT),
        },
    }
    (args.out / "qualification.json").write_text(json.dumps(qualification, indent=1) + "\n")
    print(json.dumps({k: qualification[k] for k in list(qualification)[2:8]}, indent=1))


if __name__ == "__main__":
    main()
