"""One-page trial decision, composed from records already on disk.

No new detector. The page reads the probe-03 outcome, the existing grader
checks (literal source assertion, deliverable named by the verifier but
absent from the instruction, setup/import crash), the process-job taint
flags, and token-flow's last useful edit. ``rule_id`` and ``attribution``
are the probe-03 fields the frozen hand keys use. ``whose`` is a reading
of that rule for the page: ``R-ENV-02`` is the task, even though probe-03
attributes that rule to the harness.

Facts are observed signals copied from the records. Every judgment,
including grader alignment and loop kind, is an opinion; only loop kind
carries a measured error rate (PAGE_CALIBRATION, HAR-119 frozen cohort).
``counts`` is rendered from the HAR-78 field and never computed here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evallab.upstream_fetch import confirmed_fetch

DECISION_SCHEMA = "trial_decision/v3"
_EXCERPT_CHARS = 400
#: HAR-119 published rule: 10 turns after the first confirm prompt, and at
#: least half of those turns claim completion. Not a new detector.
CLAIM_LOOP_MIN_TURNS = 10
CLAIM_LOOP_MIN_FRACTION = 0.5
COUNTED_VERDICTS = frozenset({"counted_pass", "counted_fail", "excluded"})
#: Page-predictor calibration against the frozen HAR-119 part-2 labels.
#: Loop kind, first failure and blame are measured with the frozen scorer
#: semantics (kind/presence exact; first failure within +/-2 steps; blame
#: exact under the frozen literal map). A null page field is an abstention
#: with explicit coverage, not a silent drop. Reproduce with
#: research/explorations/trace-lab/har119/score_page.py, which refuses to
#: run when the frozen labels change and reports unavailable when the
#: trial directories are missing.
PAGE_CALIBRATION = {
    "predictor": "trial_decision.classify_loop_kind (HAR-119 claim-vs-repetition rule)",
    "predictor_functions": {
        "names": [
            "trial_decision.classify_loop_kind",
            "trial_decision._claim_bearing",
            "token_flow._loop_onset",
        ],
        "sha256": "6909e778952053c8cf2b80ce52cf9804d1648a02aaa4ea74b9a4b384fe37c0ae",
    },
    "cohort": "HAR-119 part 2: 12 HAR-110 split-v2 runs",
    "frozen_at": "2026-10-01T00:00:02Z",
    "selection_sha256": "941db9c064e34a2d6f9f7209df42a92cee13b20e776c8fd28c7cc72bc4de860f",
    "in_sample": False,
    "rater_agreement_loop_kind": {"agree": 11, "n": 12},
    "rater_agreement_loop_present": {"agree": 11, "n": 12},
    "rater_agreement_first_failure": {"agree": 9, "n": 12},
    "rater_agreement_blame": {"agree": 11, "n": 12},
    "page_vs_agreed_loop_kind": {"agree": 7, "n": 11},
    "page_vs_agreed_loop_present": {"agree": 7, "n": 11},
    "eligible_n": 11,
    "excluded_rater_disagreement": 1,
    "abstentions": 0,
    "page_vs_loop_rule_kind": {"agree": 12, "n": 12},
    "page_vs_agreed_first_failure": {"agree": 1, "n": 9},
    "first_failure_coverage": {"expressed": 1, "of": 12, "abstentions": 11},
    "page_vs_agreed_blame": {"agree": 11, "n": 11},
    "blame_abstentions": 0,
    #: Separately scoped loop-kind-only calibrations of the same page
    #: predictor on later frozen cohorts. Each entry stands alone with its
    #: own denominator: never pooled with the HAR-119 cohort above, and no
    #: entry borrows first-failure/blame numbers it did not measure.
    #: Reproduce a named entry with
    #: research/explorations/trace-lab/har119/score_page.py --published-cohort
    #: --cohort <har128-har116|har128-g2-a1> --results-home
    #: <eval-lab-results> (each entry records its own labels/output), which
    #: refuses to run when the frozen labels change and records a
    #: missing/unknown page prediction as an explicit abstention.
    "additional_loop_calibrations": [
        {
            "cohort": "HAR-128 part 2: 40 HAR-116 trials",
            "frozen_at": "2026-10-01T07:57:30Z",
            "labels": "research/explorations/trace-lab/har128/labels_har116",
            "labels_manifest_sha256": "24b91001adf5707ebb65756e575a9acbc39a816173ee16a5da88cf989e99513d",
            "predictor": "trial_decision.classify_loop_kind (HAR-119 claim-vs-repetition rule)",
            "predictor_functions_sha256": "6909e778952053c8cf2b80ce52cf9804d1648a02aaa4ea74b9a4b384fe37c0ae",
            "in_sample": False,
            "rater_agreement_loop_kind": {"agree": 35, "n": 40},
            "excluded_rater_disagreement": 5,
            "page_vs_agreed_loop_kind": {"agree": 28, "n": 35},
            "abstentions": 0,
            "loop_kind_confusion": {
                "none": {"agree": 17, "n": 19},
                "repetition": {"agree": 8, "n": 13},
                "completion-claim": {"agree": 3, "n": 3},
            },
            "method": (
                "research/explorations/trace-lab/har119/score_page.py --labels "
                "research/explorations/trace-lab/har128/labels_har116 --results-home "
                "<eval-lab-results> --output "
                "research/experiments/har117-results-home/har131-page-calibration-har116.json"
            ),
            "artifact": "research/experiments/har117-results-home/har131-page-calibration-har116.json",
            "heldout": (
                "har116 001181 baseline/loopfix/loopfix-r2 (3 trials): analysis and "
                "calibration only, never training reflection"
            ),
            "limits": (
                "Loop kind only: this cohort carries no first-failure/blame calibration. "
                "Blind scout-agent raters (L116A1-8/L116B1-8), not human ground truth; "
                "rater A on har116-a-000383-baseline__igrXg8R reports off_limits_opened "
                "(config.json seen via a broad grep, arm info unused). "
                "Denominators stay per-cohort, never pooled. The artifact exports "
                "scores and report hashes only, not trial content."
            ),
        },
        {
            "cohort": "HAR-128 G2 attempt 1: 20 HAR-120 trials",
            "frozen_at": "2026-10-01T09:14:45Z",
            "labels": "research/explorations/trace-lab/har128/labels_g2_a1",
            "labels_manifest_sha256": "f6a11da4b3994c101785742f27565d169469e062764585a210baeccfb5b92674",
            "predictor": "trial_decision.classify_loop_kind (HAR-119 claim-vs-repetition rule)",
            "predictor_functions_sha256": "6909e778952053c8cf2b80ce52cf9804d1648a02aaa4ea74b9a4b384fe37c0ae",
            "in_sample": False,
            "rater_agreement_loop_kind": {"agree": 17, "n": 20},
            "excluded_rater_disagreement": 3,
            "page_vs_agreed_loop_kind": {"agree": 12, "n": 17},
            "abstentions": 0,
            "loop_kind_confusion": {
                "none": {"agree": 5, "n": 5},
                "repetition": {"agree": 4, "n": 6},
                "completion-claim": {"agree": 3, "n": 6},
            },
            "method": (
                "research/explorations/trace-lab/har119/score_page.py --published-cohort "
                "--cohort har128-g2-a1 --results-home "
                "<eval-lab-results> --output "
                "research/experiments/har117-results-home/har131-page-calibration-g2-a1.json"
            ),
            "artifact": "research/experiments/har117-results-home/har131-page-calibration-g2-a1.json",
            "heldout": (
                "G2 attempt-1 frozen cohort only: analysis and "
                "calibration only, never training reflection"
            ),
            "limits": (
                "Loop kind only: this cohort carries no first-failure/blame calibration. "
                "Blind scout-agent raters A (G2A1-4) / B (G2B1-4), not human ground truth; "
                "no reported off-limits openings. "
                "Denominators stay per-cohort, never pooled. The artifact exports "
                "scores and report hashes only, not trial content; inspection only, "
                "never training reflection."
            ),
        },
        {
            "cohort": "HAR-128 G2 re-run: 19 HAR-120 trials",
            "frozen_at": "2026-10-01T09:54:19Z",
            "labels": "research/explorations/trace-lab/har128/labels_g2_r2",
            "labels_manifest_sha256": "ddc1f2ad8bf52dc762067a367f7383b990164174b1732c108fb788167597f2ff",
            "predictor": "trial_decision.classify_loop_kind (HAR-119 claim-vs-repetition rule)",
            "predictor_functions_sha256": "6909e778952053c8cf2b80ce52cf9804d1648a02aaa4ea74b9a4b384fe37c0ae",
            "in_sample": False,
            "rater_agreement_loop_kind": {"agree": 16, "n": 19},
            "excluded_rater_disagreement": 3,
            "page_vs_agreed_loop_kind": {"agree": 10, "n": 16},
            "abstentions": 0,
            "loop_kind_confusion": {
                "none": {"agree": 1, "n": 2},
                "repetition": {"agree": 2, "n": 4},
                "completion-claim": {"agree": 7, "n": 10},
            },
            "method": (
                "research/explorations/trace-lab/har119/score_page.py --published-cohort "
                "--cohort har128-g2-r2 --labels <labels_g2_r2> --results-home "
                "<eval-lab-results> --output "
                "research/experiments/har117-results-home/har131-page-calibration-g2-r2.json"
            ),
            "artifact": "research/experiments/har117-results-home/har131-page-calibration-g2-r2.json",
            "heldout": (
                "G2 re-run frozen cohort only: analysis and "
                "calibration only, never training reflection"
            ),
            "limits": (
                "Loop kind only: this cohort carries no first-failure/blame calibration. "
                "Blind scout-agent raters A (G3A1-4) / B (G3B1-4), not human ground truth; "
                "no reported off-limits openings. "
                "Denominators stay per-cohort, never pooled. The artifact exports "
                "scores and report hashes only, not trial content; inspection only, "
                "never training reflection."
            ),
        },
        {
            "cohort": "HAR-128 G2 tail: 3 HAR-120 trials",
            "frozen_at": "2026-10-01T10:21:18Z",
            "labels": "research/explorations/trace-lab/har128/labels_g2_tail",
            "labels_manifest_sha256": "3b88eb4c2450d76f4b58533c12fae7a0ed25f0816bb45d1b3baeb72a54cdbb50",
            "predictor": "trial_decision.classify_loop_kind (HAR-119 claim-vs-repetition rule)",
            "predictor_functions_sha256": "6909e778952053c8cf2b80ce52cf9804d1648a02aaa4ea74b9a4b384fe37c0ae",
            "in_sample": False,
            "rater_agreement_loop_kind": {"agree": 3, "n": 3},
            "excluded_rater_disagreement": 0,
            "page_vs_agreed_loop_kind": {"agree": 1, "n": 3},
            "abstentions": 0,
            "loop_kind_confusion": {
                "none": {"agree": 0, "n": 0},
                "repetition": {"agree": 0, "n": 0},
                "completion-claim": {"agree": 1, "n": 3},
            },
            "method": (
                "research/explorations/trace-lab/har119/score_page.py --published-cohort "
                "--cohort har128-g2-tail --results-home <eval-lab-results> --output "
                "research/experiments/har117-results-home/har131-page-calibration-g2-tail.json"
            ),
            "artifact": "research/experiments/har117-results-home/har131-page-calibration-g2-tail.json",
            "heldout": "G2 tail freeze: analysis and calibration only, never training reflection",
            "limits": (
                "Loop kind only; no first-failure/blame calibration. Blind scout-agent "
                "raters G4A/G4B, not human ground truth; neither reports off-limits openings. "
                "Tiny cohort: three trials, all agreed labels completion-claim. "
                "Denominators stay per-cohort, never pooled. Scores and report hashes only, "
                "not trial content; never training reflection."
            ),
        },
    ],
    "grader_alignment": "opinion with no page-measured calibration on this cohort",
    "method": "research/explorations/trace-lab/har119/score_page.py",
    "artifact": "research/explorations/trace-lab/har119/page_scores.json",
    "limits": (
        "Out-of-sample for the loop rule. Loop kind: n=11 agreed cells. "
        "First failure: n=9 agreed cells with page coverage 1/12 (11 abstentions). "
        "Blame 11/11 is uninformative on this cohort: the raters say model on every "
        "failure, so a constant prior scores the same. Grader alignment stays opinion."
    ),
}

def build_decision(
    trial_dir: str | Path,
    *,
    reward: float | None,
    scored: bool,
    outcome: dict[str, Any] | None,
    first_failure: dict[str, Any] | None,
    grader_evidence: dict[str, Any] | None,
    taint: list[dict[str, Any]] | None,
    token_flow: dict[str, Any] | None,
    stop_reason: str | None = None,
    calls: int | None = None,
    tokens: dict[str, Any] | None = None,
    counts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``trial_decision/v3`` page for one trial directory."""
    from evallab import probe03

    trial = Path(trial_dir)
    outcome = outcome if isinstance(outcome, dict) else {}
    first = first_failure if isinstance(first_failure, dict) else None
    flags = [flag for flag in (taint or []) if isinstance(flag, dict)]
    rule_id = outcome.get("rule_id")
    attribution = outcome.get("attribution")
    instruction, instruction_bytes = probe03._instruction_text(trial)
    grader_gap, grader_tests = _grader_gap(
        trial, instruction, grader_evidence if isinstance(grader_evidence, dict) else None
    )
    attempts = [flag for flag in flags if flag.get("kind") == "upstream_fetch"]
    fetches = [flag for flag in attempts if confirmed_fetch(flag)]
    guards = [flag for flag in flags if flag.get("kind") == "guard_reject"]
    passed = scored and isinstance(reward, (int, float)) and float(reward) >= 1.0
    if passed and (fetches or guards):
        taint_status = "candidate"
    elif passed:
        taint_status = "clean"
    else:
        taint_status = "not_a_pass"
    edit = (token_flow or {}).get("last_useful_edit") if isinstance(token_flow, dict) else None
    edit = edit if isinstance(edit, dict) else None
    nop = _nop_same_crash(
        grader_evidence if isinstance(grader_evidence, dict) else None
    )
    fetched = _fetched_fix(fetches)
    loop = classify_loop_kind(trial, stop_reason, token_flow if isinstance(token_flow, dict) else None)
    whose = _whose(rule_id, attribution)
    rendered_counts = render_counts(counts)
    return {
        "schema": DECISION_SCHEMA,
        "reward": reward,
        "scored": scored,
        "rule_id": rule_id,
        "attribution": attribution,
        "whose": whose,
        "counts": rendered_counts,
        "facts": {
            "stop_reason": stop_reason,
            "calls": calls,
            "tokens": tokens,
            "fetches": [
                {
                    "step": flag.get("evidence"),
                    "command": flag.get("command"),
                    "target": flag.get("target"),
                    "document": flag.get("document"),
                    "call_id": flag.get("call_id"),
                    "outcome": flag.get("outcome", "unknown"),
                    "outcome_evidence": flag.get("outcome_evidence") or [],
                }
                for flag in attempts
            ],
            "verifier_message": _verifier_message(trial),
            "nop_same_crash": nop,
            "instruction_excerpt": _excerpt(instruction),
            "grader_gap": grader_gap,
        },
        "judgments": {
            "label": "opinion",
            "agreement": dict(PAGE_CALIBRATION),
            "first_failure": (
                None
                if first is None
                else {
                    "rule_id": first.get("rule_id"),
                    "attribution": first.get("attribution"),
                    "step": first.get("step_ref"),
                }
            ),
            "blame": {
                "attribution": attribution,
                "whose": whose,
                "rule_id": rule_id,
                "note": outcome.get("note"),
            },
            "loop_kind": loop,
            "grader_alignment": {
                "answer": grader_tests,
                "why": grader_gap,
            },
        },
        "asked": {
            "instruction_present": instruction is not None,
            "instruction_bytes": instruction_bytes,
            "excerpt": _excerpt(instruction),
            "grader_tests_asked": grader_tests,
            "grader_gap": grader_gap,
        },
        "did": {
            "note": outcome.get("note"),
            "step": outcome.get("step_ref"),
            "evidence_steps": list(outcome.get("evidence_step_refs") or []),
            "secondary": outcome.get("secondary"),
            "last_edit_step": edit.get("step_id") if edit else None,
            "last_edit_excerpt": edit.get("command_excerpt") if edit else None,
        },
        "reward_why": _reward_why(reward, scored, outcome.get("note")),
        "first_failure": (
            None
            if first is None
            else {
                "rule_id": first.get("rule_id"),
                "attribution": first.get("attribution"),
                "step": first.get("step_ref"),
            }
        ),
        "pass_tainted": {
            "flagged": taint_status == "candidate",
            "status": taint_status,
            "reasons": _taint_reasons(fetches, guards),
        },
        "nop_same_crash": nop,
        "fetched_fix": fetched,
    }


