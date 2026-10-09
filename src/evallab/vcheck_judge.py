"""Blind adjudicator for VerifierCheck red/blue verifier audits.

The adjudicator model sees ONLY the task instruction, the frozen requirement
map, the candidate submission diff, and the harness-observed verifier checks.
It NEVER sees the reward, the red agent's strategy/prose, or any trajectory.

Confirmation requires BOTH answers to be yes::

    (1) Is the mutant behaviorally different (rule out equivalent mutants)?
    (2) Does the instruction require the changed behavior (cite requirement id)?

Only a ``confirm`` recommendation from this blind pass — followed by
:func:`confirm_hypothesis` (human/objective confirmation) — promotes a
hypothesis to a finding.
"""

from __future__ import annotations

import inspect
import json
import re
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "ADJUDICATOR_MAX_TOKENS",
    "ADJUDICATOR_MODEL",
    "ADJUDICATOR_REASONING_EFFORT",
    "ADJUDICATOR_TEMPERATURE",
    "RECOMMENDATIONS",
    "adjudicate",
    "build_adjudication_prompt",
    "confirm_hypothesis",
    "parse_adjudication_response",
]

ADJUDICATOR_MODEL = "glm-5.3"
ADJUDICATOR_TEMPERATURE = 0.1
ADJUDICATOR_MAX_TOKENS = 4096
ADJUDICATOR_REASONING_EFFORT = "low"

RECOMMENDATIONS = ("confirm", "reject", "needs_evidence")

