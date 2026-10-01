"""G2 r2 Scout predictions builder (frozen mapping: har119 MAPPING.md + predictions_g2_r2/MAPPING_ADDENDUM.md).

Reads the frozen Scout scan (rule scanners over raw trial dirs) and
`rules.analyze_trial_rules` (same code the scanners call, current-stack src),
cross-checks scan values against direct computation, and writes one row per
trial to `predictions_g2_r2/scout.jsonl` plus per-trial raw JSON to
`predictions_g2_r2/scout/raw/`. Reuses the HAR-109/HAR-119 schema mapping:
ff falls back to the outcome rule's evidence step when first_failure is none;
blame is the literal outcome attribution (n/a->none, unclear->null/not
expressed); loop_kind and pass_copied are not expressed (null).
Addendum: LoopBreakStop exception / loop_break stop -> `loop_break`;
first_failure_step parses the trailing #N of first_failure_ref.
No new agent_timeout mapping (both tools already map AgentTimeoutError).

Usage (from worktree root):
  uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 \
    --with pyarrow --with pandas \
    python research/explorations/trace-lab/har128/predictions_g2_r2/scout_r2.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
WORKTREE = HERE.parents[5]  # har128-tools3
PRED = HERE.parent  # predictions_g2_r2
DERIVED = Path("/Users/petermakhnatch/Developer/eval-lab/derived/trace-lab/har128-g2r2")
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
    names: list[str] = [t["trial"] for t in trials]
    assert len(names) == len(trials) and len(names) > 0, names
    sel_trials: dict[str, Path] = {t["trial"]: Path(t["trial_dir"]) for t in trials}

    # Verify normalized step_ids match raw step_ids (single-file passthrough).
    # Trials with no readable trajectory have no normalized copy; they get
    # null rows from the rule output below.
    no_trajectory: set[str] = set()
    for t in names:
        raw_traj = sel_trials[t] / "agent" / "trajectory.json"
        if not raw_traj.is_file():
            no_trajectory.add(t)
            continue
        raw_ids = [s.get("step_id") for s in json.loads(raw_traj.read_text())["steps"]]
        normoggi = None
        for job_dir in sorted(NORM.iterdir()):
            if not job_dir.is_dir():
                continue
            cand = job_dir / t
            if (cand / "agent" / "trajectory.json").exists():
                normoggi = cand
                break
        assert normoggi is not None, f"normalized copy missing for {t}"
        norm_ids = [
            s.get("step_id")
            for s in json.loads((normoggi / "agent" / "trajectory.json").read_text())["steps"]
        ]
        assert raw_ids == norm_ids, f"step_id drift on {t}"
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
    for t in names:
        rules = analyze_trial_rules(sel_trials[t], evallab_src=str(WORKTREE / "src"))
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
        # Trials absent from the scan (no transcript) are skipped here; their
        # null rows still go out below.
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
                    .loc[scanners[name]["transcript_source_uri"].str.contains(t, na=False)]["value"]
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
            "trial": t,
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
                    .loc[scanners[name]["transcript_source_uri"].str.contains(t, na=False)][
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
                f"(evallab_src=current src); ff<-first_failure.step_ref else "
                f"outcome.evidence_step_refs[0]; loop<-longest rule_loops span"
            ),
        }
        (raw_dir / f"{t}.scout.json").write_text(json.dumps(rules, indent=1, default=str))
        rows.append(json.dumps(row))
    (PRED / "scout.jsonl").write_text("\n".join(rows) + "\n")
    print(f"wrote {len(rows)} rows to predictions_g2_r2/scout.jsonl")
    print("scan<->direct agreement:", agree)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