def render_counts(counts: dict[str, Any] | None) -> dict[str, Any]:
    """Render HAR-78's field. Never compute a verdict."""
    if not isinstance(counts, dict) or counts.get("verdict") not in COUNTED_VERDICTS:
        return {
            "status": "pending",
            "verdict": None,
            "reasons": [],
            "evidence": [],
            "task_ledger": {
                "status": "unavailable",
                "reason": "Counts are pending, so task usability is unknown.",
            },
            "note": (
                "Pending. HAR-78 has not written counts on this record. "
                "This page does not decide whether the result counts."
            ),
        }
    return {
        "status": "rendered",
        "verdict": counts.get("verdict"),
        "reasons": list(counts.get("reasons") or []),
        "evidence": list(counts.get("evidence") or []),
        "task_ledger": render_task_ledger(counts),
        "note": "Rendered from the HAR-78 counts field. Not computed here.",
    }


def render_task_ledger(counts: dict[str, Any]) -> dict[str, Any]:
    """Display-only read of the counts ``task_status`` block.

    Never infers usable: a missing block, or a block with no matched
    status, renders as unknown, and a digest mismatch says the ledger
    was not matched while the legacy verdict stands.
    """
    status = counts.get("task_status")
    if isinstance(status, dict):
        rendered: dict[str, Any] = {
            "status": status.get("status"),
            "ledger_status": status.get("ledger_status"),
            "source": status.get("source"),
            "path": status.get("path"),
            "source_sha256": status.get("source_sha256"),
            "task_id": status.get("task_id"),
            "run_digest": status.get("run_digest"),
            "trial_digest": status.get("trial_digest"),
            "digest_match": status.get("digest_match"),
            "reason": status.get("reason"),
            "evidence": status.get("evidence"),
        }
        if rendered["digest_match"] is False:
            rendered["reason"] = (
                (str(rendered["reason"] or "") + " " if rendered.get("reason") else "")
                + "Ledger row not matched to this run; the legacy counts verdict stands."
            ).strip()
        if rendered["status"] is None and rendered["digest_match"] is not True:
            rendered["status"] = "unknown"
            if not rendered.get("reason"):
                rendered["reason"] = (
                    "No task-status signal matched this run; "
                    "absence is not a usable label."
                )
        return rendered
    legacy = [
        item
        for item in (counts.get("evidence") or [])
        if isinstance(item, dict) and item.get("reason") == "task_not_usable"
    ]
    if legacy:
        return {
            "status": "not_usable",
            "source": "legacy",
            "reason": "Legacy counts evidence marks the task not usable.",
            "evidence": legacy,
        }
    return {
        "status": "unknown",
        "reason": "No task-status signal was provided; absence is not a usable label.",
    }


