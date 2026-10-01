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

Published-label cohort mode scores already-published ``trial_decision/v3``
pages (their ``decision.judgments.loop_kind.kind`` field, never a
reconstructed prediction) against a second frozen label root. The freeze
(expected file count and hashes) and the unique trial IDs are verified
before any page is read. A missing report or an unknown page kind is an
explicit abstention, never ``none`` and never silently dropped; a corrupt
label or report aborts. Writes a scores-and-hashes-only receipt, never
trial content, so the cohort cannot become a training export.

Usage (cwd = repo root):
  uv run python research/explorations/trace-lab/har119/score_page.py
  uv run python research/explorations/trace-lab/har119/score_page.py \\
    --published-cohort --results-home ~/Developer/eval-lab-results
  uv run python research/explorations/trace-lab/har119/score_page.py \\
    --published-cohort --cohort har128-g2-a1 --results-home ~/Developer/eval-lab-results

``--cohort`` selects a named published study (``har128-har116`` by default,
``har128-g2-a1`` for G2 attempt 1, ``har128-g2-r2`` for the first re-run
freeze, ``har128-g2-tail`` for the last three re-runs); every cohort shares
the same strict verification/scoring path with its own denominator.
"""
from __future__ import annotations

import argparse
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


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.published_cohort or args.cohort or args.labels or args.results_home or args.output:
        return _main_published_cohort(args)
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
        "generated_at": _datetime.datetime.now(_datetime.UTC).isoformat(),
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

COHORT_SCHEMA = "har131.page_loop_calibration/v1"
PUBLISHED_LOOP_KINDS = ("none", "repetition", "completion-claim")
HAR128_COHORT = "HAR-128 part 2: 40 HAR-116 trials"
HAR128_MANIFEST_SHA256 = "24b91001adf5707ebb65756e575a9acbc39a816173ee16a5da88cf989e99513d"
HAR128_LABELS_DEFAULT = REPO / "research/explorations/trace-lab/har128/labels_har116"
HAR128_OUTPUT_DEFAULT = REPO / "research/experiments/har117-results-home/har131-page-calibration-har116.json"
HAR128_EXPECTED_FILES = 80
HAR128_EXPECTED_TRIALS = 40
HAR128_HELDOUT = (
    "har116 001181 baseline/loopfix/loopfix-r2 (3 trials): analysis and "
    "calibration only, never training reflection"
)
HAR128_LIMIT = (
    "Actual published trial_decision/v3 predictions, no classifier tuning. Frozen blind "
    "scout-agent annotations, not human ground truth; rater A on "
    "har116-a-000383-baseline__igrXg8R reports off_limits_opened. "
    + HAR128_HELDOUT
    + "; do not export this cohort to training reflection. "
    "Scores and report hashes only, not trial content."
)
G2_COHORT = "HAR-128 G2 attempt 1: 20 HAR-120 trials"
G2_MANIFEST_SHA256 = "f6a11da4b3994c101785742f27565d169469e062764585a210baeccfb5b92674"
G2_LABELS_DEFAULT = REPO / "research/explorations/trace-lab/har128/labels_g2_a1"
G2_OUTPUT_DEFAULT = REPO / "research/experiments/har117-results-home/har131-page-calibration-g2-a1.json"
G2_EXPECTED_FILES = 40
G2_EXPECTED_TRIALS = 20
G2_HELDOUT = (
    "G2 attempt-1 frozen cohort only: analysis and "
    "calibration only, never training reflection"
)
G2_LIMIT = (
    "Actual published trial_decision/v3 predictions, no classifier tuning. Frozen blind "
    "scout-agent annotations A (G2A1-4) / B (G2B1-4), not human ground truth; "
    "no reported off-limits openings. Loop kind only: this cohort carries no "
    "first-failure/blame calibration. Denominators stay per-cohort, never pooled. "
    "Scores and report hashes only, not trial content; inspection only, "
    "never training reflection."
)
G2R2_COHORT = "HAR-128 G2 re-run: 19 HAR-120 trials"
G2R2_MANIFEST_SHA256 = "ddc1f2ad8bf52dc762067a367f7383b990164174b1732c108fb788167597f2ff"
G2R2_LABELS_DEFAULT = REPO / "research/explorations/trace-lab/har128/labels_g2_r2"
G2R2_OUTPUT_DEFAULT = REPO / "research/experiments/har117-results-home/har131-page-calibration-g2-r2.json"
G2R2_EXPECTED_FILES = 38
G2R2_EXPECTED_TRIALS = 19
G2R2_HELDOUT = (
    "G2 re-run frozen cohort only: analysis and "
    "calibration only, never training reflection"
)
G2R2_LIMIT = (
    "Actual published trial_decision/v3 predictions, no classifier tuning. Frozen blind "
    "scout-agent annotations A (G3A1-4) / B (G3B1-4), not human ground truth; "
    "no reported off-limits openings. Loop kind only: this cohort carries no "
    "first-failure/blame calibration. Denominators stay per-cohort, never pooled. "
    "Scores and report hashes only, not trial content; inspection only, "
    "never training reflection."
)
G2TAIL_COHORT = "HAR-128 G2 tail: 3 HAR-120 trials"
G2TAIL_MANIFEST_SHA256 = "3b88eb4c2450d76f4b58533c12fae7a0ed25f0816bb45d1b3baeb72a54cdbb50"
G2TAIL_LABELS_DEFAULT = REPO / "research/explorations/trace-lab/har128/labels_g2_tail"
G2TAIL_OUTPUT_DEFAULT = REPO / "research/experiments/har117-results-home/har131-page-calibration-g2-tail.json"
G2TAIL_EXPECTED_FILES = 6
G2TAIL_EXPECTED_TRIALS = 3
G2TAIL_HELDOUT = "G2 tail freeze: analysis and calibration only, never training reflection"
G2TAIL_LIMIT = (
    "Actual published trial_decision/v3 predictions, no classifier tuning. Frozen blind "
    "scout-agent raters G4A/G4B, not human ground truth; neither reports off-limits openings. "
    "Loop kind only; no first-failure/blame calibration. Tiny cohort: three trials, "
    "all agreed labels completion-claim. Denominators stay per-cohort, never pooled. "
    "Scores and report hashes only, not trial content; never training reflection."
)
#: Named published cohorts sharing one strict verification/scoring path.
#: Each entry stands alone with its own denominator: never pooled, and no
#: entry borrows other-field numbers it did not measure.
PUBLISHED_COHORTS = {
    "har128-har116": {
        "cohort": HAR128_COHORT,
        "labels": HAR128_LABELS_DEFAULT,
        "output": HAR128_OUTPUT_DEFAULT,
        "expected_files": HAR128_EXPECTED_FILES,
        "expected_trials": HAR128_EXPECTED_TRIALS,
        "expected_manifest_sha256": HAR128_MANIFEST_SHA256,
        "heldout": HAR128_HELDOUT,
        "limit": HAR128_LIMIT,
    },
    "har128-g2-a1": {
        "cohort": G2_COHORT,
        "labels": G2_LABELS_DEFAULT,
        "output": G2_OUTPUT_DEFAULT,
        "expected_files": G2_EXPECTED_FILES,
        "expected_trials": G2_EXPECTED_TRIALS,
        "expected_manifest_sha256": G2_MANIFEST_SHA256,
        "heldout": G2_HELDOUT,
        "limit": G2_LIMIT,
    },
    "har128-g2-r2": {
        "cohort": G2R2_COHORT,
        "labels": G2R2_LABELS_DEFAULT,
        "output": G2R2_OUTPUT_DEFAULT,
        "expected_files": G2R2_EXPECTED_FILES,
        "expected_trials": G2R2_EXPECTED_TRIALS,
        "expected_manifest_sha256": G2R2_MANIFEST_SHA256,
        "heldout": G2R2_HELDOUT,
        "limit": G2R2_LIMIT,
    },
    "har128-g2-tail": {
        "cohort": G2TAIL_COHORT,
        "labels": G2TAIL_LABELS_DEFAULT,
        "output": G2TAIL_OUTPUT_DEFAULT,
        "expected_files": G2TAIL_EXPECTED_FILES,
        "expected_trials": G2TAIL_EXPECTED_TRIALS,
        "expected_manifest_sha256": G2TAIL_MANIFEST_SHA256,
        "heldout": G2TAIL_HELDOUT,
        "limit": G2TAIL_LIMIT,
    },
}
PUBLISHED_COHORT_DEFAULT = "har128-har116"


def resolve_published_cohort(name: str | None) -> dict:
    """The named published cohort description; defaults to the HAR-116 study."""
    key = name or PUBLISHED_COHORT_DEFAULT
    try:
        spec = PUBLISHED_COHORTS[key]
    except KeyError:
        valid = ", ".join(sorted(PUBLISHED_COHORTS))
        raise SystemExit(f"unknown published cohort {key!r}; expected one of: {valid}") from None
    return {"name": key, **spec}


def _parse_args(argv: list[str] | None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--published-cohort", action="store_true",
                        help="score published trial_decision/v3 pages against a frozen label root")
    parser.add_argument("--cohort", default=None, choices=sorted(PUBLISHED_COHORTS),
                        help="named published cohort (default: har128-har116)")
    parser.add_argument("--labels", default=None,
                        help="frozen label root (default: the selected cohort's freeze)")
    parser.add_argument("--results-home", default=None,
                        help="published results home to scan for trial reports (required in cohort mode)")
    parser.add_argument("--output", default=None,
                        help="receipt path (default: the selected cohort's har117-results-home receipt)")
    parser.add_argument("--expected-files", type=int, default=None)
    parser.add_argument("--expected-trials", type=int, default=None)
    parser.add_argument("--expected-manifest-sha256", default=None,
                        help="reject the label freeze before scoring unless its manifest matches")
    return parser.parse_args(argv)


def _canonical_member(name: str) -> str:
    """A manifest path must stay in-root and be shaped rater_[ab]/<trial>.json."""
    from pathlib import PurePosixPath

    member = PurePosixPath(name)
    if member.is_absolute() or ".." in member.parts or len(member.parts) != 2:
        raise SystemExit(f"manifest declares out-of-root path {name!r}; refusing to score")
    rater, filename = member.parts
    if rater not in ("rater_a", "rater_b") or not filename.endswith(".json"):
        raise SystemExit(f"manifest declares unexpected member {name!r}; refusing to score")
    return f"{rater}/{filename}"


def manifest_label_paths(manifest: str) -> list[str]:
    """Unique canonical rater files the freeze declares; aborts on dupes or escapes."""
    members = []
    for line in manifest.splitlines():
        if not line.strip():
            continue
        if len(line.split()) != 2:
            raise SystemExit("manifest has a malformed line; refusing to score")
        members.append(_canonical_member(line.split(maxsplit=1)[1]))
    if len(set(members)) != len(members):
        raise SystemExit("manifest declares a label file twice; refusing to score")
    return sorted(members)


def verify_published_freeze(labels_root: Path, expected_files: int) -> str:
    """Verify the published cohort's frozen labels; abort on anything unexpected."""
    manifest_path = labels_root / "MANIFEST.sha256"
    if not manifest_path.is_file():
        raise SystemExit(f"no MANIFEST.sha256 in {labels_root}")
    manifest = manifest_path.read_text()
    members = manifest_label_paths(manifest)
    if len(members) != expected_files:
        raise SystemExit(f"expected {expected_files} frozen label files, manifest lists {len(members)}")
    digests = {}
    for line in manifest.splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            digests[_canonical_member(name)] = digest
    for name in members:
        target = (labels_root / name).resolve()
        if not target.is_relative_to(labels_root.resolve()):
            raise SystemExit(f"label {name} escapes the frozen root")
        if not target.is_file():
            raise SystemExit(f"label {name} listed in the freeze is missing")
        if hashlib.sha256(target.read_bytes()).hexdigest() != digests[name]:
            raise SystemExit(f"label {name} changed after the freeze")
    return manifest


