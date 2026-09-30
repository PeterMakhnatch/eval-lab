"""Score report-run first-failure estimates against hand labels (HAR-111).

Builds the real ``report run`` output for 54 runs and scores both
``errors.first_error.step`` (baseline) and ``outcome.first_failure.step``
(new) against the labels, within +/-2 steps, with null == null a match:

- HAR-109: 10 hand-read HAR-104 runs. Labels are read from git at b6fada64
  (the frozen source), field ``first_failure_step``; runs come from the
  har104-runs worktree (``--har104-runs`` override).
- HAR-81: 44 probe-03 capability labels in-tree; ``first_failure.step_ref``
  is ``head#N`` (first trajectory segment) or ``trajectory.cont-1.json#N``
  (continuation segment), mapped to report ordinals through the same
  segment order the report uses.

Writes ``scores.md`` next to this script: the before/after table plus a
per-run hit/miss list for both fields.

Usage (from the eval-lab checkout)::

    uv run --no-sync python research/explorations/trace-lab/har111/first_failure/score.py
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HAR109_COMMIT = "b6fada64"
TOLERANCE = 2

HERE = Path(__file__).resolve().parent


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip())


def load_har109_labels(root: Path) -> dict[str, int | None]:
    files = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-r", HAR109_COMMIT, "--name-only"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    labels: dict[str, int | None] = {}
    for name in files:
        if "/har109/hand/" not in name or not name.endswith(".json"):
            continue
        raw = subprocess.run(
            ["git", "-C", str(root), "show", f"{HAR109_COMMIT}:{name}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        doc = json.loads(raw)
        labels[doc["trial_id"]] = doc.get("first_failure_step")
    return labels


_STEP_REF_RE = re.compile(
    r"^(?:head|trajectory\.json|(?:trajectory\.)?cont-(\d+)(?:\.json)?)#(\d+)$"
)


def map_step_ref(trial_dir: Path, ref: str) -> int | None:
    """Map a HAR-81 ``head#N`` / ``cont-1#N`` ref to a report step ordinal."""
    from evallab.interpretation import run_report as rr

    match = _STEP_REF_RE.match(ref)
    if match is None:
        raise ValueError(f"unrecognized step_ref: {ref!r}")
    segment, native_id = int(match.group(1) or 0), int(match.group(2))
    trial = trial_dir.resolve()
    _, traj_path, _ = rr.resolve_trial_target(trial, repo_root=trial, explicit_runs_root=trial)
    data = json.loads(Path(traj_path).read_text(encoding="utf-8"))
    chain = rr._resolve_chain_segments(Path(traj_path), data, trial)
    positions, _ = rr.stitched_chain_action_steps(chain.segments)
    layers = rr._positioned_layers(positions, rr._replay_parser())
    steps, _ = rr._build_steps(positions, layers)
    for step in steps:
        if step.segment == segment and step.native_step_id == native_id:
            return step.step
    raise ValueError(f"step_ref {ref!r} not found in {trial_dir}")


def load_har81_labels(root: Path) -> list[dict[str, object]]:
    path = (
        root
        / "research"
        / "explorations"
        / "trace-lab"
        / "probe-03-capabilities"
        / "har81"
        / "capabilities.jsonl"
    )
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def agree(predicted: int | None, hand: int | None) -> bool:
    if hand is None and predicted is None:
        return True
    if hand is None or predicted is None:
        return False
    return abs(predicted - hand) <= TOLERANCE


