"""Run the frozen context-compiler policy-QA pairs on included subscriptions only.

Dry-run is the default: it writes exact private prompts and a counterbalanced plan,
without starting a gateway or making model calls. Existing research runtime only::

    ~/Developer/fxtr/harbor-kit/.venv/bin/python \
      research/explorations/agent-efficiency/run_policy_pairs.py \
      --cases /tmp/agent-efficiency/policy-probe/quote-v3-cases-01/manifest.json \
      --output /tmp/agent-efficiency/policy-probe/quote-v3-tool-dry-01 \
      --response-format tool-submit

Explicit four-call, two-family technical pilot (parent execution only)::

    ~/Developer/fxtr/harbor-kit/.venv/bin/python \
      research/explorations/agent-efficiency/run_policy_pairs.py \
      --cases /tmp/agent-efficiency/policy-probe/quote-v3-cases-01/manifest.json \
      --output /tmp/agent-efficiency/policy-probe/quote-v3-tool-pilot-01 \
      --response-format tool-submit --task policy-v1-contextpack-builder \
      --run-subscription --repeats 1 --hard-max-calls 4 \
      --concurrency 1 --timeout-seconds 180

Prerequisites: exact manifest-pinned harbor-kit/behaviors sources and installed
versions, authenticated OMP Google Antigravity routes named in the manifest, and
known fresh usage <=60% in BOTH applicable 5h and weekly buckets. No key lookup,
direct Anthropic, paid API, local inference, retry, or fallback is implemented.
make_api('omp', exact_model) and behaviors ModelRequest own all model transport.
A read-only SDK response hook observes that existing transport; it is not another
wire client. Its payload is checked with harbor_kit.models._served_model as well
as the existing transport's mismatch guard. Missing served identity fails closed.
Only self-reported gateway model identity is available, not provider attestation.
Tool admission requires the observed single-choice OpenAI chunk SSE shape, a
provider call ID, serialized string arguments, and a terminal [DONE] event.
Nonstream/alternate shapes and synthetic SDK-only call IDs are unsupported.

Scoring is fixed-choice, source/range-grounded policy QA, not implementation or
semantic quality. All planned cells and failed/missing/refused/malformed attempts
stay in receipts. Repeats are nested under original task, never independent units.
Reported token counters are distinct from estimates; absent counters/cost remain
null. Public receipts contain no prompts, responses, quotes, account identities,
or adapter error text. Raw inputs/responses/errors remain private under /tmp.

Only fresh v3 manifests with an explicit citation_format are accepted. Audit
immutable v1/v2 artifacts with their frozen source; there is no compatibility shim.
--response-format text (default) preserves the original quote/text request bytes.
--response-format native-json-schema is diagnostic only: it sends the pinned
response_format through SamplingParams.extra, but the inspected gateway drops
that field downstream. Conforming output is not evidence of enforcement.
--response-format tool-submit offers one typed submit_policy_answers terminal
carrier, forces its name, and disables parallel calls. It is never executed.
Exactly one observed streamed call must agree with ContentToolUse, with no visible
text, parse_error, duplicate keys, nonfinite values, repair, retry or fallback.

Citation presentation (quote/reference, fixed at preparation) and response format
(text/native-json-schema/tool-submit, selected here) are separate plan factors.
Each plan/attempt pins the selected actual system/prompt bytes, answer schema,
protocol and serialized wire controls. The existing SDK response observer also
retains exact private request bytes and checks their prompt/protocol pins.
Schema enforcement remains unknown, including when output conforms. Reference
output is an evidence list of distinct visible IDs, not citation objects. All bindings are revalidated
against pinned visible source bytes, then canonical source/line/quote citations
go to the unchanged strict scorer and hidden key. Unknown, ambiguous, unseen,
tampered and duplicate references fail closed. Receipts retain every decoder
error's status and position without echoing untrusted reply text; raw replies,
materialized citations and full requests/responses stay private.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import dataclasses
import hashlib
import importlib
import importlib.metadata
import json
import math
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from policy_protocol import (
    CITATION_FORMATS,
    PROMPT_VARIANTS,
    RESPONSE_FORMATS,
    SUBMIT_TOOL_NAME,
    freeze_references,
    reference_binding_errors,
    render_case_prompt,
    render_reference_authority,
    render_reference_lines,
    render_system_prompt,
    request_protocols,
)

SCRATCH = Path("/tmp/agent-efficiency/policy-probe")
ALLOWED_MODELS = frozenset(
    (
        "google-antigravity/gemini-4-argon-low",
        "google-antigravity/claude-opus-5-5",
    )
)
USAGE_FIELDS = (
    "input_tokens_total",
    "input_tokens_noncached",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens_reported",
)
_ACTIVE_HTTP: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "policy_probe_http",
    default=None,
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def private_write(path: Path, value: Any, *, raw: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as handle:
        handle.write(value if raw else encoded(value))
    path.chmod(0o600)


def checked_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or Path(relative).is_absolute():
        raise ValueError("manifest artifact path escapes frozen case directory")
    return path


def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def read_json(path: Path) -> Any:
    return json.loads(path.read_bytes(), object_pairs_hook=no_duplicate_keys)


def strict_json(text: str) -> Any:
    """Reject lossy JSON before the SDK's parsed input can hide invalid bytes."""

    def reject_constant(value: str) -> Any:
        raise ValueError("nonfinite JSON number")

    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite JSON number")
        return number

    return json.loads(
        text,
        object_pairs_hook=no_duplicate_keys,
        parse_constant=reject_constant,
        parse_float=finite_float,
    )


def strict_encoded(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode()


def wire_protocol_fields(protocol: dict[str, Any]) -> dict[str, Any]:
    """The existing SDK's serialized controls, for observation, never sending."""
    return {
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                    "strict": tool["strict"],
                },
            }
            for tool in protocol["tools"]
        ],
        "tool_choice": (
            {"type": "function", "function": {"name": protocol["tool_choice"]["name"]}}
            if protocol["tool_choice"] is not None
            else None
        ),
        "parallel_tool_calls": protocol["parallel_tool_calls"],
        "response_format": protocol["sampling_extra"].get("response_format"),
    }


