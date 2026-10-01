"""HAR-119 Docent block_idx -> trajectory step map (follow-up, offline).

Reproduces the exact conversion `docent_har119_upload.py` used
(`export_harbor.convert_trial` on the normalized trials, tags={}) without any
upload, enumerates every message of each run's single transcript, and writes
`predictions/docent_block_map.json` as {trial: {str(block_idx): step_id|None}}.

Mapping rule: block_idx is the 0-based message index in the converted
transcript (one transcript per uploaded run). The step is the message's
`metadata.atif_step_id`; a merged assistant turn covering several agent steps
(`metadata.atif_step_ids`) maps to the earliest step; a message with no step
metadata maps to None. Then patches `predictions/docent.jsonl`, appending
`first_failure_step` (step of the first citation in
`raw_first_mistake_evidence.citations` via the map, else None) to each row
with every other byte untouched.

Usage: keys run -- uv run --no-project --python 3.12 --with docent==0.1.87 \
  python research/explorations/trace-lab/har119/tools/docent_block_map.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
WORKTREE = HERE.parents[5]
HAR119 = HERE.parent.parent
PRED = HAR119 / "predictions"
NORM = PRED / "scout_work" / "normalized"

sys.path.insert(0, str(WORKTREE / "research" / "explorations" / "trace-lab" / "docent"))

from export_harbor import convert_trial  # noqa: E402


def step_of(message: dict) -> int | None:
    md = message.get("metadata") or {}
    if isinstance(md.get("atif_step_id"), int):
        return md["atif_step_id"]
    ids = [i for i in (md.get("atif_step_ids") or []) if isinstance(i, int)]
    return min(ids) if ids else None


def main() -> int:
    sel = json.loads((HAR119 / "selection.json").read_text())
    trials: list[str] = [r["trial"] for r in sel["runs"]]
    norm_trials: dict[str, Path] = {}
    for job_dir in sorted(NORM.iterdir()):
        if not job_dir.is_dir():
            continue
        for trial_dir in sorted(job_dir.iterdir()):
            if (trial_dir / "agent" / "trajectory.json").exists():
                norm_trials[trial_dir.name] = trial_dir

    block_map: dict[str, dict[str, int | None]] = {}
    for t in trials:
        run = convert_trial(norm_trials[t], NORM, {})
        payload = run.model_dump(mode="json")
        assert len(payload["transcripts"]) == 1, t
        messages = payload["transcripts"][0]["messages"]
        assert messages, t
        block_map[t] = {str(i): step_of(m) for i, m in enumerate(messages)}

    # Every block cited anywhere in the fetched readings must be covered.
    for t in trials:
        rec = json.loads((PRED / "docent" / "raw" / f"{t}.docent.json").read_text())
        out = rec.get("output") or {}
        for field, ev in out.items():
            if not isinstance(ev, dict):
                continue
            for c in ev.get("citations") or []:
                item = (c.get("target") or {}).get("item") or {}
                if item.get("item_type") == "block_content" and "block_idx" in item:
                    assert str(item["block_idx"]) in block_map[t], (t, field, item)

    (PRED / "docent_block_map.json").write_text(json.dumps(block_map, indent=1) + "\n")

    # Patch docent.jsonl: append first_failure_step, all other bytes identical.
    path = PRED / "docent.jsonl"
    lines = path.read_text().splitlines()
    assert len(lines) == 12, len(lines)
    patched: list[str] = []
    n_step = 0
    for line, t in zip(lines, trials, strict=False):
        row = json.loads(line)
        assert row["trial"] == t, (row["trial"], t)
        rec = json.loads((PRED / "docent" / "raw" / f"{t}.docent.json").read_text())
        ev = (rec.get("output") or {}).get("first_mistake_evidence") or {}
        cits = ev.get("citations") or []
        step = None
        if cits:
            item = (cits[0].get("target") or {}).get("item") or {}
            if item.get("item_type") == "block_content" and "block_idx" in item:
                step = block_map[t][str(item["block_idx"])]
        n_step += step is not None
        assert line.endswith("}"), t
        patched.append(line[:-1] + ', "first_failure_step": ' + json.dumps(step) + "}")
    path.write_text("\n".join(patched) + "\n")
    print(f"map: {sum(len(v) for v in block_map.values())} blocks over 12 trials")
    print(f"docent.jsonl: {n_step}/12 rows got a step")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
