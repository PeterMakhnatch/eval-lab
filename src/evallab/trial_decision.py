"""One-page trial decision, composed from records already on disk.

No new detector. The page reads the probe-03 outcome, the existing grader
checks (literal source assertion, deliverable named by the verifier but
absent from the instruction, setup/import crash), the process-job taint
flags, and token-flow's last useful edit. ``rule_id`` and ``attribution``
are the probe-03 fields the frozen hand keys use. ``whose`` is a reading
of that rule for the page: ``R-ENV-02`` is the task, even though probe-03
attributes that rule to the harness.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DECISION_SCHEMA = "trial_decision/v2"
_EXCERPT_CHARS = 400
#: HAR-119 published rule: 10 turns after the first confirm prompt, and at
#: least half of those turns claim completion. Not a new detector.
CLAIM_LOOP_MIN_TURNS = 10
CLAIM_LOOP_MIN_FRACTION = 0.5
COUNTED_VERDICTS = frozenset({"counted_pass", "counted_fail", "excluded"})
#: In-sample only. Replaced after measurement against the HAR-109 keys.
JUDGMENT_AGREEMENT = {
    "source": "HAR-109 hand labels, in-sample",
    "n": 10,
    "out_of_sample": "pending HAR-119 part 2; those labels are not frozen",
    "first_failure_within_2": "3/10 (30%)",
    "blame_exact": "8/10 (80%)",
    "loop_kind": "not measured; HAR-119 part 2 labels are not frozen",
    "note": (
        "First failure within +-2 steps: 3/10 (compare Who&When benchmark best: 14.2%, "
        "arXiv 2505.00212). Blame under stated mapping: 8/10 (2 misses are leaked passes "
        "graded 1.0 by the verifier). In-sample numbers from HAR-109."
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
    """Build the ``trial_decision/v2`` page for one trial directory."""
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
    fetches = [flag for flag in flags if flag.get("kind") == "upstream_fetch"]
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
    nop = _nop_same_crash(grader_evidence if isinstance(grader_evidence, dict) else None)
    fetched = _fetched_fix(fetches)
    loop = classify_loop_kind(trial, stop_reason, token_flow if isinstance(token_flow, dict) else None)
    whose = _whose(rule_id, attribution)
    return {
        "schema": DECISION_SCHEMA,
        "reward": reward,
        "scored": scored,
        "rule_id": rule_id,
        "attribution": attribution,
        "whose": whose,
        "counts": render_counts(counts),
        "facts": {
            "stop_reason": stop_reason,
            "calls": calls,
            "tokens": tokens,
            "fetches": [
                {"step": flag.get("evidence"), "command": flag.get("command")}
                for flag in fetches
            ],
            "verifier_message": _verifier_message(trial),
            "nop_same_crash": nop,
            "task_ledger": {
                "status": "unchecked",
                "reason": "No task ledger was passed to this page. HAR-115's ledger is not read here.",
            },
            "instruction_excerpt": _excerpt(instruction),
            "grader_tests_asked": grader_tests,
            "grader_gap": grader_gap,
        },
        "judgments": {
            "label": "opinion",
            "agreement": dict(JUDGMENT_AGREEMENT),
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
        "note": "Rendered from the HAR-78 counts field. Not computed here.",
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
    """Facts first, then the pending-or-rendered verdict, then opinions."""
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
    tokens = facts.get("tokens") or {}
    fetch_line = (
        "; ".join(f"`{item.get('step')}` `{item.get('command')}`" for item in fetches)
        or "no"
    )
    lines = [
        "## Decision",
        "",
        "### Counts",
        "",
        str(counts.get("note") or "Pending."),
        f"Verdict: `{counts.get('verdict') or 'pending'}`.",
        "",
        "### Facts",
        "",
        "Copied from the trial records. Not a reading of why it failed.",
        f"- Stop reason: `{facts.get('stop_reason')}`.",
        f"- Calls: `{facts.get('calls')}`.",
        (
            f"- Tokens: input `{tokens.get('input_tokens')}`, "
            f"output `{tokens.get('output_tokens')}`."
            if isinstance(tokens, dict)
            else "- Tokens: not on the record."
        ),
        f"- Fetched outside code: {fetch_line}.",
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
        (
            f"- Task ledger: `{(facts.get('task_ledger') or {}).get('status')}`. "
            f"{(facts.get('task_ledger') or {}).get('reason') or ''}"
        ),
        "- What the task asked: "
        + (facts.get("instruction_excerpt") or "instruction not located."),
        (
            f"- Does the grader test that? {facts.get('grader_tests_asked')}. "
            + (facts.get("grader_gap") or "No named grader gap.")
        ),
        "",
        "### Judgments",
        "",
        "Opinions. Do not treat these as facts.",
        (
            f"Measured agreement: {agreement.get('source')}, n={agreement.get('n')}. "
            f"Out of sample: {agreement.get('out_of_sample')}."
        ),
        (
            f"- First failure: `{first.get('rule_id')}` at `{first.get('step')}`. "
            f"In-sample within ±2 steps: `{agreement.get('first_failure_within_2')}`."
            if first
            else "- First failure: none recorded."
        ),
        (
            f"- Blame: probe-03 `{blame.get('attribution')}` (`{blame.get('rule_id')}`), "
            f"page reading `{blame.get('whose')}`. "
            f"In-sample exact match under the stated mapping: `{agreement.get('blame_exact')}`."
        ),
        (
            f"- Loop kind: `{loop.get('kind')}`. "
            f"{loop.get('claim_bearing_turns')} of {loop.get('turns_after_prompt')} "
            "turns after the confirm prompt claimed completion. "
            f"Agreement: {agreement.get('loop_kind')}."
        ),
        "",
    ]
    return lines


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
            + ". Not a semantic certificate.",
            "yes",
        )
    return "verifier passage has no test count and no named gap", "unknown"


def _ended(text: Any) -> str:
    sentence = str(text).strip()
    if not sentence:
        return "No outcome note."
    if sentence[-1] not in ".!?":
        sentence += "."
    return sentence


def _answer_words(answer: Any) -> str:
    return str(answer or "unknown").replace("_", " ")


def _step_sentence(did: dict[str, Any]) -> str:
    step = did.get("step")
    refs = [ref for ref in (did.get("evidence_steps") or []) if ref]
    if step:
        return f" Step `{step}`."
    if refs:
        return f" Evidence steps: {', '.join(f'`{ref}`' for ref in refs[:4])}."
    return " No outcome step."


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
            "answer": "not_this_shape",
            "reason": "This trial's verifier did not fail at setup or import.",
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
        return {"fetched": False, "step": None, "command": None, "kind": None}
    first = fetches[0]
    return {
        "fetched": True,
        "step": first.get("evidence"),
        "command": first.get("command"),
        "kind": first.get("rule"),
    }


def _edit_sentence(did: dict[str, Any]) -> str:
    step = did.get("last_edit_step")
    if step is None:
        return ""
    excerpt = did.get("last_edit_excerpt")
    sentence = f" Last useful edit: step `{step}`"
    if excerpt:
        sentence += f" (`{excerpt}`)"
    return sentence + "."


def _taint_sentence(tainted: dict[str, Any]) -> str:
    status = tainted.get("status")
    if status == "candidate":
        reasons = ", ".join(tainted.get("reasons") or []) or "unnamed"
        return (
            f"candidate ({reasons}). A pass with a fetch or a guard reject. "
            "Not a coordinator ruling."
        )
    if status == "clean":
        return "no. The pass has no fetch and no guard reject."
    return "not a pass, so there is nothing to exclude from training."


def _fetch_sentence(fetch: dict[str, Any]) -> str:
    if not fetch.get("fetched"):
        return "Fetched the fix: no."
    return (
        f"Fetched the fix: yes, step `{fetch.get('step')}`, "
        f"`{fetch.get('command')}`."
    )
