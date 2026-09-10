"""Harness Mechanics Lab: Bounded counterfactual ablation planning.

This module maps observational diagnostic evidence (stopping, clipping, shell)
from a harness-mechanics report into bounded experiment proposals. Each proposal
isolates exactly one changed mechanism under fixed task, model, reward, and budget
conditions to disambiguate observational hypotheses.

Strict invariants:
- Analytical artifact, NOT an executable job schema or queue format.
- execution_authorized is strictly False; proposals do not run directly in Harbor.
- No invented task IDs, profile IDs, or synthetic benchmark performance targets.
- No proposal without relevant diagnostic evidence: empty trajectories, unresolved
  references, post-hoc redaction, or missing telemetry alone do not justify an
  intervention.
- Exactly three standard mechanisms ("stopping", "clipping", "shell"); no generic registry.
- Actual source paths, sha256 digests, and RFC6901 JSON pointer locators are preserved
  in linked evidence; no arbitrary raw prompt, reasoning, or command strings.
- Pinned source facts are distinguished from causal hypotheses.
"""

from __future__ import annotations

import copy
from typing import Any

SCHEMA_VERSION = 1
ARTIFACT_KIND = "ablation-proposal"
EXPECTED_ANALYSIS_KIND = "harness-mechanics-observational"
STANDARD_MECHANISMS: tuple[str, ...] = ("stopping", "clipping", "shell")

# Diagnostic codes that represent neutral completion, absence of agent steps,
# configuration limits that were not reached, unverified references, redactions,
# or missing telemetry alone. These DO NOT justify a runtime-mechanism intervention.
_NON_INTERVENTION_CODES: frozenset[str] = frozenset(
    {
        # Empty trajectories and unexecuted canary trajectories (zero agent turns)
        "STOPPING_EMPTY_TRAJECTORY",
        "EMPTY_TRAJECTORY",
        "EMPTY_TRAJECTORY_STEPS",
        "STOPPING_TRAILING_NON_AGENT_STEP",
        "STOPPING_NO_AGENT_STEPS_RECORDED",
        "NO_AGENT_STEPS",
        # Neutral completion negative controls (conversational/task normal termination)
        "STOPPING_FINAL_ASSISTANT_NO_TOOL",
        "STOPPING_FINAL_ASSISTANT_WITH_TOOL_CALLS",
        "STOPPING_STEP_FINISH_REASON_STOP",
        "STOPPING_EPISODE_COMPLETION_OBSERVED",
        # Configured limits that were not reached / purely informative metadata
        "STOPPING_TOKEN_LIMIT_CONFIGURED",
        "STOPPING_TIMEOUT_LIMIT_CONFIGURED",
        "STOPPING_STEP_LIMIT_CONFIGURED",
        "STOPPING_VERIFIER_OUTCOME_RECORDED",
        "STOPPING_SUBMISSION_MARKER_METADATA",
        "STOPPING_SUBAGENT_TRAJECTORIES_RECORDED",
        "STOPPING_SUBAGENT_TERMINATION_RECORDED",
        # Unresolved references (unhydrated CAS or external content ref)
        "CONTENT_REF_UNRESOLVED",
        "CAS_REF_UNRESOLVED",
        "UNRESOLVED_REF",
        # Missing telemetry / omitted full output references alone
        "MISSING_FULL_OUTPUT",
        "MISSING_TELEMETRY",
        "NO_TELEMETRY",
        # Post-hoc evidence redactions (artifact withholding / secret masking)
        "EVIDENCE_REDACTION_POST_HOC",
        "POST_HOC_REDACTION",
        "REDACTED_CONTENT",
        # Marker-only / spoofed indicators without structured clipping evidence
        "OBSERVATION_MARKER_INDICATOR",
        "AMBIGUOUS_QUOTED_MARKER",
    }
)

