"""Import fxtr-lab Sol verdicts into the detectors-v1 cache (``sol_codex``).

Reads the worker-B export
``~/Developer/eval-lab/derived/detector-roots/sol_codex_export.jsonl``
(one row per corpus id; see the combined-detector handoff for the schema)
and stores each verdict under
``research/calibration/detectors-v1/cache/sol_codex/<id>.json``,
then reseals the cache (``Corpus.seal()``; labels untouched).

Usage (from the worktree root; rerun as B appends more rows)::

    uv run python research/experiments/combined-detector/import_sol.py
    uv run python research/experiments/combined-detector/import_sol.py --export PATH --refresh
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evallab.detectors import Corpus

DEFAULT_EXPORT = Path.home() / "Developer/eval-lab/derived/detector-roots/sol_codex_export.jsonl"

#: Export fields kept as cache-verdict extras (the rest maps 1:1).
_EXTRA_KEYS = (
    "model",
    "reasoning_effort",
    "codex_version",
    "fxtr_job",
    "reused",
    "quote_verified",
)


def import_sol(corpus: Corpus, export: Path, *, refresh: bool = False) -> dict[str, int]:
    """Store export rows in ``cache/sol_codex``; return ``{imported, skipped}``."""
    cached = corpus.cached("sol_codex") if not refresh else {}
    stats = {"imported": 0, "skipped": 0}
    if not export.is_file():
        return stats
    for line in export.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        row_id = row["id"]
        if row_id in cached:
            stats["skipped"] += 1
            continue
        verdict = {
            "id": row_id,
            "detector": "sol_codex",
            "flagged": row["flagged"],
            "score": row["score"],
            "explanation": row.get("explanation") or "",
            "cost_usd": 0,
            **{k: row[k] for k in _EXTRA_KEYS if k in row},
        }
        corpus.store("sol_codex", verdict)
        stats["imported"] += 1
    corpus.seal()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default="detectors-v1")
    parser.add_argument("--export", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    stats = import_sol(Corpus.load(args.corpus), args.export, refresh=args.refresh)
    print(json.dumps(stats, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
