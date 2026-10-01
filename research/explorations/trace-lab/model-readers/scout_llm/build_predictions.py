"""Scout LLM predictions builder: scan DB -> predictions/scout_llm.jsonl.

Maps each trial_reader answer to the HAR-119 prediction shape
(``trial, stop_reason, first_failure_ref, blame, loop_kind, loop_span,
pass_copied`` + ``raw_*``), grounding every cited ``[Mn]`` in the staged
trajectory: ``[Mn]`` -> position -> ``head#N`` ref, with the quote checked
against that position's step text (see ``../verify.py``). Grounding
failures are recorded in ``raw_*``, never silently repaired.

Usage (from worktree root)::

    uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 \\
      --with pyarrow --with pandas \\
      python research/explorations/trace-lab/model-readers/scout_llm/build_predictions.py \\
      --scan <scans dir>/scan_id=<id> --staging <staging root> \\
      --trials <trial>:<trial_dir> [...] --model <model string> \\
      --out predictions/scout_llm.jsonl --raw-dir predictions/scout_llm_raw
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
MODEL_READERS = HERE.parent.parent
WORKTREE = HERE.parents[5]
HAR119 = WORKTREE / "research" / "explorations" / "trace-lab" / "har119"

for p in (str(MODEL_READERS), str(WORKTREE / "research" / "explorations" / "trace-lab" / "scout")):
    if p not in sys.path:
        sys.path.insert(0, p)

from verify import (  # noqa: E402
    check_quote,
    cites_of,
    load_step_texts,
    norm_ref,
    normalize,
    step_of,
)

try:
    from scanners import _ref_index_map  # noqa: E402  (cross-check only)
except Exception:  # noqa: BLE001
    _ref_index_map = None


def ordered_refs(staged_traj: Path) -> list[str | None]:
    """Primary step ref per 1-based message position (None for obs results).

    Same walk as ``scout/scanners.py::_ref_index_map``: one message per
    step plus one per ``observation.results`` entry, skipping system steps
    carrying ``extra.context_management``.
    """
    data = json.loads(staged_traj.read_text())
    filename = staged_traj.name
    refs: list[str | None] = []
    for step in data.get("steps") or []:
        extra = step.get("extra") or {}
        if step.get("source") == "system" and "context_management" in extra:
            continue
        step_id = step.get("step_id")
        primary = f"head#{step_id}" if filename == "trajectory.json" else f"{filename}#{step_id}"
        trace_lab = extra.get("trace_lab") if isinstance(extra, dict) else None
        if isinstance(trace_lab, dict) and trace_lab.get("ref"):
            primary = str(trace_lab["ref"])
        refs.append(norm_ref(primary))
        observation = step.get("observation")
        results = observation.get("results") if isinstance(observation, dict) else None
        if isinstance(results, list):
            refs.extend([None] * len(results))
    return refs


def reward_of(trial_dir: Path) -> float | None:
    try:
        result = json.loads((trial_dir / "result.json").read_text())
    except (OSError, ValueError):
        return None
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    reward = rewards.get("reward")
    return float(reward) if isinstance(reward, (int, float)) else None
def ground_evidence(
    evidence: str | None,
    refs: list[str | None],
    step_texts: list[str],
    whole: str,
    ref_map: dict[str, int] | None,
) -> dict:
    """[Mn] cite -> ref + step, with quote grounding. Mechanical only."""
    cites = cites_of(evidence)
    pos = cites[0] if cites else None
    ref = refs[pos - 1] if pos is not None and 1 <= pos <= len(refs) else None
    if ref is None and pos is not None:
        # Observation-results messages carry no ref of their own; they
        # belong to the nearest preceding step message (owning step).
        for back in range(pos - 1, 0, -1):
            if back <= len(refs) and refs[back - 1] is not None:
                ref = refs[back - 1]
                break
    if ref is None and pos is not None and ref_map:
        # Unaligned walk: resolve via the frozen map.
        for candidate, position in ref_map.items():
            if position == pos:
                ref = norm_ref(candidate)
                break
    # Quote = text after stripping [Mn] markers (keep it simple: full field).
    quote = normalize(evidence)
    for c in cites:
        quote = quote.replace(f"[M{c}]", "").strip()
    check = check_quote(step_texts, whole, pos, quote if len(quote) <= 400 else quote[:400])
    out = {
        "cites": cites,
        "pos": pos,
        "ref": ref,
        "step": step_of(ref),
        "quote_status": check["status"],
    }
    if check.get("hits"):
        out["quote_hits"] = check["hits"]
    return out


def main(argv: list[str] | None = None) -> int:
    from inspect_scout import scan_results_df

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", required=True, help="scan results dir (scan_id=...)")
    parser.add_argument("--staging", required=True, help="staging root holding trial trajectories")
    parser.add_argument("--trials", nargs="+", required=True, help="<trial>:<trial_dir> pairs")
    parser.add_argument("--model", required=True, help="model string used for the scan")
    parser.add_argument("--out", required=True, help="predictions jsonl to write")
    parser.add_argument("--raw-dir", required=True, help="per-trial raw JSON dir")
    args = parser.parse_args(argv)

    pairs = [t.split(":", 1) for t in args.trials]
    assert all(len(p) == 2 for p in pairs), args.trials

    r = scan_results_df(args.scan)
    print("scanners:", sorted(r.scanners.keys()))
    df = r.scanners.get("trial_reader")
    if df is None:
        print("trial_reader scanner missing", file=sys.stderr)
        return 2
    print("columns:", list(df.columns))

    staging = Path(args.staging)
    raw_dir = Path(args.raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for trial, trial_dir_s in pairs:
        trial_dir = Path(trial_dir_s)
        sub = df.loc[df["transcript_source_uri"].str.contains(trial, na=False)]
        assert len(sub) == 1, f"{trial}: {len(sub)} rows"
        row = sub.iloc[0]
        value = row["value"]
        if isinstance(value, str):
            value = json.loads(value)
        assert isinstance(value, dict), f"{trial}: value type {type(value)}"

        staged_list = sorted(staging.rglob(f"{trial}/agent/trajectory.json"))
        assert staged_list, f"{trial}: no staged trajectory under {staging}"
        staged = staged_list[0]
        refs = ordered_refs(staged)
        step_texts, whole = load_step_texts(staged)
        ref_map = None
        if _ref_index_map is not None:
            try:
                ref_map = _ref_index_map(str(row["transcript_source_uri"]))
            except (OSError, ValueError):
                ref_map = None

        alignment = {
            "n_messages": int(row.get("transcript_num_messages") or 0) or None,
            "n_positions": len(refs),
            "max_map_pos": max(ref_map.values()) if ref_map else None,
        }

        ff = ground_evidence(value.get("first_failure_evidence"), refs, step_texts, whole, ref_map)
        loop_start = ground_evidence(value.get("loop_start_evidence"), refs, step_texts, whole, ref_map)
        loop_end = ground_evidence(value.get("loop_end_evidence"), refs, step_texts, whole, ref_map)
        stop_g = ground_evidence(value.get("stop_evidence"), refs, step_texts, whole, ref_map)
        blame_g = ground_evidence(value.get("blame_evidence"), refs, step_texts, whole, ref_map)
        up_g = ground_evidence(value.get("upstream_evidence"), refs, step_texts, whole, ref_map)

        reward = reward_of(trial_dir)
        is_pass = bool(value.get("first_failure_is_pass"))
        ff_ref = None if is_pass else ff["ref"]
        kind = value.get("loop_kind")
        if kind == "none":
            span = None
        else:
            span = [loop_start["ref"], loop_end["ref"]]
            if span == [None, None]:
                span = None
        pass_copied = None if reward is None or reward < 1.0 else bool(value.get("upstream_fetch"))

        out_row = {
            "trial": trial,
            "stop_reason": value.get("stop_reason"),
            "first_failure_ref": ff_ref,
            "blame": value.get("blame"),
            "loop_kind": "none" if kind == "none" else kind,
            "loop_span": None if kind == "none" else span,
            "pass_copied": pass_copied,
            "raw_first_failure_what": value.get("first_failure_what"),
            "raw_first_failure_is_pass": is_pass,
            "raw_first_failure_ground": ff,
            "raw_loop_ground_start": loop_start,
            "raw_loop_ground_end": loop_end,
            "raw_stop_ground": stop_g,
            "raw_blame_ground": blame_g,
            "raw_upstream_fetch": bool(value.get("upstream_fetch")),
            "raw_upstream_ground": up_g,
            "raw_reward": reward,
            "raw_alignment": alignment,
            "raw_model": args.model,
            "raw_scan": args.scan,
            "raw_staged": str(staged),
            "tool": "scout_llm",
            "source": f"scout trial_reader ({args.model}); [Mn]->head#N via staged walk + quote grounding",
        }
        (raw_dir / f"{trial}.scout_llm.json").write_text(json.dumps(value, indent=1))
        lines.append(json.dumps(out_row))
        print(
            f"{trial}: stop={out_row['stop_reason']} ff={ff_ref} blame={out_row['blame']} "
            f"loop={out_row['loop_kind']} span={span} copied={pass_copied} "
            f"ground={[ff['quote_status'], loop_start['quote_status'], blame_g['quote_status']]}"
        )
    Path(args.out).write_text("\n".join(lines) + "\n")
    print(f"wrote {len(lines)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