# Explicit whitelist of source-supported substantive signal categories that justify an ablation proposal.
# Default-deny: Any unknown, unverified, neutral, or availability-only code is excluded.
_ACTIONABLE_SUBSTANTIVE_CODES_BY_MECHANISM: dict[str, frozenset[str]] = {
    "stopping": frozenset(
        {
            # Explicit token / step exhaustion or cutoff
            "STOPPING_STEP_FINISH_REASON_LENGTH",
            "STOPPING_EPISODE_TOKEN_LIMIT_REACHED",
            "STOPPING_STEP_LIMIT_TERMINATION",
            # Explicit termination or timeouts
            "STOPPING_EPISODE_TIMEOUT_OBSERVED",
            "STOPPING_EPISODE_TERMINATION_EXPLICIT",
            "STOPPING_COMMAND_TIMEOUT_OBSERVED",
            # Context capture anomaly
            "STOPPING_CAPTURED_CONTEXT_OBSERVED",
            # Filter / safety halts
            "STOPPING_STEP_FINISH_REASON_CONTENT_FILTER",
            # Explicit finish reason requiring protocol disambiguation
            "STOPPING_STEP_FINISH_REASON_EXPLICIT",
            # Premature cutoff hypothesis
            "STOPPING_PREMATURE_CUTOFF_HYPOTHESIS",
        }
    ),
    "clipping": frozenset(
        {
            # Explicit structured truncation / compaction metadata
            "OBSERVATION_METADATA_TRUNCATED",
            "OBSERVATION_METADATA_COMPACTION",
            "STEP_METADATA_TRUNCATED",
            "STEP_METADATA_COMPACTION",
            "TRAJECTORY_POLICY_TRUNCATION",
            "TRAJECTORY_POLICY_COMPACTION",
            "STEP_COPIED_CONTEXT",
        }
    ),
    "shell": frozenset(
        {
            # Explicit timeout or reset events
            "SHELL_TIMEOUT_EXPLICIT",
            "SHELL_RESET_REQUESTED",
            "SHELL_RESET_COMPLETED",
            "SHELL_RESET_EXPLICIT",
            # Diagnostic hypotheses grounded in command failures
            "SHELL_TIMEOUT_HYPOTHESIS",
            "SHELL_REPEATED_FAILED_COMMAND_HYPOTHESIS",
        }
    ),
}