def classify_loop_kind(
    trial_dir: str | Path,
    stop_reason: str | None,
    token_flow: dict[str, Any] | None,
) -> dict[str, Any]:
    """HAR-119's published split: completion-claim loop vs repetition."""
    from evallab import probe03

    onset = (token_flow or {}).get("loop_onset") if isinstance(token_flow, dict) else None
    onset_step = onset.get("step_id") if isinstance(onset, dict) else None
    try:
        _coverage, assembled = probe03.assemble_trial(Path(trial_dir))
    except (OSError, ValueError):
        assembled = []
    agent_seq = [
        (doc, step)
        for doc, step in assembled
        if str(step.get("source", "")).lower() in probe03.AGENT_SOURCES
    ]
    first = None
    for index, (_doc, step) in enumerate(agent_seq):
        if probe03.CONFIRM_PROMPT_RE.search(str(probe03.obs_content(step) or "")):
            first = index
            break
    after = agent_seq[first + 1 :] if first is not None else []
    claim = sum(1 for _doc, step in after if _claim_bearing(step))
    turns = len(after)
    fraction = (claim / turns) if turns else 0.0
    if turns >= CLAIM_LOOP_MIN_TURNS and fraction >= CLAIM_LOOP_MIN_FRACTION:
        kind = "completion-claim"
    elif onset_step is not None:
        kind = "repetition"
    else:
        kind = "none"
    return {
        "kind": kind,
        "rule": "HAR-119",
        "turns_after_prompt": turns,
        "claim_bearing_turns": claim,
        "claim_fraction": round(fraction, 4),
        "first_prompt_step": agent_seq[first][1].get("step_id") if first is not None else None,
        "loop_onset_step": onset_step,
        "stop_reason": stop_reason,
    }


