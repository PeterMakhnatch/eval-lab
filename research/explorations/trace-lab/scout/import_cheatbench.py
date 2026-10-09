#!/usr/bin/env python3
"""Import the CheatBench viewer subset into an Inspect Scout transcripts DB.

Reads the pinned parquet at ``research/external/cheatbench/full.parquet``,
converts every ``cheating``/``attempt`` row with a MiMo-relevant mechanism
(see ``src/evallab/cheatbench.py``) to a stock-shaped ATIF staged trajectory,
and inserts it into a Scout transcripts directory with the CheatBench label,
mechanism, rationale and localization carried as transcript metadata. The
label card is also appended as a clearly-marked final message, so the cheat
step is findable in the transcript itself.

$0: reads local files only; no model calls, no uploads. Run from the Eval Lab
worktree root with the project env plus inspect-scout, e.g.::

    uv run --with inspect-scout==0.5.4 python \\
        research/explorations/trace-lab/scout/import_cheatbench.py \\
        --db /private/tmp/cheatbench-scout-db --staging /private/tmp/cheatbench-staging

Post-merge go-live (parent runs; see the corpus README) writes the same
staged files and inserts them into the live Trace Lab project DB.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
DEFAULT_PARQUET = REPO_ROOT / "research" / "external" / "cheatbench" / "full.parquet"

from evallab.cheatbench import (  # noqa: E402
    REVISION,
    convert_row,
    counts_table,
    load_rows,
    select_subset,
    validate_staged,
)


def build_staged(
    row: dict, staging_root: Path, *, overwrite: bool = False
) -> tuple[Path, dict] | None:
    """Write the staged ATIF trajectory for one row; return (path, meta)."""
    trace_id = row.get("trace_id")
    out_path = staging_root / trace_id / "agent" / "trajectory.json"
    if out_path.is_file() and not overwrite:
        _, meta, _ = convert_row(row)
        return out_path, meta
    trajectory, meta, warnings = convert_row(row)
    issues = validate_staged(trajectory)
    if issues:
        print(f"warn: {trace_id}: staged file invalid, skipping: {issues}", file=sys.stderr)
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(trajectory, indent=1) + "\n", encoding="utf-8")
    for warning in warnings:
        print(f"warn: {trace_id}: {warning}", file=sys.stderr)
    return out_path, meta


async def _insert(db_dir: Path, staged: list[tuple[Path, dict]]) -> int:
    from inspect_scout._transcript.database.factory import transcripts_db
    from inspect_scout.sources._atif.client import import_trajectory_model
    from inspect_scout.sources._atif.transcripts import _create_transcript

    model = import_trajectory_model()
    transcripts = []
    for staged_path, meta in staged:
        try:
            trajectory = model.model_validate_json(staged_path.read_bytes())
        except Exception as exc:  # noqa: BLE001
            print(f"warn: skipping invalid staged file {staged_path}: {exc}", file=sys.stderr)
            continue
        transcript = _create_transcript(trajectory, source_uri=str(staged_path))
        merged_metadata = {
            **(transcript.metadata or {}),
            "task": meta.get("task"),
            "trial": meta.get("trial_name"),
            "trial_id": meta.get("trial_id"),
            "job": meta.get("job"),
            "verdict": meta.get("verdict"),
            "reward": meta.get("reward"),
            "stop_reason": meta.get("stop_reason"),
            "harness": meta.get("agent"),
            "model_name": meta.get("model"),
            "cheatbench_label": meta.get("cheatbench_label"),
            "cheating_type": meta.get("cheating_type"),
            "cheating_mechanism": meta.get("cheating_mechanism"),
            "rationale": meta.get("rationale"),
            "note": meta.get("note"),
            "cheat_step_id": meta.get("cheat_step_id"),
            "cheat_message_no": meta.get("cheat_message_no"),
            "loc_corrected": meta.get("loc_corrected"),
            "loc_correction": meta.get("loc_correction"),
            "loc_line_adjusted": meta.get("loc_line_adjusted"),
            "loc_kind": meta.get("loc_kind"),
            "loc_pattern": meta.get("loc_pattern"),
            "loc_blatancy": meta.get("loc_blatancy"),
            "loc_field_path": meta.get("loc_field_path"),
            "loc_line_start": meta.get("loc_line_start"),
            "loc_line_end": meta.get("loc_line_end"),
            "loc_snippet_context": meta.get("loc_snippet_context"),
            "loc_evidence_source": meta.get("loc_evidence_source"),
            "loc_confidence": meta.get("loc_confidence"),
            "cheatbench_revision": REVISION,
            "cheatbench_source": meta.get("source"),
        }
        transcript = transcript.model_copy(
            update={
                "task_set": "cheatbench",
                "task_id": meta.get("task"),
                "score": meta.get("reward"),
                "success": meta.get("cheatbench_label") == "cheating",
                "error": None,
                "limit": None,
                "metadata": merged_metadata,
            }
        )
        transcripts.append(transcript)

    db_dir.mkdir(parents=True, exist_ok=True)
    async with transcripts_db(str(db_dir)) as db:
        await db.insert(transcripts)
    return len(transcripts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--parquet", default=str(DEFAULT_PARQUET), help="pinned full parquet")
    parser.add_argument("--db", required=True, help="Scout transcripts directory to write")
    parser.add_argument(
        "--staging", default=None, help="staged trajectories dir (default: <db>_staging)"
    )
    parser.add_argument("--overwrite", action="store_true", help="re-stage and re-insert")
    parser.add_argument("--limit", type=int, default=None, help="convert at most N subset rows")
    parser.add_argument(
        "--trace-id", action="append", default=None, help="only this trace_id (repeatable)"
    )
    args = parser.parse_args(argv)

    rows = load_rows(args.parquet)
    subset = select_subset(rows)
    if args.trace_id:
        wanted = set(args.trace_id)
        subset = [row for row in subset if row.get("trace_id") in wanted]
    if args.limit is not None:
        subset = subset[: args.limit]
    if not subset:
        raise SystemExit("no subset rows selected")

    print("counts (benchmark x mechanism x label):")
    for benchmark, mechanism, label, count in counts_table(subset):
        print(f"  {benchmark} | {mechanism} | {label} | {count}")

    db_dir = Path(args.db)
    staging_root = Path(args.staging) if args.staging else Path(str(db_dir) + "_staging")
    staged: list[tuple[Path, dict]] = []
    located = corrected = 0
    for row in subset:
        built = build_staged(row, staging_root, overwrite=args.overwrite)
        if built is None:
            continue
        staged_path, meta = built
        staged.append((staged_path, meta))
        if meta["cheat_step_id"] is not None:
            located += 1
        if meta["loc_corrected"]:
            corrected += 1
    count = asyncio.run(_insert(db_dir, staged))
    print(
        f"inserted {count} transcripts into {db_dir} (cheat located {located}, corrected {corrected})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