# Disambiguation blueprints for each standard mechanism category.
# These define the bounded experiment structure when diagnostic evidence exists.
_MECHANISM_DISAMBIGUATION_SPECS: dict[str, dict[str, Any]] = {
    "stopping": {
        "target_hypothesis": (
            "Observed termination patterns (finish_reason, silent turn without tool call, "
            "or step budget cutoff) reflect harness-level turn-taking or token boundary "
            "enforcement rather than genuine agent task completion or model verification satisfaction."
        ),
        "disambiguation_focus": (
            "Disambiguate whether the agent stopped because it believed the task was solved, "
            "because the output token window truncated generation before a tool call could emit, "
            "or because the harness lacks an explicit completion protocol."
        ),
        "one_changed_mechanism": {
            "mechanism_variable": "stopping_completion_protocol",
            "baseline_condition": (
                "Implicit termination via bare assistant text (no tool call) or harness step cutoff."
            ),
            "counterfactual_condition": (
                "Explicit completion protocol (e.g., dedicated finish tool or submission command, "
                "evaluated as a bounded candidate mechanism rather than proven cause) with distinct handling for length truncation."
            ),
        },
        "fixed_conditions": {
            "task_conditions": (
                "Fixed benchmark task instances and identical initial repository container state. "
                "No modification to task instructions or evaluation criteria."
            ),
            "model_conditions": (
                "Fixed model checkpoint weights, prompt template, context window limit, "
                "and sampling parameters (temperature, top_p, top_k, reasoning_effort)."
            ),
            "reward_conditions": (
                "Fixed evaluation verifier script and test assertion logic; ground-truth pass/fail "
                "judgments evaluated independently of agent-declared completion."
            ),
            "budget_conditions": (
                "Fixed maximum step budget (max_steps), fixed per-turn token ceiling, "
                "and identical wall-clock timeout boundaries."
            ),
        },
        "observable_checks": [
            "Check whether the agent continues productive exploration when an explicit finish tool is required instead of stopping on raw text.",
            "Measure whether finish_reason='length' occurs before or after task verification tests pass.",
            "Verify whether premature stopping correlates with token penalty decay or reasoning effort settings (b=25..100).",
            "Confirm whether independent verifier passes at the exact step where the agent halts.",
        ],
        "required_measurements": [
            "Distribution of terminal finish reasons (stop, length, tool_calls, timeout, step_limit).",
            "Step index of final action and total prompt/completion tokens consumed.",
            "Presence of uncalled tool invocations or truncated JSON in terminal steps.",
            "Independent verifier score at terminal step vs maximum score achievable across intermediate states.",
        ],
        "evidence_needed": [
            "Retained API response finish_reason headers for every generation turn.",
            "Raw assistant message endings at terminal steps to verify tool call syntactic completeness.",
            "Harness turn-boundary exit logs with exact timestamps and reason codes.",
        ],
        "held_out_evaluation_limits": [
            "Valid only for evaluating termination disambiguation on the tested task distribution.",
            "Does not generalize to human-in-the-loop approval workflows or interactive chat settings.",
        ],
        "reproduction_gaps": [
            "mini-SWE enforces 'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT' whereas DSH Minimal uses persistent bash without a mandatory submit marker.",
            "DSH sets maxTokensAsSuccess: false on sdk-jsonrpc-server; standard Harbor adapters may treat token exhaustion as normal turn completion.",
        ],
    },
    "clipping": {
        "target_hypothesis": (
            "Observed tool observation truncation (character/byte limits or head-tail compaction) "
            "causes model disorientation, repeated commands, or incorrect repair attempts due "
            "to elision of critical error diagnostics or file content."
        ),
        "disambiguation_focus": (
            "Disambiguate whether agent failures following truncated observations stem from "
            "missing evidence in the elided slice or from inability to reason over large outputs regardless of truncation."
        ),
        "one_changed_mechanism": {
            "mechanism_variable": "observation_truncation_strategy",
            "baseline_condition": (
                "Fixed character/byte cutoff with head-tail elision (e.g., 5000 head / 5000 tail chars in mini-SWE, or 16000 chars in DSH Minimal)."
            ),
            "counterfactual_condition": (
                "Full unclipped observation streaming or paging window with explicit retained byte offsets."
            ),
        },
        "fixed_conditions": {
            "task_conditions": (
                "Fixed task instances requiring large file inspection or verbose test runs, "
                "with identical environment filesystem and dependencies."
            ),
            "model_conditions": (
                "Fixed model checkpoint, 1M context window, and identical generation parameters."
            ),
            "reward_conditions": ("Identical task verifier suite and correctness assertions."),
            "budget_conditions": ("Fixed context window ceiling and identical step limits."),
        },
        "observable_checks": [
            "Check whether tool calls immediately following clipped observations repeat the same command or issue targeted sub-queries.",
            "Determine whether error recovery rate increases when full compiler/test output is preserved.",
            "Verify whether context cache efficiency degrades under unclipped observations relative to head-tail compaction.",
        ],
        "required_measurements": [
            "Exact step-level observation byte count and character count before and after truncation.",
            "Tool call retry frequency and argument similarity following clipped observations.",
            "Cumulative context tokens per rollout and KV cache memory utilization.",
            "Task resolution rate stratified by whether any observation exceeded the truncation threshold.",
        ],
        "evidence_needed": [
            "Structured observation metadata recording original length, retained length, and elided character/byte count.",
            "Full unclipped tool output captured in offloaded store (CAS) for counterfactual comparison.",
            "Harness truncation warning markers retained in observation payloads.",
        ],
        "held_out_evaluation_limits": [
            "Applies strictly to tasks with verbose tool outputs (>10KB); non-informative for short-output commands.",
            "Does not extrapolate to multimodal or visual agent scaffolds.",
        ],
        "reproduction_gaps": [
            "mini-SWE truncates at 10,000 characters with 5000/5000 head-tail elision; DSH Minimal mode clips tool outputs at 16,000 characters and prompts the agent to avoid commands producing large output.",
            "Retrospective ATIF converters without CAS blob storage lose the elided bytes permanently, preventing post-hoc reconstruction.",
        ],
    },
    "shell": {
        "target_hypothesis": (
            "Observed command execution failures, directory resets, or repetitive environment setup "
            "commands stem from subshell non-persistence (stateless execution) rather than agent planning deficits."
        ),
        "disambiguation_focus": (
            "Disambiguate whether sequential command failures are caused by working directory or environment "
            "resets between turns, or by invalid shell command syntax produced by the model."
        ),
        "one_changed_mechanism": {
            "mechanism_variable": "shell_session_persistence",
            "baseline_condition": (
                "Stateless subshell per action; working directory and environment variables reset on every step."
            ),
            "counterfactual_condition": (
                "Persistent stateful bash session (@deepseek-ai/dsh-tool-bash-persistent) preserving cwd and environment across steps."
            ),
        },
        "fixed_conditions": {
            "task_conditions": (
                "Fixed repository filesystem layout, build targets, and container shell environment (/bin/bash)."
            ),
            "model_conditions": (
                "Fixed model checkpoint and system prompt instructions regarding shell usage."
            ),
            "reward_conditions": ("Fixed task evaluation tests and hidden validation suites."),
            "budget_conditions": (
                "Fixed per-command timeout (300s) and fixed maximum rollout turns."
            ),
        },
        "observable_checks": [
            "Check whether repeated 'cd' commands or environment exports decrease under persistent shell.",
            "Observe whether relative path file operations succeed without compound 'cd dir && cmd' prefixes.",
            "Verify whether background processes or daemon jobs remain accessible across turns.",
            "Measure whether shell command execution timeout rate changes between persistent PTY and subshell.",
        ],
        "required_measurements": [
            "Working directory path before and after each command execution step.",
            "Rate of repeated identical commands within a 3-step sliding window.",
            "Shell exit code distribution and standard error frequency.",
            "Command execution latency and timeout occurrences (exceeding 300s).",
        ],
        "evidence_needed": [
            "Per-step instrumentation of PID, working directory, and active shell environment variables.",
            "Complete command string and returncode pairing retained in trajectory steps.",
            "Harness-level shell restart or crash events logged with causal reason.",
        ],
        "held_out_evaluation_limits": [
            "Valid only for bash command execution; does not apply to non-shell tools (Python REPL, file edit RPCs).",
            "Cannot determine whether persistent shell introduces unintended state contamination across failed steps.",
        ],
        "reproduction_gaps": [
            "DSH Minimal explicitly provides persistent bash with a 300s timeout; mini-SWE explicitly executes every action in a new subshell.",
            "DSH runs with danger-full-access permissions; standard Harbor sandboxes may enforce read-only mounts or user isolation.",
        ],
    },
}


