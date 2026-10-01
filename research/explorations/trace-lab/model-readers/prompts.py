"""Shared question set for the HAR-119 model readers (Traces/model-readers).

Single source of truth for the Scout llm_scanner question and the Docent
reading prompt + output schema. Both readers ask the SAME questions,
distilled from ``../har119/RATER_GUIDE.md``; only the citation mechanics
differ (Scout: ``[Mn]`` message cites; Docent: transcript block citations).

Freeze protocol: ``freeze.py`` writes ``PROMPTS.sha256``/``FROZEN_AT`` over
the exact strings below BEFORE either reader runs on the 12 HAR-119 runs.
Iteration happens only on HAR-81/HAR-104 runs.
"""

from __future__ import annotations

CORE = """You are reviewing one agent run: a MiMo model driving a Linux shell through the Terminus-2 tool to fix a Python bug-fix task. Answer every question below from the transcript only. Be literal: report only what the transcript shows, never infer intent. Every factual claim needs a cite plus a quote in "double quotes" (copy-paste verbatim from the transcript, at most 25 words, nothing inside the quotes but transcript text). If the transcript does not show something, say so in the evidence field.

1. STOP (stop_reason): exactly one of model_finished | request_ceiling | token_ceiling | infra_error | agent_timeout | other.
- model_finished: the harness accepted the model's completion and the run ended (a completed mark_task_complete handshake, or a final message the harness accepted as done).
- request_ceiling: the run used up its model-call budget (about 120 calls; ends in a budget exception after many episodes).
- token_ceiling: the input-token budget ran out (budget exception after fewer episodes, or context/budget errors in observations).
- infra_error: the proxy, sandbox or model server failed (connection, auth, or executor errors that stop the run).
- agent_timeout: the agent itself timed out.
- other: none of the above fits.
Cite and quote the ending: the completion handshake, the budget exception, or the error. The run facts below say how many episodes ran and whether the harness raised an exception; use them.

2. FIRST FAILURE: the earliest model turn after which the run was headed for failure and never recovered (wrong fix direction, bad edit, fetching the wrong upstream code, a false completion claim, a tool-misuse loop, giving up on the real bug). Give a one-line what, plus the cite and quote of that turn. Mark a pass ONLY for a clean earned pass on the model's own work. A pass built on fetched or pasted upstream code is NOT a pass here: describe the fetch in what and cite the fetch/paste turn. If the run failed only because of infrastructure, cite the first infra-error turn.

3. BLAME: exactly one of model | harness | task | infra | none.
- model: the model had what it needed and made the mistake (wrong fix, false claim, no adaptation, never engaging the deliverable).
- harness: the Terminus-2 harness blocked or misread a reasonable action (rejected valid output, refused plain text, lost context, ignored a completion it should have accepted).
- task: the instruction or the hidden tests made a pass unreasonable (check what the instruction asks against what the tests demand).
- infra: proxy, sandbox or server failure explains the failure.
- none: an earned pass on the model's own work; nothing failed.
Quote the deciding evidence.

4. LOOP: exactly one of completion-claim | repetition | none, plus the span (start and end) or null.
- completion-claim: after the model said the task was done (a completion claim, task_complete, or echo-style done turns), the run kept going and most of the remaining turns are more claims or re-checks.
- repetition: at least 10 turns repeat the same or nearly the same action without progress, and are not driven by a completion claim.
- none: neither pattern occurs.
If both occur, pick the one that consumed more turns and say so in the evidence. Cite and quote the loop start (first claim, or first repeated action) and the loop end.

5. UPSTREAM FETCH: true iff the run fetched outside code (pip download, curl, git clone, archive download) or pasted a large outside block, and that outside code is what the run stands on. Otherwise false. Quote the fetch/paste turn, or state that every edit was typed in place.
"""

SCOUT_QUESTION = (
    CORE
    + """
Transcript mechanics: messages are numbered [M1], [M2], ... in order. Put the [Mn] cite (for example [M37]) and the verbatim "quote" in every evidence field. Put the earliest, deciding evidence first in each field. The run facts appended after the questions (reward, episodes, exception) come from the run record, not the transcript.
"""
)

SCOUT_FACTS_TAIL = """
Run facts: reward={reward} (1.0 means the hidden tests passed), episodes={episodes}, harness exception={exception}.
"""

DOCENT_PROMPT = (
    """Evaluate this agent run. The blind run metadata (trial, task, reward, exception, episodes) is included; the full transcript follows. """
    + CORE
    + """
Transcript mechanics: put the verbatim "quote" from the exact transcript text in each evidence field (at most 25 words per quote); your quotes carry transcript block citations automatically. The blind run metadata (reward, episodes, exception) is included with the transcript; use it for the STOP question. Put the earliest, deciding evidence first in the first-failure and loop-start fields.
"""
)

DOCENT_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string", "citations": True},
        "stop_reason": {
            "type": "string",
            "enum": [
                "model_finished",
                "request_ceiling",
                "token_ceiling",
                "infra_error",
                "agent_timeout",
                "other",
            ],
        },
        "stop_evidence": {"type": "string", "citations": True},
        "first_failure_is_pass": {"type": "boolean"},
        "first_failure_what": {"type": "string"},
        "first_failure_evidence": {"type": "string", "citations": True},
        "blame": {
            "type": "string",
            "enum": ["model", "harness", "task", "infra", "none"],
        },
        "blame_evidence": {"type": "string", "citations": True},
        "loop_kind": {
            "type": "string",
            "enum": ["completion-claim", "repetition", "none"],
        },
        "loop_start_evidence": {"type": "string", "citations": True},
        "loop_end_evidence": {"type": "string", "citations": True},
        "upstream_fetch": {"type": "boolean"},
        "upstream_evidence": {"type": "string", "citations": True},
    },
    "required": [
        "reasoning",
        "stop_reason",
        "stop_evidence",
        "first_failure_is_pass",
        "first_failure_what",
        "first_failure_evidence",
        "blame",
        "blame_evidence",
        "loop_kind",
        "loop_start_evidence",
        "loop_end_evidence",
        "upstream_fetch",
        "upstream_evidence",
    ],
}

# The Docent reading model, frozen with the prompts. Strongest usable
# hosted-quota model at freeze time; see README for the model survey.
DOCENT_MODEL = "anthropic/claude-opus-5-5"

# The Scout LLM model, frozen with the prompts. Strongest GLM on the ZAI
# Open Platform at freeze time; invoked via the scan --model flag, e.g.
# --model openai-api/zai-openapi/glm-5.3
# --model-base-url https://api.z.ai/api/paas/v4 (ZAI_OPENAPI_API_KEY).
SCOUT_MODEL = "openai-api/zai-openapi/glm-5.3"
