"""Named, content-addressed harness policies for the lab-owned dspy.RLM agent.

A policy is the complete, reproducible description of how the Recursive Language
Model harness is configured for one trial: iteration and sub-call budgets, the
orchestration guidance prepended to the action prompt, REPL-history observation
masking, and root/sub language-model settings. Policies are plain data so a
trial can persist exactly what ran (``RlmPolicy.digest``) and so the same policy
drives both the host-only synthetic benchmark and the Harbor agent.

The ``stock`` policy reproduces Harbor 0.21.0 ``DspyRlmAgent`` defaults
(``max_iterations=20``, ``max_llm_calls=50``, ``max_output_chars=10_000``, no
separate sub-LM) so that every other policy is a one-variable step from it.

Sources:
- dspy 3.3.1 ``dspy/predict/rlm.py`` (ACTION_INSTRUCTIONS_TEMPLATE; MIT)
- alexzhang13/rlm ``rlm/utils/prompts.py`` ``ORCHESTRATOR_ADDENDUM`` (MIT); the
  orchestrator text below is adapted to dspy's ``SUBMIT``/``print`` REPL API
  and the "nudge to decompose" variant described in Zhang & Khattab,
  "Language model harnesses are compositional generalizers" (2026-07).
- Complexity Trap / SWE-agent ``LastNObservations`` semantics as already adapted
  in ``evallab.observation_masking`` (HAR-50): keep the most recent N
  observations verbatim, replace older observation bodies deterministically.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from typing import Any

POLICY_SCHEMA_VERSION = 1

#: Adapted from alexzhang13/rlm ORCHESTRATOR_ADDENDUM (MIT). Differences from
#: the source: dspy exposes ``SUBMIT(...)`` rather than an ``answer`` dict, the
#: REPL is Pyodide (no subprocesses), and there is no ``rlm_query`` (depth-1
#: only), so those sentences are rewritten; budget guidance is kept verbatim in
#: spirit.
ORCHESTRATOR_ADDENDUM = "\n\n".join(
    [
        "As a Recursive Language Model you act as an orchestrator, not a solver.",
        (
            "Directly after you probe the input variables and understand the task, pause "
            "and plan: state explicitly how the task decomposes into REPL / sub-LLM steps "
            "and sketch the concrete sequence of iterations (what each computes and which "
            "`llm_query` / `llm_query_batched` call, if any, it issues) before executing. "
            "Then execute one iteration at a time: after each step `print` a small sample "
            "of the result, verify it looks right, and only call `SUBMIT(...)` once you have "
            "actually printed the candidate answer. If you are running out of iterations "
            "without a confirmed answer, SUBMIT your best inference rather than letting the "
            "run terminate unsubmitted."
        ),
        (
            "Your own context window is small. Push every long-context operation that "
            "would not fit comfortably in your own working window (reading, summarizing, "
            "classifying, verifying, answering sub-questions) into `llm_query` / "
            "`llm_query_batched` calls instead of pulling that text into your own message "
            "stream. Conversely, if a Python keyword / regex search over the input would "
            "already pin the answer, or a single short passage contains it, just compute it "
            "directly: sub-LMs are for text that will not fit or questions that need "
            "semantic interpretation. Long REPL stdout pollutes your history the same way "
            "raw input does: keep prints small and aggregate the small results in the REPL."
        ),
        (
            "Sub-LLMs have no REPL; they only see the prompt you pass. Hand them clean, "
            "focused inputs and ask for terse, structured outputs (one value per line, "
            "JSON, or CSV) that you can parse programmatically."
        ),
        (
            "Sub-call budget is finite on two independent axes and `llm_query_batched` only "
            "parallelizes; it does not relax either. (1) Per-prompt capacity: a single "
            "sub-call answers well only when its input stays modestly sized; a useful rough "
            "ceiling is ~100K characters per prompt, less when the text is dense. Pack each "
            "prompt close to that capacity so one call accomplishes a lot of work. (2) "
            "Per-batch fan-out: keep a batch to ~20 prompts. Tiny-prompt mega-batches are the "
            "anti-pattern; fat-prompt small batches are correct. After Python-side filtering "
            "has narrowed the candidate set, batch-extract the survivors rather than reading "
            "them by hand. If the raw workload exceeds both budgets at once, filter "
            "aggressively in Python first or stage the task: a cheap coarse pass narrows "
            "candidates, then a targeted second pass extracts from the survivors."
        ),
        (
            "Reserve your own tokens for high-level decisions: what to ask next, how to "
            "combine sub-LM outputs, when to finalize. Delegate everything else."
        ),
    ]
)

#: Short verification checklist appended after the orchestrator text in the
#: ``verify`` family of policies.
VERIFY_ADDENDUM = (
    "Before SUBMIT, run one explicit verification iteration: recompute the answer by an "
    "independent route (a second parsing strategy, a sub-LM cross-check on the decisive "
    "records, or a consistency check such as totals matching per-item sums), print both "
    "values, and only SUBMIT when they agree. If they disagree, resolve the discrepancy "
    "first."
)


#: Harbor-only guidance. Observed on the first executor trial (event-summary,
#: policy ``stock``): iteration 2 called ``open("/app/input/...")`` inside the
#: host-side Pyodide sandbox, hit ``FileNotFoundError`` and burned an
#: iteration before switching to ``read_file``. dspy's template says nothing
#: about the sandbox being a different machine from the task environment.
ENVIRONMENT_BRIDGE_ADDENDUM = (
    "IMPORTANT: your Python REPL runs in an isolated sandbox that is NOT the task "
    "machine. `open()`, `os`, `subprocess`, and `pathlib` in the REPL cannot see the "
    "task's files. The task environment (its filesystem, shell, and installed tools) "
    "is reachable ONLY through the provided tools: `exec_command(command, cwd)`, "
    "`read_file(path)`, `write_file(path, content)`, `list_directory(path)`, "
    "`find_files(pattern, path)`, `search_content(pattern, path, file_glob)`, and "
    "`apply_patch(patch)`. Read task inputs with `read_file`/`exec_command`, compute in "
    "the REPL, then write results back with `write_file` and verify them with "
    "`exec_command` (e.g. `cat`, `python3 -c ...`, `ls -la`) before SUBMIT. The "
    "`file_tree` input is a snapshot of the task machine, not of the sandbox."
)

#: Appended to the environment guidance when ``container_python_tool`` is on.
CONTAINER_PYTHON_ADDENDUM = (
    "You also have `run_python(code, cwd)`: it runs a complete Python 3 script INSIDE the "
    "task environment (where the task's files live) and returns its stdout/stderr. Prefer "
    "one `run_python` call that reads inputs, computes, and writes outputs on the task "
    "machine over shuttling file contents through the sandbox; then verify with "
    "`exec_command`."
)


@dataclass(frozen=True)
class RlmPolicy:
    """Complete configuration of one RLM harness variant."""

    policy_id: str
    description: str
    source: str
    max_iters: int = 20
    max_llm_calls: int = 50
    max_output_chars: int = 10_000
    instruction_addendum: str = ""
    #: Extra guidance applied only when environment tools are attached (Harbor).
    environment_addendum: str = ""
    #: Expose ``run_python`` (executes inside the task container) on Harbor.
    container_python_tool: bool = False
    #: Salvage `reasoning`/`code` from responses that drift off dspy's field
    #: markers (fenced code + preamble) instead of spending an iteration.
    lenient_parse: bool = False
    #: Complete replacement for the action predictor's instructions (e.g. a
    #: GEPA candidate). When set, ``instruction_addendum`` is not prepended.
    action_instructions_override: str | None = None
    #: How past REPL turns are rendered for the action predictor: dspy's stock
    #: "Reasoning:/Code:" text or dspy field markers (``[[ ## code ## ]]``).
    history_style: str = "stock"
    #: Keep the most recent N REPL outputs verbatim in the action prompt; older
    #: outputs are replaced by a deterministic marker. ``None`` disables masking.
    history_window: int | None = None
    #: Replace the bare "k/N" iteration field with a remaining-budget reminder.
    iteration_reminder: bool = False
    root_thinking: bool = True
    root_max_tokens: int = 16_000
    #: ``True`` builds a separate sub-LM instance for ``llm_query``; ``False``
    #: reuses the root LM exactly like Harbor's stock agent with no sub model.
    separate_sub_lm: bool = False
    sub_thinking: bool = False
    sub_max_tokens: int = 8_000
    temperature: float | None = None

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = POLICY_SCHEMA_VERSION
        return payload

    def digest(self) -> str:
        canonical = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()

    def derive(self, policy_id: str, description: str, **changes: Any) -> RlmPolicy:
        return replace(self, policy_id=policy_id, description=description, **changes)


_STOCK = RlmPolicy(
    policy_id="stock",
    description=(
        "Harbor 0.21.0 DspyRlmAgent defaults: 20 iterations, 50 sub-LLM calls, 10k output "
        "chars, no orchestration guidance, root LM also serves llm_query."
    ),
    source="harbor 0.21.0 harbor/agents/dspy_rlm.py; dspy 3.3.1 ACTION_INSTRUCTIONS_TEMPLATE",
)

_ORCHESTRATOR = _STOCK.derive(
    "orchestrator",
    "stock + decomposition nudge adapted from alexzhang13/rlm ORCHESTRATOR_ADDENDUM",
    instruction_addendum=ORCHESTRATOR_ADDENDUM,
    source="alexzhang13/rlm rlm/utils/prompts.py ORCHESTRATOR_ADDENDUM (MIT), adapted",
)

POLICIES: dict[str, RlmPolicy] = {
    policy.policy_id: policy
    for policy in (
        _STOCK,
        _STOCK.derive(
            "sub-nothink",
            "stock + separate sub-LM with thinking disabled for llm_query (cost/latency)",
            separate_sub_lm=True,
            sub_thinking=False,
        ),
        _STOCK.derive(
            "stock-lenient",
            "stock + salvage of format-drifted actions (fenced code + preamble) instead of a retry turn",
            lenient_parse=True,
            source="observed: GLM-5.3-Flash mirrors the rendered REPL history format instead of dspy field markers (2026-09-16)",
        ),
        _ORCHESTRATOR.derive(
            "orchestrator-lenient",
            "orchestrator + salvage of format-drifted actions",
            lenient_parse=True,
        ),
        _STOCK.derive(
            "stock-markers",
            "stock + REPL history rendered with dspy field markers so mirrored turns stay parseable",
            history_style="markers",
            source="observed format drift on stock rendering (2026-09-16); dspy demos use the same markers",
        ),
        _STOCK.derive(
            "stock-markers-lenient",
            "stock-markers + salvage of any remaining drifted actions",
            history_style="markers",
            lenient_parse=True,
        ),
        _ORCHESTRATOR,
        _ORCHESTRATOR.derive(
            "orchestrator-mask4",
            "orchestrator + deterministic masking of all but the last 4 REPL outputs",
            history_window=4,
        ),
        _ORCHESTRATOR.derive(
            "orchestrator-remind",
            "orchestrator + remaining-iteration reminder in the action prompt",
            iteration_reminder=True,
        ),
        _ORCHESTRATOR.derive(
            "orchestrator-verify",
            "orchestrator + explicit verification iteration before SUBMIT",
            instruction_addendum=ORCHESTRATOR_ADDENDUM + "\n\n" + VERIFY_ADDENDUM,
        ),
        _ORCHESTRATOR.derive(
            "compact",
            "orchestrator + 3k output chars + last-6 masking + reminder + separate no-think sub-LM",
            max_output_chars=3_000,
            history_window=6,
            iteration_reminder=True,
            separate_sub_lm=True,
            sub_thinking=False,
        ),
        _STOCK.derive(
            "stock-nothink",
            "stock with root thinking disabled (isolates the reasoning-token cost)",
            root_thinking=False,
        ),
        _STOCK.derive(
            "bridge",
            "stock + Harbor sandbox/tool-bridge guidance (applied only when tools are attached)",
            environment_addendum=ENVIRONMENT_BRIDGE_ADDENDUM,
            source="observed FileNotFoundError on trial rlm-event-summary-stock-smoke (2026-09-16)",
        ),
        _ORCHESTRATOR.derive(
            "orchestrator-bridge",
            "orchestrator + Harbor sandbox/tool-bridge guidance",
            environment_addendum=ENVIRONMENT_BRIDGE_ADDENDUM,
        ),
        _ORCHESTRATOR.derive(
            "compact-bridge",
            "compact + Harbor sandbox/tool-bridge guidance",
            max_output_chars=3_000,
            history_window=6,
            iteration_reminder=True,
            separate_sub_lm=True,
            sub_thinking=False,
            environment_addendum=ENVIRONMENT_BRIDGE_ADDENDUM,
        ),
        _STOCK.derive(
            "tools-bridge",
            "bridge + run_python tool executing inside the task container",
            environment_addendum=ENVIRONMENT_BRIDGE_ADDENDUM + "\n\n" + CONTAINER_PYTHON_ADDENDUM,
            container_python_tool=True,
            source="Librarian suggestion 2026-09-16 (modular REPL tools) + observed sandbox/container split",
        ),
        _ORCHESTRATOR.derive(
            "orchestrator-tools",
            "orchestrator + bridge guidance + run_python tool executing inside the task container",
            environment_addendum=ENVIRONMENT_BRIDGE_ADDENDUM + "\n\n" + CONTAINER_PYTHON_ADDENDUM,
            container_python_tool=True,
        ),
    )
}


def resolve_policy(policy_id: str) -> RlmPolicy:
    try:
        return POLICIES[policy_id]
    except KeyError as exc:
        raise ValueError(
            f"unknown RLM policy {policy_id!r}; known: {', '.join(sorted(POLICIES))}"
        ) from exc


def policy_from_json(payload: dict[str, Any]) -> RlmPolicy:
    """Rehydrate a policy persisted by ``RlmPolicy.to_json`` (e.g. a GEPA candidate)."""
    data = {key: value for key, value in payload.items() if key != "schema_version"}
    return RlmPolicy(**data)