def _validate_report_structure(report: Any) -> None:
    """Validate that the input report conforms to the expected report schema."""
    if not isinstance(report, dict):
        raise ValueError(f"report must be a dict, got {type(report).__name__}")

    schema_version = report.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported report schema_version: {schema_version!r}. Expected {SCHEMA_VERSION}"
        )

    analysis_kind = report.get("analysis_kind")
    if analysis_kind != EXPECTED_ANALYSIS_KIND:
        raise ValueError(
            f"Unsupported report analysis_kind: {analysis_kind!r}. Expected {EXPECTED_ANALYSIS_KIND!r}"
        )

    for required_key in ("records", "summary", "limitations"):
        if required_key not in report:
            raise ValueError(f"report missing required key: {required_key!r}")

    summary = report["summary"]
    if not isinstance(summary, dict):
        raise ValueError(f"report['summary'] must be a dict, got {type(summary).__name__}")

    records = report["records"]
    if not isinstance(records, list):
        raise ValueError(f"report['records'] must be a list, got {type(records).__name__}")

    limitations = report["limitations"]
    if not isinstance(limitations, list):
        raise ValueError(f"report['limitations'] must be a list, got {type(limitations).__name__}")


def _extract_mechanism_evidence(
    records: list[dict[str, Any]],
    mechanism: str,
) -> dict[str, Any]:
    """Collect actual diagnostic observation codes, locators, and source references.

    Filters out empty trajectories, unresolved refs, post-hoc redactions, and missing
    telemetry alone so they do not falsely justify an ablation intervention.
    """
    observed_codes: set[str] = set()
    hypothesis_codes: set[str] = set()
    locators: set[str] = set()
    source_paths: set[str] = set()
    source_hashes: set[str] = set()
    unknowns: set[str] = set()
    evidence_links: list[dict[str, Any]] = []
    observation_count = 0
    records_with_evidence = 0
    has_substantive_evidence = False

    for rec in records:
        if not isinstance(rec, dict) or rec.get("status") != "analyzed":
            continue

        diagnostics = rec.get("diagnostics")
        if not isinstance(diagnostics, dict):
            continue

        diag = diagnostics.get(mechanism)
        if not isinstance(diag, dict):
            continue

        # Extract source metadata safely
        source = rec.get("source")
        source_path: str | None = None
        source_sha256: str | None = None
        if isinstance(source, dict):
            p = source.get("path")
            if isinstance(p, str) and p.strip():
                source_path = p.strip()
            h = source.get("sha256")
            if isinstance(h, str) and h.strip():
                source_sha256 = h.strip()

        rec_has_evidence = False

        # Collect unknowns
        unk_list = diag.get("unknowns")
        if isinstance(unk_list, list):
            for unk in unk_list:
                if isinstance(unk, str) and unk.strip():
                    unknowns.add(unk.strip())

        # Collect observations
        obs_list = diag.get("observations")
        if isinstance(obs_list, list):
            for obs in obs_list:
                if not isinstance(obs, dict):
                    continue

                code = obs.get("code")
                ev_kind = obs.get("evidence_kind")
                locator = obs.get("locator")

                if not (isinstance(code, str) and code.strip()):
                    continue

                code_clean = code.strip()
                locator_clean = (
                    locator.strip() if isinstance(locator, str) and locator.strip() else None
                )

                # Check if this observation constitutes substantive runtime evidence.
                # Default-deny: must be in the explicit whitelist of actionable substantive
                # codes for this mechanism AND not in the non-intervention exclusion set.
                actionable_for_mech = _ACTIONABLE_SUBSTANTIVE_CODES_BY_MECHANISM.get(
                    mechanism, frozenset()
                )
                is_substantive = (
                    code_clean in actionable_for_mech and code_clean not in _NON_INTERVENTION_CODES
                )
                if ev_kind == "observed":
                    observed_codes.add(code_clean)
                    observation_count += 1
                    rec_has_evidence = True
                    if is_substantive:
                        has_substantive_evidence = True
                elif ev_kind == "hypothesis":
                    hypothesis_codes.add(code_clean)
                    observation_count += 1
                    rec_has_evidence = True
                    if is_substantive:
                        has_substantive_evidence = True

                if locator_clean is not None:
                    locators.add(locator_clean)

                # Record structured evidence link (preserves source path, hash, and JSON pointer; no raw text)
                link_entry: dict[str, Any] = {
                    "code": code_clean,
                    "evidence_kind": (
                        ev_kind if ev_kind in ("observed", "hypothesis") else "observed"
                    ),
                }
                if source_path is not None:
                    link_entry["source_path"] = source_path
                    source_paths.add(source_path)
                if source_sha256 is not None:
                    link_entry["source_sha256"] = source_sha256
                    source_hashes.add(source_sha256)
                if locator_clean is not None:
                    link_entry["locator"] = locator_clean

                evidence_links.append(link_entry)

        if rec_has_evidence:
            records_with_evidence += 1

    return {
        "mechanism": mechanism,
        "observed_codes": sorted(observed_codes),
        "hypothesis_codes": sorted(hypothesis_codes),
        "locators": sorted(locators),
        "source_paths": sorted(source_paths),
        "source_hashes": sorted(source_hashes),
        "evidence_links": evidence_links,
        "unknowns": sorted(unknowns),
        "observation_count": observation_count,
        "records_affected_count": records_with_evidence,
        "has_substantive_evidence": has_substantive_evidence,
    }