def published_trial_ids(
    labels_root: Path, expected_trials: int, members: list[str] | None = None
) -> list[str]:
    """Unique trial IDs shared by both raters; abort on mismatch.

    When the manifest's declared members are given, they must be exactly the
    rater files scored: no unscored-but-verified label, no scored-but-unverified one.
    """
    a_ids = {path.stem for path in (labels_root / "rater_a").glob("*.json")}
    b_ids = {path.stem for path in (labels_root / "rater_b").glob("*.json")}
    if not a_ids or a_ids != b_ids:
        raise SystemExit(
            f"rater label sets differ: rater_a={len(a_ids)} rater_b={len(b_ids)} "
            f"overlap={len(a_ids & b_ids)}"
        )
    if len(a_ids) != expected_trials:
        raise SystemExit(f"expected {expected_trials} trials, found {len(a_ids)}")
    if members is not None:
        scored = sorted([f"rater_a/{trial}.json" for trial in a_ids]
                        + [f"rater_b/{trial}.json" for trial in a_ids])
        if sorted(members) != scored:
            raise SystemExit("manifest members differ from the rater files scored; refusing to score")
    return sorted(a_ids)


def read_rater_kind(path: Path) -> str:
    """One rater's loop kind; a corrupt, mislabeled or unknown label aborts, never guesses."""
    try:
        row = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"label {path} is unreadable: {exc}") from exc
    if not isinstance(row, dict) or row.get("trial") != path.stem:
        raise SystemExit(f"label {path} is not the trial its filename claims; refusing to guess")
    kind = har119_score.rater_view(row)["loop_kind"]
    if kind not in PUBLISHED_LOOP_KINDS:
        raise SystemExit(f"label {path} has unknown loop_kind {kind!r}; refusing to guess")
    return kind


