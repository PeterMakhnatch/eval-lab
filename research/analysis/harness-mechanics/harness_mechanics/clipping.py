"""Harness Mechanics Lab: Observation Clipping and Compaction Diagnostic.

Exports inspect(ir: TrajectoryIR) -> dict[str, Any] over canonical TrajectoryIR.
Adheres strictly to the frozen diagnostic contract:
- mechanism: "clipping"
- observations: list of dicts with keys:
    code: str
    step_id: int | None
    tool_call_id: str | None
    locator: str
    evidence_kind: "observed" | "hypothesis"
    summary: str
- unknowns: list of str

Diagnostic limits:
- Detect explicit retained truncation/compaction markers and metadata.
- A standalone sentinel can be printed by the task itself (marker spoofing); absent retained
  structured harness metadata, all unstructured marker matches are treated strictly as
  indicators/hypotheses.
- Locators are RFC6901 JSON pointers into original raw trajectory fields using zero-based array positions.
- Post-hoc evidence redaction (e.g. <<evallab-redacted: ...>>, evallab_redaction)
  indicates artifact withholding or secret masking, NOT runtime harness clipping
  observed by the model; it is accounted as an evidence-availability unknown.
- Report discarded information quantity only when actually stated.
- Preserve character versus byte units distinctly; never conflate them.
- Differentiate observed truncation indicators from causal/evidence-loss hypotheses.
- Explicitly declare that unretained content remains unknown.
- Never claim discarded content contained an error or caused failure.
- Do not fetch referenced CAS or remote data automatically.
- No raw prompt, reasoning, command, or output text in summaries or locators.
"""

from __future__ import annotations

import re
from typing import Any

from evallab.trajectory_ir import TrajectoryIR

# Post-hoc evidence redaction pattern (downstream promotion/withholding/secret masking)
_REDACTION_PATTERN: re.Pattern[str] = re.compile(
    r"<<evallab-redacted:\s*(?P<bytes>\d+)\s*bytes(?:,\s*(?P<digest>sha256:[0-9a-fA-F]+|[0-9a-fA-F]+))?>>",
    re.IGNORECASE,
)

# Known harness-level truncation and compaction marker patterns
_MARKER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "evallab_truncated",
        re.compile(
            r"<<evallab-truncated:\s*(?P<amount>\d+)\s*(?P<unit>bytes)\s*omitted(?:,\s*full\s*(?P<digest>[0-9a-fA-F]+))?>>"
        ),
    ),
    (
        "output_truncated_with_amount",
        re.compile(
            r"\[Output\s+(?:was\s+)?truncated(?:\s+to\s+(?P<amount>\d+)\s*(?P<unit>bytes|characters|chars|lines|tokens))?\]",
            re.IGNORECASE,
        ),
    ),
    (
        "output_exceeds_limit",
        re.compile(
            r"\[Output\s+exceeds(?:\s+size\s+limit)?(?:\s+of\s+(?P<amount>\d+)\s*(?P<unit>bytes|characters|chars|lines))?,\s*truncated\]",
            re.IGNORECASE,
        ),
    ),
    (
        "omitted_units",
        re.compile(
            r"\[(?:\.\.\.\s*)?(?P<amount>\d+)\s*(?P<unit>bytes|characters|chars|lines|tokens)\s*omitted(?:\s*\.\.\.)?\]",
            re.IGNORECASE,
        ),
    ),
    (
        "generic_bracket_truncated",
        re.compile(
            r"\[(?:\.\.\.\s*)?(?:OUTPUT\s+)?TRUNCATED(?:\s*\.\.\.)?\]",
            re.IGNORECASE,
        ),
    ),
    (
        "html_comment_truncated",
        re.compile(
            r"<!--\s*truncated:\s*true\s*-->",
            re.IGNORECASE,
        ),
    ),
    (
        "html_comment_tokens_shed",
        re.compile(
            r"<!--\s*tokens-shed:\s*(?P<amount>\d+)\s*-->",
            re.IGNORECASE,
        ),
    ),
    (
        "context_pack_notice",
        re.compile(
            r"###?\s*(?:⚠️\s*)?Context Pack Truncation Notice",
            re.IGNORECASE,
        ),
    ),
    (
        "system_output_truncated",
        re.compile(
            r"\[SYSTEM:\s*Output\s+truncated(?:\s+to\s+(?P<amount>\d+)\s*(?P<unit>bytes|characters|chars|lines))?\]",
            re.IGNORECASE,
        ),
    ),
    (
        "tool_output_truncated",
        re.compile(
            r"\[Tool\s+output\s+truncated\]",
            re.IGNORECASE,
        ),
    ),
    (
        "dsh_response_clipped",
        re.compile(
            r"<response\s+clipped>(?:<NOTE>.*?</NOTE>)?",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "remaining_omitted",
        re.compile(
            r"\[\.\.\.\s*remaining\s*(?:output\s*)?omitted\s*\.\.\.\]",
            re.IGNORECASE,
        ),
    ),
]