def _build_proposal_for_mechanism(
    mechanism: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Construct a bounded ablation proposal for a standard mechanism with verified evidence."""
    spec = _MECHANISM_DISAMBIGUATION_SPECS[mechanism]

    return {
        "mechanism": mechanism,
        "target_hypothesis": spec["target_hypothesis"],
        "disambiguation_focus": spec["disambiguation_focus"],
        "linked_evidence": {
            "source_paths": evidence["source_paths"],
            "source_hashes": evidence["source_hashes"],
            "observed_codes": evidence["observed_codes"],
            "hypothesis_codes": evidence["hypothesis_codes"],
            "locators": evidence["locators"],
            "evidence_links": evidence["evidence_links"],
            "observation_count": evidence["observation_count"],
            "records_affected_count": evidence["records_affected_count"],
            "retained_unknowns": evidence["unknowns"],
        },
        "one_changed_mechanism": copy.deepcopy(spec["one_changed_mechanism"]),
        "fixed_conditions": copy.deepcopy(spec["fixed_conditions"]),
        "observable_checks": list(spec["observable_checks"]),
        "required_measurements": list(spec["required_measurements"]),
        "evidence_needed": list(spec["evidence_needed"]),
        "held_out_evaluation_limits": list(spec["held_out_evaluation_limits"]),
        "reproduction_gaps": list(spec["reproduction_gaps"]),
        "execution_authorized": False,
    }


def build_ablation_plan(report: dict[str, Any]) -> dict[str, Any]:
    """Generate a bounded counterfactual ablation plan from an observational report.

    Args:
        report: Validated harness-mechanics observational report dictionary.

    Returns:
        Structured ablation plan dictionary conforming to contract schema.

    Raises:
        ValueError: If report structure or schema version is invalid.
    """
    _validate_report_structure(report)

    evidence_kind = report.get("evidence_kind", "historical")
    records = report.get("records", [])

    proposals: list[dict[str, Any]] = []

    # Iterate exactly over standard mechanisms; no generic registry
    for mech in STANDARD_MECHANISMS:
        evidence = _extract_mechanism_evidence(records, mech)

        # Invariant: No proposal without relevant diagnostic evidence.
        # Empty trajectories, unresolved refs, redactions, or missing telemetry
        # alone do not justify an intervention.
        if not evidence["has_substantive_evidence"]:
            continue

        proposal = _build_proposal_for_mechanism(mech, evidence)
        proposals.append(proposal)

    # Sort proposals deterministically by mechanism name
    proposals.sort(key=lambda p: p["mechanism"])

    # Methodological limitations and non-claims
    limitations: list[str] = [
        "Proposals are bounded analytical experiment specifications, NOT executable jobs or approval for live evaluation.",
        "execution_authorized is strictly False; live execution requires explicit human operator authorization and allocated compute budget.",
        "No causal conclusions may be drawn from historical trajectories alone; observational evidence establishes diagnostic relevance, not verified causality.",
        "Each proposal isolates exactly one changed mechanism while fixing tasks, model, reward, and budgets to avoid confounding variables.",
        "Proposals do not specify synthetic task IDs, profile IDs, or performance expectations; evaluation suites must be drawn from validated benchmark sets.",
        "Reproduction gaps between DeepSeek report infrastructure (DSec, custom builds, cache purges) and standard Harbor runners must be reconciled before counterfactual testing.",
    ]

    if not proposals:
        limitations.append(
            "No ablation proposals were generated because the input report contained zero "
            "substantive diagnostic observations or hypotheses justifying runtime intervention."
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": ARTIFACT_KIND,
        "execution_authorized": False,
        "evidence_kind": evidence_kind,
        "proposals": proposals,
        "limitations": limitations,
    }