def index_published_reports(results_home: Path) -> dict[str, list[str]]:
    """Index canonical publications, not nested training/selection copies."""
    from evallab.results_home import _published_jobs

    index: dict[str, list[str]] = {}
    for job, _provenance in _published_jobs(results_home):
        for path in sorted((job / "processed").glob("trial-*.json")):
            trial = path.name[len("trial-"):-len(".json")]
            index.setdefault(trial, []).append(str(path))
    return index


def find_published_report(index: dict[str, list[str]], trial: str) -> Path | None:
    """The single published report for a trial; ambiguity aborts, absence abstains."""
    hits = index.get(trial, [])
    if len(hits) > 1:
        raise SystemExit(f"trial {trial}: {len(hits)} published reports; refusing to pick one")
    return Path(hits[0]) if hits else None


def read_page_kind(report_path: Path, trial: str) -> tuple[str | None, str | None, str]:
    """A published page's loop kind, bound to the expected trial identity.

    A report for another trial aborts; a missing/unknown kind is an abstention, never none.
    """
    digest = _sha256(report_path)
    try:
        report = json.loads(report_path.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"report {report_path} is corrupt: {exc}") from exc
    if not isinstance(report, dict) or report.get("trial_name") != trial:
        raise SystemExit(f"report {report_path} is not trial {trial}; refusing to score it")
    decision = report.get("decision") if isinstance(report, dict) else None
    judgments = decision.get("judgments") if isinstance(decision, dict) else None
    loop = judgments.get("loop_kind") if isinstance(judgments, dict) else None
    kind = loop.get("kind") if isinstance(loop, dict) else None
    if kind not in PUBLISHED_LOOP_KINDS:
        return None, f"page kind {kind!r} missing or unknown", digest
    return kind, None, digest