def _claim_bearing(step: dict) -> bool:
    from evallab import probe03

    text = str(step.get("message") or "")
    if probe03.COMPLETION_CLAIM_RE.search(text) or probe03.ECHO_TASK_COMPLETE_RE.search(text):
        return True
    if "task_complete" in text.lower():
        return True
    raw_extra = step.get("extra")
    extra: dict[str, Any] = raw_extra if isinstance(raw_extra, dict) else {}
    raw_layer = extra.get("step_layers")
    layer: dict[str, Any] = raw_layer if isinstance(raw_layer, dict) else {}
    return layer.get("task_complete") is True or bool(layer.get("prose_completion"))


def render_decision_markdown(decision: dict[str, Any] | None) -> list[str]:
    """Facts (observed signals), then the counts verdict, then opinions."""
    if not decision:
        return ["## Decision", "", "Unavailable. The composer did not run.", ""]
    facts = decision.get("facts") or {}
    judgments = decision.get("judgments") or {}
    counts = decision.get("counts") or {}
    agreement = judgments.get("agreement") or {}
    nop = facts.get("nop_same_crash") or {}
    fetches = facts.get("fetches") or []
    blame = judgments.get("blame") or {}
    first = judgments.get("first_failure") or {}
    loop = judgments.get("loop_kind") or {}
    align = judgments.get("grader_alignment") or {}
    tokens = facts.get("tokens") or {}
    tainted = decision.get("pass_tainted") or {}
    fetch = decision.get("fetched_fix") or {}
    ledger = counts.get("task_ledger") or {}
    fetch_line = (
        "; ".join(
            f"`{item.get('step')}` `{item.get('command')}` — `{item.get('outcome', 'unknown')}`"
            for item in fetches
        )
        or "no fetch command observed"
    )
    lines = [
        "## Decision",
        "",
        "### Counts",
        "",
        str(counts.get("note") or "Pending."),
        f"Verdict: `{counts.get('verdict') or 'pending'}`.",
        f"Reasons: {_backticked(counts.get('reasons'))}.",
        f"Evidence: {_evidence_line(counts.get('evidence'))}.",
        (
            f"Task ledger: `{ledger.get('status')}`"
            + (f" ({ledger.get('source')})" if ledger.get("source") else "")
            + (f". {ledger.get('reason')}" if ledger.get("reason") else "")
        ),
        "",
        "### Facts",
        "",
        "Observed signals copied from the trial records. Not a reading of why it failed.",
        f"- Stop reason: `{facts.get('stop_reason')}`.",
        f"- Calls: `{facts.get('calls')}`.",
        (
            f"- Tokens: input `{tokens.get('input_tokens')}`, "
            f"output `{tokens.get('output_tokens')}`; "
            f"source `{tokens.get('source') or 'unknown'}`, "
            f"attribution `{tokens.get('attribution') or 'unknown'}`"
            + (f"; {tokens['reason']}." if tokens.get("reason") else ".")
            if isinstance(tokens, dict)
            else "- Tokens: not on the record."
        ),
        f"- Fetch commands observed: {fetch_line}.",
        f"- Verifier: reward `{decision.get('reward')}`"
        + (
            f"; `{str(facts.get('verifier_message'))[:180]}`."
            if facts.get("verifier_message")
            else "."
        ),
        (
            f"- Nop crash the same way: {_answer_words(nop.get('answer'))}. "
            f"{nop.get('reason')}"
        ),
        "- What the task asked: "
        + (facts.get("instruction_excerpt") or "instruction not located."),
        "- Grader signal (literal checks only, not a semantic verdict): "
        + (facts.get("grader_gap") or "no signal."),
        "",
        "### Judgments",
        "",
        "Opinions. Do not treat these as facts.",
        _calibration_line(agreement),
        (
            f"- First failure: `{first.get('rule_id')}` at `{first.get('step')}`. "
            + _field_calibration(agreement, "page_vs_agreed_first_failure", "first_failure_coverage")
            if first
            else "- First failure: none recorded. "
            + _field_calibration(agreement, "page_vs_agreed_first_failure", "first_failure_coverage")
        ),
        (
            f"- Blame: probe-03 `{blame.get('attribution')}` (`{blame.get('rule_id')}`), "
            f"page reading `{blame.get('whose')}`. "
            + _field_calibration(agreement, "page_vs_agreed_blame", None)
        ),
        (
            f"- Loop kind: `{loop.get('kind')}`. "
            f"{loop.get('claim_bearing_turns')} of {loop.get('turns_after_prompt')} "
            "turns after the confirm prompt claimed completion. "
            + _field_calibration(agreement, "page_vs_agreed_loop_kind", None)
        ),
        (
            f"- Grader alignment: `{align.get('answer')}`. {align.get('why') or ''} "
            "Opinion: a nonzero test count alone never means the grader tests the instruction."
        ),
        (
            f"- Upstream acquisition: confirmed at `{fetch.get('step')}`. "
            "Whether it supplied the graded fix is unjudged."
            if fetch.get("fetched")
            else "- Upstream acquisition: not confirmed; failed/unknown attempts are non-deciding."
        ),
        (
            f"- Pass taint: `{tainted.get('status')}` "
            f"({', '.join(tainted.get('reasons') or []) or 'no reasons'}). "
            "A flag for counts, not a ruling."
        ),
        "",
    ]
    return lines


