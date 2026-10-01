"""HAR-119 part 2: score rater vs rater and tool vs raters on the frozen labels.

Usage: python3 score.py   (reads labels/, predictions/*.jsonl, out/loop_kind.jsonl;
writes scores.json and scores.md)

Refuses to run if any label file differs from labels/MANIFEST.sha256.

Match rules (fixed before any label or prediction was read):
- stop_reason, blame, loop_kind, pass_copied: exact match. pass_copied is
  scored on passes only (failed runs are null by definition).
- first_failure: both null, or both steps within STEP_TOLERANCE. All 12 runs
  have a single trajectory.json, so `head#N` is step N.
- loop_onset: when both see a loop, span starts within LOOP_TOLERANCE steps.
A field a tool does not express (per predictions/MAPPING.md) is reported as
not expressed, never as right or wrong.

Adapters (written by the parent after the label freeze and before any
tool-vs-label comparison was computed; unit conversions, no thresholds):
- evallab first_failure: `outcome.first_failure.step` is a stitched ordinal.
  On all 12 runs the chain has one segment and ordinal == native step_id
  (checked with run_report `_resolve_chain_segments` + `_build_steps`), so
  it maps to step N; `first_failure: null` ("none found") stays null.
- docent first_failure: the first citation of `first_mistake_evidence` is
  mapped block -> step by `predictions/docent_block_map.json` (the upload's
  own offline conversion; `first_failure_step` in docent.jsonl).
- loop_rule: Part 1's `loop_kind.py` (rules frozen 2026-09-30 ~23:33Z,
  before the 12 runs were picked) scored as a fourth tool for the loop
  fields; onset = first confirm prompt + 1 (completion-claim) or HAR-114's
  onset (repetition).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
LABELS = HERE / "labels"
PRED = HERE / "predictions"
RATERS = ("rater_a", "rater_b")
TOOLS = ("evallab", "scout", "docent", "loop_rule")
FIELDS = (
    "stop_reason",
    "first_failure",
    "blame",
    "loop_present",
    "loop_kind",
    "loop_onset",
    "pass_copied",
)
NOT_EXPRESSED_BY = {
    "evallab": {"blame", "loop_kind"},
    "scout": {"loop_kind", "pass_copied"},
    "docent": {"stop_reason", "loop_present", "loop_kind", "loop_onset", "pass_copied"},
    "loop_rule": {"stop_reason", "first_failure", "blame", "pass_copied"},
}
STEP_TOLERANCE = 2
LOOP_TOLERANCE = 5
NE = "not expressed"


def verify_freeze() -> None:
    for line in (LABELS / "MANIFEST.sha256").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        if hashlib.sha256((LABELS / name).read_bytes()).hexdigest() != digest:
            raise SystemExit(f"label {name} changed after the freeze")


def step_of(ref) -> int | None:
    if ref is None:
        return None
    if isinstance(ref, dict):
        ref = ref.get("ref")
    if isinstance(ref, int):
        return ref
    match = re.search(r"#(\d+)", str(ref)) or re.fullmatch(r"\s*(\d+)\s*", str(ref))
    return int(match.group(1)) if match else None


def rater_view(row: dict) -> dict:
    span = row.get("loop_span")
    return {
        "stop_reason": row["stop_reason"],
        "first_failure": step_of(row["first_failure"]),
        "blame": row["blame"],
        "loop_present": row["loop_kind"] != "none",
        "loop_kind": row["loop_kind"],
        "loop_onset": step_of(span[0]) if span else None,
        "pass_copied": row["pass_copied"],
    }


def tool_view(tool: str, row: dict, trial_dir: Path) -> dict:
    span = row.get("loop_span")
    view = {
        "stop_reason": row.get("stop_reason"),
        "first_failure": step_of(row.get("first_failure_ref")),
        "blame": row.get("blame"),
        "loop_present": bool(span),
        "loop_kind": row.get("loop_kind"),
        "loop_onset": step_of(span[0]) if span else None,
        "pass_copied": row.get("pass_copied"),
    }
    if tool == "evallab":
        step = row.get("raw_first_failure_step")
        view["first_failure"] = step if isinstance(step, int) else None
    if tool == "docent":
        step = row.get("first_failure_step")
        view["first_failure"] = step if isinstance(step, int) else NE
        if row.get("blame") is None:
            view["blame"] = NE  # `unclear`
    if tool == "scout" and row.get("blame") is None:
        view["blame"] = NE  # `unclear`
    for field in NOT_EXPRESSED_BY[tool]:
        view[field] = NE
    return view


def loop_rule_rows() -> dict[str, dict]:
    rows = {}
    for row in map(json.loads, (HERE / "out" / "loop_kind.jsonl").read_text().splitlines()):
        kind = row["loop_kind"]
        onset = None
        if kind == "completion-claim":
            onset = row["first_confirm_prompt_step"] + 1
        elif kind == "repetition":
            onset = row["har114_loop_onset_step"]
        rows[row["trial"]] = {
            "loop_kind": kind,
            "loop_span": [f"head#{onset}", None] if onset is not None else None,
        }
    return rows


def load_pred(tool: str) -> dict[str, dict]:
    if tool == "loop_rule":
        return loop_rule_rows()
    path = PRED / f"{tool}.jsonl"
    return {r["trial"]: r for r in map(json.loads, path.read_text().splitlines()) if r}


def match(field: str, a, b, reward: float) -> bool | None:
    if field == "pass_copied" and reward != 1.0:
        return None
    if field == "first_failure":
        if a is None or b is None:
            return a is None and b is None
        return abs(a - b) <= STEP_TOLERANCE
    if field == "loop_onset":
        return None if a is None or b is None else abs(a - b) <= LOOP_TOLERANCE
    return a == b


def compare(left: dict, right: dict, rewards: dict, only=None) -> dict:
    result = {}
    for field in FIELDS:
        hits = total = unexpressed = 0
        rows = []
        for trial, reward in rewards.items():
            if only is not None and (trial, field) not in only:
                continue
            a, b = left[trial][field], right[trial][field]
            if b == NE:
                unexpressed += 1
                continue
            ok = match(field, a, b, reward)
            if ok is None:
                continue
            total += 1
            hits += ok
            rows.append({"trial": trial, "rater": a, "other": b, "match": ok})
        result[field] = {"agree": hits, "n": total, "not_expressed": unexpressed, "rows": rows}
    return result


def main() -> int:
    verify_freeze()
    selection = json.loads((HERE / "selection.json").read_text())
    runs = {r["trial"]: r for r in selection["runs"]}
    rewards = {t: r["reward"] for t, r in runs.items()}
    raters = {
        rater: {t: rater_view(json.loads((LABELS / rater / f"{t}.json").read_text())) for t in runs}
        for rater in RATERS
    }
    a, b = raters["rater_a"], raters["rater_b"]
    agreed = {
        (t, f) for t in runs for f in FIELDS if match(f, a[t][f], b[t][f], rewards[t]) is True
    }
    scores = {"rater_a_vs_rater_b": compare(a, b, rewards)}
    for tool in TOOLS:
        raw = load_pred(tool)
        views = {t: tool_view(tool, raw[t], Path(runs[t]["trial_dir"])) for t in runs}
        scores[f"{tool}_vs_rater_a"] = compare(a, views, rewards)
        scores[f"{tool}_vs_rater_b"] = compare(b, views, rewards)
        scores[f"{tool}_vs_agreed"] = compare(a, views, rewards, only=agreed)
    (HERE / "scores.json").write_text(json.dumps(scores, indent=2, default=str) + "\n")

    lines = [
        "# HAR-119 part 2 scores (out of sample, 12 HAR-110 v2 runs)",
        "",
        f"first_failure: within ±{STEP_TOLERANCE} steps (both null counts as agreement). "
        f"loop onset: within ±{LOOP_TOLERANCE} steps when both see a loop. "
        "pass_copied: passes only (2 runs). `—` = the tool does not express the field. "
        "`_vs_agreed` scores only the run×field cells where the two raters agree.",
        "",
        "| comparison | " + " | ".join(FIELDS) + " |",
        "|---|" + "---|" * len(FIELDS),
    ]
    for name, fields in scores.items():
        cells = []
        for field in FIELDS:
            cell = fields[field]
            if cell["n"] == 0 and cell["not_expressed"]:
                cells.append("—")
            else:
                extra = f" ({cell['not_expressed']} n/e)" if cell["not_expressed"] else ""
                cells.append(f"{cell['agree']}/{cell['n']}{extra}")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    (HERE / "scores.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