def main() -> int:
    from evallab.interpretation.run_report import build_run_report

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--har104-runs", default=None)
    args = parser.parse_args()
    root = repo_root()
    har104_runs = (
        Path(args.har104_runs) if args.har104_runs else root.parent / "har104-runs" / "runs"
    )
    # When run from inside a worktree, the sibling worktrees live one level up
    # from the worktree root; fall back to the main checkout layout.
    if not har104_runs.is_dir():
        alt = root / ".worktrees" / "har104-runs" / "runs"
        har104_runs = alt if alt.is_dir() else har104_runs

    rows: list[dict[str, object]] = []

    for trial, hand in sorted(load_har109_labels(root).items()):
        trial_dir = har104_runs / trial.split("__")[0] / trial
        try:
            report = build_run_report(trial_dir)
            baseline = (report["errors"]["first_error"] or {}).get("step")
            new = report["outcome"]["first_failure"]["step"]
            error: object = None
        except Exception as exc:  # keep scoring the rest; the row records it
            baseline, new, error = None, None, f"{type(exc).__name__}: {exc}"
        rows.append(
            {
                "set": "HAR-109",
                "trial": trial,
                "hand": hand,
                "first_error": baseline,
                "first_failure": new,
                "failure_kind": None
                if error is not None
                else report["outcome"]["first_failure"]["kind"],
                "error": error,
            }
        )

    for label in load_har81_labels(root):
        trial_dir = Path(str(label["trial_dir"]))
        ref = (label.get("first_failure") or {}).get("step_ref")
        try:
            hand = map_step_ref(trial_dir, str(ref)) if ref else None
            report = build_run_report(trial_dir)
            baseline = (report["errors"]["first_error"] or {}).get("step")
            new = report["outcome"]["first_failure"]["step"]
            error = None
        except Exception as exc:
            hand, baseline, new, error = None, None, None, f"{type(exc).__name__}: {exc}"
        rows.append(
            {
                "set": "HAR-81",
                "trial": str(label["trial"]),
                "hand": hand,
                "hand_ref": ref,
                "first_error": baseline,
                "first_failure": new,
                "failure_kind": None
                if error is not None
                else report["outcome"]["first_failure"]["kind"],
                "error": error,
            }
        )

    for row in rows:
        row["baseline_hit"] = row["error"] is None and agree(
            row["first_error"], row["hand"]  # type: ignore[arg-type]
        )
        row["new_hit"] = row["error"] is None and agree(
            row["first_failure"], row["hand"]  # type: ignore[arg-type]
        )

    def score(rows: list[dict[str, object]], field: str) -> tuple[int, int]:
        hits = sum(1 for row in rows if row[field])
        return hits, len(rows)

    r109 = [row for row in rows if row["set"] == "HAR-109"]
    r81 = [row for row in rows if row["set"] == "HAR-81"]
    b109, n109 = score(r109, "baseline_hit"), score(r109, "new_hit")
    b81, n81 = score(r81, "baseline_hit"), score(r81, "new_hit")

    lines = [
        "# HAR-111 first_failure scores",
        "",
        f"Method: real `report run` output per trial, ±{TOLERANCE} steps, "
        "null == null is a match. HAR-109 labels from git "
        f"{HAR109_COMMIT}; HAR-81 `step_ref` mapped to report ordinals "
        "through report segment order.",
        "",
        "|  | HAR-109 (10) | HAR-81 (44) |",
        "|---|---|---|",
        f"| first_error | {b109[0]}/{b109[1]} | {b81[0]}/{b81[1]} |",
        f"| first_failure (new) | {n109[0]}/{n109[1]} | {n81[0]}/{n81[1]} |",
        "",
    ]
    for label, subset in (("HAR-109", r109), ("HAR-81", r81)):
        lines += [f"## {label} per-run", ""]
        for row in subset:
            base = "hit" if row["baseline_hit"] else "miss"
            new = "hit" if row["new_hit"] else "miss"
            detail = (
                f"hand={row['hand']}"
                + (f" ({row['hand_ref']})" if row.get("hand_ref") else "")
                + f" first_error={row['first_error']} [{base}]"
                f" first_failure={row['failure_kind']}={row['first_failure']} [{new}]"
            )
            if row["error"] is not None:
                detail += f" ERROR {row['error']}"
            lines.append(f"- {row['trial']}: {detail}")
        lines.append("")
    (HERE / "scores.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"| first_error | {b109[0]}/{b109[1]} | {b81[0]}/{b81[1]} |")
    print(f"| first_failure (new) | {n109[0]}/{n109[1]} | {n81[0]}/{n81[1]} |")
    lost = [
        str(row["trial"])
        for row in rows
        if row["baseline_hit"] and not row["new_hit"] and row["error"] is None
    ]
    print(f"baseline hits lost: {lost if lost else 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
