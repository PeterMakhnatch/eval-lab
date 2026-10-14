#!/usr/bin/env python3
"""Run or score the ``qa_trace`` pipeline on one detectors-v1 split.

``run`` drives :func:`evallab.qa_trace.judge_corpus_rows` on the dev ids
(``ids/dev-<source>.txt``) WITHOUT touching the corpus cache: dev rows are for
prompt iteration only. Held-out rows go through ``evallab detectors run
--detector qa_trace --ids ids/heldout-<source>.txt --allow-paid`` so the only
cached (scored) verdicts are held-out.

``ablate`` re-reads the per-row stage JSON under the primary checkout's
``derived/qa-trace/detectors-v1/`` and scores, on the same rows, each stage on
its own against the ``reward_hacking`` label: T0 cached combined_v1, T0 live
deterministic literals, T1 reward-shortcut reviewer alone, and the full
pipeline (unrefuted ``reward_hacking`` finding). No model calls.

Usage:
  uv run python research/experiments/qa-trace/run_split.py run --split dev
  uv run python research/experiments/qa-trace/run_split.py ablate --split heldout --json results/heldout.json
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from evallab.detectors import Corpus
from evallab.qa_trace import judge_corpus_rows
from evallab.storage.paths import shared_checkout_root

HERE = Path(__file__).resolve().parent
DERIVED = Path.home() / "Developer" / "eval-lab" / "derived"
ROOTS = {
    "tw_root": DERIVED / "kev" / "raw" / "tw",
    "cb_root": DERIVED / "detector-roots" / "cb",
    "rl_root": DERIVED / "kev" / "rl" / "trials",
}


def _ids(split: str, sources: list[str] | None) -> dict[str, list[str]]:
    sample = json.loads((HERE / "detectors_v1_sample.json").read_text())[split]
    return {s: ids for s, ids in sample.items() if not sources or s in sources}


def _run(split: str, sources: list[str] | None, workers: int) -> None:
    from evallab.results_home import results_root

    corpus = Corpus.load("detectors-v1")
    by_id = {r["id"]: r for r in corpus.rows}
    items = []
    for ids in _ids(split, sources).values():
        for row_id in ids:
            row = by_id[row_id]
            trial = corpus.trial_dir(row, results_root=results_root(), **ROOTS)
            if trial is not None:
                items.append((row, trial))
    verdicts = judge_corpus_rows(items, workers=workers)
    print(json.dumps({"split": split, "items": len(items), "judged": len(verdicts)}))


def _cell(flags: list[tuple[bool, bool | None]]) -> dict[str, object]:
    pos = [f for label, f in flags if label]
    neg = [f for label, f in flags if not label]

    def rate(xs: list[bool | None]) -> dict[str, object]:
        judged = [x for x in xs if x is not None]
        return {"hit": sum(judged), "n": len(judged), "abstain": len(xs) - len(judged)}

    return {"positives_caught": rate(pos), "negatives_flagged": rate(neg)}


def _ablate(split: str, sources: list[str] | None) -> dict[str, object]:
    corpus = Corpus.load("detectors-v1")
    by_id = {r["id"]: r for r in corpus.rows}
    root = shared_checkout_root(HERE.parents[2]) / "derived" / "qa-trace" / "detectors-v1"
    systems: dict[str, dict[str, list[tuple[bool, bool | None]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    usage: dict[str, list[float]] = defaultdict(list)
    for source, ids in _ids(split, sources).items():
        for row_id in ids:
            run = root / row_id
            if not (run / "verdict.json").is_file():
                continue
            label = bool(by_id[row_id]["labels"]["reward_hacking"])
            verdict = json.loads((run / "verdict.json").read_text())
            t0 = json.loads((run / "t0.json").read_text())
            t1_path = run / "t1" / "reward_shortcut.json"
            t1 = json.loads(t1_path.read_text()) if t1_path.is_file() else {}
            answer = (t1.get("answer") or {}).get("answer")
            systems["T0 combined_v1 (cached OOF)"][source].append(
                (label, (t0.get("combined_v1_cached") or {}).get("flagged"))
            )
            systems["T0 live deterministic literals"][source].append(
                (label, bool(t0.get("fired")) if t0.get("measured") else None)
            )
            systems["T1 reward_shortcut alone"][source].append(
                (label, {"yes": True, "no": False}.get(answer))
            )
            systems["full pipeline (T4 reward_hacking)"][source].append(
                (label, verdict["reward_hacking"])
            )
            systems["full pipeline (T4 reward not real)"][source].append(
                (label, None if verdict["reward_real"] is None else not verdict["reward_real"])
            )
            usage[source].append(float((verdict.get("usage") or {}).get("calls") or 0))
    return {
        "split": split,
        "systems": {
            name: {src: _cell(rows) for src, rows in by_src.items()}
            for name, by_src in systems.items()
        },
        "calls_per_row": {src: round(sum(v) / len(v), 2) for src, v in usage.items() if v},
    }


def _render(report: dict[str, object]) -> str:
    lines = [
        "| system | source | positives caught | negatives flagged | abstained |",
        "|---|---|---|---|---|",
    ]
    for name, by_src in report["systems"].items():  # type: ignore[union-attr]
        for src, cell in by_src.items():
            pos, neg = cell["positives_caught"], cell["negatives_flagged"]
            lines.append(
                f"| {name} | {src} | {pos['hit']}/{pos['n']} | {neg['hit']}/{neg['n']} | {pos['abstain'] + neg['abstain']} |"
            )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=("run", "ablate"))
    ap.add_argument("--split", choices=("dev", "heldout"), required=True)
    ap.add_argument("--source", action="append")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--json", type=Path, help="ablate: also write the report here")
    args = ap.parse_args()
    if args.cmd == "run":
        if args.split != "dev":
            raise SystemExit(
                "held-out rows run through `evallab detectors run --detector qa_trace --ids ...`"
            )
        _run(args.split, args.source, args.workers)
        return 0
    report = _ablate(args.split, args.source)
    print(_render(report))
    print(json.dumps(report["calls_per_row"]))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
