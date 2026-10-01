"""HAR-119 Scout predictions builder (frozen mapping, see ../predictions/MAPPING.md).

Reads the frozen Scout scan (rule scanners over normalized trials) and
`rules.analyze_trial_rules` (same code the scanners call, current-stack src),
cross-checks scan values against direct computation, and writes one row per
trial to `predictions/scout.jsonl` plus per-trial raw JSON to
`predictions/scout/raw/`. Reuses the HAR-109 schema mapping
(`har109/predictions/scout.notes.md`): ff falls back to the outcome rule's
evidence step when first_failure is none; blame is the literal outcome
attribution (n/a->none, unclear->null/not expressed); loop_kind and
pass_copied are not expressed (null).

Usage (from worktree root):
  uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 \
    --with pyarrow --with pandas \
    python research/explorations/trace-lab/har119/tools/scout_har119.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
WORKTREE = HERE.parents[5]  # har119-trace-review
HAR119 = HERE.parent.parent
PRED = HAR119 / "predictions"
WORK = PRED / "scout_work"
SCAN_ID = "jni9V9mxsrdqrLkr7UWcbU"
SCAN_DB = "scout_raw"  # raw trial dirs (caps present for ceilings)
SCAN_NOTE = (
    "scan v1 (nBLkdhPYEMLYBTzfoEBKFn) imported the normalized mirror, which "
    "lacks job-level caps files, so rule_stop fell back to "
    "ceiling:trial_budget on 11/12 trials. Scan v2 (this file) imports the "
    "raw trial dirs (identical steps; see step_id assert); rule values match "
    "capabilities.jsonl rows. scanners.py untouched; mapping unchanged."
)

sys.path.insert(0, str(WORKTREE / "research" / "explorations" / "trace-lab" / "scout"))
from rules import analyze_trial_rules  # noqa: E402


def norm_ref(ref: str | None) -> str | None:
    if not ref:
        return None
    m = re.fullmatch(r"trajectory\.cont-(\d+)\.json#(\d+)", ref)
    if m:
        return f"cont-{m.group(1)}#{m.group(2)}"
    return ref


def map_stop(value: str | None, exception_type: str | None) -> str:
    v = value or "unknown"
    if v in ("ceiling:input_tokens", "ceiling:output_tokens", "ceiling:total_tokens"):
        return "token_ceiling"
    if v == "ceiling:requests":
        return "request_ceiling"
    if v == "task_complete_confirmed":
        return "model_finished"
    if v == "agent_timeout":
        return "agent_timeout"
    if v in ("model_auth_error",) or (v == "unknown" and exception_type):
        return "infra_error"
    return "other"


def map_blame(attribution: str | None) -> str | None:
    if attribution == "n/a":
        return "none"
    if attribution == "model":
        return "model"
    if attribution == "harness":
        return "harness"
    return None  # unclear or anything else: not expressed


def main() -> int:
    from inspect_scout import scan_results_df

    sel = json.loads((HAR119 / "selection.json").read_text())
    trials: list[str] = [r["trial"] for r in sel["runs"]]
    assert len(trials) == 12, trials

    # Raw trial dirs (same steps as normalized; caps present for ceilings).
    sel_trials: dict[str, Path] = {r["trial"]: Path(r["trial_dir"]) for r in sel["runs"]}
    norm_trials: dict[str, Path] = dict(sel_trials)

    # Verify normalized step_ids match raw step_ids (single-file passthrough).
    for t in trials:
        raw_traj = sel_trials[t] / "agent" / "trajectory.json"
        raw_ids = [s.get("step_id") for s in json.loads(raw_traj.read_text())["steps"]]
        normoggi = None
        for job_dir in sorted((WORK / "normalized").iterdir()):
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

    r = scan_results_df(str(WORK / SCAN_DB / "scans" / f"scan_id={SCAN_ID}"))
    scanners = r.scanners
    print("scanners:", sorted(scanners.keys()))
    for name, df in scanners.items():
        if name.startswith("rule_"):
            print(f"--- {name} n={len(df)} ids:", list(df["transcript_id"])[:3])

    raw_dir = PRED / "scout" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    for t in trials:
        rules = analyze_trial_rules(norm_trials[t], evallab_src=str(WORKTREE / "src"))
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

        hs = handshake.get("confirmed") if handshake else None
        row = {
            "trial": t,
            "stop_reason": map_stop(stop.get("reason"), stop.get("exception_type")),
            "first_failure_ref": ff_ref,
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
                f"scout scan_id={SCAN_ID} rule_outcome/rule_first_failure/"
                f"rule_handshake/rule_loops/rule_stop/rule_wedge "
                f"(scanners.py as merged) + rules.analyze_trial_rules "
                f"(evallab_src=current src); ff<-first_failure.step_ref else "
                f"outcome.evidence_step_refs[0]; loop<-longest rule_loops span"
            ),
        }
        (raw_dir / f"{t}.scout.json").write_text(json.dumps(rules, indent=1, default=str))
        rows.append(json.dumps(row))
    (PRED / "scout.jsonl").write_text("\n".join(rows) + "\n")
    print(f"wrote {len(rows)} rows to predictions/scout.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
