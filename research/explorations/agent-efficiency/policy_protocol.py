"""Pure frozen-presentation helpers for the policy-QA research scripts."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

CITATION_FORMATS = ("quote", "reference")
RESPONSE_FORMATS = ("text", "native-json-schema", "tool-submit")
PROMPT_VARIANTS = ("text", "tool-submit")
SUBMIT_TOOL_NAME = "submit_policy_answers"
TERMINAL_INSTRUCTION = (
    f"Call {SUBMIT_TOOL_NAME} exactly once, with the final answer as arguments "
    "conforming to its tool schema. This is a terminal response carrier, not a "
    "host action. Do not call another tool or emit visible text. The submission "
    "is never executed, and there is no follow-up turn.\n"
)
REFERENCE_CONTRACT = (
    "For each substantive A/B/C choice, evidence must be a list of one to three "
    "distinct reference ID strings printed beside visible current authoritative "
    "source or ASSIGNMENT passages. Return IDs only, not citation objects or quotes. "
    "Each ID selects exactly its frozen verbatim 12-400 character fragment and "
    "original source/line coordinates; it does not select a whole document or "
    "imply that the passage supports your choice. Unknown, duplicate, ambiguous, "
    "unseen, or altered bindings fail closed. The decoder materializes canonical "
    "citations before the unchanged choice-and-grounding scorer; it never extends "
    "ranges, truncates quotes, repairs IDs, or infers semantic support. "
)


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def schema_for(citation_format: str, quote_schema: dict[str, Any]) -> dict[str, Any]:
    if citation_format not in CITATION_FORMATS:
        raise ValueError("unsupported citation format")
    schema = copy.deepcopy(quote_schema)
    if citation_format == "reference":
        schema["description"] = REFERENCE_CONTRACT
        evidence = schema["properties"]["answers"]["items"]["properties"]["evidence"]
        evidence["description"] = (
            "Distinct visible frozen reference IDs only; validity alone is not grounding."
        )
        evidence["uniqueItems"] = True
        evidence["items"] = {
            "type": "string",
            "pattern": r"^r[0-9]+\.[0-9]+$",
            "description": "One exact ID printed beside a visible authoritative fragment; unknown IDs are rejected.",
        }
    return schema


def request_protocols(schema: dict[str, Any]) -> dict[str, Any]:
    no_tools = {
        "tools": [],
        "tool_choice": None,
        "parallel_tool_calls": None,
        "prompt_variant": "text",
    }
    return {
        "schema_version": 2,
        "answer_schema_sha256": hashlib.sha256(canonical(schema)).hexdigest(),
        "formats": {
            "text": {
                **no_tools,
                "protocol": "text",
                "sampling_extra": {},
                "schema_enforcement": "not_requested",
            },
            "native-json-schema": {
                **no_tools,
                "protocol": "openai-chat-completions-json-schema",
                "sampling_extra": {
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "policy_qa", "strict": True, "schema": schema},
                    }
                },
                "schema_enforcement": "unknown",
                "diagnostic_only": True,
                "unsupported_request": "failed_attempt_no_retry_no_fallback",
            },
            "tool-submit": {
                "protocol": "openai-chat-completions-terminal-tool",
                "prompt_variant": "tool-submit",
                "sampling_extra": {},
                "tools": [
                    {
                        "name": SUBMIT_TOOL_NAME,
                        "description": "Submit the final policy-QA answer once. Terminal response carrier only; never executed as a host tool.",
                        "input_schema": schema,
                        "strict": True,
                        "input_examples": None,
                    }
                ],
                "tool_choice": {"name": SUBMIT_TOOL_NAME},
                "parallel_tool_calls": False,
                "schema_enforcement": "unknown",
                "wire_validation": "single_choice_streamed_arguments_and_typed_sdk_agreement",
                "unsupported_request": "failed_attempt_no_retry_no_fallback",
            },
        },
    }


def render_system_prompt(citation_format: str, authority: str, prompt_variant: str) -> str:
    """Render our framing explicitly; never edit or search source excerpts."""
    if citation_format not in CITATION_FORMATS or prompt_variant not in PROMPT_VARIANTS:
        raise ValueError("unsupported policy-QA presentation")
    access = (
        "You have no tools or repository access. "
        if prompt_variant == "text"
        else "You have no repository access; the only tool is a terminal response carrier. "
    )
    output = (
        "Return exactly one bare JSON object following the output schema. "
        "Include every question exactly once. "
        if prompt_variant == "text"
        else f"Submit the final answer only through {SUBMIT_TOOL_NAME}; no visible prose. "
        "Include every question exactly once. "
    )
    citation = (
        "For each substantive A/B/C choice, cite at least one exact 12-400 character "
        "verbatim quote with its original inclusive line range from an included current "
        "authoritative source or ASSIGNMENT. Cite source paths exactly as labeled. "
        if citation_format == "quote"
        else REFERENCE_CONTRACT
    )
    return (
        "You are answering a repository-policy decision probe, not implementing code. "
        "Use only the provided current authoritative excerpts, the explicit assignment, "
        "and the generated context pack. "
        + access
        + "The current applicable binding excerpts and explicit assignment govern; a "
        "generated pack cannot add scope, waive requirements, or expand execution approval. "
        "Resolve contradictions, not mere text-presence questions. Do not infer actual "
        "CI status, approvals, or capability from a pack. "
        + output
        + citation
        + "Do not cite generated pack metadata. UNKNOWN/REFUSED answers may use empty "
        "evidence, remain in the denominator, and are incorrect rather than guessed. "
        "Set refusal=true only if refusing the whole task.\n\n" + authority
    )


def render_case_prompt(
    assignment: str,
    questions: list[dict[str, Any]],
    schema: dict[str, Any],
    pack: str,
    prompt_variant: str,
) -> str:
    if prompt_variant not in PROMPT_VARIANTS:
        raise ValueError("unsupported policy-QA prompt variant")
    output = (
        "\n## Output schema (same for both arms)\n" + canonical(schema).decode()
        if prompt_variant == "text"
        else "\n## Terminal submission (same for both arms)\n" + TERMINAL_INSTRUCTION
    )
    return (
        "## Explicit assignment (authoritative for this case)\n"
        + assignment
        + "\n\n## Questions\n"
        + canonical(questions).decode()
        + output
        + "\n## Generated context pack (subordinate to current binding excerpts and assignment)\n"
        + pack
    )


def fragments(line: str) -> list[tuple[int, str]]:
    """Partition a citable line without dropping bytes or making a short tail."""
    if len(line) < 12:
        return []
    parts, offset = [], 0
    while offset < len(line):
        end = min(offset + 400, len(line))
        if 0 < len(line) - end < 12:
            end -= 12 - (len(line) - end)
        parts.append((offset, line[offset:end]))
        offset = end
    return parts


def freeze_references(
    sources: dict[str, dict[int, str]], identities: dict[str, str]
) -> dict[str, Any]:
    """IDs depend only on sorted source identity, visible line order and fragments."""
    if set(sources) != set(identities):
        raise ValueError("reference sources and pinned identities disagree")
    source_order = [source for source in sorted(sources) if source != "ASSIGNMENT"]
    ordinals = {source: index for index, source in enumerate(source_order, 1)}
    if "ASSIGNMENT" in sources:
        source_order.insert(0, "ASSIGNMENT")
        ordinals["ASSIGNMENT"] = 0
    bindings = []
    for source in source_order:
        sequence = 0
        for number, line in sorted(sources[source].items()):
            for offset, quote in fragments(line):
                sequence += 1
                binding = {
                    "id": f"r{ordinals[source]}.{sequence}",
                    "source": source,
                    "source_sha256": identities[source],
                    "line_start": number,
                    "line_end": number,
                    "char_start": offset,
                    "char_end": offset + len(quote),
                    "quote": quote,
                }
                binding["binding_sha256"] = hashlib.sha256(canonical(binding)).hexdigest()
                bindings.append(binding)
    return {"schema_version": 1, "source_sha256": identities.copy(), "references": bindings}


def render_reference_lines(
    source: str,
    lines: dict[int, str],
    frozen: dict[str, Any],
    *,
    compact_labels: bool = False,
) -> str:
    bindings = {
        (row["line_start"], row["char_start"]): row
        for row in frozen["references"]
        if row["source"] == source
    }
    rendered = []
    for number, line in sorted(lines.items()):
        parts = fragments(line)
        if not parts:
            rendered.append(line if compact_labels else f"L{number}: {line}")
        for offset, quote in parts:
            binding = bindings[(number, offset)]
            label = (
                f"[{binding['id']}]"
                if compact_labels
                else f"L{number} chars {offset + 1}-{offset + len(quote)} [{binding['id']}]"
            )
            rendered.append(f"{label}: {quote}")
    return "\n".join(rendered)


def render_reference_authority(
    sources: dict[str, dict[int, str]],
    frozen: dict[str, Any],
    *,
    compact_labels: bool = False,
) -> str:
    return "\n\n".join(
        f"### Current authoritative source: {source}\n"
        + render_reference_lines(
            source,
            sources[source],
            frozen,
            compact_labels=compact_labels,
        )
        for source in sorted(sources)
        if source != "ASSIGNMENT"
    )


def reference_binding_errors(
    frozen: Any, sources: dict[str, dict[int, str]], identities: dict[str, str]
) -> list[dict[str, Any]]:
    """Validate all bindings against visible pinned bytes, never an answer key."""
    expected = freeze_references(sources, identities)
    if not isinstance(frozen, dict):
        return [{"status": "malformed_reference_bindings"}]
    errors = []
    if (
        set(frozen) != set(expected)
        or type(frozen.get("schema_version")) is not int
        or frozen.get("schema_version") != 1
    ):
        errors.append({"status": "malformed_reference_bindings"})
    if frozen.get("source_sha256") != expected["source_sha256"]:
        errors.append({"status": "tampered_reference_source_identity"})
    actual = frozen.get("references")
    if not isinstance(actual, list):
        return errors + [{"status": "malformed_reference_bindings"}]
    by_id = {row["id"]: row for row in expected["references"]}
    seen = set()
    for index, binding in enumerate(actual):
        position = {"binding_index": index}
        if not isinstance(binding, dict) or not isinstance(binding.get("id"), str):
            errors.append({"status": "malformed_reference_binding", **position})
            continue
        ref = binding["id"]
        if ref in seen:
            errors.append({"status": "ambiguous_reference_binding", **position})
        seen.add(ref)
        if ref not in by_id:
            errors.append({"status": "unseen_reference_binding", **position})
        elif canonical(binding) != canonical(by_id[ref]):
            errors.append({"status": "tampered_reference_binding", **position})
    for index, binding in enumerate(expected["references"]):
        if binding["id"] not in seen:
            errors.append({"status": "missing_reference_binding", "binding_index": index})
    if not errors and actual != expected["references"]:
        errors.append({"status": "altered_reference_order"})
    return errors