_CONFIRMATION_QUESTIONS = (
    "(1) Is the mutant behaviorally different from a correct solution "
    "(rule out equivalent mutants)? "
    "(2) Does the task instruction require the changed behavior "
    "(cite the requirement id)?"
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _requirement_lines(requirement_map: Any) -> list[str]:
    if isinstance(requirement_map, dict):
        items: Any = requirement_map.get("items", requirement_map)
    else:
        items = getattr(requirement_map, "items", requirement_map)
        if isinstance(items, dict):
            items = items.get("items", items)
    lines: list[str] = []
    if isinstance(items, dict):
        for req_id, text in items.items():
            lines.append(f"{req_id}: {text}")
        return lines
    for item in items or []:
        if isinstance(item, dict):
            req_id = item.get("req_id", item.get("id", "?"))
            text = item.get("text", "")
        else:
            req_id = getattr(item, "req_id", getattr(item, "id", "?"))
            text = getattr(item, "text", "")
        lines.append(f"{req_id}: {text}")
    return lines


def build_adjudication_prompt(
    *,
    instruction: str,
    requirement_map: Any,
    submission_diff: str,
    observed_checks: list[str],
) -> str:
    """Build the blind-adjudication prompt from ONLY the allowed inputs.

    There is deliberately no ``reward`` or ``strategy`` parameter: anything
    the caller knows about the verifier's reward or the red agent's prose
    cannot be forwarded because there is nowhere to put it.
    """
    req_lines = _requirement_lines(requirement_map)
    requirements = "\n".join(req_lines) if req_lines else "(no requirements recorded)"
    checks = "\n".join(f"- {c}" for c in observed_checks) if observed_checks else "(none)"
    return (
        "You are a blind adjudicator auditing whether a benchmark verifier "
        "(grader) is defective. You see ONLY the task instruction, the frozen "
        "requirement map, the candidate submission diff, and the verifier "
        "checks the harness observed. You do NOT see the verifier's reward, "
        "the red agent's strategy, or any trajectory prose — do not ask for them.\n"
        "\n"
        "Confirm the defect ONLY if both confirmation questions are answered yes:\n"
        f"{_CONFIRMATION_QUESTIONS}\n"
        "\n"
        "## Task instruction\n"
        f"{instruction}\n"
        "\n"
        "## Frozen requirement map\n"
        f"{requirements}\n"
        "\n"
        "## Candidate submission diff\n"
        f"{submission_diff}\n"
        "\n"
        "## Harness-observed verifier checks\n"
        f"{checks}\n"
        "\n"
        "Reply with a single JSON object and nothing else: "
        '{"recommend": "confirm" | "reject" | "needs_evidence", '
        '"rationale": "<one or two sentences>", '
        '"requirement_id": "<req id, required when recommend is confirm>"}. '
        'Recommend "confirm" only when the submission is behaviorally wrong '
        "yet the changed behavior is required by a cited requirement; "
        '"reject" for equivalent mutants or behavior the instruction does not '
        'require; "needs_evidence" when the inputs above are insufficient.'
    )


def parse_adjudication_response(text: str) -> dict[str, str]:
    """Parse the adjudicator's reply into a constrained recommend/rationale dict.

    Unparseable or out-of-vocabulary replies collapse to ``needs_evidence``
    so callers only ever observe values in :data:`RECOMMENDATIONS`.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {
            "recommend": "needs_evidence",
            "rationale": "Adjudicator reply contained no JSON object.",
        }
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {
            "recommend": "needs_evidence",
            "rationale": "Adjudicator reply was not valid JSON.",
        }
    if not isinstance(payload, dict):
        return {
            "recommend": "needs_evidence",
            "rationale": "Adjudicator reply was not a JSON object.",
        }
    recommend = str(payload.get("recommend", "needs_evidence")).strip().lower()
    if recommend not in RECOMMENDATIONS:
        return {
            "recommend": "needs_evidence",
            "rationale": f"Unknown recommend value {payload.get('recommend')!r}; "
            "treated as needs_evidence.",
        }
    rationale = str(payload.get("rationale", "")).strip()
    if not rationale:
        rationale = "No rationale supplied by adjudicator."
    return {"recommend": recommend, "rationale": rationale}


def _response_text(response: Any) -> str:
    """Extract reply text from the CLIENT response shapes (dict/OpenAI-like)."""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        for key in ("content", "text", "output"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message", first)
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        parts = [
                            p.get("text", "")
                            for p in content
                            if isinstance(p, dict) and isinstance(p.get("text"), str)
                        ]
                        if parts:
                            return "".join(parts)
                text = first.get("text")
                if isinstance(text, str):
                    return text
        return ""
    for attr in ("content", "text"):
        value = getattr(response, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _call_client(client: Any, *, model: str, messages: list[dict[str, str]], budget: Any) -> Any:
    """Invoke the CLIENT chat-completion surface across its calling conventions.

    The CLIENT worker owns ``chat_completion(model, messages, ..., budget,
    run_dir, key_provider)``; fakes in tests expose a subset. Signature
    inspection keeps both working without importing the sibling module.
    """
    fn = getattr(client, "chat_completion", None)
    if fn is None and callable(client):
        fn = client
    if fn is None or not callable(fn):
        raise TypeError("client must expose chat_completion(...) or be callable")
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": ADJUDICATOR_TEMPERATURE,
        "max_tokens": ADJUDICATOR_MAX_TOKENS,
        "budget": budget,
        "run_dir": None,
        "key_provider": None,
    }
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    if params and not accepts_kwargs:
        kwargs = {k: v for k, v in kwargs.items() if k in params}
        if "reasoning_effort" in params:
            kwargs["reasoning_effort"] = ADJUDICATOR_REASONING_EFFORT
    elif not params:
        pass  # Builtin/callable without signature: send the full surface.
    else:
        kwargs["reasoning_effort"] = ADJUDICATOR_REASONING_EFFORT
    return fn(**kwargs)


def adjudicate(
    *,
    instruction: str,
    requirement_map: Any,
    submission_diff: str,
    observed_checks: list[str],
    client: Any,
    budget: Any,
) -> dict[str, str]:
    """Run one blind adjudication; return ``{recommend, rationale}``.

    The model sees ONLY ``instruction``, ``requirement_map``,
    ``submission_diff`` and ``observed_checks`` — this function has no
    ``reward`` or ``strategy`` parameter, so those values cannot leak into
    the prompt even when the caller holds them.
    """
    prompt = build_adjudication_prompt(
        instruction=instruction,
        requirement_map=requirement_map,
        submission_diff=submission_diff,
        observed_checks=observed_checks,
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You are a blind verifier auditor. Judge only what you are "
                "shown. Never use rewards, agent strategies, or trajectories; "
                "they are hidden from you by design."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    response = _call_client(client, model=ADJUDICATOR_MODEL, messages=messages, budget=budget)
    return parse_adjudication_response(_response_text(response))


def _get_field(hypothesis: Any, name: str, default: Any = None) -> Any:
    if isinstance(hypothesis, dict):
        return hypothesis.get(name, default)
    return getattr(hypothesis, name, default)


def _set_field(hypothesis: Any, name: str, value: Any) -> None:
    if isinstance(hypothesis, dict):
        hypothesis[name] = value
    else:
        setattr(hypothesis, name, value)


def confirm_hypothesis(hypothesis: Any, verdict: Any, actor: str) -> Any:
    """Human/objective confirmation path: record actor + timestamp, promote.

    ``verdict`` is the objective confirmation evidence (e.g. a re-grade
    outcome); it is stored in the new history entry, and the hypothesis
    status moves to ``promoted``. Returns the same hypothesis object.
    """
    if not actor or not str(actor).strip():
        raise ValueError("confirm_hypothesis requires a non-empty actor")
    previous = _get_field(hypothesis, "status")
    history = _get_field(hypothesis, "history", None)
    if history is None:
        history = []
        _set_field(hypothesis, "history", history)
    history.append(
        {
            "at": _utc_now_iso(),
            "actor": actor,
            "transition": "confirm",
            "from": previous,
            "to": "promoted",
            "verdict": verdict,
        }
    )
    _set_field(hypothesis, "status", "promoted")
    return hypothesis