def pin_observed_request(
    captured: list[dict[str, Any]],
    row: dict[str, Any],
    protocol: dict[str, Any],
    system: str,
    prompt: str,
) -> bool:
    row["request_pin_verification"] = "unknown_or_mismatched"
    if len(captured) != 1 or not captured[0].get("request_utf8_valid"):
        return False
    exchange = captured[0]
    row["wire_request_sha256"] = exchange["request_body_sha256"]
    try:
        body = strict_json(exchange["request_body"])
        if not isinstance(body, dict):
            return False
        controls = {
            field: body.get(field, [] if field == "tools" else None)
            for field in wire_protocol_fields(protocol)
        }
        row["observed_wire_protocol_sha256"] = sha256(strict_encoded(controls))
        messages = body.get("messages")
        if (
            not isinstance(messages, list)
            or len(messages) != 2
            or not all(isinstance(message, dict) for message in messages)
        ):
            return False
        if not all(isinstance(message.get("content"), str) for message in messages):
            return False
        row["observed_system_sha256"] = sha256(messages[0]["content"].encode())
        row["observed_prompt_sha256"] = sha256(messages[1]["content"].encode())
        observed_schema = None
        if row["response_format"] == "tool-submit":
            tools = controls["tools"]
            if isinstance(tools, list) and len(tools) == 1 and isinstance(tools[0], dict):
                function = tools[0].get("function")
                if isinstance(function, dict):
                    observed_schema = function.get("parameters")
        elif row["response_format"] == "native-json-schema":
            response_format = controls["response_format"]
            if isinstance(response_format, dict) and isinstance(
                response_format.get("json_schema"), dict
            ):
                observed_schema = response_format["json_schema"].get("schema")
        row["observed_schema_sha256"] = (
            sha256(strict_encoded(observed_schema)) if observed_schema is not None else None
        )
        matches = (
            body.get("model") == row["model"]
            and body.get("stream") is True
            and messages
            == [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
            and row["observed_system_sha256"] == row["system_sha256"]
            and row["observed_prompt_sha256"] == row["prompt_sha256"]
            and row["observed_wire_protocol_sha256"] == row["wire_protocol_sha256"]
            and row["observed_schema_sha256"] == row["requested_schema_sha256"]
        )
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return False
    row["request_pin_verification"] = "matched" if matches else "unknown_or_mismatched"
    return matches


def check_runtime(runtime: dict[str, Any]) -> dict[str, Any]:
    """Verify actual imported module bytes, not just stale editable paths."""
    observed = []
    for expected in runtime["sources"]:
        name = expected["module"]
        if name.startswith("harbor-kit/"):
            module = importlib.import_module("harbor_kit.models")
            path = Path(module.__file__).resolve().parents[2] / name.split("/", 1)[1]
        else:
            path = Path(importlib.import_module(name).__file__).resolve()
        digest = sha256(path.read_bytes())
        if digest != expected["sha256"]:
            raise ValueError(f"research runtime source changed: {name}; prepare a new manifest")
        observed.append({"module": name, "sha256": digest})
    for name, version in runtime["distributions"].items():
        if importlib.metadata.version(name) != version:
            raise ValueError(f"research runtime distribution changed: {name}")
    omp_version = subprocess.check_output(["omp", "--version"], timeout=30).decode().strip()
    if omp_version != runtime["omp_version"]:
        raise ValueError("OMP gateway version changed; prepare a new manifest")
    if sys.version != runtime["python"]:
        raise ValueError("research Python version changed; use the preparation interpreter")
    helper = importlib.import_module("harbor_kit.models")
    if not callable(getattr(helper, "_served_model", None)):
        raise ValueError("pinned harbor-kit served-model verifier is unavailable")
    return {
        "python": sys.version,
        "distributions": runtime["distributions"],
        "omp_version": omp_version,
        "source_hashes": observed,
        "transport": "harbor_kit.models.make_api('omp', exact_model) -> omp auth-gateway stdio",
        "served_identity_verifier": "harbor_kit.models._served_model plus existing transport mismatch guard",
    }


def load_cases(manifest_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    root = manifest_path.parent.resolve()
    if not root.is_relative_to(SCRATCH.resolve()):
        raise ValueError(f"frozen private cases must remain under {SCRATCH}")
    manifest = read_json(manifest_path)
    if manifest.get("schema_version") != 3 or manifest.get("study") != "contextpack-policy-qa-v3":
        raise ValueError(
            "unsupported policy-QA manifest; v1/v2 artifacts are immutable and require their frozen source; prepare fresh v3 cases"
        )
    citation_format = manifest.get("citation_format")
    if citation_format not in CITATION_FORMATS:
        raise ValueError("v3 manifests require an explicit supported citation_format")
    if manifest.get("pack_token_budget") != 12_000:
        raise ValueError("this study requires 12k pack budgets")
    models = manifest.get("models")
    if (
        not isinstance(models, list)
        or not models
        or len(set(models)) != len(models)
        or not set(models) <= ALLOWED_MODELS
    ):
        raise ValueError("manifest models must be exact included Google Antigravity routes")
    records = {}
    for item in manifest["artifacts"]:
        if item["path"] in records:
            raise ValueError("duplicate frozen artifact path")
        data = checked_path(root, item["path"]).read_bytes()
        if sha256(data) != item["sha256"] or len(data) != item["bytes"]:
            raise ValueError(f"frozen input bytes changed: {item['path']}")
        records[item["path"]] = data

    def artifact(relative: str) -> bytes:
        if relative not in records:
            raise ValueError(f"unhashed referenced artifact: {relative}")
        return records[relative]

    def json_artifact(relative: str) -> Any:
        return json.loads(artifact(relative), object_pairs_hook=no_duplicate_keys)

    schema = json_artifact(manifest["answer_schema_file"])
    if (
        artifact(manifest["answer_schema_file"]) != encoded(schema)
        or sha256(encoded(schema)) != manifest["answer_schema_sha256"]
    ):
        raise ValueError("answer schema pin disagrees with canonical frozen schema")
    protocols = json_artifact(manifest["request_protocols_file"])
    if sha256(artifact(manifest["request_protocols_file"])) != manifest[
        "request_protocols_sha256"
    ] or encoded(protocols) != encoded(request_protocols(schema)):
        raise ValueError("request protocols disagree with the frozen schema or supported contracts")
    questions = json_artifact(manifest["questions_file"])
    keys = json_artifact(manifest["answer_key_file"])
    authority = json_artifact(manifest["governing_excerpts_file"])
    system_records = manifest["system_prompts"]
    if set(system_records) != set(PROMPT_VARIANTS):
        raise ValueError("v3 manifests require exact text and tool-submit system prompts")
    systems = {}
    for variant, record in system_records.items():
        data = artifact(record["path"])
        if sha256(data) != record["sha256"] or len(data) != record["bytes"]:
            raise ValueError("system prompt record disagrees with frozen inventory")
        systems[variant] = data.decode()
    if len(questions) != 8 or len({q["question_id"] for q in questions}) != 8:
        raise ValueError("expected eight fixed, unique public questions")
    for name in ("run_policy_pairs.py", "policy_protocol.py"):
        if sha256(Path(__file__).with_name(name).read_bytes()) != sha256(
            artifact(f"study-sources/{name}")
        ):
            raise ValueError(
                "runner/protocol source changed after preparation; freeze a new case manifest"
            )
    if set(authority) != set(manifest["governing_source_sha256"]):
        raise ValueError("governing source identities disagree")
    visible_authority, authority_identities = {}, {}
    for source, record in authority.items():
        data = artifact(f"governing-sources/{source}")
        digest = sha256(data)
        if (
            digest != record["source_sha256"]
            or digest != manifest["governing_source_sha256"][source]
            or len(data) != record["source_bytes"]
        ):
            raise ValueError("governing source pin disagrees with frozen bytes")
        original = data.decode().splitlines()
        visible: dict[int, str] = {}
        for excerpt in record["excerpts"]:
            first, last = excerpt["line_start"], excerpt["line_end"]
            if (
                type(first) is not int
                or type(last) is not int
                or not 1 <= first <= last <= len(original)
            ):
                raise ValueError("invalid governing excerpt coordinates")
            if excerpt["text"] != "\n".join(original[first - 1 : last]):
                raise ValueError("governing excerpt is not verbatim pinned source")
            for number in range(first, last + 1):
                if number in visible:
                    raise ValueError("ambiguous governing excerpt line")
                visible[number] = original[number - 1]
        visible_authority[source] = visible
        authority_identities[source] = digest
    source_order = manifest["governing_source_order"]
    if (
        not isinstance(source_order, list)
        or len(source_order) != len(authority)
        or set(source_order) != set(authority)
    ):
        raise ValueError("governing presentation order disagrees with pinned sources")
    reference_labels = manifest["reference_label_style"]
    if citation_format == "reference" and reference_labels not in {"coordinates", "compact"}:
        raise ValueError("unsupported frozen reference label style")
    if citation_format == "quote" and reference_labels is not None:
        raise ValueError("quote cases must not carry reference labels")
    if citation_format == "reference":
        rendered_authority = render_reference_authority(
            visible_authority,
            freeze_references(visible_authority, authority_identities),
            compact_labels=reference_labels == "compact",
        )
    else:
        rendered_authority = "\n\n".join(
            f"### Current authoritative source: {source}\n"
            + "\n".join(
                f"L{number}: {line}" for number, line in sorted(visible_authority[source].items())
            )
            for source in source_order
        )
    for variant, system in systems.items():
        if system != render_system_prompt(citation_format, rendered_authority, variant):
            raise ValueError(
                "frozen system prompt does not exactly render its pinned authority and protocol framing"
            )
    cases = {}
    for case in manifest["cases"]:
        task_id = case["task_id"]
        if task_id in cases or task_id not in keys:
            raise ValueError("duplicate task or missing hidden key")
        if set(case["arms"]) != {"baseline", "candidate"}:
            raise ValueError("each original task needs both arms")
        if set(keys[task_id]) != {q["question_id"] for q in questions}:
            raise ValueError("hidden question identity mismatch")
        assignment_bytes = artifact(case["assignment_file"])
        if sha256(assignment_bytes) != case["assignment_sha256"]:
            raise ValueError("assignment identity disagrees with frozen bytes")
        assignment = assignment_bytes.decode()
        sources = {"ASSIGNMENT": dict(enumerate(assignment.splitlines(), 1)), **visible_authority}
        identities = {"ASSIGNMENT": sha256(assignment_bytes), **authority_identities}
        frozen = None
        if citation_format == "reference":
            frozen_bytes = artifact(case["reference_bindings_file"])
            if sha256(frozen_bytes) != case["reference_bindings_sha256"]:
                raise ValueError("reference binding pin disagrees with manifest")
            frozen = json_artifact(case["reference_bindings_file"])
            errors = reference_binding_errors(frozen, sources, identities)
            if errors:
                raise ValueError(
                    "invalid frozen reference bindings: "
                    + ", ".join(sorted({error["status"] for error in errors}))
                )
            rendered_assignment = render_reference_lines(
                "ASSIGNMENT",
                sources["ASSIGNMENT"],
                frozen,
                compact_labels=reference_labels == "compact",
            )
        else:
            if (
                case["reference_bindings_file"] is not None
                or case["reference_bindings_sha256"] is not None
            ):
                raise ValueError("quote cases must not carry a reference-presentation arm")
            rendered_assignment = "\n".join(
                f"L{index}: {line}" for index, line in sources["ASSIGNMENT"].items()
            )
        for arm in case["arms"].values():
            pack_record = arm["pack"]
            pack_bytes = artifact(pack_record["path"])
            if (
                sha256(pack_bytes) != pack_record["sha256"]
                or len(pack_bytes) != pack_record["bytes"]
            ):
                raise ValueError("pack artifact record disagrees with frozen inventory")
            if set(arm["prompts"]) != set(PROMPT_VARIANTS):
                raise ValueError("each arm requires exact text and tool-submit prompt variants")
            for variant, record in arm["prompts"].items():
                data = artifact(record["path"])
                if sha256(data) != record["sha256"] or len(data) != record["bytes"]:
                    raise ValueError("prompt artifact record disagrees with frozen inventory")
                expected = render_case_prompt(
                    rendered_assignment,
                    questions,
                    schema,
                    pack_bytes.decode(),
                    variant,
                )
                if data != expected.encode():
                    raise ValueError(
                        "compiler arms must share the exact frozen assignment, questions, schema and reference presentation"
                    )
        cases[task_id] = {
            "case": case,
            "key": keys[task_id],
            "sources": sources,
            "source_sha256": identities,
            "reference_bindings": frozen,
        }
    if set(keys) != set(cases):
        raise ValueError("hidden key contains a different task set")
    return manifest, {
        "root": root,
        "cases": cases,
        "schema": schema,
        "protocols": protocols,
        "questions": questions,
        "records": records,
        "systems": systems,
    }


def empty_usage() -> dict[str, None]:
    return dict.fromkeys(USAGE_FIELDS)


def integer(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def payload_chunks(payload: Any, *, strict: bool = False) -> list[dict[str, Any]]:
    if strict:
        if not isinstance(payload, str):
            raise ValueError("unsupported tool wire body")
        chunks, done, in_event = [], False, False
        for line in payload.splitlines():
            if not line:
                in_event = False
                continue
            if done or in_event or not line.startswith("data:"):
                raise ValueError("unsupported tool wire event")
            in_event = True
            data = line[5:].strip()
            if data == "[DONE]":
                done = True
                continue
            value = strict_json(data)
            if not isinstance(value, dict):
                raise ValueError("unsupported tool wire chunk")
            chunks.append(value)
        if not chunks or not done:
            raise ValueError("incomplete tool wire stream")
        return chunks
    if isinstance(payload, dict):
        return [payload]
    chunks = []
    if isinstance(payload, str):
        for line in payload.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]":
                    try:
                        value = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        chunks.append(value)
    return chunks


def decode_tool_submission(
    response: Any,
    captured: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]], str | None]:
    """Admit one exact observed call; never execute it or trust parsed-only args."""
    from behaviors.types import AssistantMessage, ContentReasoning, ContentText, ContentToolUse

    raw_arguments = None

    def rejected(status: str) -> tuple[None, list[dict[str, Any]], str | None]:
        return None, [{"status": status}], raw_arguments

    if (
        len(captured) != 1
        or captured[0]["status_code"] != 200
        or not captured[0].get("response_utf8_valid")
    ):
        return rejected("unsupported_tool_wire")
    try:
        chunks = payload_chunks(captured[0]["payload"], strict=True)
    except (ValueError, TypeError, RecursionError, OverflowError):
        return rejected("malformed_tool_wire")
    calls: dict[tuple[int, int], dict[str, Any]] = {}
    response_ids, finished = set(), False
    for chunk in chunks:
        if (
            set(chunk)
            - {"id", "object", "created", "model", "system_fingerprint", "choices", "usage"}
            or chunk.get("object") != "chat.completion.chunk"
            or not isinstance(chunk.get("id"), str)
            or not chunk["id"]
            or type(chunk.get("created")) is not int
            or chunk["created"] < 0
            or not isinstance(chunk.get("model"), str)
            or not chunk["model"]
            or not isinstance(chunk.get("choices"), list)
        ):
            return rejected("unsupported_tool_wire")
        response_ids.add(chunk["id"])
        if len(response_ids) != 1 or chunk["id"] != response.id:
            return rejected("tool_sdk_mismatch")
        choices = chunk["choices"]
        if len(choices) > 1:
            return rejected("multiple_tool_choices")
        if not choices and (not finished or not isinstance(chunk.get("usage"), dict)):
            return rejected("unsupported_tool_wire")
        for choice in choices:
            if (
                not isinstance(choice, dict)
                or set(choice) - {"index", "delta", "finish_reason", "logprobs"}
                or type(choice.get("index")) is not int
                or choice["index"] != 0
                or not isinstance(choice.get("delta"), dict)
            ):
                return rejected("unsupported_tool_wire")
            if finished:
                return rejected("duplicate_tool_completion")
            delta = choice["delta"]
            if set(delta) - {
                "role",
                "content",
                "tool_calls",
                "reasoning_content",
                "reasoning",
                "refusal",
            }:
                return rejected("unsupported_tool_wire")
            if delta.get("role") not in (None, "assistant"):
                return rejected("unsupported_tool_wire")
            if delta.get("refusal") not in (None, ""):
                return rejected("refused")
            if delta.get("content") not in (None, ""):
                return rejected("visible_text_with_tool_submission")
            for field in ("reasoning_content", "reasoning"):
                if delta.get(field) is not None and not isinstance(delta[field], str):
                    return rejected("unsupported_tool_wire")
            raw_calls = delta.get("tool_calls")
            if raw_calls is not None and not isinstance(raw_calls, list):
                return rejected("unsupported_tool_wire")
            seen_indices = set()
            for raw_call in raw_calls or []:
                if (
                    not isinstance(raw_call, dict)
                    or set(raw_call) - {"index", "id", "type", "function"}
                    or type(raw_call.get("index")) is not int
                    or raw_call["index"] < 0
                ):
                    return rejected("unsupported_tool_wire")
                index = (choice["index"], raw_call["index"])
                if index in seen_indices:
                    return rejected("duplicate_tool_submission")
                seen_indices.add(index)
                state = calls.setdefault(
                    index, {"id": [], "name": [], "arguments": [], "function": False}
                )
                if len(calls) > 1:
                    return rejected("multiple_tool_submissions")
                if raw_call.get("type") not in (None, "function"):
                    return rejected("unsupported_tool_wire")
                if raw_call.get("type") == "function":
                    state["function"] = True
                call_id = raw_call.get("id")
                if call_id is not None:
                    if not isinstance(call_id, str):
                        return rejected("unsupported_tool_wire")
                    state["id"].append(call_id)
                function = raw_call.get("function")
                if function is None:
                    function = {}
                if not isinstance(function, dict) or set(function) - {"name", "arguments"}:
                    return rejected("unsupported_tool_wire")
                for field in ("name", "arguments"):
                    fragment = function.get(field)
                    if fragment is not None:
                        if not isinstance(fragment, str):
                            return rejected(
                                "malformed_tool_arguments"
                                if field == "arguments"
                                else "unsupported_tool_wire"
                            )
                        state[field].append(fragment)
            finish = choice.get("finish_reason")
            if finish is not None:
                if finish == "content_filter":
                    return rejected("refused")
                if finish not in ("stop", "tool_calls"):
                    return rejected("incomplete_tool_stop")
                finished = True
    if not calls:
        return rejected("missing_tool_submission")
    if set(calls) != {(0, 0)}:
        return rejected("unsupported_tool_wire")
    state = calls[(0, 0)]
    name, call_id = "".join(state["name"]), "".join(state["id"])
    raw_arguments = "".join(state["arguments"])
    if name != SUBMIT_TOOL_NAME:
        return rejected("wrong_tool_submission")
    if not call_id or not state["function"]:
        return rejected("unsupported_tool_wire")
    if not finished or getattr(response.stop_reason, "type", None) != "tool_use":
        return rejected("incomplete_tool_stop")
    if not raw_arguments.strip():
        return rejected("missing_tool_arguments")
    try:
        arguments = strict_json(raw_arguments)
        if not isinstance(arguments, dict):
            return rejected("malformed_tool_arguments")
    except (ValueError, TypeError, RecursionError, OverflowError):
        return rejected("malformed_tool_arguments")
    if len(response.output) != 1 or not isinstance(response.output[0], AssistantMessage):
        return rejected("unsupported_tool_sdk_output")
    submissions = []
    for block in response.output[0].content:
        if isinstance(block, ContentToolUse):
            submissions.append(block)
        elif isinstance(block, ContentText):
            if block.text:
                return rejected("visible_text_with_tool_submission")
        elif not isinstance(block, ContentReasoning):
            return rejected("unsupported_tool_sdk_output")
    if len(submissions) != 1:
        return rejected(
            "missing_tool_submission" if not submissions else "multiple_tool_submissions"
        )
    submission = submissions[0]
    if submission.parse_error is not None or not isinstance(submission.input, dict):
        return rejected("malformed_tool_arguments")
    if submission.name != name or submission.id != call_id:
        return rejected("tool_sdk_mismatch")
    try:
        materialized = strict_encoded(arguments)
        if materialized != strict_encoded(submission.input):
            return rejected("tool_sdk_mismatch")
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return rejected("malformed_tool_arguments")
    return materialized.decode(), [], raw_arguments