def _backticked(values: Any) -> str:
    items = [f"`{value}`" for value in (values or []) if value is not None]
    return ", ".join(items) or "none"


def _evidence_line(evidence: Any) -> str:
    if not isinstance(evidence, list) or not evidence:
        return "none"
    parts = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        bits = [str(item.get("reason") or "evidence"), str(item.get("detector") or "unknown")]
        if item.get("step") is not None:
            bits.append(f"step {item.get('step')}")
        if item.get("command"):
            bits.append(f"`{str(item.get('command'))[:80]}`")
        elif item.get("excerpt"):
            bits.append(str(item.get("excerpt"))[:80])
        if item.get("path"):
            bits.append(str(item.get("path")))
        parts.append(" ".join(bits))
    return "; ".join(parts) or "none"


def _calibration_line(agreement: dict[str, Any]) -> str:
    if not isinstance(agreement, dict) or not agreement.get("predictor"):
        return "Calibration: unavailable for this page."
    kind = agreement.get("page_vs_agreed_loop_kind") or {}
    rater = agreement.get("rater_agreement_loop_kind") or {}
    text = (
        f"Calibration: {agreement.get('predictor')}; cohort {agreement.get('cohort')} "
        f"(frozen {agreement.get('frozen_at')}); page loop kind "
        f"`{kind.get('agree')}/{kind.get('n')}` vs agreed rater cells "
        f"(eligible {agreement.get('eligible_n')}, "
        f"{agreement.get('excluded_rater_disagreement')} excluded on rater disagreement, "
        f"{agreement.get('abstentions')} abstentions); rater agreement "
        f"`{rater.get('agree')}/{rater.get('n')}`; out-of-sample: "
        f"{'no' if agreement.get('in_sample') else 'yes'}. "
        f"Method: {agreement.get('method')}. {agreement.get('limits')}"
    )
    for extra in agreement.get("additional_loop_calibrations") or []:
        if not isinstance(extra, dict):
            continue
        extra_kind = extra.get("page_vs_agreed_loop_kind") or {}
        extra_rater = extra.get("rater_agreement_loop_kind") or {}
        text += (
            f"\n\nAdditional loop-kind-only calibration, same predictor, separately "
            f"denominated cohort {extra.get('cohort')} (frozen {extra.get('frozen_at')}): "
            f"page loop kind `{extra_kind.get('agree')}/{extra_kind.get('n')}` vs agreed "
            f"rater cells ({extra_rater.get('agree')} agreed of {extra_rater.get('n')}, "
            f"{extra.get('excluded_rater_disagreement')} excluded on rater disagreement, "
            f"{extra.get('abstentions')} abstentions); rater agreement "
            f"`{extra_rater.get('agree')}/{extra_rater.get('n')}`. Loop kind only on this "
            f"cohort: no first-failure/blame calibration, denominators never pooled. "
            f"Method: {extra.get('method')}. Artifact: {extra.get('artifact')}. "
            f"{extra.get('limits')}"
        )
    return text
