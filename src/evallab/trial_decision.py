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

DECISION_SCHEMA = "trial_decision/v1"
_EXCERPT_CHARS = 400


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
) -> dict[str, Any]:
    """Build the ``trial_decision/v1`` page for one trial directory."""
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
    return {
        "schema": DECISION_SCHEMA,
        "reward": reward,
        "scored": scored,
        "rule_id": rule_id,
        "attribution": attribution,
        "whose": _whose(rule_id, attribution),
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
        "nop_same_crash": _nop_same_crash(
            grader_evidence if isinstance(grader_evidence, dict) else None
        ),
        "fetched_fix": _fetched_fix(fetches),
    }


def render_decision_markdown(decision: dict[str, Any] | None) -> list[str]:
    """Plain-language lines for the top of the trial report."""
    if not decision:
        return ["## Decision", "", "Unavailable. The composer did not run.", ""]
    asked = decision.get("asked") or {}
    did = decision.get("did") or {}
    tainted = decision.get("pass_tainted") or {}
    nop = decision.get("nop_same_crash") or {}
    fetch = decision.get("fetched_fix") or {}
    lines = [
        "## Decision",
        "",
        decision.get("reward_why") or "Reward unknown.",
        (
            f"Probe-03 attribution `{decision.get('attribution')}` "
            f"(`{decision.get('rule_id')}`). The page reads that as "
            f"**{decision.get('whose')}**."
        ),
        "",
        "**What the task asked.** "
        + (
            asked["excerpt"]
            if asked.get("excerpt")
            else "Instruction not located, so this page cannot quote it."
        ),
        "",
        f"**Does the grader test that?** {asked.get('grader_tests_asked')}. "
        + (asked.get("grader_gap") or "No named grader gap."),
        "",
        "**What the model did.** "
        + (did.get("secondary") or did.get("note") or "No outcome note.")
        + _step_sentence(did)
        + _edit_sentence(did),
        "",
        "**Pass tainted?** "
        + _taint_sentence(tainted)
        + " "
        + _fetch_sentence(fetch),
        "",
        "**Nop crash the same way?** "
        + f"{nop.get('answer')}. {nop.get('reason')}",
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
    if isinstance(note, str) and note:
        return f"{base} {note}"
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