def reported_usage(chunks: list[dict[str, Any]]) -> dict[str, int | None]:
    usage = next(
        (chunk["usage"] for chunk in reversed(chunks) if isinstance(chunk.get("usage"), dict)), {}
    )
    prompt = integer(usage.get("prompt_tokens"))
    completion = integer(usage.get("completion_tokens"))
    details = usage.get("prompt_tokens_details") or {}
    cached = integer(usage.get("cache_read_input_tokens"))
    if cached is None and isinstance(details, dict):
        cached = integer(details.get("cached_tokens"))
    completion_details = usage.get("completion_tokens_details") or {}
    reasoning = (
        integer(completion_details.get("reasoning_tokens"))
        if isinstance(completion_details, dict)
        else None
    )
    # Only explicit reported counters or arithmetic on known compatible counters.
    # behaviors' absent-usage default zeros are deliberately not used here.
    return {
        "input_tokens_total": prompt,
        "input_tokens_noncached": prompt - cached
        if prompt is not None and cached is not None and cached <= prompt
        else None,
        "cache_read_input_tokens": cached,
        "cache_write_input_tokens": integer(usage.get("cache_creation_input_tokens")),
        "output_tokens": completion,
        "reasoning_output_tokens": reasoning,
        "total_tokens_reported": integer(usage.get("total_tokens")),
    }