def _field_calibration(
    agreement: dict[str, Any], score_key: str, coverage_key: str | None
) -> str:
    """One judgment's measured error rate plus its coverage, from PAGE_CALIBRATION."""
    if not isinstance(agreement, dict):
        return "Opinion; calibration unavailable."
    score = agreement.get(score_key) or {}
    if not isinstance(score, dict) or score.get("n") is None:
        return "Opinion; calibration unavailable."
    text = (
        f"Opinion; {agreement.get('cohort')}: page "
        f"`{score.get('agree')}/{score.get('n')}` vs agreed rater cells"
    )
    if score_key == "page_vs_agreed_loop_kind":
        for extra in agreement.get("additional_loop_calibrations") or []:
            if not isinstance(extra, dict):
                continue
            extra_score = extra.get(score_key) or {}
            if isinstance(extra_score, dict) and extra_score.get("n") is not None:
                text += (
                    f"; {extra.get('cohort')}: page "
                    f"`{extra_score.get('agree')}/{extra_score.get('n')}`"
                )
    if coverage_key:
        coverage = agreement.get(coverage_key) or {}
        text += (
            f" (coverage {coverage.get('expressed')}/{coverage.get('of')}, "
            f"{coverage.get('abstentions')} abstentions)"
        )
    if score_key == "page_vs_agreed_blame":
        text += " (uninformative: raters near-constant model)"
    return text + "."


