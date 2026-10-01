"""HAR-128 part 2: rater vs rater and tool vs raters on frozen labels, with 95% Wilson CIs.

Usage: python3 score.py <labels_dir> <predictions_dir> [--trials-from-results]
Writes <predictions_dir>/../scores_<labels_dir name>.{json,md}.

Match rules are HAR-119's (`../har119/score.py`), unchanged:
- exact match for stop_reason, blame, loop_kind, pass_copied (passes only);
- first_failure: both null, or both steps within 2;
- loop_onset: within 5 steps when both see a loop.

loop_present is derived (rater loop_kind != none; a tool's loop span present). A field a tool does not express
is reported as not expressed, never right or wrong. Refuses to run if any label differs from its MANIFEST.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from pathlib import Path

RATERS = ("rater_a", "rater_b")
FIELDS = (
    "stop_reason",
    "first_failure",
    "blame",
    "loop_present",
    "loop_kind",
    "loop_onset",
    "pass_copied",
)
STEP_TOLERANCE = 2
LOOP_TOLERANCE = 5
NE = "not expressed"
NOT_EXPRESSED_BY = {
    "evallab": {"blame", "loop_kind"},
    "scout": {"loop_kind", "pass_copied"},
    "docent_opus": set(),
}


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    z = 1.96
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def verify_freeze(labels: Path) -> None:
    for line in (labels / "MANIFEST.sha256").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        if hashlib.sha256((labels / name).read_bytes()).hexdigest() != digest:
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


def tool_view(tool: str, row: dict) -> dict:
    span = row.get("loop_span")
    step = row.get("first_failure_step")
    view = {
        "stop_reason": row.get("stop_reason") or NE,
        "first_failure": step if isinstance(step, int) else step_of(row.get("first_failure_ref")),
        "blame": row.get("blame") or NE,
        "loop_present": bool(span),
        "loop_kind": row.get("loop_kind") or NE,
        "loop_onset": step_of(span[0]) if span else None,
        "pass_copied": row.get("pass_copied"),
    }
    for field in NOT_EXPRESSED_BY.get(tool, set()):
        view[field] = NE
    return view


def match(field: str, a, b, passed: bool) -> bool | None:
    if field == "pass_copied" and not passed:
        return None
    if field == "first_failure":
        if a is None or b is None:
            return a is None and b is None
        return abs(a - b) <= STEP_TOLERANCE
    if field == "loop_onset":
        return None if a is None or b is None else abs(a - b) <= LOOP_TOLERANCE
    return a == b


def compare(left: dict, right: dict, passed: dict, only=None) -> dict:
    out = {}
    for field in FIELDS:
        hits = total = unexpressed = 0
        rows = []
        for trial in left:
            if only is not None and (trial, field) not in only:
                continue
            a, b = left[trial][field], right[trial][field]
            if b == NE:
                unexpressed += 1
                continue
            ok = match(field, a, b, passed[trial])
            if ok is None:
                continue
            total += 1
            hits += ok
            rows.append({"trial": trial, "rater": a, "other": b, "match": ok})
        lo, hi = wilson(hits, total)
        out[field] = {
            "agree": hits,
            "n": total,
            "ci95": [lo, hi],
            "not_expressed": unexpressed,
            "rows": rows,
        }
    return out


def main(argv: list[str]) -> int:
    labels = Path(argv[1]).resolve()
    preds = Path(argv[2]).resolve()
    verify_freeze(labels)
    trials = sorted(p.stem for p in (labels / "rater_a").glob("*.json"))
    raters = {
        r: {t: json.loads((labels / r / f"{t}.json").read_text()) for t in trials} for r in RATERS
    }
    # A run passed if either rater gave it pass_copied true/false (both raters label passes only).
    passed = {
        t: raters["rater_a"][t]["pass_copied"] is not None
        or raters["rater_b"][t]["pass_copied"] is not None
        for t in trials
    }
    a = {t: rater_view(raters["rater_a"][t]) for t in trials}
    b = {t: rater_view(raters["rater_b"][t]) for t in trials}
    agreed = {
        (t, f) for t in trials for f in FIELDS if match(f, a[t][f], b[t][f], passed[t]) is True
    }
    scores = {"rater_a_vs_rater_b": compare(a, b, passed)}
    for path in sorted(preds.glob("*.jsonl")):
        tool = path.stem
        rows = {r["trial"]: r for r in map(json.loads, path.read_text().splitlines()) if r}
        missing = [t for t in trials if t not in rows]
        if missing:
            raise SystemExit(f"{tool}: {len(missing)} trials missing")
        views = {t: tool_view(tool, rows[t]) for t in trials}
        scores[f"{tool}_vs_agreed"] = compare(a, views, passed, only=agreed)
    stem = f"scores_{labels.name}"
    (preds.parent / f"{stem}.json").write_text(json.dumps(scores, indent=2, default=str) + "\n")
    lines = [
        f"# HAR-128 part 2 scores: {labels.name} ({len(trials)} runs)",
        "",
        f"Cell = agree/n [95% Wilson CI]. first_failure within ±{STEP_TOLERANCE} steps (both null agrees); "
        f"loop onset within ±{LOOP_TOLERANCE}; pass_copied on passes only. Tool rows score only cells where "
        "both raters agree. `—` = not expressed.",
        "",
        "| comparison | " + " | ".join(FIELDS) + " |",
        "|---|" + "---|" * len(FIELDS),
    ]
    for name, fields in scores.items():
        cells = []
        for field in FIELDS:
            c = fields[field]
            if c["n"] == 0:
                cells.append("—" if c["not_expressed"] else "0/0")
            else:
                lo, hi = c["ci95"]
                cells.append(f"{c['agree']}/{c['n']} [{lo:.2f}–{hi:.2f}]")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    (preds.parent / f"{stem}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