def failure_score(question_ids: list[str], reason: str) -> dict[str, Any]:
    return {
        "correct_items": 0,
        "total_items": len(question_ids),
        "all_correct": False,
        "questions": [{"question_id": q, "status": reason, "correct": False} for q in question_ids],
    }


def score_reply(
    text: str, task_id: str, key: dict[str, Any], sources: dict[str, dict[int, str]]
) -> tuple[str, dict[str, Any]]:
    ids = list(key)
    if not text.strip():
        return "missing_response", failure_score(ids, "missing")
    try:
        answer = json.loads(text, object_pairs_hook=no_duplicate_keys)
    except ValueError:
        refusal = re.search(
            r"\b(i (?:cannot|can't|won't)|unable to (?:answer|comply)|cannot (?:answer|comply))\b",
            text,
            re.I,
        )
        status = "refused" if refusal else "malformed"
        return status, failure_score(ids, status)
    if not isinstance(answer, dict) or set(answer) != {"task_id", "refusal", "answers"}:
        return "malformed", failure_score(ids, "malformed")
    if (
        answer["task_id"] != task_id
        or type(answer["refusal"]) is not bool
        or not isinstance(answer["answers"], list)
    ):
        return "malformed", failure_score(ids, "malformed")
    if answer["refusal"]:
        return "refused", failure_score(ids, "refused")
    by_id = {}
    for item in answer["answers"]:
        if not isinstance(item, dict) or set(item) != {"question_id", "choice", "evidence"}:
            return "malformed", failure_score(ids, "malformed")
        qid = item["question_id"]
        if not isinstance(qid, str) or qid not in key or qid in by_id:
            return "malformed", failure_score(ids, "malformed")
        if (
            item["choice"] not in ("A", "B", "C", "UNKNOWN", "REFUSED")
            or not isinstance(item["evidence"], list)
            or len(item["evidence"]) > 3
        ):
            return "malformed", failure_score(ids, "malformed")
        for citation in item["evidence"]:
            if not isinstance(citation, dict) or set(citation) != {
                "source",
                "line_start",
                "line_end",
                "quote",
            }:
                return "malformed", failure_score(ids, "malformed")
            if (
                not isinstance(citation["source"], str)
                or type(citation["line_start"]) is not int
                or type(citation["line_end"]) is not int
                or not isinstance(citation["quote"], str)
            ):
                return "malformed", failure_score(ids, "malformed")
            if (
                citation["line_start"] < 1
                or citation["line_end"] < 1
                or not 12 <= len(citation["quote"]) <= 400
            ):
                return "malformed", failure_score(ids, "malformed")
        by_id[qid] = item
    if set(by_id) != set(ids):
        score = failure_score(ids, "malformed")
        for item in score["questions"]:
            if item["question_id"] not in by_id:
                item["status"] = "missing"
        return "malformed", score
    results = []
    for qid in ids:
        item, expected = by_id[qid], key[qid]
        grounded = False
        for cite in item["evidence"]:
            first, last = cite["line_start"], cite["line_end"]
            available = sources.get(cite["source"], {})
            if (
                last < first
                or last - first > 12
                or any(i not in available for i in range(first, last + 1))
            ):
                continue
            passage = "\n".join(available[i] for i in range(first, last + 1))
            if cite["quote"] not in passage:
                continue
            relevant = [
                span
                for span in expected["evidence_spans"]
                if span["source"] == cite["source"]
                and first <= span["line_end"]
                and last >= span["line_start"]
            ]
            # Require the quote itself, not merely a broad cited range, to touch
            # a hidden relevant original line. This is grounding, not entailment.
            offset = passage.index(cite["quote"])
            quote_first = first + passage.count("\n", 0, offset)
            quote_last = first + passage.count("\n", 0, offset + len(cite["quote"]))
            if any(
                quote_first <= span["line_end"] and quote_last >= span["line_start"]
                for span in relevant
            ):
                grounded = True
        choice = item["choice"]
        correct = choice == expected["choice"] and grounded
        status = (
            "correct"
            if correct
            else (
                "refused"
                if choice == "REFUSED"
                else "unknown"
                if choice == "UNKNOWN"
                else "incorrect_choice"
                if choice != expected["choice"]
                else "ungrounded"
            )
        )
        results.append({"question_id": qid, "status": status, "correct": correct})
    points = sum(row["correct"] for row in results)
    score = {
        "correct_items": points,
        "total_items": len(ids),
        "all_correct": points == len(ids),
        "questions": results,
    }
    status = (
        "scored"
        if all(row["status"] not in ("refused", "unknown") for row in results)
        else "partial_abstention"
    )
    return status, score


