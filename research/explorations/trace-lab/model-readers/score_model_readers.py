"""Score the model readers against the frozen HAR-119 labels.

Pure-function copy of ``../har119/score.py`` match logic (``step_of``,
``match``, ``compare``, ``rater_view``; har119/ itself is never edited)
plus two new tools: ``scout_llm`` (Scout trial_reader on glm-5.3) and
``docent_strong`` (Docent reading on anthropic/claude-opus-5-5).

Unlike the shipped tools, both readers express every field, so neither
has a NOT_EXPRESSED entry: ``null`` means a real prediction (pass,
loop-free run, failed run for pass_copied), exactly like the raters.

Usage: uv run python research/explorations/trace-lab/model-readers/score_model_readers.py
Reads labels + old predictions from har119/ (read-only) and the new
predictions from model-readers/predictions/. Writes scores/scores.json
and prints the comparison table.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HAR119 = HERE.parent / "har119"
LABELS = HAR119 / "labels"
OLD_PRED = HAR119 / "predictions"
PRED = HERE / "predictions"

RATERS = ("rater_a", "rater_b")
OLD_TOOLS = ("evallab", "scout", "docent", "loop_rule")
NEW_TOOLS = ("scout_llm", "docent_strong")
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
    "scout_llm": set(),
    "docent_strong": set(),
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


def tool_view(tool: str, row: dict) -> dict:
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


def load_pred(tool: str) -> dict[str, dict]:
    if tool == "loop_rule":
        rows = {}
        for row in map(
            json.loads, (HAR119 / "out" / "loop_kind.jsonl").read_text().splitlines()
        ):
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
    base = OLD_PRED if tool in OLD_TOOLS else PRED
    name = f"{tool}.jsonl"
    path = base / name
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
    selection = json.loads((HAR119 / "selection.json").read_text())
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
    for tool in OLD_TOOLS + NEW_TOOLS:
        raw = load_pred(tool)
        missing = [t for t in runs if t not in raw]
        if missing:
            print(f"{tool}: missing trials {missing}", file=sys.stderr)
            return 2
        views = {t: tool_view(tool, raw[t]) for t in runs}
        scores[f"{tool}_vs_rater_a"] = compare(a, views, rewards)
        scores[f"{tool}_vs_rater_b"] = compare(b, views, rewards)
        scores[f"{tool}_vs_agreed"] = compare(a, views, rewards, only=agreed)
    out = HERE / "scores" / "scores.json"
    out.write_text(json.dumps(scores, indent=2, default=str) + "\n")

    lines = [
        "# Model readers vs frozen HAR-119 labels (out of sample, 12 HAR-110 v2 runs)",
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
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