def predictor_functions_sha() -> tuple[list[str], str]:
    from evallab import token_flow as _token_flow_mod
    from evallab import trial_decision as _trial_decision_mod

    names = [
        "trial_decision.classify_loop_kind",
        "trial_decision._claim_bearing",
        "token_flow._loop_onset",
    ]
    sources = [
        inspect.getsource(_trial_decision_mod.classify_loop_kind),
        inspect.getsource(_trial_decision_mod._claim_bearing),
        inspect.getsource(_token_flow_mod._loop_onset),
    ]
    return names, hashlib.sha256("".join(sources).encode()).hexdigest()


def score_published_cohort(
    *,
    labels_root: Path,
    results_home: Path,
    output: Path,
    expected_files: int = HAR128_EXPECTED_FILES,
    expected_trials: int = HAR128_EXPECTED_TRIALS,
    expected_manifest_sha256: str = HAR128_MANIFEST_SHA256,
    cohort: str | None = None,
    heldout: str | None = None,
    limit: str | None = None,
) -> dict:
    """Score published pages against frozen labels; write the durable receipt.

    The manifest identity is rejected before anything is scored or written.
    ``page_vs_agreed.n`` stays the full rater-agreed denominator: an
    abstaining page counts as an abstention with no hit, never shrinks n.
    ``cohort``/``heldout``/``limit`` describe the study scored; when omitted
    they fall back to the default (HAR-116) cohort description so existing
    callers keep current behavior.
    """
    manifest = verify_published_freeze(labels_root, expected_files)
    manifest_sha256 = hashlib.sha256(manifest.encode()).hexdigest()
    if manifest_sha256 != expected_manifest_sha256:
        raise SystemExit(
            f"unexpected label freeze {manifest_sha256}; refusing to score or write a receipt"
        )
    trials = published_trial_ids(labels_root, expected_trials, manifest_label_paths(manifest))
    index = index_published_reports(results_home)
    frozen_path = labels_root / "FROZEN_AT"
    frozen_at = frozen_path.read_text().splitlines()[0] if frozen_path.is_file() else "unknown"
    names, functions_sha = predictor_functions_sha()

    rows: list[dict] = []
    agreed = hits = abstentions = 0
    confusion: dict[str, dict[str, int]] = {kind: {"agree": 0, "n": 0} for kind in PUBLISHED_LOOP_KINDS}
    for trial in trials:
        rater_a = read_rater_kind(labels_root / "rater_a" / f"{trial}.json")
        rater_b = read_rater_kind(labels_root / "rater_b" / f"{trial}.json")
        rater_agreed = rater_a == rater_b
        report_path = find_published_report(index, trial)
        if report_path is None:
            page, reason, digest = None, "published report missing", None
        else:
            page, reason, digest = read_page_kind(report_path, trial)
        match: bool | None = None
        if rater_agreed:
            agreed += 1
            confusion[rater_a]["n"] += 1
            if page is None:
                abstentions += 1
            else:
                match = page == rater_a
                hits += match
                confusion[rater_a]["agree"] += match
        rows.append(
            {
                "trial": trial,
                "rater_a": rater_a,
                "rater_b": rater_b,
                "rater_agreed": rater_agreed,
                "page": page,
                "page_matches": match,
                "abstention_reason": reason,
                "report_path": str(report_path) if report_path else None,
                "report_sha256": digest,
            }
        )
    disagreements = len(trials) - sum(1 for row in rows if row["rater_agreed"])
    payload = {
        "schema": COHORT_SCHEMA,
        "cohort": cohort if cohort is not None else HAR128_COHORT,
        "generated_at": _datetime.datetime.now(_datetime.UTC).isoformat(),
        "labels_root": str(labels_root),
        "labels_frozen_at": frozen_at,
        "labels_manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
        "results_home": str(results_home),
        "predictor": "trial_decision.classify_loop_kind (HAR-119 claim-vs-repetition rule)",
        "predictor_functions": {"names": names, "sha256": functions_sha},
        "scorer_sha256": _sha256(Path(__file__).resolve()),
        "in_sample": False,
        "rater_agreement": {"agree": sum(1 for row in rows if row["rater_agreed"]), "n": len(trials)},
        "disagreements": disagreements,
        "page_vs_agreed": {"agree": hits, "n": agreed},
        "page_abstentions": abstentions,
        "loop_kind_confusion": confusion,
        "rows": rows,
        "heldout": heldout if heldout is not None else HAR128_HELDOUT,
        "limit": limit if limit is not None else HAR128_LIMIT,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    return payload


def check_published_drift(payload: dict, functions_sha: str) -> list[str]:
    """The embedded additional_loop_calibrations entry must match this measurement."""
    from evallab.trial_decision import PAGE_CALIBRATION

    entries = PAGE_CALIBRATION.get("additional_loop_calibrations") or []
    entries = [entry for entry in entries if isinstance(entry, dict)]
    want = next((entry for entry in entries if entry.get("cohort") == payload["cohort"]), None)
    if want is None:
        return [f"no additional_loop_calibrations entry for cohort {payload['cohort']}"]
    drift = []
    scored = {
        "rater_agreement_loop_kind": payload["rater_agreement"],
        "page_vs_agreed_loop_kind": payload["page_vs_agreed"],
        "loop_kind_confusion": payload["loop_kind_confusion"],
    }
    for key, got in scored.items():
        if got != want.get(key):
            drift.append(f"{key}: code says {want.get(key)!r}, measured {got!r}")
    for key, got in (
        ("excluded_rater_disagreement", payload["disagreements"]),
        ("abstentions", payload["page_abstentions"]),
        ("labels_manifest_sha256", payload["labels_manifest_sha256"]),
    ):
        if got != want.get(key):
            drift.append(f"{key}: code says {want.get(key)!r}, measured {got!r}")
    if functions_sha != want.get("predictor_functions_sha256"):
        drift.append("predictor_functions_sha256: page loop rule source changed; recalibrate")
    return drift


def _main_published_cohort(args) -> int:
    spec = resolve_published_cohort(getattr(args, "cohort", None))
    labels_root = Path(args.labels) if args.labels else Path(spec["labels"])
    output = Path(args.output) if args.output else Path(spec["output"])
    if not args.results_home:
        raise SystemExit("--results-home is required in published-cohort mode")
    payload = score_published_cohort(
        labels_root=labels_root,
        results_home=Path(args.results_home),
        output=output,
        expected_files=args.expected_files if args.expected_files is not None else spec["expected_files"],
        expected_trials=args.expected_trials if args.expected_trials is not None else spec["expected_trials"],
        expected_manifest_sha256=(
            args.expected_manifest_sha256
            if args.expected_manifest_sha256 is not None
            else spec["expected_manifest_sha256"]
        ),
        cohort=spec["cohort"],
        heldout=spec["heldout"],
        limit=spec["limit"],
    )
    kind, rater = payload["page_vs_agreed"], payload["rater_agreement"]
    print(
        f"page loop_kind vs agreed: {kind['agree']}/{kind['n']} "
        f"({rater['agree']} agreed of {rater['n']}, "
        f"{payload['disagreements']} rater-disagreement excluded, "
        f"{payload['page_abstentions']} abstentions)"
    )
    print(f"rater agreement loop_kind: {rater['agree']}/{rater['n']}")
    print(f"labels manifest sha256: {payload['labels_manifest_sha256']}")
    print(f"predictor functions sha256: {payload['predictor_functions']['sha256'][:16]}...")
    print(f"receipt: {output}")
    for row in payload["rows"]:
        if row["rater_agreed"] and row["page"] is not None and not row["page_matches"]:
            print(f"  loop miss: {row['trial']} rater={row['rater_a']} page={row['page']}")
    drift = check_published_drift(payload, payload["predictor_functions"]["sha256"])
    if drift:
        print("DRIFT: trial_decision.PAGE_CALIBRATION additional entry no longer matches:")
        for line in drift:
            print(f"  {line}")
        return 1
    print("MATCH: trial_decision.PAGE_CALIBRATION additional entry agrees with this measurement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