def _extract_amounts_and_units(extra: dict[str, Any]) -> list[tuple[int, str]]:
    """Extract omitted/truncated quantities and exact units from metadata.

    Preserves character, byte, line, and token units distinctly.
    """
    results: list[tuple[int, str]] = []
    candidates: list[tuple[str, str]] = [
        ("truncated_bytes", "bytes"),
        ("bytes_omitted", "bytes"),
        ("omitted_bytes", "bytes"),
        ("truncated_chars", "characters"),
        ("chars_omitted", "characters"),
        ("omitted_chars", "characters"),
        ("truncated_lines", "lines"),
        ("lines_omitted", "lines"),
        ("omitted_lines", "lines"),
        ("tokens_shed", "tokens"),
        ("dropped_tokens", "tokens"),
        ("tokens_dropped", "tokens"),
    ]

    for key, unit in candidates:
        val = extra.get(key)
        if type(val) is int and val > 0:
            if not any(u == unit for _, u in results):
                results.append((val, unit))
        elif (
            isinstance(val, str)
            and val.strip().isdigit()
            and int(val.strip()) > 0
            and not any(u == unit for _, u in results)
        ):
            results.append((int(val.strip()), unit))

    return results


def _extract_content_text(content: Any) -> str | None:
    """Extract inspectable string content from raw observation content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts) if parts else None
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return None


def inspect(ir: TrajectoryIR) -> dict[str, Any]:
    """Inspect TrajectoryIR for observation clipping and compaction evidence.

    Returns:
        dict with:
            mechanism: 'clipping'
            observations: list[dict[str, Any]]
            unknowns: list[str]
    """
    if not isinstance(ir, TrajectoryIR):
        raise ValueError(f"Expected TrajectoryIR instance, got {type(ir).__name__}")

    observations: list[dict[str, Any]] = []
    unknowns: list[str] = []

    has_any_clipping_evidence = False

    # 1. Trajectory-level policy checks
    ir_extra = ir.extra if isinstance(ir.extra, dict) else {}
    if ir_extra.get("truncation"):
        has_any_clipping_evidence = True
        observations.append(
            {
                "code": "TRAJECTORY_POLICY_TRUNCATION",
                "step_id": None,
                "tool_call_id": None,
                "locator": "/extra/truncation",
                "evidence_kind": "observed",
                "summary": "Trajectory configuration declares explicit truncation policy.",
            }
        )
    if ir_extra.get("compaction_settings"):
        has_any_clipping_evidence = True
        observations.append(
            {
                "code": "TRAJECTORY_POLICY_COMPACTION",
                "step_id": None,
                "tool_call_id": None,
                "locator": "/extra/compaction_settings",
                "evidence_kind": "observed",
                "summary": "Trajectory configuration declares explicit compaction settings.",
            }
        )

    if not ir.steps:
        unknowns.append(
            "empty_trajectory_steps: Trajectory contains no steps to inspect for clipping."
        )
        return {
            "mechanism": "clipping",
            "observations": observations,
            "unknowns": unknowns,
        }

    for step_idx, step in enumerate(ir.steps):
        step_id = step.step_id

        # 2. Step-level metadata checks
        step_extra = step.extra if isinstance(step.extra, dict) else {}

        # Post-hoc evidence redaction check at step level (metadata or message)
        step_msg_str = _extract_content_text(step.message)
        has_step_redaction = bool(
            step_extra.get("evallab_redaction")
            or step_extra.get("redacted")
            or step_extra.get("is_redacted")
            or step_extra.get("secrets_masked")
            or (step_msg_str and _REDACTION_PATTERN.search(step_msg_str))
            or (step.reasoning_content and _REDACTION_PATTERN.search(step.reasoning_content))
        )
        if has_step_redaction:
            unknowns.append(
                f"evidence_redaction_post_hoc: Step {step_id} metadata or message records post-hoc evidence "
                f"redaction; indicates artifact withholding or secret masking, not runtime harness "
                f"clipping observed by the model"
            )

        is_step_truncated = (
            step_extra.get("truncated") is True
            or (
                isinstance(step_extra.get("truncated"), str)
                and step_extra["truncated"].lower() in ("true", "1")
            )
            or step_extra.get("is_truncated") is True
        )

        if is_step_truncated:
            has_any_clipping_evidence = True
            amounts = _extract_amounts_and_units(step_extra)
            detail = ", " + ", ".join(f"amount: {amt} {u}" for amt, u in amounts) if amounts else ""
            observations.append(
                {
                    "code": "STEP_METADATA_TRUNCATED",
                    "step_id": step_id,
                    "tool_call_id": None,
                    "locator": f"/steps/{step_idx}/extra",
                    "evidence_kind": "observed",
                    "summary": f"Step metadata explicitly records context truncation (truncated=True{detail}).",
                }
            )

        if step_extra.get("ring_buffer_truncated") is True:
            has_any_clipping_evidence = True
            observations.append(
                {
                    "code": "STEP_METADATA_TRUNCATED",
                    "step_id": step_id,
                    "tool_call_id": None,
                    "locator": f"/steps/{step_idx}/extra/ring_buffer_truncated",
                    "evidence_kind": "observed",
                    "summary": "Step metadata explicitly records ring buffer context truncation (ring_buffer_truncated=True).",
                }
            )

        if (
            step_extra.get("compaction")
            or step_extra.get("context_compaction")
            or step_extra.get("forced_compaction")
        ):
            has_any_clipping_evidence = True
            observations.append(
                {
                    "code": "STEP_METADATA_COMPACTION",
                    "step_id": step_id,
                    "tool_call_id": None,
                    "locator": f"/steps/{step_idx}/extra",
                    "evidence_kind": "observed",
                    "summary": "Step metadata explicitly records context compaction operation.",
                }
            )

        if step.is_copied_context is True:
            has_any_clipping_evidence = True
            observations.append(
                {
                    "code": "STEP_COPIED_CONTEXT",
                    "step_id": step_id,
                    "tool_call_id": None,
                    "locator": f"/steps/{step_idx}/is_copied_context",
                    "evidence_kind": "observed",
                    "summary": "Step record indicates copied or shared context boundary across turns.",
                }
            )

        # 3. Observation-level checks
        for obs_idx, obs in enumerate(step.observation_results):
            tool_call_id = obs.source_call_id
            obs_locator_base = f"/steps/{step_idx}/observation/results/{obs_idx}"
            obs_extra = obs.extra if isinstance(obs.extra, dict) else {}

            obs_clipped_observed = False
            obs_clipped_hypothesis = False

            # Post-hoc evidence redaction check at observation level
            content_str = _extract_content_text(obs.content)
            has_post_hoc_redaction = bool(
                obs_extra.get("evallab_redaction")
                or obs_extra.get("redacted")
                or obs_extra.get("is_redacted")
                or obs_extra.get("secrets_masked")
                or (content_str and _REDACTION_PATTERN.search(content_str))
            )
            if has_post_hoc_redaction:
                unknowns.append(
                    f"evidence_redaction_post_hoc: Step {step_id} tool_call {tool_call_id or 'none'} "
                    f"contains post-hoc evidence redaction (evallab-redacted); indicates artifact "
                    f"withholding or secret masking, not runtime harness clipping observed by the model"
                )

            # Check explicit observation metadata
            is_meta_truncated = (
                obs_extra.get("truncated") is True
                or obs_extra.get("is_truncated") is True
                or obs_extra.get("clipped") is True
                or obs_extra.get("is_clipped") is True
                or (
                    isinstance(obs_extra.get("truncated"), str)
                    and obs_extra["truncated"].lower() in ("true", "1")
                )
            )
            amounts = _extract_amounts_and_units(obs_extra)

            if is_meta_truncated or amounts:
                has_any_clipping_evidence = True
                obs_clipped_observed = True
                detail = (
                    ", " + ", ".join(f"amount: {amt} {u}" for amt, u in amounts) if amounts else ""
                )
                observations.append(
                    {
                        "code": "OBSERVATION_METADATA_TRUNCATED",
                        "step_id": step_id,
                        "tool_call_id": tool_call_id,
                        "locator": f"{obs_locator_base}/extra",
                        "evidence_kind": "observed",
                        "summary": f"Observation metadata explicitly records output truncation{detail}.",
                    }
                )

            if obs_extra.get("compaction") is True or obs_extra.get("compacted") is True:
                has_any_clipping_evidence = True
                obs_clipped_observed = True
                observations.append(
                    {
                        "code": "OBSERVATION_METADATA_COMPACTION",
                        "step_id": step_id,
                        "tool_call_id": tool_call_id,
                        "locator": f"{obs_locator_base}/extra",
                        "evidence_kind": "observed",
                        "summary": "Observation metadata explicitly records observation compaction.",
                    }
                )

            # Check content text for harness truncation markers.
            # A standalone sentinel can be printed by the task itself (marker spoofing).
            # Absent retained structured harness metadata, all unstructured marker matches
            # are treated strictly as indicators/hypotheses.
            if content_str:
                for marker_name, pat in _MARKER_PATTERNS:
                    match = pat.search(content_str)
                    if match:
                        has_any_clipping_evidence = True
                        m_groups = match.groupdict()
                        raw_amt = m_groups.get("amount")
                        m_amt = (
                            int(raw_amt)
                            if raw_amt and raw_amt.isdigit() and int(raw_amt) > 0
                            else None
                        )
                        m_unit = m_groups.get("unit")
                        if m_unit:
                            m_unit = (
                                "bytes"
                                if "byte" in m_unit
                                else "characters"
                                if "char" in m_unit
                                else m_unit
                            )

                        amt_detail = (
                            f", amount: {m_amt} {m_unit}"
                            if m_amt is not None and m_unit is not None
                            else ""
                        )

                        obs_clipped_hypothesis = True
                        observations.append(
                            {
                                "code": "OBSERVATION_MARKER_INDICATOR",
                                "step_id": step_id,
                                "tool_call_id": tool_call_id,
                                "locator": f"{obs_locator_base}/content",
                                "evidence_kind": "hypothesis",
                                "summary": (
                                    f"Unstructured truncation marker detected in output content "
                                    f"(marker: {marker_name}{amt_detail}); treated as indicator/hypothesis "
                                    f"absent retained structured harness metadata."
                                ),
                            }
                        )
                        # Detect primary marker pattern per observation to avoid redundant duplication
                        break

            # 4. Evidence-availability accounting: post-hoc redactions and unresolved references
            # remain strictly in unknowns, not in observed event counts.
            # Canonical IR may synthesize a digest content_ref even when inline content is fully
            # available. Treat content_ref as unavailable inline evidence only when inline content
            # is absent. When clipping is actually indicated, retain unknown full-output extent.
            if obs.content_ref and obs.content is None:
                unknowns.append(
                    f"content_ref_unresolved: Step {step_id} tool_call {tool_call_id or 'none'} "
                    f"references external CAS/content_ref which remains unhydrated"
                )
            elif obs_clipped_observed:
                unknowns.append(
                    f"missing_full_output: Step {step_id} tool_call {tool_call_id or 'none'} "
                    f"truncated output has no retained full content reference"
                )
            elif obs_clipped_hypothesis:
                unknowns.append(
                    f"ambiguous_marker_unverified: Step {step_id} tool_call {tool_call_id or 'none'} "
                    f"contains marker-like text without structured harness metadata; unverified whether any output was omitted"
                )
    # 5. Non-causal unknowns accounting
    if has_any_clipping_evidence:
        unknowns.append(
            "unretained_content_unknown: Contents of discarded or omitted observation data are unknown; "
            "cannot determine whether omitted output contained errors or affected trajectory outcome."
        )
    else:
        unknowns.append(
            "unannotated_observation_boundaries: Trajectory contains no explicit clipping metadata or "
            "retained markers; unannotated outputs cannot be certified as untruncated without "
            "harness-level instrumentation."
        )

    return {
        "mechanism": "clipping",
        "observations": observations,
        "unknowns": unknowns,
    }
