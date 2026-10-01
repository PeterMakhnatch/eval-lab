"""G2 attempt-1 Eval Lab predictions builder (frozen mapping: har119 MAPPING.md + predictions_g2_a1/MAPPING_ADDENDUM.md).

Reads per-trial `evallab report run --json` outputs + per-trial `process-job`
records (raw only) and writes one normalized row per trial to
`predictions_g2_a1/evallab.jsonl`. No thresholds tuned here; mapping literal.
Addendum: LoopBreakStop exception / loop_break stop -> `loop_break`;
first_failure_step is null (not expressed, same as ref).

Usage (from worktree root): uv run python research/explorations/trace-lab/har128/predictions_g2_a1/evallab_g2.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve()
PRED = HERE.parent  # predictions_g2_a1
RAW = PRED / "evallab" / "raw"


def map_stop(sr: str | None, sd: str | None) -> str:
    if "loop_break" in (sr or "") or "LoopBreakStop" in (sd or ""):
        return "loop_break"
    m = re.search(r"binding ceiling: (\w+)", sd or "")
    ceil = m.group(1) if m else None
    if sr in ("task_complete", "prose_completion"):
        return "model_finished"
    if sr == "trial_budget_exhausted" and ceil == "requests":
        return "request_ceiling"
    if sr == "trial_budget_exhausted" and ceil in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
    ):
        return "token_ceiling"
    if sr == "agent_timeout":
        return "agent_timeout"
    if sr == "error":
        return "infra_error"


def fallback_row(t: str, trial_dir: str, err: str) -> dict:
    """Row for a trial with no readable trajectory (e.g. infra failure
    before agent start): stop from result.json exception, rest null."""
    exc_type = exc_msg = reward = None
    try:
        d = json.loads((Path(trial_dir) / "result.json").read_text())
        ei = d.get("exception_info") or {}
        exc_type = ei.get("exception_type")
        exc_msg = str(ei.get("exception_message") or "")[:300]
        reward = ((d.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    except Exception as exc:  # noqa: BLE001
        err = f"{err}; result.json unreadable: {exc}"
    stop = (
        "loop_break"
        if "LoopBreakStop" in (exc_msg or "") or "loop_break" in (exc_type or "")
        else ("infra_error" if exc_type else "other")
    )
    return {
        "trial": t,
        "stop_reason": stop,
        "first_failure_ref": None,
        "first_failure_step": None,
        "blame": None,
        "loop_kind": None,
        "loop_span": None,
        "pass_copied": None,
        "raw_error": err,
        "raw_exception_type": exc_type,
        "raw_exception_message": exc_msg,
        "raw_reward": reward,
        "tool": "evallab",
        "source": f"result.json fallback (no readable trajectory for trial {t})",
    }


def main() -> int:
    trials = json.loads((PRED / "trials.json").read_text())
    names: list[str] = [t["trial"] for t in trials]
    assert len(names) == len(trials) and len(names) > 0, names
    out_lines: list[str] = []
    for t in names:
        tdir = next(r["trial_dir"] for r in trials if r["trial"] == t)
        try:
            rep = json.loads((RAW / f"{t}.run_report.json").read_text())
            pj = json.loads((RAW / f"{t}.process_job.json").read_text())
        except Exception as exc:  # noqa: BLE001
            out_lines.append(json.dumps(fallback_row(t, tdir, f"raw missing/unreadable: {exc}")))
            continue
        o = rep["outcome"]
        ff = o.get("first_failure") or {}
        r = rep.get("revisits") or {}
        lir = r.get("longest_identical_run")
        cyc = r.get("longest_cycle")
        ls = (r.get("loop_suspicion") or {}).get("detected")
        span: list[int] | None = None
        if ls:
            if cyc and cyc.get("start_step") is not None:
                span = [cyc["start_step"], cyc.get("end_step")]
            elif lir and (lir.get("length") or 0) >= 2 and lir.get("start_step") is not None:
                span = [lir["start_step"], lir.get("end_step")]
        reward = o.get("reward")
        copied = rep.get("pass_may_be_copied")
        pc = (copied is not None) if (reward is not None and reward >= 1.0) else None
        of = rep.get("outside_fetches") or {}
        tf = (pj.get("token_flow") or {}) if isinstance(pj.get("token_flow"), dict) else {}
        row = {
            "trial": t,
            "stop_reason": map_stop(o.get("stop_reason"), o.get("stop_detail")),
            "first_failure_ref": None,
            "first_failure_step": None,
            "blame": None,
            "loop_kind": None,
            "loop_span": span,
            "pass_copied": pc,
            "raw_stop_reason": o.get("stop_reason"),
            "raw_stop_detail": o.get("stop_detail"),
            "raw_completion": o.get("completion"),
            "raw_verdict": o.get("verdict"),
            "raw_reward": reward,
            "raw_first_failure_step": ff.get("step"),
            "raw_first_failure_kind": ff.get("kind"),
            "raw_first_failure_evidence": ff.get("evidence"),
            "raw_first_failure_confidence": ff.get("confidence"),
            "raw_loop_suspicion": ls,
            "raw_longest_identical_run": (
                {
                    "length": lir.get("length"),
                    "start_step": lir.get("start_step"),
                    "end_step": lir.get("end_step"),
                }
                if lir
                else None
            ),
            "raw_longest_cycle": cyc,
            "raw_outside_fetch_count": of.get("count"),
            "raw_pass_may_be_copied": copied,
            "raw_probe03_stop": pj.get("stop_reason"),
            "raw_probe03_first_failure": pj.get("first_failure"),
            "raw_probe03_outcome": pj.get("outcome_failure"),
            "raw_probe03_handshake": pj.get("handshake"),
            "raw_probe03_loop_spans": ((pj.get("loop_cost") or {}).get("spans")),
            "raw_token_flow_loop_onset": tf.get("loop_onset"),
            "raw_token_flow_last_edit": tf.get("last_useful_edit"),
            "raw_decision": pj.get("decision"),
            "tool": "evallab",
            "source": (
                f"evallab report run --json <trial_dir> --output-dir "
                f"predictions_g2_a1/evallab/raw + evallab process-job --no-ingest "
                f"--no-publish --json (trial {t})"
            ),
        }
        out_lines.append(json.dumps(row))
    (PRED / "evallab.jsonl").write_text("\n".join(out_lines) + "\n")
    print(f"wrote {len(out_lines)} rows to predictions_g2_a1/evallab.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
