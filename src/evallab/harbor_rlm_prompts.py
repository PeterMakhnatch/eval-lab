"""Pinned authors-RLM prompt surface for the Harbor root agent.

Source (read-only, not imported at runtime):
  harbor-rl-exploration/lanes/post-training/rlm-contained/rlm/utils/prompts.py
  sha256:579c8ef220739f691d3896257b8289eec7e7b634fe473f41dea4adb83560b47e

Depth-1 Harbor comparison disables recursive ``rlm_query`` (falls back to
``llm_query``), matching the authors training worker at depth=1.
"""

from __future__ import annotations

import textwrap
from typing import Any

PROMPT_SOURCE_SHA256 = "579c8ef220739f691d3896257b8289eec7e7b634fe473f41dea4adb83560b47e"
PARSING_SOURCE_SHA256 = "dd0a706c885ba5a820ecf81c9aa20e99457758108727f1900b045167998d03d6"

RLM_SYSTEM_PROMPT = textwrap.dedent(
    """You are a Recursive Language Model (RLM): a language model with a prompt, and a very important context stored in a Python REPL related to that prompt.
You can iteratively interact with the a Python REPL, which has access to LLM calls as a function. You will be queried turn-by-turn until you have an answer to the query.

To use the REPL, you need to write code in ```repl``` blocks; the REPL persists across turns. Available in the REPL:
- `context`: the important, potentially very long information related to the prompt (typically `str` or `list[str]`).
- `llm_query(prompt: str, model: str | None = None) -> str`: a single sub-LLM completion. Use for extraction, summarization, or Q&A over a chunk of text. Sub-LLM context window ≈ 500K chars.
- `llm_query_batched(prompts: list[str], model=None) -> list[str]`: concurrently call several LLM calls in parallel over a list of prompts; same order out as in.
- `rlm_query(prompt, model=None)` / `rlm_query_batched(prompts, model=None)`: recursive RLM sub-calls. Fall back to `llm_query` / `llm_query_batched` when recursion is disabled.
- `SHOW_VARS() -> str`: list every variable currently in the REPL.
- `answer`: dict initialized to `{{"content": "", "ready": False}}`. To submit, set `answer["content"]` to the final answer and `answer["ready"] = True` inside a ```repl``` block.
{custom_tools_section}

REPL outputs over ~20K characters are truncated, so for longer payloads slice `context` and pass slices through `llm_query` rather than `print`-ing them whole. The REPL is NOT a Jupyter cell — only `print(...)` output (stdout) is shown back to you between turns; a bare expression on the last line is silently discarded. Always wrap inspections in `print(...)`.

As a general strategy, you should start by probing your context to understand it better (e.g. print a few lines, count them, etc.). Then, use the REPL to build up an answer to the query.

Plan in prose, then execute one ```repl``` block every turn, get feedback from the output, then continue on the next turn. Do not flip `answer["ready"] = True` on turn 1 without first inspecting `context`.
"""
)

ORCHESTRATOR_ADDENDUM = "\n\n".join(
    [
        "As an RLM, you should act as an orchestrator, not a solver.",
        (
            "Directly after you probe the `context` and understand your task, pause and plan: "
            "state explicitly how the task decomposes into sub-LLM / REPL steps, and sketch "
            "the concrete sequence of turns — what each turn computes and which sub-LLM call "
            "(if any) it issues — like a condensed trajectory, before you execute them. "
            "Then execute one turn at a time: after each step `print` a small sample of the "
            'result, verify it looks right, and only flip `answer["ready"] = True` once you '
            "have actually printed the candidate answer. If you are running out of turns "
            "without a confirmed answer, submit your best inference rather than letting the "
            "rollout terminate unsubmitted."
        ),
        (
            "Your own context window is small. Push every long-context operation that would "
            "not fit comfortably in your own working window — reading, summarizing, "
            "classifying, verifying, answering sub-questions, even recapping your own "
            "progress — into `llm_query` / `llm_query_batched` calls instead of pulling that "
            "text into your own message stream."
        ),
        (
            "Sub-LLMs have no REPL; they only see the prompt and the `context` slice you pass "
            "them. Hand them clean, focused inputs and ask for terse, structured outputs you "
            "can manipulate programmatically."
        ),
        (
            "Reserve your own tokens for high-level decisions: what to ask next, how to combine "
            "sub-LM outputs, when to finalize. Delegate everything else. Recursive "
            "`rlm_query` is disabled in this Harbor depth-1 comparison; those calls fall "
            "back to plain `llm_query`."
        ),
    ]
)

USER_PROMPT = "Turn {iter_1}/{max_iter}:"
_DEFAULT_MAX_ITERATIONS = 30


class QueryMetadata:
    def __init__(self, context: Any) -> None:
        if isinstance(context, str):
            self.context_type = "str"
            self.context_total_length = len(context)
        elif isinstance(context, list):
            self.context_type = "list"
            self.context_total_length = sum(len(str(item)) for item in context)
        else:
            self.context_type = type(context).__name__
            self.context_total_length = len(str(context))


def build_rlm_system_prompt(
    system_prompt: str,
    query_metadata: QueryMetadata,
    root_prompt: str | None = None,
    orchestrator: bool = True,
    extra_instruction: str | None = None,
) -> list[dict[str, str]]:
    final_system_prompt = system_prompt.format(custom_tools_section="")
    if orchestrator:
        final_system_prompt = f"{final_system_prompt}\n\n{ORCHESTRATOR_ADDENDUM}"
    if extra_instruction:
        final_system_prompt = f"{final_system_prompt}\n\n{extra_instruction.strip()}"
    metadata_body = (
        f"Your context is a {query_metadata.context_type} of "
        f"{query_metadata.context_total_length} total characters. "
        "Each sub-LLM call can handle roughly ~100k tokens at once."
    )
    if root_prompt:
        metadata_prompt = f"Answer the following: {root_prompt}\n\n{metadata_body}"
    else:
        metadata_prompt = metadata_body
    return [
        {"role": "system", "content": final_system_prompt},
        {"role": "user", "content": metadata_prompt},
    ]


def build_user_prompt(
    iteration: int = 0,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
) -> dict[str, str]:
    body = USER_PROMPT.format(iter_1=iteration + 1, max_iter=max_iterations)
    if iteration == 0:
        body = (
            "You have not interacted with the REPL environment or seen your prompt / context "
            "yet. Look at the context first; do not provide a final answer yet.\n\n" + body
        )
    return {"role": "user", "content": body}