def compare_hand_key(decision: dict[str, Any], hand_row: dict[str, Any]) -> dict[str, Any]:
    """Compare the page's probe-03 fields to one frozen hand-key row.

    The hand key's ``outcome.rule`` and ``outcome.attribution`` are the
    contract. ``whose`` is not compared: it is a page reading, not a label
    the readers wrote.
    """
    raw_outcome = hand_row.get("outcome")
    outcome: dict[str, Any] = raw_outcome if isinstance(raw_outcome, dict) else {}
    return {
        "rule_match": decision.get("rule_id") == outcome.get("rule"),
        "attribution_match": decision.get("attribution") == outcome.get("attribution"),
        "decision_rule": decision.get("rule_id"),
        "hand_rule": outcome.get("rule"),
        "decision_attribution": decision.get("attribution"),
        "hand_attribution": outcome.get("attribution"),
    }


def _whose(rule_id: Any, attribution: Any) -> str:
    if rule_id == "R-ENV-02":
        return "task"
    if attribution == "harness":
        return "harness"
    if attribution == "model":
        return "model"
    if attribution == "n/a":
        return "none"
    return "unclear"


def _excerpt(instruction: str | None) -> str | None:
    if not instruction:
        return None
    collapsed = " ".join(instruction.split())
    if len(collapsed) <= _EXCERPT_CHARS:
        return collapsed
    return collapsed[: _EXCERPT_CHARS - 1].rstrip() + "…"


