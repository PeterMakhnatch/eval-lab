"""Analyze the completed, frozen policy-QA generations without calling models.

Stdlib only. The four input cells are deliberately explicit: the original quote /
text main has two nested repeats, and the three follow-ups have one. Run::

    python3 research/explorations/agent-efficiency/analyze_policy.py \
      --probe-root /tmp/agent-efficiency/policy-probe \
      --output /tmp/agent-efficiency/policy-analysis-v1

The output directory must not exist. policy-analysis.json is an aggregate-only,
public-compatible projection; the findings, row ledger, source index and exact
reconciliation discrepancies remain private. Every input byte read is hashed.
No controller is imported or executed, and recorded strict scores are never
recomputed, repaired, or replaced by choice/citation diagnostics. An attempted
call means one runner SDK generate invocation, not a CLI episode or a physical
provider request. Provider-side retries and absent counters remain unknown.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

MODELS = (
    "google-antigravity/gemini-4-argon-low",
    "google-antigravity/claude-opus-5-5",
)
ARMS = ("baseline", "candidate")
USAGE_FIELDS = (
    "input_tokens_total",
    "input_tokens_noncached",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens_reported",
)
# label, run directory, case directory, citation presentation, response carrier, repeats
RUNS = (
    ("quote-text", "main-01", "cases", "quote", "text", 2),
    (
        "reference-text",
        "tool-submit-v3-02/reference-text-main",
        "tool-submit-v3-02/reference-cases",
        "reference",
        "text",
        1,
    ),
    (
        "quote-tool",
        "tool-submit-v3-02/quote-tool-main",
        "tool-submit-v3-02/quote-cases",
        "quote",
        "tool-submit",
        1,
    ),
    (
        "reference-tool",
        "tool-submit-v3-02/reference-tool-main",
        "tool-submit-v3-02/reference-cases",
        "reference",
        "tool-submit",
        1,
    ),
    (
        "reference-compact-text",
        "compact-reference-v2/text-main",
        "compact-reference-v2/cases",
        "reference",
        "text",
        1,
    ),
)
SAFE_STATUSES = frozenset(
    (
        "scored",
        "partial_abstention",
        "correct",
        "incorrect_choice",
        "ungrounded",
        "malformed",
        "missing",
        "missing_response",
        "refused",
        "unknown",
        "not_attempted",
        "pending",
        "running",
        "interrupted",
        "timeout",
        "not_attempted_route_guard",
        "not_attempted_quota_guard",
        "not_attempted_preflight_failure",
        "not_attempted_interrupted",
        "adapter_exception",
        "adapter_failure",
        "route_identity_failure",
        "request_pin_failure",
        "incomplete_stop",
        "tool_decode_error",
        "reference_decode_error",
        "missing_receipt",
        "not_recorded",
        "matched",
        "unknown_or_mismatched",
        "not_attempted",
        "authentication",
        "rate_limited",
        "invalid_request",
        "context_length",
        "server_error",
        "other",
        "unknown_error",
    )
)
TOPICS = {
    "analysis_scope": "analysis_denominator",
    "authority": "instruction_authority",
    "delivery": "delivery_evidence",
    "evidence": "evidence_preservation",
    "execution": "execution_authorization",
    "omitted_rules": "omitted_requirement",
    "scope": "assignment_scope",
    "worker_verification": "worker_verification",
}
LIMITS = (
    "Exploratory fixed-order observations, not a randomized estimate of protocol effect.",
    "Protocol order, cache state, quota/account state, elapsed time, controller version and prompt framing are confounded.",
    "The original cell has two nested repeats; follow-ups have one. Protocol contrasts use per-generation means, not raw totals.",
    "Four original assignment contexts reuse eight policy questions; items and repeats are correlated, not independent capability trials.",
    "Frozen strict acceptance requires the correct choice and a valid quote touching an enumerated hidden source line; this is not full semantic entailment.",
    "Choice-only and citation diagnostics do not change strict acceptance or recover failed cells.",
    "Reported input includes cache reads; noncached input is the recorded compatible difference. Cached and noncached input are not added again to total input.",
    "Unknown cache writes, reasoning counters and physical upstream retries are null, not zero. Reported token subtotals are neither a full invoice nor subscription quota accounting.",
    "One attempted generation is one runner SDK generate invocation, not a CLI episode or physical upstream request. No physical request count is established.",
    "Reported route identity is gateway self-report, not provider attestation. Requested tool/schema controls do not prove upstream schema enforcement.",
    "These policy-QA observations do not establish coding quality, implementation success, or safe universal compiler/protocol adoption.",
)


def encoded(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse_json(data: bytes) -> Any:
    def reject_constant(value: str) -> Any:
        raise ValueError("nonfinite JSON number")

    return json.loads(data, object_pairs_hook=unique_object, parse_constant=reject_constant)


def read_source(path: Path, run: str, role: str, sources: list[dict[str, Any]]) -> bytes:
    data = path.read_bytes()
    sources.append(
        {
            "run": run,
            "role": role,
            "path": str(path.resolve()),
            "sha256": digest(data),
            "bytes": len(data),
        }
    )
    return data


def read_json(path: Path, run: str, role: str, sources: list[dict[str, Any]]) -> Any:
    return parse_json(read_source(path, run, role, sources))


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise ValueError("case artifact escapes case root")
    return path


def compare(
    discrepancies: list[dict[str, Any]],
    run: str,
    scope: str,
    recomputed: Any,
    existing: Any,
    **group: Any,
) -> None:
    if recomputed != existing:
        discrepancies.append(
            {"run": run, "scope": scope, **group, "recomputed": recomputed, "existing": existing}
        )


def safe_counts(values: Any) -> dict[str, int]:
    return dict(
        sorted(Counter(value if value in SAFE_STATUSES else "other" for value in values).items())
    )


def ratio(numerator: int | float, denominator: int | float) -> float | None:
    return numerator / denominator if denominator else None


def usage_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for field in USAGE_FIELDS:
        values = [row.get("provider_usage", {}).get(field) for row in rows]
        if any(value is not None and (type(value) is not int or value < 0) for value in values):
            raise ValueError("unsupported usage counter")
        known = [value for value in values if value is not None]
        result[field] = {
            "reported_generations": len(known),
            "unknown_generations": len(rows) - len(known),
            "reported_subtotal": sum(known) if known else None,
            "all_counters_known": len(known) == len(rows),
            "mean_per_generation_with_counter": ratio(sum(known), len(known)),
        }
    return result


def outcome(row: dict[str, Any]) -> str:
    status = row["status"]
    if status == "missing_receipt":
        return "missing_receipt"
    if not row.get("attempted"):
        return "not_attempted"
    if status == "malformed":
        return "serialization_or_answer_schema_failure"
    if status in ("tool_decode_error", "reference_decode_error"):
        return status
    score = row.get("score")
    if score and status in ("scored", "partial_abstention"):
        if score["all_correct"]:
            return "strict_accepted"
        failures = {item["status"] for item in score["questions"] if not item["correct"]}
        if "incorrect_choice" in failures:
            return "semantic_choice_failure"
        if "ungrounded" in failures:
            return "grounding_failure"
        return "abstention_or_other_item_failure"
    if status in ("refused", "missing_response", "incomplete_stop"):
        return status
    return "transport_or_control_failure"


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [row["score"] for row in rows if row.get("score") is not None]
    correct = sum(score["correct_items"] for score in scores)
    whole = sum(score["all_correct"] for score in scores)
    total = sum(row["expected_items"] for row in rows)
    attempted = sum(row.get("attempted") is True for row in rows)
    usage = usage_stats(rows)
    covered = [
        row
        for row in rows
        if row.get("provider_usage", {}).get("total_tokens_reported") is not None
    ]
    token_denominator = sum(row["provider_usage"]["total_tokens_reported"] for row in covered)
    covered_correct = sum(row["score"]["correct_items"] for row in covered if row.get("score"))
    categories = Counter(outcome(row) for row in rows)
    failed_usage = {}
    for category in sorted(categories):
        if category != "strict_accepted":
            selected = [row for row in rows if outcome(row) == category]
            failed_usage[category] = {
                "generations": len(selected),
                "reported_usage": usage_stats(selected),
            }
    sdk_retries = [row.get("runner_sdk_retry_count", row.get("retry_count")) for row in rows]
    upstream = [row.get("upstream_retry_count") for row in rows]
    return {
        "planned_sdk_generations": sum(row["scheduled"] for row in rows),
        "receipt_rows": sum(row["receipt_present"] for row in rows),
        "attempted_sdk_generations": attempted,
        "attempted_unknown_generations": sum(row.get("attempted") is None for row in rows),
        "original_contexts": len({row["original_task_id"] for row in rows}),
        "nested_repeat_count": len({row["repeat"] for row in rows}),
        "status_counts": safe_counts(row["status"] for row in rows),
        "outcome_category_counts": dict(sorted(categories.items())),
        "adapter_error_categories": safe_counts(
            row["adapter_failure_category"] for row in rows if "adapter_failure_category" in row
        ),
        "tool_decode_error_categories": safe_counts(
            error["status"] for row in rows for error in row.get("tool_decode_errors", [])
        ),
        "reference_decode_error_categories": safe_counts(
            error["status"] for row in rows for error in row.get("reference_decode_errors", [])
        ),
        "item_status_counts": safe_counts(
            item["status"] for score in scores for item in score["questions"]
        ),
        "strict": {
            "correct_items": correct,
            "total_items_including_failed_or_missing": total,
            "whole_cells_accepted": whole,
            "whole_cells_denominator": len(rows),
            "score_unknown_generations": len(rows) - len(scores),
            "item_acceptance_rate": ratio(correct, total),
            "whole_cell_acceptance_rate": ratio(whole, len(rows)),
            "correct_items_per_scheduled_generation": ratio(
                correct, sum(row["scheduled"] for row in rows)
            ),
            "correct_items_per_attempted_generation": ratio(correct, attempted),
        },
        "reported_usage": usage,
        "known_failure_usage": failed_usage,
        "observed_efficiency": {
            "strict_correct_items_with_reported_total": covered_correct,
            "reported_total_token_denominator": token_denominator if covered else None,
            "generations_with_reported_total": len(covered),
            "generations_without_reported_total": len(rows) - len(covered),
            "all_generation_totals_reported": len(covered) == len(rows),
            "strict_correct_items_per_1000_reported_tokens": ratio(
                1000 * covered_correct, token_denominator
            ),
            "coverage": "complete" if len(covered) == len(rows) else "reported_subset_only",
        },
        "retry_observability": {
            "runner_sdk_retry_reported_generations": sum(
                value is not None for value in sdk_retries
            ),
            "runner_sdk_retry_reported_subtotal": sum(
                value for value in sdk_retries if value is not None
            )
            if any(value is not None for value in sdk_retries)
            else None,
            "physical_upstream_retry_known_generations": sum(
                value is not None for value in upstream
            ),
            "physical_upstream_retry_unknown_generations": sum(value is None for value in upstream),
            "physical_upstream_retry_reported_subtotal": sum(
                value for value in upstream if value is not None
            )
            if any(value is not None for value in upstream)
            else None,
            "physical_upstream_request_count": None,
        },
        "route_verification_counts": safe_counts(
            row.get("route_verification", "not_recorded") for row in rows
        ),
        "request_pin_verification_counts": safe_counts(
            row.get("request_pin_verification", "not_recorded") for row in rows
        ),
    }


def summary_projection(rows: list[dict[str, Any]]) -> dict[str, Any]:
    usage = usage_stats(rows)
    return {
        "planned_cells": len(rows),
        "attempted_calls": sum(row["attempted"] for row in rows),
        "status_counts": dict(Counter(row["status"] for row in rows)),
        "correct_items": sum(row["score"]["correct_items"] for row in rows),
        "total_items_including_failed_cells": sum(row["expected_items"] for row in rows),
        "all_correct_cells": sum(row["score"]["all_correct"] for row in rows),
        "reported_tokens": {
            field: {
                "reported_attempts": stats["reported_generations"],
                "planned_attempts": len(rows),
                "reported_subtotal": stats["reported_subtotal"],
                "all_counters_known": stats["all_counters_known"],
            }
            for field, stats in usage.items()
        },
    }


def citation_diagnostic(
    cite: dict[str, Any], sources: dict[str, dict[int, str]], spans: list[dict[str, Any]]
) -> dict[str, Any]:
    """Describe frozen coordinates only; this does not award acceptance."""
    source, first, last, quote = (
        cite.get(name) for name in ("source", "line_start", "line_end", "quote")
    )
    result = {
        "source": source,
        "line_start": first,
        "line_end": last,
        "quote_length": len(quote) if isinstance(quote, str) else None,
        "visible_verbatim": False,
        "quote_touches_oracle_line": False,
    }
    if (
        not isinstance(source, str)
        or type(first) is not int
        or type(last) is not int
        or not isinstance(quote, str)
        or not quote
        or last < first
        or last - first > 12
    ):
        return result
    available = sources.get(source, {})
    if any(line not in available for line in range(first, last + 1)):
        return result
    passage = "\n".join(available[line] for line in range(first, last + 1))
    if quote not in passage:
        return result
    result["visible_verbatim"] = True
    offset = passage.index(quote)
    quote_first = first + passage.count("\n", 0, offset)
    quote_last = first + passage.count("\n", 0, offset + len(quote))
    result["quote_touches_oracle_line"] = any(
        span["source"] == source
        and quote_first <= span["line_end"]
        and quote_last >= span["line_start"]
        for span in spans
    )
    return result


def analyze_run(
    spec: tuple[Any, ...],
    root: Path,
    sources: list[dict[str, Any]],
    discrepancies: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    label, run_relative, case_relative, citation, response, repeats = spec
    run_root, case_root = root / run_relative, root / case_relative
    documents = {
        name: read_json(run_root / f"{name}.json", label, name, sources)
        for name in ("plan", "receipts", "summary", "manifest")
    }
    plan, receipts, summary, manifest = (
        documents[name] for name in ("plan", "receipts", "summary", "manifest")
    )
    case_manifest_bytes = read_source(case_root / "manifest.json", label, "case_manifest", sources)
    recorded_manifest_hash = next(
        source["sha256"]
        for source in sources
        if source["run"] == label and source["role"] == "manifest"
    )
    compare(
        discrepancies,
        label,
        "case_manifest_sha256",
        digest(case_manifest_bytes),
        recorded_manifest_hash,
    )
    artifacts = {record["path"]: record for record in manifest["artifacts"]}

    def case_bytes(relative: str, role: str) -> bytes:
        data = read_source(inside(case_root, relative), label, role, sources)
        compare(
            discrepancies,
            label,
            "frozen_artifact_sha256",
            digest(data),
            artifacts[relative]["sha256"],
            artifact=relative,
        )
        return data

    key = parse_json(case_bytes(manifest["answer_key_file"], "answer_key"))
    excerpts = parse_json(case_bytes(manifest["governing_excerpts_file"], "governing_excerpts"))
    source_lines = {
        name: {
            excerpt["line_start"] + offset: text
            for excerpt in record["excerpts"]
            for offset, text in enumerate(excerpt["text"].splitlines())
        }
        for name, record in excerpts.items()
    }
    assignments = {}
    for case in manifest["cases"]:
        assignment = case_bytes(case["assignment_file"], "assignment").decode()
        assignments[case["task_id"]] = {
            index: line for index, line in enumerate(assignment.splitlines(), 1)
        }
    scorer_source = case_bytes("study-sources/run_policy_pairs.py", "runner_controller").decode()
    case_bytes("study-sources/prepare_policy_cases.py", "case_preparer")
    if "study-sources/policy_protocol.py" in artifacts:
        case_bytes("study-sources/policy_protocol.py", "protocol_controller")
    scorer_node = next(
        node
        for node in ast.parse(scorer_source).body
        if isinstance(node, ast.FunctionDef) and node.name == "score_reply"
    )
    scorer_hash = digest(ast.get_source_segment(scorer_source, scorer_node).encode())
    receipt_rows = receipts["rows"]
    planned = {row["attempt_id"]: row for row in plan["rows"]}
    observed = {row["attempt_id"]: row for row in receipt_rows}
    if len(planned) != len(plan["rows"]) or len(observed) != len(receipt_rows):
        raise ValueError("duplicate generation identity; refusing a lossy join")
    compare(discrepancies, label, "planned_receipt_membership", sorted(observed), sorted(planned))
    compare(discrepancies, label, "expected_schedule_count", len(planned), 16 * repeats)
    compare(
        discrepancies, label, "summary_planned_calls", len(receipt_rows), summary["planned_calls"]
    )
    compare(
        discrepancies,
        label,
        "summary_attempted_calls",
        sum(row["attempted"] for row in receipt_rows),
        summary["attempted_calls"],
    )
    compare(
        discrepancies,
        label,
        "receipt_manifest_sha256",
        receipts["manifest_sha256"],
        digest(encoded(manifest)),
    )
    compare(discrepancies, label, "completed_run", summary["dry_run"], False)
    rows = []
    for identity in list(planned) + [identity for identity in observed if identity not in planned]:
        row = dict(observed[identity] if identity in observed else planned[identity])
        row.update(
            scheduled=identity in planned,
            receipt_present=identity in observed,
            protocol=label,
            expected_items=len(key[row["original_task_id"]]),
        )
        if identity not in observed:
            row.update(status="missing_receipt", attempted=None, score=None, provider_usage={})
        if row["model"] not in MODELS or row["arm"] not in ARMS:
            raise ValueError("unrecognized model or compiler arm")
        if identity in planned and identity in observed:
            for field in ("model", "arm", "original_task_id", "repeat"):
                compare(
                    discrepancies,
                    label,
                    "generation_plan_identity",
                    row[field],
                    planned[identity][field],
                    attempt_id=identity,
                    field=field,
                )
        if manifest["schema_version"] == 3:
            compare(discrepancies, label, "citation_factor", row["citation_format"], citation)
            compare(discrepancies, label, "response_factor", row["response_format"], response)
        score = row.get("score")
        if score:
            compare(
                discrepancies,
                label,
                "score_item_arithmetic",
                score["correct_items"],
                sum(item["correct"] for item in score["questions"]),
                attempt_id=identity,
            )
            compare(
                discrepancies,
                label,
                "score_whole_arithmetic",
                score["all_correct"],
                score["correct_items"] == row["expected_items"],
                attempt_id=identity,
            )
            compare(
                discrepancies,
                label,
                "score_denominator",
                score["total_items"],
                row["expected_items"],
                attempt_id=identity,
            )
        usage = row.get("provider_usage", {})
        input_total, cached, noncached, output, total = (
            usage.get(field)
            for field in (
                "input_tokens_total",
                "cache_read_input_tokens",
                "input_tokens_noncached",
                "output_tokens",
                "total_tokens_reported",
            )
        )
        if input_total is not None and cached is not None and noncached is not None:
            compare(
                discrepancies,
                label,
                "input_usage_arithmetic",
                input_total,
                cached + noncached,
                attempt_id=identity,
            )
        if input_total is not None and output is not None and total is not None:
            compare(
                discrepancies,
                label,
                "total_usage_arithmetic",
                total,
                input_total + output,
                attempt_id=identity,
            )
        rows.append(row)
    for existing in summary["by_model_arm"]:
        selected = [
            row
            for row in rows
            if row["receipt_present"]
            and row["model"] == existing["model"]
            and row["arm"] == existing["arm"]
        ]
        for field, value in summary_projection(selected).items():
            compare(
                discrepancies,
                label,
                "summary_" + field,
                value,
                existing[field],
                model=existing["model"],
                arm=existing["arm"],
            )

    original_choices = {}
    malformed = defaultdict(list)
    if label == "quote-text":
        diagnostics = read_json(
            run_root / "choice-only-diagnostics.json", label, "existing_choice_diagnostic", sources
        )
        compare(
            discrepancies,
            label,
            "choice_receipt_provenance",
            diagnostics["strict_receipts_sha256"],
            digest((run_root / "receipts.json").read_bytes()),
        )
        compare(
            discrepancies,
            label,
            "choice_key_provenance",
            diagnostics["hidden_key_sha256"],
            digest((case_root / manifest["answer_key_file"]).read_bytes()),
        )
        original_choices = {row["attempt_id"]: row for row in diagnostics["rows"]}
        malformed_rows = read_json(
            run_root / "malformed-diagnostics.json", label, "existing_malformed_diagnostic", sources
        )
        for item in malformed_rows:
            malformed[item["attempt_id"]].extend(item["reasons"])
    for row in rows:
        identity = row["attempt_id"]
        diagnostic = {
            "choice_correct_items": None,
            "all_choices_correct": None,
            "parse": "not_available",
            "format_causes": [],
            "grounding_failures": [],
        }
        if label == "quote-text":
            existing = original_choices.get(identity)
            if existing:
                if row.get("score") is not None:
                    compare(
                        discrepancies,
                        label,
                        "choice_strict_score_unchanged",
                        existing["strict_correct_items_UNCHANGED"],
                        row["score"]["correct_items"],
                        attempt_id=identity,
                    )
                diagnostic.update(
                    choice_correct_items=existing["diagnostic_choice_correct_items"],
                    all_choices_correct=existing["diagnostic_choice_correct_items"]
                    == row["expected_items"],
                    parse=(
                        "outer_fence_removed_diagnostic_only"
                        if existing["diagnostic_parse"] != "exact_json"
                        else "exact_json"
                    ),
                )
            for reason in malformed[identity]:
                if reason == "fenced_json":
                    diagnostic["format_causes"].append("bare_json_fence_violation")
                elif reason.startswith("citation_quote_length:"):
                    diagnostic["format_causes"].append("citation_quote_length_violation")
                else:
                    diagnostic["format_causes"].append("other_format_violation")
        else:
            if not identity.startswith("P") or not identity[1:].isdigit():
                raise ValueError("unexpected private answer identity")
            suffixes = (
                ["materialized.json", "submission.json", "reply.txt"]
                if citation == "reference"
                else ["submission.json", "reply.txt"]
            )
            answer_path = next(
                (
                    run_root / "private" / f"{identity}-{suffix}"
                    for suffix in suffixes
                    if (run_root / "private" / f"{identity}-{suffix}").is_file()
                ),
                None,
            )
            answer = None
            if answer_path is not None:
                data = read_source(answer_path, label, "structured_answer_diagnostic", sources)
                try:
                    answer = parse_json(data)
                    diagnostic["parse"] = "exact_json"
                except (ValueError, UnicodeError):
                    diagnostic["parse"] = "invalid_json"
                    diagnostic["format_causes"].append("invalid_json_serialization")
            if isinstance(answer, dict) and isinstance(answer.get("answers"), list):
                answer_items = defaultdict(list)
                for item in answer["answers"]:
                    if isinstance(item, dict) and isinstance(item.get("question_id"), str):
                        answer_items[item["question_id"]].append(item)
                task_key = key[row["original_task_id"]]
                diagnostic["choice_correct_items"] = sum(
                    len(answer_items[qid]) == 1
                    and answer_items[qid][0].get("choice") == expected["choice"]
                    for qid, expected in task_key.items()
                )
                diagnostic["all_choices_correct"] = (
                    diagnostic["choice_correct_items"] == row["expected_items"]
                    and len(answer["answers"]) == row["expected_items"]
                )
                for items in answer_items.values():
                    for item in items:
                        for cite in item.get("evidence", []):
                            if (
                                isinstance(cite, dict)
                                and isinstance(cite.get("quote"), str)
                                and not 12 <= len(cite["quote"]) <= 400
                            ):
                                diagnostic["format_causes"].append(
                                    "citation_quote_length_violation"
                                )
                visible = {**source_lines, "ASSIGNMENT": assignments[row["original_task_id"]]}
                for item in (row.get("score") or {}).get("questions", []):
                    if item["status"] != "ungrounded":
                        continue
                    qid = item["question_id"]
                    selected = answer_items[qid]
                    citations = [
                        citation_diagnostic(cite, visible, task_key[qid]["evidence_spans"])
                        for entry in selected
                        for cite in entry.get("evidence", [])
                        if isinstance(cite, dict)
                    ]
                    diagnostic["grounding_failures"].append(
                        {
                            "question_id": qid,
                            "oracle_spans": task_key[qid]["evidence_spans"],
                            "citations": citations,
                            "visible_verbatim_but_outside_oracle": any(
                                cite["visible_verbatim"] for cite in citations
                            )
                            and not any(cite["quote_touches_oracle_line"] for cite in citations),
                        }
                    )
        row["diagnostic_only"] = diagnostic
    if label == "quote-text":
        for existing in diagnostics["groups"]:
            selected = [
                row
                for row in rows
                if row["model"] == existing["model"] and row["arm"] == existing["arm"]
            ]
            compare(
                discrepancies,
                label,
                "choice_group_arithmetic",
                sum(row["diagnostic_only"]["choice_correct_items"] or 0 for row in selected),
                existing["diagnostic_choice_correct_items"],
                model=existing["model"],
                arm=existing["arm"],
            )

    groups = []
    for model in MODELS:
        for arm in ARMS:
            selected = [row for row in rows if row["model"] == model and row["arm"] == arm]
            result = {
                "model": model,
                "compiler_arm": arm,
                "protocol": label,
                "citation_presentation": citation,
                "response_carrier": response,
                **aggregate(selected),
            }
            result["diagnostics_only"] = diagnostic_aggregate(selected)
            groups.append(result)
    source_hashes = {}
    for role in sorted({source["role"] for source in sources if source["run"] == label}):
        hashes = [
            source["sha256"]
            for source in sources
            if source["run"] == label and source["role"] == role
        ]
        source_hashes[role] = (
            {"sha256": hashes[0], "files": 1}
            if len(hashes) == 1
            else {
                "sorted_sha256_list_sha256": digest(encoded(sorted(hashes))),
                "files": len(hashes),
            }
        )
    return {
        "protocol": label,
        "citation_presentation": citation,
        "response_carrier": response,
        "recorded_schema_version": receipts["schema_version"],
        "expected_nested_repeats": repeats,
        "strict_scorer_function_sha256": scorer_hash,
        "source_hashes": source_hashes,
        "totals": aggregate(rows),
        "diagnostics_only": diagnostic_aggregate(rows),
        "by_model_compiler_arm": groups,
    }, rows


def diagnostic_aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    diagnostics = [row["diagnostic_only"] for row in rows]
    known = [item for item in diagnostics if item["choice_correct_items"] is not None]
    grounding = [failure for item in diagnostics for failure in item["grounding_failures"]]
    return {
        "acceptance_unchanged": True,
        "choice_only": {
            "generations_with_diagnostic": len(known),
            "generations_without_diagnostic": len(rows) - len(known),
            "correct_choice_items": sum(item["choice_correct_items"] for item in known)
            if known
            else None,
            "all_choices_correct_generations": sum(item["all_choices_correct"] for item in known)
            if known
            else None,
            "citation_and_format_not_validated_by_choice_diagnostic": True,
            "parse_counts": dict(sorted(Counter(item["parse"] for item in diagnostics).items())),
        },
        "format_cause_generations": {
            cause: sum(cause in item["format_causes"] for item in diagnostics)
            for cause in sorted({cause for item in diagnostics for cause in item["format_causes"]})
        },
        "format_cause_occurrences": dict(
            sorted(
                Counter(cause for item in diagnostics for cause in item["format_causes"]).items()
            )
        ),
        "grounding_failure_topics": dict(
            sorted(
                Counter(
                    TOPICS.get(item["question_id"], "other")
                    for row in rows
                    for item in (row.get("score") or {}).get("questions", [])
                    if item["status"] == "ungrounded"
                ).items()
            )
        ),
        "citation_coordinate_diagnostics": {
            "strict_ungrounded_items_inspected": len(grounding),
            "visible_verbatim_citation_but_no_oracle_line_overlap": sum(
                item["visible_verbatim_but_outside_oracle"] for item in grounding
            ),
            "original_response_reasoning_not_read": True,
            "original_quote_text_citation_coordinates_not_inspected": any(
                row["protocol"] == "quote-text" for row in rows
            ),
        },
    }


def comparisons(runs: list[dict[str, Any]]) -> tuple[list[Any], list[Any]]:
    compiler, protocols = [], []
    for run in runs:
        for model in MODELS:
            baseline, candidate = [
                next(
                    group
                    for group in run["by_model_compiler_arm"]
                    if group["model"] == model and group["compiler_arm"] == arm
                )
                for arm in ARMS
            ]
            compiler.append(
                {"protocol": run["protocol"], "model": model, **contrast(baseline, candidate)}
            )
    for index, earlier in enumerate(runs):
        for later in runs[index + 1 :]:
            for model in MODELS:
                for arm in ARMS:
                    first, second = [
                        next(
                            group
                            for group in run["by_model_compiler_arm"]
                            if group["model"] == model and group["compiler_arm"] == arm
                        )
                        for run in (earlier, later)
                    ]
                    protocols.append(
                        {
                            "earlier_protocol": earlier["protocol"],
                            "later_protocol": later["protocol"],
                            "model": model,
                            "compiler_arm": arm,
                            "exploratory_not_causal": True,
                            **contrast(first, second),
                        }
                    )
    return compiler, protocols


def contrast(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    tokens = {}
    for field in USAGE_FIELDS:
        left, right = [
            group["reported_usage"][field]["mean_per_generation_with_counter"]
            for group in (first, second)
        ]
        tokens[field] = {
            "first_mean_per_generation_with_counter": left,
            "second_mean_per_generation_with_counter": right,
            "second_minus_first": right - left if left is not None and right is not None else None,
            "relative_change": (right / left - 1) if left and right is not None else None,
        }
    return {
        "first_attempted_sdk_generations": first["attempted_sdk_generations"],
        "second_attempted_sdk_generations": second["attempted_sdk_generations"],
        "strict_correct_items_per_generation_delta": second["strict"][
            "correct_items_per_scheduled_generation"
        ]
        - first["strict"]["correct_items_per_scheduled_generation"],
        "strict_item_acceptance_rate_delta": second["strict"]["item_acceptance_rate"]
        - first["strict"]["item_acceptance_rate"],
        "whole_cell_acceptance_rate_delta": second["strict"]["whole_cell_acceptance_rate"]
        - first["strict"]["whole_cell_acceptance_rate"],
        "reported_tokens_normalized_per_generation": tokens,
    }


def findings(
    report: dict[str, Any], private_rows: list[dict[str, Any]], discrepancies: list[dict[str, Any]]
) -> str:
    totals = report["pooled_descriptive_totals"]
    lines = [
        "# Private policy analysis",
        "",
        f"Observed {totals['planned_sdk_generations']} scheduled / {totals['attempted_sdk_generations']} attempted SDK generations; physical provider requests/retries are unknown.",
        f"Strict acceptance: {totals['strict']['correct_items']}/{totals['strict']['total_items_including_failed_or_missing']} items and {totals['strict']['whole_cells_accepted']}/{totals['strict']['whole_cells_denominator']} whole generations. This pooled count is descriptive only; contexts/questions repeat.",
        f"Recorded summaries reconciled with {len(discrepancies)} discrepancies; exact discrepancies are retained, not repaired. All source bytes read are SHA-256 indexed privately; aggregate source digests are in policy-analysis.json.",
        "",
        "## Recorded results",
        "",
        "Reported input includes cached input. Unknown cache-write/reasoning counters are not zeros. Total tokens include failed generations.",
        "",
        "| Protocol | Model | Arm | SDK generations | Strict items | Whole | Input | Cached | Noncached | Output | Total | Strict items / 1k tokens |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in report["runs"]:
        for group in run["by_model_compiler_arm"]:

            def token(field, usage=group["reported_usage"]):
                return usage[field]["reported_subtotal"]

            strict = group["strict"]
            model = "Gemini" if "gemini" in group["model"] else "Opus"
            lines.append(
                f"| {run['protocol']} | {model} | {group['compiler_arm']} | {group['attempted_sdk_generations']} | {strict['correct_items']}/{strict['total_items_including_failed_or_missing']} | {strict['whole_cells_accepted']}/{strict['whole_cells_denominator']} | {token('input_tokens_total')} | {token('cache_read_input_tokens')} | {token('input_tokens_noncached')} | {token('output_tokens')} | {token('total_tokens_reported')} | {group['observed_efficiency']['strict_correct_items_per_1000_reported_tokens']:.6f} |"
            )
    lines += ["", "## Failure decomposition (diagnostic only)", ""]
    for run in report["runs"]:
        diag = run["diagnostics_only"]
        mean_total = run["totals"]["reported_usage"]["total_tokens_reported"][
            "mean_per_generation_with_counter"
        ]
        mean_output = run["totals"]["reported_usage"]["output_tokens"][
            "mean_per_generation_with_counter"
        ]
        lines.append(
            f"- {run['protocol']}: statuses {run['totals']['status_counts']}; item failures {run['totals']['item_status_counts']}; choice-only {diag['choice_only']['correct_choice_items']} correct; format causes {diag['format_cause_generations']}. Mean total/output tokens per generation: {mean_total:.4f}/{mean_output:.4f}."
        )
    lines += [
        "",
        "## Narrow grounding oracle",
        "",
        "The frozen key for omitted_rules accepts only docs/NOW.md:144. The quote itself must touch an enumerated line; correct choices and other valid authoritative citations do not suffice. Delivery similarly accepts only agents/CHECKS.md:4 or :155. This is a narrow line-overlap oracle, not a full entailment check.",
        "The analyzer inspected only final structured submissions/materialized citations for follow-ups, not response reasoning. Examples below omit quote text. Some alternative citations appear relevant to the policy claim [INFERENCE], but strict failures remain unchanged.",
    ]
    examples = []
    for row in private_rows:
        for failure in row["diagnostic_only"]["grounding_failures"]:
            if failure["question_id"] == "omitted_rules" and len(examples) < 2:
                cites = ", ".join(
                    f"{cite['source']}:{cite['line_start']}-{cite['line_end']} (verbatim={cite['visible_verbatim']})"
                    for cite in failure["citations"]
                )
                examples.append(
                    f"- {row['protocol']} {row['attempt_id']} {row['model']} {row['arm']}: omitted_rules is strict ungrounded; citations {cites}; required oracle anchor docs/NOW.md:144."
                )
    lines += examples
    inspected = sum(
        run["diagnostics_only"]["citation_coordinate_diagnostics"][
            "strict_ungrounded_items_inspected"
        ]
        for run in report["runs"]
    )
    outside = sum(
        run["diagnostics_only"]["citation_coordinate_diagnostics"][
            "visible_verbatim_citation_but_no_oracle_line_overlap"
        ]
        for run in report["runs"]
    )
    lines.append(
        f"Follow-up coordinate inspection covered {inspected} strict-ungrounded items; {outside} had visible verbatim citations but no hidden-anchor overlap. Original citation coordinates were not inspected; original failure labels and preexisting choice/format diagnostics were consumed unchanged."
    )
    lines += [
        "",
        "## Candidate decisions",
        "",
        "- Compiler candidate: retain for controlled, quality-gated investigation, not universal adoption. Original Gemini is tied at 40/64 items per arm; original Opus drops 42 to 37/64 despite lower tokens. Reference/text Opus also drops 32 to 31/32. Other follow-up cells favor the candidate, but the direction is not uniformly quality-preserving.",
        "- Reference presentation: promising output/serialization-efficiency candidate. Reference cells have zero malformed/decode failures and much smaller output means, but strict grounding failures persist and input overhead/cache changes must remain visible. Do not promote choice-only 100% to strict quality.",
        "- Quote/tool: reject as a guaranteed schema repair. One terminal submission still violates the frozen quote-length bound despite zero tool-decode errors; the original 11 malformed generations comprise six quote-length schema failures and five fenced-JSON serialization failures.",
        "- Terminal tool versus text: no causal adoption claim. Compare normalized means in the JSON; tools alter framing, were run later, and upstream schema enforcement is unknown. Reference/tool is not uniformly cheaper than reference/text.",
        "- Coordinates-free compact labels remain experimental, not an adoption default: exact host-side provenance binding does not establish model-side retrieval-quality parity. Compare their strict scores as well as their smaller prompts.",
        "- Reject broad coding-performance, no-quality-loss, invoice-cost, or provider-request-count claims from this policy probe.",
        "",
        "## Limits",
        "",
    ]
    lines.extend(f"- {limit}" for limit in LIMITS)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--probe-root", type=Path, default=Path("/tmp/agent-efficiency/policy-probe")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--private-root",
        type=Path,
        default=Path("/tmp/agent-efficiency"),
        help="Existing owner-private evidence root; outputs cannot enter a Git checkout",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    private_root = args.private_root.resolve()
    if not private_root.is_dir() or private_root.stat().st_mode & 0o077:
        raise ValueError("--private-root must be an existing owner-private directory")
    if any((parent / ".git").exists() for parent in (private_root, *private_root.parents)):
        raise ValueError("private analysis outputs cannot enter a Git checkout")
    if not output.is_relative_to(private_root):
        raise ValueError("analysis outputs must remain under --private-root")
    if output.exists():
        raise FileExistsError("refusing to overwrite an existing analysis directory")
    sources: list[dict[str, Any]] = []
    discrepancies: list[dict[str, Any]] = []
    runs, rows = [], []
    for spec in RUNS:
        result, run_rows = analyze_run(spec, args.probe_root.resolve(), sources, discrepancies)
        runs.append(result)
        rows.extend(run_rows)
    compiler, protocols = comparisons(runs)
    public_discrepancies = [
        {key: item[key] for key in ("run", "scope", "model", "arm") if key in item}
        for item in discrepancies
    ]
    report = {
        "schema_version": 1,
        "analysis": "frozen-policy-generation-efficiency",
        "analyzer_source_sha256": digest(Path(__file__).read_bytes()),
        "generation_unit": "one runner SDK generate invocation",
        "protocol_sequence": [run["protocol"] for run in runs],
        "strict_acceptance_regraded": False,
        "same_strict_scorer_function_across_cells": len(
            {run["strict_scorer_function_sha256"] for run in runs}
        )
        == 1,
        "runs": runs,
        "pooled_descriptive_totals": aggregate(rows),
        "compiler_contrasts_candidate_minus_baseline": compiler,
        "protocol_contrasts_later_minus_earlier_per_generation": protocols,
        "reconciliation": {
            "discrepancy_count": len(discrepancies),
            "discrepancies": public_discrepancies,
            "existing_summaries_overwritten": False,
        },
        "limits": list(LIMITS),
    }
    public_bytes = encoded(report)
    # Only controlled aggregate labels/numbers above are exported. Check the
    # known private identities/paths as well; never serialize a source object.
    private_values = {
        row[field] for row in rows for field in ("attempt_id", "original_task_id", "pair_id")
    }
    private_values.update(source["path"] for source in sources)
    if any(value.encode() in public_bytes for value in private_values):
        raise ValueError("private identifier or path reached aggregate projection")
    note = findings(report, rows, discrepancies)
    output.mkdir(parents=True, mode=0o700)
    outputs = {
        "policy-analysis.json": public_bytes,
        "rows.private.json": encoded(rows),
        "source-index.private.json": encoded(sources),
        "discrepancies.private.json": encoded(discrepancies),
        "findings.md": note.encode(),
    }
    for name, data in outputs.items():
        descriptor = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    print(
        json.dumps(
            {
                "scheduled_sdk_generations": report["pooled_descriptive_totals"][
                    "planned_sdk_generations"
                ],
                "attempted_sdk_generations": report["pooled_descriptive_totals"][
                    "attempted_sdk_generations"
                ],
                "strict_correct_items": report["pooled_descriptive_totals"]["strict"][
                    "correct_items"
                ],
                "reported_total_tokens": report["pooled_descriptive_totals"]["reported_usage"][
                    "total_tokens_reported"
                ]["reported_subtotal"],
                "reconciliation_discrepancies": len(discrepancies),
                "output_files": list(outputs),
            },
            sort_keys=True,
        )
    )
    return 1 if discrepancies else 0


if __name__ == "__main__":
    raise SystemExit(main())
