"""HAR-131 page calibration: score the CURRENT trial-decision page predictor.

Measures ``trial_decision.classify_loop_kind`` (the exact function the
generated pages use) against the frozen HAR-119 part-2 rater labels, with
exact numerator/denominator/abstentions. No tuning: the labels are
verified frozen before anything is read, and the classifier is imported
as-is. Writes ``page_scores.json`` next to this script.

Refuses to fabricate: a label that changed after the freeze aborts, and
missing trial directories report UNAVAILABLE instead of a score.

Usage (cwd = repo root):
  uv run python research/explorations/trace-lab/har119/score_page.py
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

import score as har119_score  # noqa: E402

FIELDS = ("loop_kind", "loop_present")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    har119_score.verify_freeze()
    selection = json.loads((HERE / "selection.json").read_text())
    runs = {row["trial"]: row for row in selection["runs"]}
    if len(runs) != 12:
        raise SystemExit(f"expected 12 selected runs, found {len(runs)}")

    from evallab.token_flow import analyze_token_flow
    from evallab.trial_decision import PAGE_CALIBRATION, classify_loop_kind

    missing = [t for t, row in runs.items() if not Path(row["trial_dir"]).is_dir()]
    if missing:
        print(f"UNAVAILABLE: {len(missing)} trial directories are missing; not scoring.")
        for trial in missing:
            print(f"  missing: {trial} ({runs[trial]['trial_dir']})")
        return 1

    raters = {
        rater: {
            trial: har119_score.rater_view(
                json.loads((HERE / "labels" / rater / f"{trial}.json").read_text())
            )
            for trial in runs
        }
        for rater in har119_score.RATERS
    }
    rater_a, rater_b = raters["rater_a"], raters["rater_b"]
    rewards = {trial: row["reward"] for trial, row in runs.items()}
    agreed = {
        (trial, field)
        for trial in runs
        for field in FIELDS
        if har119_score.match(field, rater_a[trial][field], rater_b[trial][field], rewards[trial])
        is True
    }

    loop_rule_rows = har119_score.loop_rule_rows()
    page_rows: dict[str, dict] = {}
    for trial, row in runs.items():
        trial_dir = Path(row["trial_dir"])
        token_flow = analyze_token_flow(trial_dir, trial_dir.parent)
        from evallab import probe03

        try:
            analysis = probe03.analyze_trial_core(trial_dir, trial_dir.parent)
            stop = analysis["stop_reason"]
        except Exception:  # noqa: BLE001 -- record, do not fail the cohort
            stop = None
        kind = classify_loop_kind(
            trial_dir, stop, token_flow if isinstance(token_flow, dict) else None
        )
        page_rows[trial] = {
            "loop_kind": kind["kind"],
            "loop_present": kind["kind"] != "none",
            "turns_after_prompt": kind["turns_after_prompt"],
            "claim_bearing_turns": kind["claim_bearing_turns"],
            "first_prompt_step": kind["first_prompt_step"],
            "loop_onset_step": kind["loop_onset_step"],
        }

    def score_vs_agreed(field: str) -> dict:
        hits = total = 0
        rows = []
        for trial in runs:
            if (trial, field) not in agreed:
                continue
            expected = rater_a[trial][field]
            got = page_rows[trial][field]
            ok = har119_score.match(field, expected, got, rewards[trial]) is True
            total += 1
            hits += ok
            rows.append({"trial": trial, "rater": expected, "page": got, "match": ok})
        return {"agree": hits, "n": total, "rows": rows}

    vs_agreed = {field: score_vs_agreed(field) for field in FIELDS}
    rule_hits = sum(
        1 for trial in runs if page_rows[trial]["loop_kind"] == loop_rule_rows[trial]["loop_kind"]
    )
    first_expressed = 0
    for trial, row in runs.items():
        from evallab import probe03

        try:
            analysis = probe03.analyze_trial_core(Path(row["trial_dir"]), Path(row["trial_dir"]).parent)
            if analysis["first_failure"] is not None:
                first_expressed += 1
        except Exception:  # noqa: BLE001
            pass

    manifest = (HERE / "labels" / "MANIFEST.sha256").read_text()
    payload = {
        "predictor": "trial_decision.classify_loop_kind (HAR-119 claim-vs-repetition rule)",
        "predictor_code_sha256": _sha256(REPO / "src" / "evallab" / "trial_decision.py"),
        "cohort": "HAR-119 part 2: 12 HAR-110 split-v2 runs",
        "in_sample": False,
        "frozen_at": (HERE / "labels" / "FROZEN_AT").read_text().splitlines()[0],
        "selection_sha256": _sha256(HERE / "selection.json"),
        "labels_manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
        "labels_manifest": manifest,
        "rater_agreement_loop_kind": har119_score.compare(rater_a, rater_b, rewards)["loop_kind"],
        "rater_agreement_loop_present": har119_score.compare(rater_a, rater_b, rewards)["loop_present"],
        "page_vs_agreed_loop_kind": vs_agreed["loop_kind"],
        "page_vs_agreed_loop_present": vs_agreed["loop_present"],
        "eligible_n": len({trial for trial, _ in agreed if _ == "loop_kind"}),
        "excluded_rater_disagreement": 12 - len({trial for trial, _ in agreed if _ == "loop_kind"}),
        "abstentions": 0,
        "page_vs_loop_rule_kind": {"agree": rule_hits, "n": 12},
        "page_first_failure_expressed": {"n": first_expressed, "of": 12},
        "generated_at": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
        "page_rows": page_rows,
    }
    (HERE / "page_scores.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")

    kind, present = vs_agreed["loop_kind"], vs_agreed["loop_present"]
    print("page loop_kind vs agreed:   "
          f"{kind['agree']}/{kind['n']} (eligible {payload['eligible_n']}, "
          f"{payload['excluded_rater_disagreement']} rater-disagreement excluded, 0 abstentions)")
    print(f"page loop_present vs agreed: {present['agree']}/{present['n']}")
    print(f"page vs frozen loop_rule:     {rule_hits}/12 kind agreement (same rule, live token_flow)")
    print(f"page first_failure expressed: {first_expressed}/12 (calibration unavailable)")
    for row in kind["rows"]:
        if not row["match"]:
            print(f"  miss: {row['trial']} rater={row['rater']} page={row['page']}")

    drift = []
    for key, got in (
        ("page_vs_agreed_loop_kind", kind),
        ("page_vs_agreed_loop_present", present),
    ):
        want = PAGE_CALIBRATION[key]
        if (got["agree"], got["n"]) != (want["agree"], want["n"]):
            drift.append(f"{key}: code says {want['agree']}/{want['n']}, measured {got['agree']}/{got['n']}")
    if drift:
        print("DRIFT: trial_decision.PAGE_CALIBRATION no longer matches this measurement:")
        for line in drift:
            print(f"  {line}")
        return 1
    print("MATCH: trial_decision.PAGE_CALIBRATION agrees with this measurement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
