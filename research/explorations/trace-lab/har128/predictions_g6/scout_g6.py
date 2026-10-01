"""G6 Scout predictions builder (frozen mapping: har119 MAPPING.md + predictions_g6/MAPPING_ADDENDUM.md).

Rows are keyed by opaque id (`trial: "g6-NN"`); real trial names never enter
rows or filenames (raws are `<id>.scout.json`). Scan lookups match on the
real trial dir basename internally but only the opaque id is emitted.
Reads the frozen Scout scan (rule scanners over raw trial dirs) and
`rules.analyze_trial_rules` (same code the scanners call, current-stack src),
cross-checks scan values against direct computation, and writes one row per
trial to `predictions_g6/scout.jsonl` plus per-trial raw JSON to
`predictions_g6/scout/raw/`. ff falls back to the outcome rule's evidence
step when first_failure is none; blame is the literal outcome attribution
(n/a->none, unclear->null/not expressed); loop_kind and pass_copied are not
expressed (null). Addendum: LoopBreakStop exception / loop_break stop ->
`loop_break` (HEAD b1619aa3 maps it natively in probe03 too; the
`value == "loop_break"` clause covers it, outcome identical — see NOTES.md);
first_failure_step parses the trailing #N of first_failure_ref.

Usage (from worktree root):
  uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 \
    --with pyarrow --with pandas \
    python research/explorations/trace-lab/har128/predictions_g6/scout_g6.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
WORKTREE = HERE.parents[5]  # har128-g6
PRED = HERE.parent  # predictions_g6
DERIVED = Path("/Users/petermakhnatch/Developer/eval-lab/derived/trace-lab/har128-g6-tools")
NORM = DERIVED / "normalized"
SCAN_DB = "scout_raw"  # raw trial dirs (caps present for ceilings)

sys.path.insert(0, str(WORKTREE / "research" / "explorations" / "trace-lab" / "scout"))
from rules import analyze_trial_rules  # noqa: E402


def find_scan_id() -> str:
    cands = sorted((DERIVED / SCAN_DB / "scans").glob("scan_id=*"))
    assert len(cands) == 1, [p.name for p in cands]
    return cands[0].name.split("scan_id=", 1)[1]


def norm_ref(ref: str | None) -> str | None:
    if not ref:
        return None
    ref = str(ref).replace("trajectory.cont-", "cont-").replace("trajectory.json#", "head#")
    return ref


def ref_step(ref: str | None) -> int | None:
    if not ref:
        return None
    m = re.search(r"#(\d+)$", ref)
    return int(m.group(1)) if m else None


def map_stop(value: str | None, exception_type: str | None) -> str:
    if value == "loop_break" or "LoopBreakStop" in (exception_type or ""):
        return "loop_break"
    v = value or "unknown"
    if v in ("ceiling:input_tokens", "ceiling:output_tokens", "ceiling:total_tokens"):
        return "token_ceiling"
    if v == "ceiling:requests":
        return "request_ceiling"
    if v == "task_complete_confirmed":
        return "model_finished"
    if v == "agent_timeout":
        return "agent_timeout"
    if v in ("model_auth_error", "unknown"):
        return "infra_error" if exception_type else "other"
    return "other"


def map_blame(attribution: str | None) -> str | None:
    if attribution == "n/a":
        return "none"
    if attribution in ("model", "harness"):
        return attribution
    return None  # unclear or anything else: not expressed


def main() -> int:
    from inspect_scout import scan_results_df

    trials = json.loads((PRED / "trials.json").read_text())
    assert len(trials) == 60, len(trials)
    by_id: dict[str, Path] = {t["id"]: Path(t["trial_dir"]) for t in trials}
    names: list[str] = [t["id"] for t in trials]

    # Verify normalized step_ids match raw step_ids (single-file passthrough).
    no_trajectory: set[str] = set()
    for gid in names:
        raw_traj = by_id[gid] / "agent" / "trajectory.json"
        if not raw_traj.is_file():
            no_trajectory.add(gid)
            continue
        raw_ids = [s.get("step_id") for s in json.loads(raw_traj.read_text())["steps"]]
        trial_name = by_id[gid].name
        normoggi = None
        for job_dir in sorted(NORM.iterdir()):
            if not job_dir.is_dir():
                continue
            cand = job_dir / trial_name
            if (cand / "agent" / "trajectory.json").exists():
                normoggi = cand
                break
        assert normoggi is not None, f"normalized copy missing for {gid}"
        norm_ids = [
            s.get("step_id")
            for s in json.loads((normoggi / "agent" / "trajectory.json").read_text())["steps"]
        ]
        assert raw_ids == norm_ids, f"step_id drift on {gid}"
    print(
        f"step_id check: {len(names) - len(no_trajectory)}/{len(names)} raw==normalized; no-trajectory: {sorted(no_trajectory) or 'none'}"
    )

    scan_id = find_scan_id()
    r = scan_results_df(str(DERIVED / SCAN_DB / "scans" / f"scan_id={scan_id}"))
    scanners = r.scanners
    print("scanners:", sorted(scanners.keys()))

    agree = {"outcome": 0, "first_failure": 0, "handshake": 0, "loops": 0, "stop": 0, "wedge": 0}
    raw_dir = PRED / "scout" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    for gid in names:
        # Scan URIs carry real trial names; match internally, emit opaque id.
        tkey = by_id[gid].name
        rules = analyze_trial_rules(by_id[gid], evallab_src=str(WORKTREE / "src"))
        stop = rules["stop"] or {}
        first = rules.get("first_failure")
        outcome = rules.get("outcome") or {}
        handshake = rules.get("handshake")
        loops = rules.get("loops") or {}
        wedge = rules.get("wedge") or {}

        if first:
            ff_ref = norm_ref(first.get("step_ref"))
            ff_what = first.get("rule_id")
        else:
            refs = outcome.get("evidence_step_refs") or []
            ff_ref = norm_ref(refs[0]) if refs else None
            ff_what = outcome.get("rule_id")

        spans = loops.get("spans") or []
        loop_span = None
        if spans:
            longest = max(spans, key=lambda s: s.get("length") or 0)
            sp = longest.get("span") or [None, None]
            if sp[0] is not None:
                loop_span = [norm_ref(sp[0]), norm_ref(sp[1])]

        # Scan<->direct agreement (same six rule dimensions as HAR-119).
        scan_vals: dict[str, object] = {}
        for name in [
            "rule_outcome",
            "rule_first_failure",
            "rule_handshake",
            "rule_loops",
            "rule_stop",
            "rule_wedge",
        ]:
            if name in scanners:
                hits = (
                    scanners[name]
                    .loc[scanners[name]["transcript_source_uri"].str.contains(tkey, na=False)][
                        "value"
                    ]
                    .tolist()
                )
                scan_vals[name] = hits[0] if hits else None
        direct_vals = {
            "outcome": (outcome.get("tag"), outcome.get("attribution"), outcome.get("rule_id")),
            "first_failure": ((first or {}).get("rule_id") if first else "none"),
            "handshake": (
                ("confirmed" if (handshake or {}).get("confirmed") else "unconfirmed")
                if handshake
                else "none"
            ),
            "loops": max([(s.get("length") or 0) for s in spans] or [0]),
            "stop": stop.get("reason"),
            "wedge": bool(wedge.get("stretches") or []),
        }
        for key, scan_name in [
            ("outcome", "rule_outcome"),
            ("first_failure", "rule_first_failure"),
            ("handshake", "rule_handshake"),
            ("loops", "rule_loops"),
            ("stop", "rule_stop"),
            ("wedge", "rule_wedge"),
        ]:
            sv = scan_vals.get(scan_name)
            if sv is None:
                continue  # trial not in scan (no transcript); null row below
            dv = direct_vals[key]
            match = False
            try:
                if key == "outcome":
                    combo = "/".join(str(x or "none") for x in dv)
                    match = combo in str(sv) or str(sv) in combo
                elif key in ("first_failure", "handshake", "stop"):
                    match = str(dv) in str(sv)
                elif key == "loops":
                    match = int(sv) == int(dv)
                elif key == "wedge":
                    match = str(bool(sv)).lower() in str(dv).lower() or str(sv) == str(dv)
            except (TypeError, ValueError):
                match = False
            if match:
                agree[key] += 1

        hs = handshake.get("confirmed") if handshake else None
        row = {
            "trial": gid,
            "stop_reason": map_stop(stop.get("reason"), stop.get("exception_type")),
            "first_failure_ref": ff_ref,
            "first_failure_step": ref_step(ff_ref),
            "blame": map_blame(outcome.get("attribution")),
            "loop_kind": None,
            "loop_span": loop_span,
            "pass_copied": None,
            "raw_rule_stop": stop.get("reason"),
            "raw_exception_type": stop.get("exception_type"),
            "raw_natural_completion": stop.get("natural_completion"),
            "raw_first_failure_rule": (first or {}).get("rule_id") if first else "none",
            "raw_first_failure_what": ff_what,
            "raw_outcome": "/".join(
                str(outcome.get(k) or "none") for k in ("tag", "attribution", "rule_id")
            ),
            "raw_outcome_note": outcome.get("note"),
            "raw_handshake": (("confirmed" if hs else "unconfirmed") if handshake else "none"),
            "raw_first_prompt_ref": (handshake or {}).get("first_prompt_ref"),
            "raw_loop_value": max([(s.get("length") or 0) for s in spans] or [0]),
            "raw_loop_spans": spans,
            "raw_wedge": bool(wedge.get("stretches") or []),
            "raw_scan_values": {
                name: (
                    scanners[name]
                    .loc[scanners[name]["transcript_source_uri"].str.contains(tkey, na=False)][
                        ["value"]
                    ]
                    .to_dict(orient="records")
                    if name in scanners
                    else None
                )
                for name in [
                    "rule_outcome",
                    "rule_first_failure",
                    "rule_handshake",
                    "rule_loops",
                    "rule_stop",
                    "rule_wedge",
                ]
            },
            "tool": "scout",
            "source": (
                f"scout scan_id={scan_id} rule_outcome/rule_first_failure/"
                f"rule_handshake/rule_loops/rule_stop/rule_wedge "
                f"(scanners.py as merged) + rules.analyze_trial_rules "
                f"(evallab_src=current src); sealed opaque id"
            ),
        }
        (raw_dir / f"{gid}.scout.json").write_text(json.dumps(rules, indent=1, default=str))
        rows.append(json.dumps(row))
    (PRED / "scout.jsonl").write_text("\n".join(rows) + "\n")
    print(f"wrote {len(rows)} rows to predictions_g6/scout.jsonl")
    print("scan<->direct agreement:", agree)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
