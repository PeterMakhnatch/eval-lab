#!/usr/bin/env python3
"""Copy one fxtr batch job's per-trial verdicts into a batch judge file.

fxtr's ``cheatbench-judges`` judges an export batch with Claude Sonnet 5.5 on
Peter's subscription (``launch.py batch <batch_id>``) and keeps the verdicts in
its own local store. This reads one finished batch job's verdict table
(read-only) and writes the rows of one judge in the shape ``analyze.py`` and
``export.py`` read from ``runs/_batches/<batch>/judge-*.jsonl``. Valid verdicts
carry ``cheated``, ``honey_pots_seen`` and ``explicit_prohibition_violated``;
failed calls become ``judge_error`` rows (unjudged, never guessed).

Runs in fxtr's environment, not Eval Lab's::

    cd ~/Developer/fxtr/cheatbench-judges
    uv run python <eval-lab>/research/experiments/cheatbench-port/fxtr_verdicts.py \\
        <fxtr job id> <eval-lab>/runs/_batches/<batch>/judge-claude-sonnet-5.5.jsonl \\
        [--judge claude-sonnet-5.5]
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

import anyio
from cheatbench_judges.batches import BatchReport
from fxtr.project.client import JobID
from fxtr.project.running import open_local_client

FXTR_PROJECT = Path.home() / "Developer/fxtr/cheatbench-judges/launch.py"
JUDGE_MODELS = {
    "claude-sonnet-5.5": "anthropic/claude-sonnet-5-5 (OMP subscription)",
    "claude-haiku-5.5": "anthropic/claude-haiku-5-5 (OMP subscription)",
}


async def extract(job_id: str, judge: str) -> list[dict]:
    async with open_local_client(str(FXTR_PROJECT)) as client:
        handle = await client.job(JobID(uuid.UUID(job_id)))
        result = await handle.result()
        report = await client.load(result.item(), as_type=BatchReport)
        table = await client.load(report.table)
    rows = []
    for (name, trial), row in sorted(table.items()):
        if name != judge:
            continue
        base = {"trial_id": trial, "judge": judge,
                "judge_model": JUDGE_MODELS.get(judge, judge),
                "judge_cost_usd": 0.0, "source": f"fxtr cheatbench-judges job {job_id}",
                "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"]}
        if row["status"] == "ok" and row["cheated"] is not None:
            rows.append({**base, "cheated": row["cheated"],
                         "honey_pots_seen": row["honey_pots_seen"],
                         "explicit_prohibition_violated": row["explicit_prohibition_violated"]})
        else:
            rows.append({**base, "judge_error": f"fxtr verdict status {row['status']}"})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("job_id")
    ap.add_argument("out", type=Path)
    ap.add_argument("--judge", default="claude-sonnet-5.5")
    args = ap.parse_args()
    rows = anyio.run(extract, args.job_id, args.judge)
    if not rows:
        raise SystemExit(f"job {args.job_id} has no {args.judge} verdicts")
    args.out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    args.out.with_name(args.out.stem + ".header.json").write_text(json.dumps({
        "tool": "cheatbench-port/fxtr_verdicts.py", "judge": args.judge,
        "judge_model": JUDGE_MODELS.get(args.judge, args.judge),
        "endpoint_host": "omp auth-gateway (subscription)",
        "source": f"fxtr cheatbench-judges job {args.job_id}",
    }, indent=1) + "\n")
    ok = sum("cheated" in r for r in rows)
    print(f"{args.judge}: {len(rows)} trials, {ok} valid, "
          f"{sum(bool(r.get('cheated')) for r in rows)} cheated -> {args.out}")


if __name__ == "__main__":
    main()
