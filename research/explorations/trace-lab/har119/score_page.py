"""HAR-131 page calibration: score the CURRENT trial-decision page predictor.

Measures ``trial_decision.classify_loop_kind`` (the exact function the
generated pages use) plus the page's actual first-failure and blame fields
against the frozen HAR-119 part-2 rater labels, with exact
numerator/denominator/abstentions. No tuning: the labels are verified
frozen before anything is read, the classifier runs as-is, and rater
semantics come from the frozen ``score.py`` match rules (first failure
within +/-2 steps, exact blame). A page field that is null is reported as
an abstention with explicit coverage, never silently dropped. Writes
``page_scores.json`` next to this script.

Predictor identity is the sha256 of the three function sources that decide
the page loop kind (``classify_loop_kind``, ``_claim_bearing``,
``token_flow._loop_onset``) -- never the whole module, which also carries
the embedded calibration constants.

Refuses to fabricate: a label that changed after the freeze aborts, and
missing trial directories report UNAVAILABLE instead of a score.

Usage (cwd = repo root):
  uv run python research/explorations/trace-lab/har119/score_page.py
"""
from __future__ import annotations

import datetime as _datetime
import hashlib
import inspect
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
    # First failure and blame use the same frozen scorer semantics
    # (within +/-2 steps; exact blame). A null page field is scorer
    # semantics below, with coverage reported separately.
    agreed_ff = {
        trial
        for trial in runs
        if har119_score.match(
            "first_failure", rater_a[trial]["first_failure"], rater_b[trial]["first_failure"],
            rewards[trial],
        )
        is True
    }
    agreed_bl = {
        trial
        for trial in runs
        if har119_score.match("blame", rater_a[trial]["blame"], rater_b[trial]["blame"], rewards[trial])
        is True
    }

    loop_rule_rows = har119_score.loop_rule_rows()
    # Page blame uses the frozen literal map (MAPPING.md scout rule):
    # model/harness pass through, R-NONE-01 n/a reads as none (earned
    # pass), anything else abstains because the raters have no such value.
    BLAME_MAP = {"model": "model", "harness": "harness", "n/a": "none"}
    page_rows: dict[str, dict] = {}
    for trial, row in runs.items():
        trial_dir = Path(row["trial_dir"])
        token_flow = analyze_token_flow(trial_dir, trial_dir.parent)
        from evallab import probe03

        try:
            analysis = probe03.analyze_trial_core(trial_dir, trial_dir.parent)
            stop = analysis["stop_reason"]
        except Exception:  # noqa: BLE001 -- record, do not fail the cohort
            analysis, stop = None, None
        kind = classify_loop_kind(
            trial_dir, stop, token_flow if isinstance(token_flow, dict) else None
        )
        first = (analysis or {}).get("first_failure") if analysis else None
        first_step = har119_score.step_of((first or {}).get("step_ref")) if first else None
        outcome = (analysis or {}).get("outcome_failure") if analysis else None
        attribution = (outcome or {}).get("attribution") if outcome else None
        page_rows[trial] = {
            "loop_kind": kind["kind"],
            "loop_present": kind["kind"] != "none",
            "turns_after_prompt": kind["turns_after_prompt"],
            "claim_bearing_turns": kind["claim_bearing_turns"],
            "first_prompt_step": kind["first_prompt_step"],
            "loop_onset_step": kind["loop_onset_step"],
            "first_failure": first_step,
            "blame": BLAME_MAP.get(attribution, har119_score.NE),
            "raw_attribution": attribution,
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

    def score_first_failure() -> dict:
        hits = total = 0
        rows = []
        for trial in runs:
            if trial not in agreed_ff:
                continue
            expected = rater_a[trial]["first_failure"]
            got = page_rows[trial]["first_failure"]
            ok = har119_score.match("first_failure", expected, got, rewards[trial]) is True
            total += 1
            hits += ok
            rows.append({"trial": trial, "rater": expected, "page": got, "match": ok})
        return {"agree": hits, "n": total, "rows": rows}

    def score_blame() -> dict:
        hits = total = abstained = 0
        rows = []
        for trial in runs:
            if trial not in agreed_bl:
                continue
            got = page_rows[trial]["blame"]
            if got == har119_score.NE:
                abstained += 1
                continue
            expected = rater_a[trial]["blame"]
            ok = har119_score.match("blame", expected, got, rewards[trial]) is True
            total += 1
            hits += ok
            rows.append(
                {
                    "trial": trial,
                    "rater": expected,
                    "page": got,
                    "raw_attribution": page_rows[trial]["raw_attribution"],
                    "match": ok,
                }
            )
        return {"agree": hits, "n": total, "abstained": abstained, "rows": rows}

    ff = score_first_failure()
    blame = score_blame()
    ff_expressed = sum(1 for trial in runs if page_rows[trial]["first_failure"] is not None)
    ff_abstentions = len(runs) - ff_expressed

    from evallab import token_flow as _token_flow_mod
    from evallab import trial_decision as _trial_decision_mod

    function_names = [
        "trial_decision.classify_loop_kind",
        "trial_decision._claim_bearing",
        "token_flow._loop_onset",
    ]
    function_sources = [
        inspect.getsource(_trial_decision_mod.classify_loop_kind),
        inspect.getsource(_trial_decision_mod._claim_bearing),
        inspect.getsource(_token_flow_mod._loop_onset),
    ]
    functions_sha256 = hashlib.sha256("".join(function_sources).encode()).hexdigest()

    rater_scores = har119_score.compare(rater_a, rater_b, rewards)
    manifest = (HERE / "labels" / "MANIFEST.sha256").read_text()
    payload = {
        "predictor": "trial_decision.classify_loop_kind (HAR-119 claim-vs-repetition rule)",
        "predictor_functions": {"names": function_names, "sha256": functions_sha256},
        "cohort": "HAR-119 part 2: 12 HAR-110 split-v2 runs",
        "in_sample": False,
        "frozen_at": (HERE / "labels" / "FROZEN_AT").read_text().splitlines()[0],
        "selection_sha256": _sha256(HERE / "selection.json"),
        "labels_manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
        "labels_manifest": manifest,
        "rater_agreement_loop_kind": rater_scores["loop_kind"],
        "rater_agreement_loop_present": rater_scores["loop_present"],
        "rater_agreement_first_failure": rater_scores["first_failure"],
        "rater_agreement_blame": rater_scores["blame"],
        "page_vs_agreed_loop_kind": vs_agreed["loop_kind"],
        "page_vs_agreed_loop_present": vs_agreed["loop_present"],
        "page_vs_agreed_first_failure": ff,
        "first_failure_coverage": {
            "expressed": ff_expressed,
            "of": len(runs),
            "abstentions": ff_abstentions,
        },
        "page_vs_agreed_blame": blame,
        "blame_abstentions": blame["abstained"],
        "eligible_n": len({trial for trial, _ in agreed if _ == "loop_kind"}),
        "excluded_rater_disagreement": 12 - len({trial for trial, _ in agreed if _ == "loop_kind"}),
        "abstentions": 0,
        "page_vs_loop_rule_kind": {"agree": rule_hits, "n": 12},
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
    print(f"page first_failure vs agreed: {ff['agree']}/{ff['n']} "
          f"(coverage {ff_expressed}/12, {ff_abstentions} abstentions)")
    print(f"page blame vs agreed:          {blame['agree']}/{blame['n']} "
          f"({blame['abstained']} abstentions; raters near-constant model: uninformative)")
    print(f"predictor functions sha256:    {functions_sha256[:16]}... ({', '.join(function_names)})")
    for row in kind["rows"]:
        if not row["match"]:
            print(f"  loop miss: {row['trial']} rater={row['rater']} page={row['page']}")
    for row in ff["rows"]:
        if not row["match"]:
            print(f"  first-failure miss: {row['trial']} rater={row['rater']} page={row['page']}")
    for row in blame["rows"]:
        if not row["match"]:
            print(f"  blame miss: {row['trial']} rater={row['rater']} page={row['page']}")

    drift = []
    for key, got in (
        ("page_vs_agreed_loop_kind", kind),
        ("page_vs_agreed_loop_present", present),
        ("page_vs_agreed_first_failure", ff),
        ("page_vs_agreed_blame", blame),
    ):
        want = PAGE_CALIBRATION[key]
        if (got["agree"], got["n"]) != (want["agree"], want["n"]):
            drift.append(f"{key}: code says {want['agree']}/{want['n']}, measured {got['agree']}/{got['n']}")
    for key, got in (
        ("first_failure_coverage", payload["first_failure_coverage"]),
        ("blame_abstentions", payload["blame_abstentions"]),
        ("predictor_functions", payload["predictor_functions"]),
    ):
        want = PAGE_CALIBRATION[key]
        if key == "predictor_functions":
            if got["sha256"] != want["sha256"]:
                drift.append("predictor_functions: page loop rule source changed; recalibrate")
        elif got != want:
            drift.append(f"{key}: code says {want!r}, measured {got!r}")
    if drift:
        print("DRIFT: trial_decision.PAGE_CALIBRATION no longer matches this measurement:")
        for line in drift:
            print(f"  {line}")
        return 1
    print("MATCH: trial_decision.PAGE_CALIBRATION agrees with this measurement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