def decode_reference_reply(
    text: str,
    frozen: Any,
    sources: dict[str, dict[int, str]],
    identities: dict[str, str],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Materialize only exact visible bindings; diagnostics never echo reply text."""
    errors = reference_binding_errors(frozen, sources, identities)
    if not text.strip():
        return None, errors + [{"status": "missing_response"}]
    try:
        answer = json.loads(text, object_pairs_hook=no_duplicate_keys)
    except ValueError:
        return None, errors + [{"status": "malformed_json"}]
    if not isinstance(answer, dict) or not isinstance(answer.get("answers"), list):
        return None, errors + [{"status": "malformed_reference_reply"}]
    expected = {
        binding["id"]: binding for binding in freeze_references(sources, identities)["references"]
    }
    actual: dict[str, list[dict[str, Any]]] = {}
    bindings = frozen.get("references", []) if isinstance(frozen, dict) else []
    if isinstance(bindings, list):
        for binding in bindings:
            if isinstance(binding, dict) and isinstance(binding.get("id"), str):
                actual.setdefault(binding["id"], []).append(binding)
    for answer_index, item in enumerate(answer["answers"]):
        position = {"answer_index": answer_index}
        if not isinstance(item, dict) or not isinstance(item.get("evidence"), list):
            errors.append({"status": "malformed_reference_reply", **position})
            continue
        citations, seen = [], set()
        for evidence_index, ref in enumerate(item["evidence"]):
            location = {**position, "evidence_index": evidence_index}
            if not isinstance(ref, str):
                errors.append({"status": "malformed_reference", **location})
                continue
            if ref in seen:
                errors.append({"status": "duplicate_reference", **location})
            seen.add(ref)
            candidates = actual.get(ref, [])
            if not candidates:
                errors.append({"status": "unknown_reference", **location})
            elif len(candidates) != 1:
                errors.append({"status": "ambiguous_reference", **location})
            elif ref not in expected:
                errors.append({"status": "unseen_reference", **location})
            elif encoded(candidates[0]) != encoded(expected[ref]):
                errors.append({"status": "tampered_reference", **location})
            else:
                binding = expected[ref]
                citations.append(
                    {
                        field: binding[field]
                        for field in ("source", "line_start", "line_end", "quote")
                    }
                )
        item["evidence"] = citations
    return (None, errors) if errors else (encoded(answer).decode(), [])


def score_reference_reply(
    text: str,
    task_id: str,
    key: dict[str, Any],
    sources: dict[str, dict[int, str]],
    identities: dict[str, str],
    frozen: Any,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], str | None]:
    materialized, errors = decode_reference_reply(text, frozen, sources, identities)
    if materialized is not None:
        status, score = score_reply(materialized, task_id, key, sources)
    elif errors in ([{"status": "missing_response"}], [{"status": "malformed_json"}]):
        status, score = score_reply(text, task_id, key, sources)
    else:
        status = "reference_decode_error"
        score = failure_score(list(key), status)
    return status, score, errors, materialized


async def observe_response(response: Any) -> None:
    """Observe the existing SDK transport; never construct or send a request."""
    active = _ACTIVE_HTTP.get()
    if active is None:
        return
    response_bytes = await response.aread()
    request_bytes = await response.request.aread()
    response_utf8_valid = request_utf8_valid = True
    try:
        raw = response_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raw = response_bytes.decode("utf-8", errors="replace")
        response_utf8_valid = False
    try:
        request_body = request_bytes.decode("utf-8")
    except UnicodeDecodeError:
        request_body = request_bytes.decode("utf-8", errors="replace")
        request_utf8_valid = False
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = raw
    exchange = {
        "status_code": response.status_code,
        "payload": payload,
        "response_utf8_valid": response_utf8_valid,
        "request_utf8_valid": request_utf8_valid,
        "response_body_sha256": sha256(response_bytes),
        "request_body_sha256": sha256(request_bytes),
        "request_body": request_body,
    }
    if not response_utf8_valid:
        exchange["response_body_hex"] = response_bytes.hex()
    if not request_utf8_valid:
        exchange["request_body_hex"] = request_bytes.hex()
    active.append(exchange)


def quota_decision(payload: dict[str, Any], model: str, now_ms: int) -> tuple[bool, dict[str, Any]]:
    prefix = "gemini" if "/gemini-" in model else "3p"
    windows = {"5h": f"{prefix}-5h", "weekly": f"{prefix}-weekly"}
    public: dict[str, Any] = {"family_bucket": prefix, "threshold_percent": 60, "buckets": []}
    reports = payload.get("reports")
    if not isinstance(reports, list) or not reports:
        return False, {**public, "reason": "unknown_reports"}
    for field in ("accountsWithoutUsage", "disabledCredentials"):
        values = payload.get(field, [])
        if not isinstance(values, list) or any(
            not isinstance(row, dict) or row.get("provider") == "google-antigravity"
            for row in values
        ):
            return False, {**public, "reason": "unavailable_account_usage"}
    found_report = False
    for report in reports:
        if not isinstance(report, dict) or report.get("provider") != "google-antigravity":
            continue
        found_report = True
        fetched = report.get("fetchedAt")
        if (
            type(fetched) not in (int, float)
            or not math.isfinite(fetched)
            or not -30_000 <= now_ms - fetched <= 600_000
        ):
            return False, {**public, "reason": "unknown_or_stale_usage"}
        limits = report.get("limits")
        if not isinstance(limits, list):
            return False, {**public, "reason": "unknown_limits"}
        for window, suffix in windows.items():
            applicable = [
                limit
                for limit in limits
                if isinstance(limit, dict)
                and isinstance(limit.get("id"), str)
                and limit["id"].endswith(":" + suffix)
            ]
            if not applicable:
                return False, {**public, "reason": "missing_applicable_bucket"}
            for limit in applicable:
                amount = limit.get("amount") or {}
                used = amount.get("usedFraction") if isinstance(amount, dict) else None
                if (
                    limit.get("status") != "ok"
                    or type(used) not in (int, float)
                    or not math.isfinite(used)
                    or not 0 <= used <= 1
                ):
                    return False, {**public, "reason": "unknown_applicable_usage"}
                public["buckets"].append(
                    {
                        "window": window,
                        "used_percent": used * 100,
                        "fetched_at_utc": datetime.fromtimestamp(fetched / 1000, UTC).isoformat(),
                    }
                )
                if used > 0.60:
                    return False, {**public, "reason": "applicable_bucket_above_60_percent"}
    return found_report, {
        **public,
        "reason": "admitted" if found_report else "missing_provider_report",
    }


async def inspect_quota(output: Path, attempt_id: str, model: str) -> tuple[bool, dict[str, Any]]:
    process = await asyncio.create_subprocess_exec(
        "omp",
        "usage",
        "--provider",
        "google-antigravity",
        "--json",
        "--redact",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except TimeoutError:
        process.kill()
        stdout, stderr = await process.communicate()
        private_write(
            output / "private" / f"{attempt_id}-quota.json",
            {
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
                "timeout": True,
            },
        )
        return False, {"reason": "usage_probe_timeout"}
    private_write(
        output / "private" / f"{attempt_id}-quota.json",
        {
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "returncode": process.returncode,
        },
    )
    if process.returncode != 0:
        return False, {"reason": "usage_probe_failed"}
    try:
        payload = json.loads(stdout)
        if not isinstance(payload, dict):
            raise ValueError("usage is not an object")
        return quota_decision(payload, model, time.time_ns() // 1_000_000)
    except (ValueError, TypeError, OverflowError):
        return False, {"reason": "unknown_usage_shape"}


def plan_pairs(
    manifest: dict[str, Any],
    tasks: list[str],
    models: list[str],
    repeats: int,
    response_format: str,
    protocols: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    rows, pairs = [], []
    protocol = protocols["formats"][response_format]
    protocol_bytes = encoded(protocol)
    protocol_sha256 = sha256(protocol_bytes)
    wire_sha256 = sha256(strict_encoded(wire_protocol_fields(protocol)))
    variant = protocol["prompt_variant"]
    system_record = manifest["system_prompts"][variant]
    cases_by_task = {case["task_id"]: case for case in manifest["cases"]}
    for task_index, task_id in enumerate(tasks):
        for model_index, model in enumerate(models):
            for repeat in range(repeats):
                arms = ["baseline", "candidate"]
                if (task_index + model_index + repeat) % 2:
                    arms.reverse()
                pair = []
                for order, arm in enumerate(arms, 1):
                    prompt_record = cases_by_task[task_id]["arms"][arm]["prompts"][variant]
                    row = {
                        "attempt_id": f"P{len(rows) + 1:03}",
                        "original_task_id": task_id,
                        "model": model,
                        "repeat": repeat,
                        "arm": arm,
                        "arm_order": order,
                        "pair_id": f"{task_id}/{model}/{manifest['citation_format']}/{response_format}/repeat-{repeat}",
                        "citation_format": manifest["citation_format"],
                        "response_format": response_format,
                        "requested_protocol": protocol["protocol"],
                        "protocol_sha256": protocol_sha256,
                        "answer_schema_sha256": manifest["answer_schema_sha256"],
                        "requested_schema_sha256": manifest["answer_schema_sha256"]
                        if response_format != "text"
                        else None,
                        "protocol_bytes": len(protocol_bytes),
                        "wire_protocol_sha256": wire_sha256,
                        "prompt_variant": variant,
                        "system_prompt_file": system_record["path"],
                        "system_sha256": system_record["sha256"],
                        "system_bytes": system_record["bytes"],
                        "prompt_file": prompt_record["path"],
                        "prompt_sha256": prompt_record["sha256"],
                        "prompt_bytes": prompt_record["bytes"],
                        "reference_bindings_sha256": cases_by_task[task_id][
                            "reference_bindings_sha256"
                        ],
                        "schema_enforcement": protocol["schema_enforcement"],
                        "status": "pending",
                        "attempted": False,
                        "runner_sdk_retry_count": 0,
                        "upstream_retry_count": None,
                        "latency_seconds": None,
                        "stop_reason": None,
                        "actual_model": None,
                        "route_verification": "not_attempted",
                        "request_pin_verification": "not_attempted",
                        "wire_request_sha256": None,
                        "observed_wire_protocol_sha256": None,
                        "observed_schema_sha256": None,
                        "observed_system_sha256": None,
                        "observed_prompt_sha256": None,
                        "provider_usage": empty_usage(),
                        "monetary_cost_usd": None,
                    }
                    rows.append(row)
                    pair.append(row)
                pairs.append(pair)
    return rows, pairs


def summarize(
    rows: list[dict[str, Any]], manifest: dict[str, Any], *, dry_run: bool
) -> dict[str, Any]:
    nested: dict[str, Any] = {}
    for row in rows:
        task = nested.setdefault(row["original_task_id"], {})
        model = task.setdefault(row["model"], {})
        repeat = model.setdefault(str(row["repeat"]), {})
        repeat[row["arm"]] = {
            "attempt_id": row["attempt_id"],
            "status": row["status"],
            "attempted": row["attempted"],
            "score": row.get("score"),
        }
    summaries = []
    paired_deltas = []
    for task_id, models in nested.items():
        for model, repeats in models.items():
            differences = [
                arms["candidate"]["score"]["correct_items"]
                - arms["baseline"]["score"]["correct_items"]
                for arms in repeats.values()
            ]
            paired_deltas.append(
                {
                    "original_task_id": task_id,
                    "model": model,
                    "nested_repeat_count": len(differences),
                    "mean_candidate_minus_baseline_correct_items": None
                    if dry_run
                    else sum(differences) / len(differences),
                }
            )
    for model in dict.fromkeys(row["model"] for row in rows):
        for arm in ("baseline", "candidate"):
            selected = [row for row in rows if row["model"] == model and row["arm"] == arm]
            status_counts: dict[str, int] = {}
            for row in selected:
                status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
            tokens = {}
            for field in USAGE_FIELDS:
                known = [
                    row["provider_usage"][field]
                    for row in selected
                    if row["provider_usage"][field] is not None
                ]
                tokens[field] = {
                    "reported_attempts": len(known),
                    "planned_attempts": len(selected),
                    "reported_subtotal": sum(known) if known else None,
                    "all_counters_known": len(known) == len(selected),
                }
            summaries.append(
                {
                    "model": model,
                    "arm": arm,
                    "planned_cells": len(selected),
                    "attempted_calls": sum(row["attempted"] for row in selected),
                    "status_counts": status_counts,
                    "correct_items": None
                    if dry_run
                    else sum(row["score"]["correct_items"] for row in selected),
                    "total_items_including_failed_cells": len(selected) * 8,
                    "all_correct_cells": None
                    if dry_run
                    else sum(row["score"]["all_correct"] for row in selected),
                    "reported_tokens": tokens,
                }
            )
    return {
        "schema_version": 3,
        "study": manifest["study"],
        "dry_run": dry_run,
        "citation_format": manifest["citation_format"],
        "response_format": rows[0]["response_format"],
        "requested_protocol": rows[0]["requested_protocol"],
        "protocol_sha256": rows[0]["protocol_sha256"],
        "wire_protocol_sha256": rows[0]["wire_protocol_sha256"],
        "requested_schema_sha256": rows[0]["requested_schema_sha256"],
        "answer_schema_sha256": manifest["answer_schema_sha256"],
        "schema_enforcement": rows[0]["schema_enforcement"],
        "finished_at_utc": datetime.now(UTC).isoformat(),
        "planned_calls": len(rows),
        "attempted_calls": sum(row["attempted"] for row in rows),
        "by_model_arm": summaries,
        "nested_original_tasks": nested,
        "paired_task_means": paired_deltas,
        "limits": manifest["limits"],
        "interpretation": "No quality/performance result from dry-run."
        if dry_run
        else "Exploratory source-grounded policy QA only; not coding/implementation quality. Repeats and reused policy questions are correlated. No significance claim or provider identity attestation.",
        "usage_semantics": "OpenAI-compatible reported prompt_tokens includes cache reads; noncached input is derived only when both prompt and cache counts are reported. Output includes provider-reported completion tokens; reasoning is a reported subset. Unknown is null, not zero; subtotals are not complete usage or an invoice.",
    }


async def execute(
    args: argparse.Namespace,
    manifest: dict[str, Any],
    loaded: dict[str, Any],
    rows: list[dict[str, Any]],
    pairs: list[list[dict[str, Any]]],
    output: Path,
) -> None:
    from behaviors.types import (
        AssistantMessage,
        ContentText,
        ModelCallFailure,
        ModelRequest,
        SamplingParams,
        SystemMessage,
        ToolChoiceTool,
        ToolParam,
        UserMessage,
    )
    from harbor_kit import models as kit_models

    apis = {
        model: kit_models.make_api("omp", model)
        for model in dict.fromkeys(row["model"] for row in rows)
    }
    clients = []
    for api in apis.values():
        # Existing OpenAICompatibleAPI injects the kit's SDK client. Observe its
        # shared HTTPX response hooks without replacing transport or credentials.
        if api._client.max_retries != 0:
            raise ValueError("pinned adapter unexpectedly enables retries")
        client = api._client._client
        if not any(client is previous for previous in clients):
            client.event_hooks["response"].append(observe_response)
            clients.append(client)
    blocked_models: set[str] = set()
    quota_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(args.concurrency)
    records = loaded["records"]
    protocol = loaded["protocols"]["formats"][args.response_format]
    variant = protocol["prompt_variant"]
    system = loaded["systems"][variant]
    tools = tuple(ToolParam(**tool) for tool in protocol["tools"])
    tool_choice = (
        ToolChoiceTool(**protocol["tool_choice"]) if protocol["tool_choice"] is not None else None
    )
    helper_hash = next(
        row["sha256"]
        for row in manifest["runtime"]["sources"]
        if row["module"] == "harbor_kit.models"
    )

    async def call(row: dict[str, Any]) -> None:
        task = loaded["cases"][row["original_task_id"]]
        key = task["key"]
        row["score"] = failure_score(list(key), "not_attempted")
        if row["model"] in blocked_models:
            row["status"] = "not_attempted_route_guard"
            return
        try:
            async with quota_lock:
                admitted, quota = await inspect_quota(output, row["attempt_id"], row["model"])
            row["quota"] = quota
            if not admitted:
                row["status"] = "not_attempted_quota_guard"
                blocked_models.add(row["model"])
                return
            if row["model"] in blocked_models:
                row["status"] = "not_attempted_route_guard"
                return
            prompt_record = task["case"]["arms"][row["arm"]]["prompts"][variant]
            prompt = records[prompt_record["path"]].decode()
            request = ModelRequest(
                context=(
                    SystemMessage(content=(ContentText(text=system),)),
                    UserMessage(content=(ContentText(text=prompt),)),
                ),
                tools=tools,
                tool_choice=tool_choice,
                parallel_tool_calls=protocol["parallel_tool_calls"],
                sampling=SamplingParams(
                    max_tokens=args.max_output_tokens,
                    temperature=0.0,
                    extra=protocol["sampling_extra"],
                ),
            )
            request_data = dataclasses.asdict(request)
            row["model_request_sha256"] = sha256(encoded(request_data))
            private_write(output / "private" / f"{row['attempt_id']}-request.json", request_data)
            captured: list[dict[str, Any]] = []
            context_token = _ACTIVE_HTTP.set(captured)
            started = time.perf_counter()
            row["attempted"] = True
            row["status"] = "running"
            response = None
            try:
                response = await asyncio.wait_for(
                    apis[row["model"]].generate(request), timeout=args.timeout_seconds
                )
            except TimeoutError:
                row["status"] = "timeout"
            except Exception as exc:
                row["status"] = "adapter_exception"
                row["exception_type"] = type(exc).__name__
                private_write(
                    output / "private" / f"{row['attempt_id']}-exception.json",
                    {"type": type(exc).__name__, "description": str(exc)},
                )
                blocked_models.add(row["model"])
            finally:
                row["latency_seconds"] = time.perf_counter() - started
                _ACTIVE_HTTP.reset(context_token)
                private_write(output / "private" / f"{row['attempt_id']}-http.json", captured)
            request_matches = pin_observed_request(captured, row, protocol, system, prompt)
            chunks = [
                chunk for exchange in captured for chunk in payload_chunks(exchange["payload"])
            ]
            row["provider_usage"] = reported_usage(chunks)
            row["provider_finish_reasons"] = [
                choice["finish_reason"]
                for chunk in chunks
                for choice in chunk.get("choices", [])
                if isinstance(choice, dict) and isinstance(choice.get("finish_reason"), str)
            ]
            served = [kit_models._served_model(exchange["payload"]) for exchange in captured]
            identities = {value for value in served if value is not None}
            # Also reject later conflicting chunks, not just the helper's first.
            identities.update(
                chunk["model"] for chunk in chunks if isinstance(chunk.get("model"), str)
            )
            row["actual_model"] = next(iter(identities)) if len(identities) == 1 else None
            row["route_provenance"] = {
                "requested_provider": "omp",
                "subscription_provider": "google-antigravity",
                "helper_source_sha256": helper_hash,
                "verifier": "harbor_kit.models._served_model",
                "reported_models": sorted(identities),
                "identity_is_self_reported": True,
            }
            route_matches = (
                len(captured) == 1
                and all(value == row["model"] for value in served)
                and identities == {row["model"]}
            )
            row["route_verification"] = "matched" if route_matches else "unknown_or_mismatched"
            if response is None:
                if row["status"] not in ("timeout", "adapter_exception"):
                    row["status"] = "missing_response"
                if not route_matches:
                    blocked_models.add(row["model"])
                return
            private_write(
                output / "private" / f"{row['attempt_id']}-response.json",
                dataclasses.asdict(response),
            )
            if isinstance(response, ModelCallFailure):
                row["status"] = "adapter_failure"
                row["adapter_failure_category"] = response.category
                row["provider_http_status"] = response.provider_http_status
                # A stream may report usage before returning an adapter failure.
                if not route_matches or response.category in ("authentication", "rate_limited"):
                    blocked_models.add(row["model"])
                return
            row["stop_reason"] = dataclasses.asdict(response.stop_reason)
            if not route_matches:
                row["status"] = "route_identity_failure"
                blocked_models.add(row["model"])
                return
            if not request_matches:
                row["status"] = "request_pin_failure"
                blocked_models.add(row["model"])
                return
            text = "".join(
                block.text
                for message in response.output
                if isinstance(message, AssistantMessage)
                for block in message.content
                if isinstance(block, ContentText)
            )
            private_write(
                output / "private" / f"{row['attempt_id']}-reply.txt", text.encode(), raw=True
            )
            reason = getattr(response.stop_reason, "type", None)
            if reason == "content_filter":
                row["status"] = "refused"
                row["score"] = failure_score(list(key), "refused")
                return
            if args.response_format == "tool-submit":
                text, errors, raw_arguments = decode_tool_submission(response, captured)
                row["tool_decode_errors"] = errors
                if raw_arguments is not None:
                    raw_bytes = raw_arguments.encode()
                    row["tool_arguments_sha256"] = sha256(raw_bytes)
                    private_write(
                        output / "private" / f"{row['attempt_id']}-tool-arguments.txt",
                        raw_bytes,
                        raw=True,
                    )
                if text is None:
                    row["status"] = (
                        "refused" if errors == [{"status": "refused"}] else "tool_decode_error"
                    )
                    row["score"] = failure_score(list(key), row["status"])
                    return
                private_write(
                    output / "private" / f"{row['attempt_id']}-submission.json",
                    text.encode(),
                    raw=True,
                )
            elif reason != "end_turn":
                row["status"] = "incomplete_stop"
                row["score"] = failure_score(list(key), "incomplete_stop")
                return
            if manifest["citation_format"] == "reference":
                status, score, errors, materialized = score_reference_reply(
                    text,
                    row["original_task_id"],
                    key,
                    task["sources"],
                    task["source_sha256"],
                    task["reference_bindings"],
                )
                row["status"], row["score"] = status, score
                row["reference_decode_errors"] = errors
                if materialized is not None:
                    private_write(
                        output / "private" / f"{row['attempt_id']}-materialized.json",
                        materialized.encode(),
                        raw=True,
                    )
            else:
                row["status"], row["score"] = score_reply(
                    text, row["original_task_id"], key, task["sources"]
                )
        except Exception as exc:
            row["status"] = (
                "adapter_exception" if row["attempted"] else "not_attempted_preflight_failure"
            )
            row["exception_type"] = type(exc).__name__
            private_write(
                output / "private" / f"{row['attempt_id']}-exception.json",
                {"type": type(exc).__name__, "description": str(exc)},
            )
            blocked_models.add(row["model"])

    async def pair_calls(pair: list[dict[str, Any]]) -> None:
        async with semaphore:
            for row in pair:
                await call(row)

    try:
        await asyncio.gather(*(pair_calls(pair) for pair in pairs))
    finally:
        for client in clients:
            client.event_hooks["response"].remove(observe_response)
        for api in apis.values():
            await api.aclose()
        for row in rows:
            if row["status"] in ("pending", "running"):
                row["status"] = "interrupted" if row["attempted"] else "not_attempted_interrupted"
                row["score"] = failure_score(
                    list(loaded["cases"][row["original_task_id"]]["key"]), row["status"]
                )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cases", type=Path, default=SCRATCH / "quote-v3-cases-01/manifest.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--run-subscription",
        action="store_true",
        help="Explicitly enable included-subscription calls; no paid route or fallback exists",
    )
    parser.add_argument(
        "--task", action="append", help="Original task identity, repeatable; default all four"
    )
    parser.add_argument(
        "--model",
        action="append",
        help="Exact allowlisted manifest model ID, repeatable; default both",
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--hard-max-calls", type=int, default=16)
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=2)
    parser.add_argument("--timeout-seconds", type=float, default=180)
    parser.add_argument("--max-output-tokens", type=int, default=2400)
    parser.add_argument(
        "--response-format",
        choices=RESPONSE_FORMATS,
        default="text",
        help="Original text, diagnostic native JSON schema, or one terminal typed tool submission; no enforcement claim, execution, retry or fallback",
    )
    args = parser.parse_args()
    if not 1 <= args.repeats <= 4 or not 1 <= args.hard_max_calls <= 64:
        parser.error("repeats must be 1..4 and hard-max-calls 1..64")
    if not math.isfinite(args.timeout_seconds) or not 1 <= args.timeout_seconds <= 300:
        parser.error("timeout-seconds must be finite and 1..300")
    if not 512 <= args.max_output_tokens <= 4096:
        parser.error("max-output-tokens must be 512..4096")
    output = args.output.resolve()
    if not output.is_relative_to(SCRATCH.resolve()):
        parser.error(f"private outputs must remain under {SCRATCH}")
    manifest_path = args.cases.resolve()
    manifest, loaded = load_cases(manifest_path)
    runtime = check_runtime(manifest["runtime"])
    tasks = args.task or list(loaded["cases"])
    models = args.model or manifest["models"]
    if len(set(tasks)) != len(tasks) or not set(tasks) <= set(loaded["cases"]):
        parser.error("tasks must be unique original identities from the manifest")
    if (
        len(set(models)) != len(models)
        or not set(models) <= set(manifest["models"])
        or not set(models) <= ALLOWED_MODELS
    ):
        parser.error("models must be unique exact included-subscription manifest IDs")
    rows, pairs = plan_pairs(
        manifest, tasks, models, args.repeats, args.response_format, loaded["protocols"]
    )
    if len(rows) > args.hard_max_calls:
        parser.error(
            f"planned {len(rows)} calls exceeds hard max {args.hard_max_calls}; no partial sweep is silently substituted"
        )
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    output.chmod(0o700)
    private_write(output / "manifest.json", manifest)
    private_write(output / "runtime-provenance.json", runtime)
    protocol = loaded["protocols"]["formats"][args.response_format]
    variant = protocol["prompt_variant"]
    for relative in [
        manifest["system_prompts"][variant]["path"],
        manifest["answer_schema_file"],
        manifest["questions_file"],
        manifest["request_protocols_file"],
    ]:
        private_write(output / "private" / relative, loaded["records"][relative], raw=True)
    private_write(output / "private/selected-request-protocol.json", protocol)
    private_write(output / "private/selected-wire-protocol.json", wire_protocol_fields(protocol))
    for task_id in tasks:
        case = loaded["cases"][task_id]["case"]
        if case["reference_bindings_file"] is not None:
            relative = case["reference_bindings_file"]
            private_write(output / "private" / relative, loaded["records"][relative], raw=True)
        for arm in case["arms"].values():
            relative = arm["prompts"][variant]["path"]
            private_write(output / "private" / relative, loaded["records"][relative], raw=True)
    for row in rows:
        key = loaded["cases"][row["original_task_id"]]["key"]
        row["score"] = failure_score(
            list(key), "not_run_dry_run" if not args.run_subscription else "not_attempted"
        )
    private_write(
        output / "plan.json",
        {
            "schema_version": 3,
            "study": manifest["study"],
            "factors": {
                "context_compiler": ["baseline", "candidate"],
                "citation_format": manifest["citation_format"],
                "response_format": args.response_format,
            },
            "factor_design": "Citation presentation is fixed at preparation; response protocol and its explicitly rendered prompt variant are selected independently at call time. Only compiler arms are paired within this plan.",
            "requested_protocol": rows[0]["requested_protocol"],
            "protocol_sha256": rows[0]["protocol_sha256"],
            "wire_protocol_sha256": rows[0]["wire_protocol_sha256"],
            "requested_schema_sha256": rows[0]["requested_schema_sha256"],
            "system_sha256": rows[0]["system_sha256"],
            "answer_schema_sha256": manifest["answer_schema_sha256"],
            "request_protocols_sha256": manifest["request_protocols_sha256"],
            "schema_enforcement": rows[0]["schema_enforcement"],
            "manifest_sha256": sha256(manifest_path.read_bytes()),
            "run_subscription": args.run_subscription,
            "hard_max_calls": args.hard_max_calls,
            "concurrency": args.concurrency,
            "timeout_seconds": args.timeout_seconds,
            "max_output_tokens": args.max_output_tokens,
            "runner_sdk_max_retries": 0,
            "upstream_retry_count": None,
            "counterbalance": "First arm alternates by original-task index + model index + nested-repeat index; arms are sequential within a pair.",
            "rows": rows,
        },
    )
    fatal: BaseException | None = None
    try:
        if args.run_subscription:
            asyncio.run(execute(args, manifest, loaded, rows, pairs, output))
        else:
            for row in rows:
                row["status"] = "not_run_dry_run"
    except (Exception, KeyboardInterrupt) as exc:
        fatal = exc
        private_write(
            output / "private/fatal.json", {"type": type(exc).__name__, "description": str(exc)}
        )
    finally:
        for row in rows:
            if row["status"] in ("pending", "running"):
                row["status"] = "interrupted" if row["attempted"] else "not_attempted_interrupted"
                row["score"] = failure_score(
                    list(loaded["cases"][row["original_task_id"]]["key"]), row["status"]
                )
        private_write(
            output / "receipts.json",
            {
                "schema_version": 3,
                "study": manifest["study"],
                "manifest_sha256": sha256(manifest_path.read_bytes()),
                "research_runtime": runtime,
                "rows": rows,
            },
        )
        summary = summarize(rows, manifest, dry_run=not args.run_subscription)
        private_write(output / "summary.json", summary)
    print(
        json.dumps(
            {
                "output": str(output),
                "dry_run": not args.run_subscription,
                "planned_calls": len(rows),
                "attempted_calls": summary["attempted_calls"],
                "summary_file": str(output / "summary.json"),
                "receipts_file": str(output / "receipts.json"),
                "fatal_error_type": type(fatal).__name__ if fatal else None,
            },
            indent=2,
        )
    )
    if fatal is not None:
        return 1
    if args.run_subscription and any(row["status"] != "scored" for row in rows):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
