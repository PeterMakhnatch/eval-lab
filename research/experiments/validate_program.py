#!/usr/bin/env python3
"""Validate the PROGRAM ledger and evidence-facing claims without Harbor I/O."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

STATUSES = {
    "idea",
    "designed",
    "proposed",
    "waiting",
    "approved",
    "running",
    "completed",
    "analyzed",
    "stopped",
    "superseded",
}
ACTIVE_PROPOSAL_STATUSES = {
    "idea",
    "designed",
    "proposed",
    "waiting",
    "approved",
    "running",
}
PROVENANCE_STATUSES = {
    "reviewed_primary",
    "mixed",
    "inherited_unresolved",
    "design_only",
}
TOP_LEVEL_KEYS = {"schema_version", "updated_at", "title", "experiments"}
REQUIRED = {
    "id",
    "research_question",
    "hypothesis",
    "primary_variable",
    "fixed_elicitation",
    "task_cohort",
    "agent",
    "model",
    "profile",
    "k",
    "power_rationale",
    "status",
    "references",
    "evidence_provenance",
    "blocker",
    "next_action",
    "predecessor",
    "decision_rule",
    "stopping_condition",
    "notes",
}
OPTIONAL = {"n_tasks", "representative_attempts"}
STRING_FIELDS = {
    "id",
    "research_question",
    "hypothesis",
    "primary_variable",
    "fixed_elicitation",
    "task_cohort",
    "agent",
    "profile",
    "power_rationale",
    "status",
    "blocker",
    "next_action",
    "predecessor",
    "stopping_condition",
    "notes",
}
REF_KEYS = {"spec", "queue", "jobs", "analysis", "cards"}
DECISION_KEYS = {"declared_k", "representative_attempts", "rule"}
PROVENANCE_KEYS = {"status", "basis"}
HIDDEN_INPUT_TERMS = ("tests/test_outputs.py", "official tests", "official tests/")
UNSUPPORTED_CAUSAL_PATTERNS = (
    re.compile(r"first failed .*vector class.*(?:iframe|srcdoc)", re.IGNORECASE),
    re.compile(r"filter misses .*iframe.*srcdoc", re.IGNORECASE),
    re.compile(r"same [`']?srcdoc[`']? (?:first )?vectors?", re.IGNORECASE),
)
FAILURE_TEXT_PATTERNS = (
    re.compile(r"\bscript failed\b", re.IGNORECASE),
    re.compile(r"\bcommand failed\b", re.IGNORECASE),
    re.compile(r"traceback \(most recent call last\).*?assertionerror", re.IGNORECASE | re.DOTALL),
)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _load_json(path: Path, errors: list[str], label: str) -> Any | None:
    try:
        return json.loads(path.read_text())
    except OSError as exc:
        errors.append(f"{label} cannot be read: {exc}")
    except json.JSONDecodeError as exc:
        errors.append(f"{label} is not valid JSON: {exc.msg} at line {exc.lineno}")
    return None


def _validate_local_reference(
    value: object,
    *,
    prefix: str,
    repo_root: Path,
    errors: list[str],
) -> Path | None:
    if not _nonempty_string(value):
        errors.append(f"{prefix} must be a non-empty repository-relative path")
        return None
    reference = Path(value)
    if reference.is_absolute() or ".." in reference.parts:
        errors.append(f"{prefix} must stay inside the repository: {value!r}")
        return None
    candidate = (repo_root / reference).resolve()
    root = repo_root.resolve()
    if not candidate.is_relative_to(root):
        errors.append(f"{prefix} resolves outside the repository: {value!r}")
        return None
    if not candidate.exists():
        errors.append(f"{prefix} does not exist: {value}")
        return None
    return candidate


def _lint_analysis_claims(path: Path, *, prefix: str, errors: list[str]) -> None:
    if path.suffix.lower() != ".md":
        return
    try:
        text = path.read_text()
    except OSError:
        return
    for pattern in UNSUPPORTED_CAUSAL_PATTERNS:
        if pattern.search(text):
            errors.append(
                f"{prefix} names iframe/srcdoc as a culprit although verifier output only "
                "identifies a failed batch"
            )
            break


def _referenced_spec_attempts(paths: list[Path], errors: list[str]) -> set[int]:
    attempts: set[int] = set()
    json_paths: list[Path] = []
    for path in paths:
        if path.is_dir():
            json_paths.extend(sorted(path.glob("*.json")))
        elif path.suffix.lower() == ".json":
            json_paths.append(path)
    for path in json_paths:
        payload = _load_json(path, errors, str(path))
        if not isinstance(payload, dict) or "attempts" not in payload:
            continue
        value = payload["attempts"]
        if not _is_int(value) or value <= 0:
            errors.append(f"{path} attempts must be a positive integer")
            continue
        attempts.add(value)
    return attempts


def _validate_references(
    item: dict[str, Any],
    *,
    prefix: str,
    repo_root: Path,
    errors: list[str],
) -> tuple[dict[str, list[Path]], set[int]]:
    resolved = {key: [] for key in REF_KEYS}
    refs = item.get("references")
    if not isinstance(refs, dict):
        errors.append(f"{prefix}.references must be an object")
        return resolved, set()
    unknown = sorted(set(refs) - REF_KEYS)
    if unknown:
        errors.append(f"{prefix}.references has unknown fields {unknown}")
    missing = sorted(REF_KEYS - set(refs))
    if missing:
        errors.append(f"{prefix}.references missing {missing}")
    for key in sorted(REF_KEYS):
        values = refs.get(key)
        if not isinstance(values, list):
            errors.append(f"{prefix}.references.{key} must be a list")
            continue
        for index, value in enumerate(values):
            ref_prefix = f"{prefix}.references.{key}[{index}]"
            path = _validate_local_reference(
                value,
                prefix=ref_prefix,
                repo_root=repo_root,
                errors=errors,
            )
            if path is not None:
                resolved[key].append(path)
                if key == "analysis":
                    _lint_analysis_claims(path, prefix=ref_prefix, errors=errors)
    return resolved, _referenced_spec_attempts(resolved["spec"], errors)


def _validate_decision_rule(
    item: dict[str, Any],
    *,
    prefix: str,
    spec_attempts: set[int],
    errors: list[str],
) -> None:
    decision = item.get("decision_rule")
    if not isinstance(decision, dict):
        errors.append(f"{prefix}.decision_rule must be an object")
        return
    unknown = sorted(set(decision) - DECISION_KEYS)
    if unknown:
        errors.append(f"{prefix}.decision_rule has unknown fields {unknown}")
    missing = sorted(DECISION_KEYS - set(decision))
    if missing:
        errors.append(f"{prefix}.decision_rule missing {missing}")
    declared_k = decision.get("declared_k")
    if not _is_int(declared_k) or declared_k <= 0:
        errors.append(f"{prefix}.decision_rule.declared_k must be a positive integer")
    elif declared_k != item.get("k"):
        errors.append(f"{prefix}.decision_rule.declared_k must equal k")
    representative = decision.get("representative_attempts")
    item_representative = item.get("representative_attempts")
    if representative is not None and (not _is_int(representative) or representative <= 0):
        errors.append(
            f"{prefix}.decision_rule.representative_attempts must be null or a positive integer"
        )
    if representative != item_representative:
        errors.append(
            f"{prefix}.decision_rule.representative_attempts must match "
            "representative_attempts"
        )
    if representative is not None and representative == item.get("k"):
        errors.append(f"{prefix}.representative_attempts must differ from intended k")
    if not _nonempty_string(decision.get("rule")):
        errors.append(f"{prefix}.decision_rule.rule must be a non-empty string")
    expected_attempt = representative if representative is not None else item.get("k")
    if spec_attempts and expected_attempt not in spec_attempts:
        errors.append(
            f"{prefix} references spec attempts {sorted(spec_attempts)}, which do not include "
            f"the decision attempt count {expected_attempt!r}"
        )


def _validate_provenance(
    item: dict[str, Any],
    *,
    prefix: str,
    resolved: dict[str, list[Path]],
    errors: list[str],
) -> None:
    provenance = item.get("evidence_provenance")
    if not isinstance(provenance, dict):
        errors.append(f"{prefix}.evidence_provenance must be an object")
        return
    unknown = sorted(set(provenance) - PROVENANCE_KEYS)
    if unknown:
        errors.append(f"{prefix}.evidence_provenance has unknown fields {unknown}")
    missing = sorted(PROVENANCE_KEYS - set(provenance))
    if missing:
        errors.append(f"{prefix}.evidence_provenance missing {missing}")
    status = provenance.get("status")
    if status not in PROVENANCE_STATUSES:
        errors.append(f"{prefix}.evidence_provenance.status {status!r} not in enum")
    if not _nonempty_string(provenance.get("basis")):
        errors.append(f"{prefix}.evidence_provenance.basis must be a non-empty string")
    analysis_paths = resolved["analysis"]
    if status == "reviewed_primary":
        if not analysis_paths:
            errors.append(f"{prefix} reviewed_primary evidence needs a retained analysis record")
        elif all(path.name == "JOURNAL.md" for path in analysis_paths):
            errors.append(
                f"{prefix} is journal-only and must be inherited_unresolved, not reviewed_primary"
            )


def _validate_hidden_input_boundary(
    item: dict[str, Any], *, prefix: str, errors: list[str]
) -> None:
    if item.get("status") not in ACTIVE_PROPOSAL_STATUSES:
        return
    decision = item.get("decision_rule")
    decision_text = decision.get("rule", "") if isinstance(decision, dict) else ""
    fields = (
        "research_question",
        "hypothesis",
        "primary_variable",
        "fixed_elicitation",
        "next_action",
    )
    text = "\n".join(str(item.get(field, "")) for field in fields) + f"\n{decision_text}"
    lowered = text.lower()
    exposed = next((term for term in HIDDEN_INPUT_TERMS if term in lowered), None)
    if exposed is not None:
        errors.append(
            f"{prefix} active proposal exposes hidden verifier input {exposed!r} to the agent"
        )


def _validate_experiment(
    item: object,
    *,
    index: int,
    repo_root: Path,
    seen: set[str],
    errors: list[str],
) -> None:
    prefix = f"experiments[{index}]"
    if not isinstance(item, dict):
        errors.append(f"{prefix} is not an object")
        return
    unknown = sorted(set(item) - REQUIRED - OPTIONAL)
    if unknown:
        errors.append(f"{prefix} has unknown fields {unknown}")
    missing = sorted(REQUIRED - set(item))
    if missing:
        errors.append(f"{prefix} missing {missing}")
    for field in sorted(STRING_FIELDS):
        if field in item and not _nonempty_string(item[field]):
            errors.append(f"{prefix}.{field} must be a non-empty string")
    exp_id = item.get("id")
    if isinstance(exp_id, str) and not exp_id.startswith("EXP-"):
        errors.append(f"{prefix}.id must start with EXP-")
    if isinstance(exp_id, str):
        if exp_id in seen:
            errors.append(f"duplicate id {exp_id}")
        seen.add(exp_id)
    model = item.get("model")
    if model is not None and not _nonempty_string(model):
        errors.append(f"{prefix}.model must be null or a non-empty string")
    k = item.get("k")
    if not _is_int(k) or k <= 0:
        errors.append(f"{prefix}.k must be a positive integer")
    if "n_tasks" in item and (not _is_int(item["n_tasks"]) or item["n_tasks"] <= 0):
        errors.append(f"{prefix}.n_tasks must be a positive integer")
    if "representative_attempts" in item and (
        not _is_int(item["representative_attempts"]) or item["representative_attempts"] <= 0
    ):
        errors.append(f"{prefix}.representative_attempts must be a positive integer")
    if item.get("status") not in STATUSES:
        errors.append(f"{prefix}.status {item.get('status')!r} not in enum")
    resolved, spec_attempts = _validate_references(
        item,
        prefix=prefix,
        repo_root=repo_root,
        errors=errors,
    )
    _validate_decision_rule(item, prefix=prefix, spec_attempts=spec_attempts, errors=errors)
    _validate_provenance(item, prefix=prefix, resolved=resolved, errors=errors)
    _validate_hidden_input_boundary(item, prefix=prefix, errors=errors)


def validate(path: Path, repo_root: Path | None = None) -> list[str]:
    """Return all deterministic schema, reference, and claim-boundary errors."""
    errors: list[str] = []
    payload = _load_json(path, errors, str(path))
    if not isinstance(payload, dict):
        if payload is not None:
            errors.append("PROGRAM root must be an object")
        return errors
    unknown = sorted(set(payload) - TOP_LEVEL_KEYS)
    if unknown:
        errors.append(f"PROGRAM has unknown fields {unknown}")
    missing = sorted(TOP_LEVEL_KEYS - set(payload))
    if missing:
        errors.append(f"PROGRAM missing {missing}")
    if payload.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if not _nonempty_string(payload.get("title")):
        errors.append("title must be a non-empty string")
    updated_at = payload.get("updated_at")
    if not _nonempty_string(updated_at):
        errors.append("updated_at must be an ISO date string")
    else:
        try:
            date.fromisoformat(updated_at)
        except ValueError:
            errors.append("updated_at must be an ISO date string")
    experiments = payload.get("experiments")
    if not isinstance(experiments, list) or not experiments:
        errors.append("experiments must be a non-empty list")
        return errors
    root = repo_root or path.resolve().parents[2]
    seen: set[str] = set()
    for index, item in enumerate(experiments):
        _validate_experiment(
            item,
            index=index,
            repo_root=root,
            seen=seen,
            errors=errors,
        )
    return errors


def observation_text_is_failure(content: object) -> bool:
    """Return whether a tool observation text reports a command/assertion failure."""
    text = content if isinstance(content, str) else json.dumps(content, sort_keys=True)
    return any(pattern.search(text) for pattern in FAILURE_TEXT_PATTERNS)


def count_failed_command_observations(trajectory: object) -> int:
    """Count failed tool-result observations without relying on optional exit-code keys."""
    if not isinstance(trajectory, dict) or not isinstance(trajectory.get("steps"), list):
        raise ValueError("trajectory must be an object with a steps list")
    count = 0
    for step in trajectory["steps"]:
        if not isinstance(step, dict):
            continue
        observation = step.get("observation")
        if not isinstance(observation, dict):
            continue
        results = observation.get("results")
        if not isinstance(results, list):
            continue
        count += sum(
            observation_text_is_failure(result.get("content"))
            for result in results
            if isinstance(result, dict)
        )
    return count


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("program", nargs="?", type=Path, help="PROGRAM JSON path")
    parser.add_argument("--repo-root", type=Path, help="root for repository-relative references")
    parser.add_argument(
        "--count-observation-failures",
        type=Path,
        metavar="TRAJECTORY",
        help="print failed command/assertion observations from an ATIF trajectory",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.count_observation_failures is not None:
        payload = json.loads(args.count_observation_failures.read_text())
        print(count_failed_command_observations(payload))
        return 0
    root = Path(__file__).resolve().parent
    path = args.program or root / "PROGRAM.json"
    errors = validate(path, repo_root=args.repo_root)
    if errors:
        print(f"{path} INVALID")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(f"{path} OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