def _grader_gap(
    trial: Path, instruction: str | None, grader_evidence: dict[str, Any] | None
) -> tuple[str | None, str]:
    from evallab import probe03

    assertion = probe03.source_text_assertion(trial)
    if assertion:
        return assertion, "no"
    message = _verifier_message(trial)
    note = probe03.completion_grader_check(trial, message)
    if isinstance(note, str) and note.startswith("deliverable_not_in_instruction"):
        return note, "no"
    if isinstance(note, str) and note.startswith("deliverable_partially_in_instruction"):
        return note, "partial"
    if grader_evidence:
        return str(grader_evidence.get("note") or "suspect grader"), "no"
    if instruction is None:
        return "instruction.md not located; deliverable check skipped", "unknown"
    passage = probe03._verifier_passage(trial)
    total = passage.get("total")
    if isinstance(total, int) and total > 0:
        fails = passage.get("fails")
        return (
            f"no named gap; verifier ran {total} tests"
            + (f", {fails} failed" if isinstance(fails, int) else "")
            + ". A nonzero test count alone never means the grader tests the instruction.",
            "unknown",
        )
    return "verifier passage has no test count and no named gap", "unknown"




def _answer_words(answer: Any) -> str:
    return str(answer or "unknown").replace("_", " ")




def _verifier_message(trial: Path) -> str:
    path = trial / "verifier" / "result.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("message") or "")


def _reward_why(reward: float | None, scored: bool, note: Any) -> str:
    if not scored:
        base = "Unscored. The reward is not a capability result."
    elif reward is None:
        base = "Scored, but the reward value is missing."
    else:
        base = f"Reward `{reward}`."
    if isinstance(note, str) and note.strip():
        detail = note.strip()
        detail = detail[0].upper() + detail[1:]
        if detail[-1] not in ".!?":
            detail += "."
        return f"{base} {detail}"
    return base


def _taint_reasons(fetches: list[dict[str, Any]], guards: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    for flag in fetches:
        reasons.append(f"upstream_fetch:{flag.get('rule')}")
    reasons.extend("guard_reject" for _flag in guards)
    return reasons


def _nop_same_crash(grader_evidence: dict[str, Any] | None) -> dict[str, Any]:
    if not grader_evidence:
        return {
            "answer": "unknown",
            "reason": "No same-task nop crash comparison was recorded.",
            "nop_trial": None,
        }
    confirm = grader_evidence.get("nop_control_confirms")
    nop_trial = grader_evidence.get("nop_trial")
    if confirm == "true":
        answer, reason = "yes", "The nop control crashes the same way."
    elif confirm == "false":
        answer, reason = "no", "The nop control does not crash the same way."
    else:
        answer, reason = "unknown", "No same-task nop was supplied, so the crash is unconfirmed."
    if nop_trial:
        reason = f"{reason} Nop trial `{nop_trial}`."
    return {"answer": answer, "reason": reason, "nop_trial": nop_trial}


def _fetched_fix(fetches: list[dict[str, Any]]) -> dict[str, Any]:
    if not fetches:
        return {"fetched": False, "step": None, "command": None, "kind": None, "note": None}
    first = fetches[0]
    return {
        "fetched": True,
        "step": first.get("evidence"),
        "command": first.get("command"),
        "kind": first.get("rule"),
        "target": first.get("target"),
        "outcome_evidence": first.get("outcome_evidence"),
        "note": (
            "Upstream acquisition was confirmed. Whether it supplied the graded fix is unjudged: "
            "acquisition is a signal, not proof of copied code."
        ),
    }


