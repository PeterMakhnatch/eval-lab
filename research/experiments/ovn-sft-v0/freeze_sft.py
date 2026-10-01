#!/usr/bin/env python3
"""Freeze the G3 SFT set from a selection record (HAR-127).

Steps, all with existing tools:

1. ``evallab.sft_terminus export --selection --per-turn-stride 1`` over the
   selected trials' job roots, to measure every call's sequence tokens
   (recorded ``prompt_tokens + completion_tokens``);
2. choose the per-call stride: the smallest ``N`` whose total sequence tokens
   are at most ``TARGET_TOKENS`` (Research-Harbor 04:53Z: one epoch, target
   about 2.0M, never above 2.5M); every conversation's last call is always
   kept;
3. export again at that stride into ``--out`` (the frozen set);
4. ``fidelity.py`` on it (prompt and completion tokens, plus a byte compare
   of each captured trial against its own delivered calls, linked by
   ``g3_capture.split_capture``); any failing row aborts the freeze;
5. capture health: every row of a ``captured`` trial must be identical to its
   own delivered call (no call backing two rows), its link must not be
   ambiguous and no captured call may be unassigned; per-trial link
   completeness goes into ``freeze.json``;
6. ``reconstructed_validated`` trials need ``--qualification``: the file the
   selection recorded (same sha256), and the same producer digests
   (exporter, checker, linker, tokenizer files, library versions);
7. write ``data_card.md`` and ``freeze.json`` (stride rule, token totals,
   per-source and per-task counts, every exclusion with its reason, and the
   sha256 of ``conversations.jsonl``, ``manifest.json`` and the selection).

Usage (from the checkout root):
    uv run python research/experiments/ovn-sft-v0/freeze_sft.py \\
        --selection DIR/selection.json --exclusions DIR/selection_exclusions.json \\
        --root LABEL=JOB_DIR ... --tokenizer TOKDIR --out OUT \\
        [--capture-dir CAPTURE_DIR --capture-job JOB_DIR ...] [--qualification QUAL.json]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from g3_capture import check_fidelity, producer, sha256, split_capture, witness  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SPLIT = ROOT / "research/experiments/har81-mimo-sft/split.json"
TARGET_TOKENS = 2_000_000
CAP_TOKENS = 2_500_000


def export(roots: list[str], selection: Path, stride: int, out: Path) -> dict:
    if out.exists():
        shutil.rmtree(out)
    command = [
        sys.executable,
        "-m",
        "evallab.sft_terminus",
        "export",
        "--split-manifest",
        str(SPLIT),
        "--selection",
        str(selection),
        "--per-turn-stride",
        str(stride),
        "--out",
        str(out),
    ]
    for root in roots:
        command += ["--root", root]
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f"export failed: {done.stdout}{done.stderr}")
    return json.loads((out / "manifest.json").read_text())


def call_tokens(manifest: dict) -> dict[str, list[tuple[int, int]]]:
    """conversation id -> [(turn index, sequence tokens)] for every call."""
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for row in manifest["rows"]:
        prompt, completion = row["prompt_tokens_recorded"], row["completion_tokens_recorded"]
        if prompt is None or completion is None:
            raise SystemExit(f"row {row['row_id']} has no recorded token counts")
        out[row["conversation_id"]].append((row["turn_index"], prompt + completion))
    return out


def kept_tokens(calls: dict[str, list[tuple[int, int]]], stride: int) -> int:
    """Tokens the exporter keeps at ``stride``: it strides over each
    conversation's eligible calls (the stride-1 rows, copied context already
    left out) by position and always keeps the last one."""
    total = 0
    for turns in calls.values():
        ordered = [tokens for _, tokens in sorted(turns)]
        total += sum(
            tokens
            for position, tokens in enumerate(ordered)
            if position % stride == stride - 1 or position == len(ordered) - 1
        )
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--exclusions", type=Path, required=True)
    parser.add_argument("--root", action="append", required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--capture-dir", type=Path, help="evallab capture serve output")
    parser.add_argument(
        "--capture-job", type=Path, action="append", default=[], help="every job using it"
    )
    parser.add_argument("--qualification", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    full = export(args.root, args.selection, 1, args.out.with_name(args.out.name + ".stride1"))
    calls = call_tokens(full)
    stride = next(n for n in range(1, 10_000) if kept_tokens(calls, n) <= TARGET_TOKENS)
    planned = kept_tokens(calls, stride)
    if planned > CAP_TOKENS:
        raise SystemExit(f"stride {stride} keeps {planned} tokens, above the {CAP_TOKENS} cap")
    manifest = export(args.root, args.selection, stride, args.out)

    selection = json.loads(args.selection.read_text())
    sources = Counter(t["source"] for t in selection["trials"])
    qualification = None
    if sources["reconstructed_validated"]:
        if args.qualification is None or sha256(args.qualification) != selection.get(
            "qualification"
        ):
            raise SystemExit("reconstructed_validated trials need the selection's qualification")
        qualification = json.loads(args.qualification.read_text())
        if not qualification["admit_reconstructed"]:
            raise SystemExit("the qualification does not admit reconstructed trials")
        if qualification["producer"] != producer(args.tokenizer):
            raise SystemExit("producer digests differ from the qualified reconstructor")

    capture = None
    if args.capture_dir is not None:
        capture = split_capture(
            args.capture_dir, args.capture_job, args.out.with_name(args.out.name + ".capture")
        )
    checked = check_fidelity(args.out, args.tokenizer, capture, show=3)
    summary = checked["summary"]
    if summary["failing_rows"]:
        raise SystemExit(f"fidelity failed on {len(summary['failing_rows'])} rows; not frozen")

    trial_of = {c["conversation_id"]: Path(c["trial"]).name for c in manifest["conversations"]}
    results_by_trial = defaultdict(list)
    for info, result in zip(manifest["rows"], checked["rows"], strict=True):
        results_by_trial[trial_of[info["conversation_id"]]].append(result)
    capture_links = {}
    for t in selection["trials"]:
        if t["source"] != "captured":
            continue
        entry = (capture or {"trials": {}})["trials"].get(t["trial"])
        cover = witness(results_by_trial[t["trial"]], entry)
        capture_links[t["trial"]] = cover
        if (
            entry is None
            or cover["rows_identical"] != cover["rows"]
            or not cover["one_call_per_row"]
            or cover["link_ambiguous"]
        ):
            raise SystemExit(f"captured trial {t['trial']} fails capture health: {cover}")
    if capture is not None and capture["unassigned"]:
        raise SystemExit(f"{len(capture['unassigned'])} captured calls are unassigned")

    source_of = {(t["job"], t["trial"]): t["source"] for t in selection["trials"]}
    conv = {c["conversation_id"]: c for c in manifest["conversations"]}
    rows = manifest["rows"]
    per_source: dict[str, Counter] = defaultdict(Counter)
    per_task: Counter = Counter()
    row_tokens = []
    for row in rows:
        c = conv[row["conversation_id"]]
        source = source_of[(c["job"], Path(c["trial"]).name)]
        tokens = row["prompt_tokens_recorded"] + row["completion_tokens_recorded"]
        per_source[source]["rows"] += 1
        per_source[source]["tokens"] += tokens
        per_source[source]["target_tokens"] += row["completion_tokens_recorded"]
        per_task[c["task_id"]] += 1
        row_tokens.append(tokens)
    freeze = {
        "stride": stride,
        "stride_rule": (
            f"smallest N with total sequence tokens <= {TARGET_TOKENS}; keep assistant "
            "turns with index % N == N-1 plus every conversation's last"
        ),
        "tokens_all_calls": kept_tokens(calls, 1),
        "tokens_kept": sum(row_tokens),
        "rows": len(rows),
        "trials": len(selection["trials"]),
        "per_source": {k: dict(v) for k, v in per_source.items()},
        "per_task_rows": dict(sorted(per_task.items())),
        "row_tokens_min_median_max": [
            min(row_tokens),
            sorted(row_tokens)[len(row_tokens) // 2],
            max(row_tokens),
        ],
        "fidelity": summary,
        "capture_links": capture_links,
        "calls_unassigned": capture["unassigned"] if capture else None,
        "producer": producer(args.tokenizer),
        "qualification": sha256(args.qualification) if qualification else None,
        "sha256": {
            "conversations.jsonl": sha256(args.out / "conversations.jsonl"),
            "manifest.json": sha256(args.out / "manifest.json"),
            "selection.json": sha256(args.selection),
            "selection_exclusions.json": sha256(args.exclusions),
            "split.json": sha256(SPLIT),
        },
    }
    (args.out / "freeze.json").write_text(json.dumps(freeze, indent=1) + "\n")
    exclusions = json.loads(args.exclusions.read_text())
    card = [
        "# G3 SFT set: data card",
        "",
        f"- Rows: {len(rows)} (one model call each), from {len(selection['trials'])} trials on "
        f"{len(per_task)} tasks.",
        f"- Sequence tokens: {sum(row_tokens):,} kept of {kept_tokens(calls, 1):,} (stride "
        f"{stride}); per row min/median/max {freeze['row_tokens_min_median_max']}.",
        f"- Fidelity: prompt tokens exact {summary['prompt_exact']}/{summary['rows']}, "
        f"completion tokens exact {summary['target_exact']}/{summary['rows']}, captured "
        f"messages identical {summary['captured_identical']}/{summary['captured_checked']}.",
        f"- conversations.jsonl {freeze['sha256']['conversations.jsonl']}",
        "",
        "## Sources",
        "",
        "| source | rows | sequence tokens | target tokens |",
        "|---|---|---|---|",
        *[
            f"| {k} | {v['rows']} | {v['tokens']:,} | {v['target_tokens']:,} |"
            for k, v in sorted(per_source.items())
        ],
        "",
        "## Trials",
        "",
        "`format_warning_steps_kept` (Traces, HAR-128): kept steps whose observation carries a"
        " Terminus-2 warning (missing duration, missing newline, parse error); the assistant"
        " output that caused it is trained on.",
        "",
        "| job | trial | source | cut_step_id | format_warning_steps_kept |",
        "|---|---|---|---|---|",
        *[
            f"| {t['job']} | {t['trial']} | {t['source']} | {t['cut_step_id']} "
            f"| {t.get('format_warning_steps_kept')} |"
            for t in selection["trials"]
        ],
        "",
        "## Rows per task",
        "",
        "| task | rows |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in sorted(per_task.items())],
        "",
        "## Exclusions",
        "",
        "| job | trial | task | reason |",
        "|---|---|---|---|",
        *[f"| {e['job']} | {e['trial']} | {e['task_id']} | {e['reason']} |" for e in exclusions],
        "",
    ]
    (args.out / "data_card.md").write_text("\n".join(card))
    print(json.dumps({k: freeze[k] for k in ("stride", "tokens_kept", "rows", "sha256")}, indent=1))


if __name__ == "__main__":
    main()
