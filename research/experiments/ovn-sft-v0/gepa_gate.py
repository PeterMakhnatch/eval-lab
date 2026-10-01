#!/usr/bin/env python3
"""HAR-135: Overnight GEPA experiment gate (G2 minibatch freeze and selection).

Pure functions and CLI for:
1. Freezing the 10-task training minibatch before G2 outcomes:
   - Takes first 10 rows of research/experiments/python-task-ledger/har120_proposal.csv.
   - Binds proposal SHA256 and eval tasks CSV SHA256.
   - Verifies zero collision with 20 held-out tasks (task IDs and normalized repositories).
   - Verifies task package digests and task uniqueness.
   - Enforces immutability: idempotent on byte-identical rerun, refuses overwrite on change.
2. Candidate selection:
   - Baseline comparison against exactly G2 attempt/repeat 1 for the 10 tasks.
   - At most 2 candidates (rejects >2).
   - Rejects duplicate candidate IDs, duplicate/extra task rows, digest drift, malformed counts,
     and held-out collisions.
   - Authoritative counts (evallab.counts/v1): pass => 1.0, fail => 0.0, excluded => ineligible.
   - Excluded rows or missing task rows produce explicit ineligible coverage explanations;
     never converted to failure, never ranked as full coverage.
   - Unknown-preserving token tiebreak: unknown token sums cannot establish improvement.
   - Promotion requires strictly beating seed by counted passes, or known strictly lower
     total tokens on equal passes. Exact ties retain seed.
   - Deterministic candidate ordering by freeze timestamp then digest.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

# Ensure src is on sys.path for evallab imports if executed directly
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FREEZE_SCHEMA_VERSION = "har135.freeze/v1"
SELECTION_SCHEMA_VERSION = "har135.selection/v1"
COUNTS_SCHEMA = "evallab.counts/v1"
SELECTION_RULE = "counted_pass_then_total_tokens"
BASELINE_REPEAT = 1
MAX_CANDIDATES = 2
MAX_GATE_COST_USD = 2.0
TASK_COUNT = 10

VERDICTS = frozenset({"counted_pass", "counted_fail", "excluded"})
EXCLUSION_REASONS = frozenset({"copied_fix", "pass_tainted", "task_not_usable", "infra"})
CANDIDATE_ID_PATTERN = re.compile(r"^(sha256:)?[0-9a-f]{64}$")
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Exceptions Hierarchy
# ---------------------------------------------------------------------------


class GateError(Exception):
    """Base error for HAR-135 GEPA gate operations."""


class GateValidationError(GateError, ValueError):
    """Raised when gate input validation fails."""

    def __init__(self, message: str, decision: str = "rejected"):
        super().__init__(message)
        self.message = message
        self.decision = decision

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SELECTION_SCHEMA_VERSION,
            "status": "rejected",
            "decision": self.decision,
            "winner": "seed",
            "promoted": False,
            "reasons": [self.message],
        }


class HeldOutCollisionError(GateValidationError):
    """Raised when training candidate or minibatch collides with held-out eval set."""

    def __init__(self, message: str):
        super().__init__(message, decision="held_out_collision")


class HeldOutTaskCollisionError(HeldOutCollisionError):
    """Raised when training task ID collides with a held-out eval task ID."""


class HeldOutRepositoryCollisionError(HeldOutCollisionError):
    """Raised when training repository collides with a held-out eval repository."""


class TaskDigestDriftError(GateValidationError):
    """Raised when a task package digest does not match the frozen manifest."""

    def __init__(self, message: str):
        super().__init__(message, decision="wrong_digest")


class TooManyCandidatesError(GateValidationError):
    """Raised when more than max_candidates (2) are evaluated."""

    def __init__(self, message: str):
        super().__init__(message, decision="too_many_candidates")


class DuplicateCandidateError(GateValidationError):
    """Raised when duplicate candidate IDs are provided."""

    def __init__(self, message: str):
        super().__init__(message, decision="duplicate_candidate_ids")


class DuplicateTaskRowError(GateValidationError):
    """Raised when duplicate task rows are provided for a candidate or seed."""

    def __init__(self, message: str):
        super().__init__(message, decision="duplicate_task_rows")


class ExtraTaskRowError(GateValidationError):
    """Raised when extra task rows are provided outside the frozen manifest."""

    def __init__(self, message: str):
        super().__init__(message, decision="extra_task_rows")


class MissingTaskRowError(GateValidationError):
    """Raised when task rows are missing from seed."""

    def __init__(self, message: str):
        super().__init__(message, decision="missing_data")


class MalformedCountsError(GateValidationError):
    """Raised when counts dict fails canonical schema or consistency checks."""

    def __init__(self, message: str):
        super().__init__(message, decision="malformed_counts")


class InvalidBaselineRepeatError(GateValidationError):
    """Raised when seed rows are not from baseline repeat 1."""

    def __init__(self, message: str):
        super().__init__(message, decision="invalid_baseline_repeat")


# ---------------------------------------------------------------------------
# Pure Helper Functions
# ---------------------------------------------------------------------------


def repo_key(project: str | None) -> str | None:
    """Normalize project identifier to repository key for collision checks."""
    if not project or project.startswith("format-code-task-"):
        return None
    return project.rstrip("/").rsplit("/", 1)[-1].lower().replace("-", "_")


def parse_iso8601_utc(timestamp_str: str) -> datetime:
    """Parse ISO8601 UTC timestamp string to aware datetime."""
    if not isinstance(timestamp_str, str) or not timestamp_str.strip():
        raise GateValidationError(f"Invalid timestamp string: {timestamp_str!r}", decision="malformed_timestamp")
    normalized = timestamp_str.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except Exception as exc:
        raise GateValidationError(f"Invalid ISO8601 timestamp {timestamp_str!r}: {exc}", decision="malformed_timestamp") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def extract_row_tokens(usage: dict[str, Any] | None) -> tuple[int | None, int | None, int | None]:
    """Extract (input_tokens, output_tokens, total_tokens) preserving unknown None values."""
    if not isinstance(usage, dict):
        return None, None, None
    inp = usage.get("input_tokens")
    if inp is None:
        inp = usage.get("n_input_tokens")
    outp = usage.get("output_tokens")
    if outp is None:
        outp = usage.get("n_output_tokens")

    valid_inp = isinstance(inp, int) and not isinstance(inp, bool)
    valid_outp = isinstance(outp, int) and not isinstance(outp, bool)

    inp_val = inp if valid_inp else None
    outp_val = outp if valid_outp else None
    tot_val = (inp + outp) if (valid_inp and valid_outp) else None
    return inp_val, outp_val, tot_val


def validate_counts_dict(counts: Any) -> tuple[bool, str | None]:
    """Validate canonical counts schema and verdict/reward consistency."""
    if not isinstance(counts, dict):
        return False, "counts must be a JSON object"
    if counts.get("schema") != COUNTS_SCHEMA:
        return False, f"counts schema must be {COUNTS_SCHEMA!r}, got {counts.get('schema')!r}"
    verdict = counts.get("verdict")
    if verdict not in VERDICTS:
        return False, f"invalid verdict {verdict!r}; expected one of {sorted(VERDICTS)}"
    raw_reward = counts.get("raw_reward")
    if verdict == "counted_pass":
        if not isinstance(raw_reward, (int, float)) or isinstance(raw_reward, bool) or raw_reward < 1.0:
            return False, f"counted_pass must have raw_reward == 1.0, got {raw_reward!r}"
        if not counts.get("scored"):
            return False, "counted_pass must have scored=True"
    elif verdict == "counted_fail":
        if not isinstance(raw_reward, (int, float)) or isinstance(raw_reward, bool) or raw_reward >= 1.0:
            return False, f"counted_fail must have raw_reward < 1.0, got {raw_reward!r}"
        if not counts.get("scored"):
            return False, "counted_fail must have scored=True"
    elif verdict == "excluded":
        reasons = counts.get("reasons")
        if not isinstance(reasons, list) or not reasons:
            return False, "excluded verdict must provide non-empty list of exclusion reasons"
    return True, None


def validate_seed_repeat(row: dict[str, Any]) -> tuple[bool, str | None]:
    """Ensure seed trial is exactly baseline repeat 1."""
    repeat = row.get("repeat")
    if repeat is not None and repeat != BASELINE_REPEAT:
        return False, f"seed row repeat={repeat} != {BASELINE_REPEAT}"
    attempt = row.get("attempt")
    if attempt is not None and attempt != BASELINE_REPEAT:
        return False, f"seed row attempt={attempt} != {BASELINE_REPEAT}"
    trial_id = str(row.get("trial_id", ""))
    if re.search(r"[-_](?:repeat|attempt)[-_]?([2-9]|\d{2,})", trial_id, re.IGNORECASE):
        return False, f"seed trial_id {trial_id!r} indicates attempt > 1"
    return True, None


# ---------------------------------------------------------------------------
# Pure Gate Functions: freeze_minibatch and select_candidate
# ---------------------------------------------------------------------------


def freeze_minibatch(proposal_path: Path | str, eval_tasks_path: Path | str) -> dict[str, Any]:
    """Freeze the immutable 10-task training minibatch before G2 outcomes.

    Reads first 10 rows of proposal CSV in image order, binds proposal SHA256 and
    eval CSV SHA256, verifies full task IDs and package digests, and enforces zero
    collision with held-out eval tasks and repositories.
    """
    proposal_path = Path(proposal_path)
    eval_tasks_path = Path(eval_tasks_path)

    if not proposal_path.is_file():
        raise FileNotFoundError(f"Proposal CSV not found: {proposal_path}")
    if not eval_tasks_path.is_file():
        raise FileNotFoundError(f"Eval tasks CSV not found: {eval_tasks_path}")

    prop_bytes = proposal_path.read_bytes()
    eval_bytes = eval_tasks_path.read_bytes()
    proposal_sha256 = hashlib.sha256(prop_bytes).hexdigest()
    eval_tasks_sha256 = hashlib.sha256(eval_bytes).hexdigest()

    with eval_tasks_path.open(encoding="utf-8") as handle:
        eval_rows = list(csv.DictReader(handle))

    if not eval_rows:
        raise GateValidationError("Held-out eval tasks set is empty", decision="empty_eval_tasks")

    eval_task_ids: set[str] = set()
    eval_repos: set[str] = set()
    for row in eval_rows:
        t_id = row.get("task") or row.get("task_id")
        if t_id:
            eval_task_ids.add(t_id.strip())
        rk = repo_key(row.get("repo") or row.get("project"))
        if rk:
            eval_repos.add(rk)

    with proposal_path.open(encoding="utf-8") as handle:
        proposal_rows = list(csv.DictReader(handle))

    if len(proposal_rows) < TASK_COUNT:
        raise GateValidationError(
            f"Proposal CSV has only {len(proposal_rows)} rows; minimum {TASK_COUNT} required",
            decision="insufficient_rows",
        )

    first_10 = proposal_rows[:TASK_COUNT]
    tasks: list[dict[str, Any]] = []
    seen_task_ids: set[str] = set()

    for idx, row in enumerate(first_10):
        task_id = (row.get("task_id") or row.get("task") or "").strip()
        digest = (row.get("run_digest") or row.get("digest") or row.get("task_package_digest") or "").strip()
        project = (row.get("project") or row.get("repo") or "").strip()

        if not task_id:
            raise GateValidationError(f"Row {idx} missing task_id in proposal", decision="malformed_proposal")
        if not digest or not SHA256_PATTERN.fullmatch(digest):
            raise GateValidationError(
                f"Row {idx} ({task_id}) invalid task package digest {digest!r}",
                decision="malformed_digest",
            )
        if not project:
            raise GateValidationError(f"Row {idx} ({task_id}) missing project in proposal", decision="malformed_proposal")

        if task_id in seen_task_ids:
            raise DuplicateTaskRowError(f"Duplicate task ID in proposal first 10: {task_id}")
        seen_task_ids.add(task_id)

        if task_id in eval_task_ids:
            raise HeldOutTaskCollisionError(f"Held-out task ID collision: {task_id}")

        rk = repo_key(project)
        if rk and rk in eval_repos:
            raise HeldOutRepositoryCollisionError(f"Held-out repository collision: {project} ({rk})")

        task_entry: dict[str, Any] = {
            "task_id": task_id,
            "task_package_digest": digest,
            "project": project,
        }
        if "image_mib" in row:
            try:
                task_entry["image_mib"] = int(row["image_mib"])
            except (ValueError, TypeError):
                pass
        if "run" in row:
            task_entry["run"] = row["run"]

        tasks.append(task_entry)

    return {
        "schema_version": FREEZE_SCHEMA_VERSION,
        "proposal_sha256": proposal_sha256,
        "eval_tasks_sha256": eval_tasks_sha256,
        "selection_rule": SELECTION_RULE,
        "baseline_repeat": BASELINE_REPEAT,
        "max_candidates": MAX_CANDIDATES,
        "max_gate_cost_usd": MAX_GATE_COST_USD,
        "task_count": len(tasks),
        "tasks": tasks,
    }


def select_candidate(
    manifest: dict[str, Any],
    seed_rows: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    raise_on_reject: bool = False,
) -> dict[str, Any]:
    """Select winning candidate against stock/seed repeat 1 under scientific contract.

    Rules:
    - Exactly repeat 1 seed baseline comparison.
    - Max 2 candidates (rejects >2).
    - Rejects duplicate candidate IDs, duplicate/extra task rows, digest drift,
      malformed counts, and held-out collisions.
    - Full countable coverage required: candidate excluded or missing task rows
      produce explicit ineligible coverage explanations (never converted to failure).
    - Unknown token sums cannot establish token improvement.
    - Promotion requires strictly beating seed by counted passes, or strictly lower
      measured total tokens on equal passes (all token observations known).
    - Exact ties retain stock/seed.
    - Deterministic candidate ordering by freeze timestamp then digest.
    """
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()

    def _reject(decision: str, reason: str, exc_type: type[GateValidationError] = GateValidationError) -> dict[str, Any]:
        if raise_on_reject:
            raise exc_type(reason)
        return {
            "schema_version": SELECTION_SCHEMA_VERSION,
            "status": "rejected",
            "decision": decision,
            "winner": "seed",
            "promoted": False,
            "reasons": [reason],
            "manifest_digest": manifest_digest,
        }

    # 1. Manifest Validation
    if not isinstance(manifest, dict) or "tasks" not in manifest or not isinstance(manifest["tasks"], list):
        return _reject("malformed_manifest", "Manifest must contain 'tasks' list")

    manifest_tasks: dict[str, str] = {}
    for t in manifest["tasks"]:
        tid = t.get("task_id")
        tdigest = t.get("task_package_digest")
        if not tid or not tdigest:
            return _reject("malformed_manifest", f"Manifest task entry missing task_id or digest: {t}")
        manifest_tasks[tid] = tdigest

    if len(manifest_tasks) != TASK_COUNT:
        return _reject(
            "malformed_manifest",
            f"Manifest must define exactly {TASK_COUNT} distinct tasks; found {len(manifest_tasks)}",
        )

    max_cands = manifest.get("max_candidates", MAX_CANDIDATES)

    # 2. Candidate Count Validation
    if len(candidates) > max_cands:
        return _reject(
            "too_many_candidates",
            f"Candidate count ({len(candidates)}) exceeds maximum allowed ({max_cands})",
            TooManyCandidatesError,
        )

    # 3. Duplicate Candidate IDs Validation
    seen_cids: set[str] = set()
    for c in candidates:
        if not isinstance(c, dict):
            return _reject("malformed_candidate", "Candidate must be a JSON object")
        cid = c.get("candidate_id")
        if not isinstance(cid, str) or not CANDIDATE_ID_PATTERN.fullmatch(cid):
            return _reject("malformed_candidate_id", f"Invalid candidate_id {cid!r}")
        if cid in seen_cids:
            return _reject("duplicate_candidate_ids", f"Duplicate candidate ID: {cid}", DuplicateCandidateError)
        seen_cids.add(cid)

    # 4. Seed Rows Validation
    if not isinstance(seed_rows, list):
        return _reject("malformed_seed", "seed_rows must be a list of dicts")

    seen_seed_tasks: set[str] = set()
    seed_row_map: dict[str, dict[str, Any]] = {}
    for idx, row in enumerate(seed_rows):
        if not isinstance(row, dict):
            return _reject("malformed_seed_row", f"Seed row {idx} is not a dict")
        tid = row.get("task_id")
        if not tid or not isinstance(tid, str):
            return _reject("malformed_seed_row", f"Seed row {idx} missing task_id")
        if tid not in manifest_tasks:
            return _reject("extra_task_rows", f"Seed contains extra task row: {tid}", ExtraTaskRowError)
        if tid in seen_seed_tasks:
            return _reject("duplicate_task_rows", f"Seed contains duplicate task row for {tid}", DuplicateTaskRowError)
        seen_seed_tasks.add(tid)

        expected_digest = manifest_tasks[tid]
        actual_digest = row.get("task_package_digest")
        if actual_digest != expected_digest:
            return _reject(
                "wrong_digest",
                f"Task package digest drift in seed row for task {tid}: expected {expected_digest}, got {actual_digest}",
                TaskDigestDriftError,
            )

        valid_repeat, repeat_err = validate_seed_repeat(row)
        if not valid_repeat:
            return _reject("invalid_baseline_repeat", f"Seed row for {tid} violates repeat 1: {repeat_err}", InvalidBaselineRepeatError)

        valid_counts, counts_err = validate_counts_dict(row.get("counts"))
        if not valid_counts:
            return _reject("malformed_counts", f"Seed row for {tid} has malformed counts: {counts_err}", MalformedCountsError)

        seed_row_map[tid] = row

    if seen_seed_tasks != set(manifest_tasks.keys()):
        missing = sorted(set(manifest_tasks.keys()) - seen_seed_tasks)
        return _reject("missing_data", f"Seed is missing task rows: {missing}", MissingTaskRowError)

    # 5. Candidate Rows Validation
    validated_candidates: list[dict[str, Any]] = []
    for c in candidates:
        cid = c["candidate_id"]
        frozen_at_str = c.get("frozen_at")
        try:
            frozen_dt = parse_iso8601_utc(frozen_at_str)
        except Exception as exc:
            return _reject("malformed_timestamp", f"Candidate {cid} invalid frozen_at {frozen_at_str!r}: {exc}")

        cand_rows = c.get("rows")
        if not isinstance(cand_rows, list):
            return _reject("malformed_candidate", f"Candidate {cid} rows must be a list")

        seen_cand_tasks: set[str] = set()
        c_row_map: dict[str, dict[str, Any]] = {}
        for idx, row in enumerate(cand_rows):
            if not isinstance(row, dict):
                return _reject("malformed_candidate_row", f"Candidate {cid} row {idx} is not a dict")
            tid = row.get("task_id")
            if not tid or not isinstance(tid, str):
                return _reject("malformed_candidate_row", f"Candidate {cid} row {idx} missing task_id")
            if tid not in manifest_tasks:
                return _reject("extra_task_rows", f"Candidate {cid} contains extra task row: {tid}", ExtraTaskRowError)
            if tid in seen_cand_tasks:
                return _reject("duplicate_task_rows", f"Candidate {cid} contains duplicate task row for {tid}", DuplicateTaskRowError)
            seen_cand_tasks.add(tid)

            expected_digest = manifest_tasks[tid]
            actual_digest = row.get("task_package_digest")
            if actual_digest != expected_digest:
                return _reject(
                    "wrong_digest",
                    f"Task package digest drift in candidate {cid} for task {tid}: expected {expected_digest}, got {actual_digest}",
                    TaskDigestDriftError,
                )

            valid_counts, counts_err = validate_counts_dict(row.get("counts"))
            if not valid_counts:
                return _reject("malformed_counts", f"Candidate {cid} row for {tid} has malformed counts: {counts_err}", MalformedCountsError)

            c_row_map[tid] = row

        validated_candidates.append({
            "candidate_id": cid,
            "frozen_at": frozen_dt.isoformat(),
            "frozen_dt": frozen_dt,
            "candidate_path": c.get("candidate_path"),
            "row_map": c_row_map,
            "seen_tasks": seen_cand_tasks,
        })

    # 6. Seed Metrics Rollup
    seed_passes = 0
    seed_fails = 0
    seed_excluded = 0
    seed_exclusions: list[dict[str, Any]] = []
    seed_tokens_list: list[int | None] = []

    for tid in manifest["tasks"]:
        task_id = tid["task_id"]
        row = seed_row_map[task_id]
        verdict = row["counts"]["verdict"]
        if verdict == "counted_pass":
            seed_passes += 1
        elif verdict == "counted_fail":
            seed_fails += 1
        elif verdict == "excluded":
            seed_excluded += 1
            seed_exclusions.append({"task_id": task_id, "reasons": row["counts"].get("reasons", [])})

        _, _, tot = extract_row_tokens(row.get("usage"))
        seed_tokens_list.append(tot)

    seed_has_unknown_tokens = any(t is None for t in seed_tokens_list)
    seed_unknown_token_count = sum(1 for t in seed_tokens_list if t is None)
    seed_total_tokens = sum(seed_tokens_list) if not seed_has_unknown_tokens else None

    seed_summary = {
        "candidate_id": "seed",
        "task_count": TASK_COUNT,
        "counted_passes": seed_passes,
        "counted_fails": seed_fails,
        "excluded": seed_excluded,
        "total_tokens": seed_total_tokens,
        "unknown_token_count": seed_unknown_token_count,
        "has_unknown_tokens": seed_has_unknown_tokens,
        "exclusions": seed_exclusions,
    }

    # 7. Candidate Coverage & Eligibility Assessment
    cand_summaries: list[dict[str, Any]] = []
    for vc in validated_candidates:
        cid = vc["candidate_id"]
        row_map = vc["row_map"]
        seen_tasks = vc["seen_tasks"]

        missing_tasks = sorted(set(manifest_tasks.keys()) - seen_tasks)
        c_passes = 0
        c_fails = 0
        c_excluded = 0
        c_exclusions: list[dict[str, Any]] = []
        c_tokens_list: list[int | None] = []

        for tid in manifest["tasks"]:
            task_id = tid["task_id"]
            if task_id in row_map:
                row = row_map[task_id]
                verdict = row["counts"]["verdict"]
                if verdict == "counted_pass":
                    c_passes += 1
                elif verdict == "counted_fail":
                    c_fails += 1
                elif verdict == "excluded":
                    c_excluded += 1
                    c_exclusions.append({"task_id": task_id, "reasons": row["counts"].get("reasons", [])})
                _, _, tot = extract_row_tokens(row.get("usage"))
                c_tokens_list.append(tot)
            else:
                c_tokens_list.append(None)

        c_has_unknown_tokens = any(t is None for t in c_tokens_list)
        c_unknown_token_count = sum(1 for t in c_tokens_list if t is None)
        c_total_tokens = sum(c_tokens_list) if (not c_has_unknown_tokens and len(c_tokens_list) == TASK_COUNT) else None

        eligible = True
        ineligible_reasons: list[str] = []
        if missing_tasks:
            eligible = False
            ineligible_reasons.append(f"Missing task rows: {missing_tasks}")
        if c_excluded > 0:
            eligible = False
            ineligible_reasons.append(
                f"Excluded tasks ({c_excluded}): {[e['task_id'] + ' ' + str(e['reasons']) for e in c_exclusions]}"
            )

        pass_delta = c_passes - seed_passes
        token_delta = (c_total_tokens - seed_total_tokens) if (c_total_tokens is not None and seed_total_tokens is not None) else None

        cand_summaries.append({
            "candidate_id": cid,
            "frozen_at": vc["frozen_at"],
            "frozen_dt": vc["frozen_dt"],
            "candidate_path": vc["candidate_path"],
            "eligible": eligible,
            "ineligible_reasons": ineligible_reasons,
            "task_count": len(seen_tasks),
            "counted_passes": c_passes,
            "counted_fails": c_fails,
            "excluded": c_excluded,
            "total_tokens": c_total_tokens,
            "unknown_token_count": c_unknown_token_count,
            "has_unknown_tokens": c_has_unknown_tokens,
            "exclusions": c_exclusions,
            "pass_delta_vs_seed": pass_delta,
            "token_delta_vs_seed": token_delta,
        })

    # 8. Decision and Winner Determination
    if not cand_summaries:
        return {
            "schema_version": SELECTION_SCHEMA_VERSION,
            "status": "seed_retained",
            "decision": "seed_tie",
            "winner": "seed",
            "promoted": False,
            "seed": seed_summary,
            "candidates": [],
            "reasons": ["No candidates provided; stock/seed retained."],
            "manifest_digest": manifest_digest,
        }

    eligible_cands = [cs for cs in cand_summaries if cs["eligible"]]

    if not eligible_cands:
        # Distinguish exclusions vs missing_data
        has_exclusions = any(cs["excluded"] > 0 for cs in cand_summaries)
        has_missing = any(cs["task_count"] < TASK_COUNT for cs in cand_summaries)
        decision = "exclusions" if has_exclusions else "missing_data"

        all_ineligible_reasons: list[str] = []
        for cs in cand_summaries:
            all_ineligible_reasons.extend(cs["ineligible_reasons"])

        # Format candidates without internal helper keys
        formatted_candidates = [
            {k: v for k, v in cs.items() if k != "frozen_dt"}
            for cs in cand_summaries
        ]
        return {
            "schema_version": SELECTION_SCHEMA_VERSION,
            "status": "ineligible",
            "decision": decision,
            "winner": "seed",
            "promoted": False,
            "seed": seed_summary,
            "candidates": formatted_candidates,
            "reasons": all_ineligible_reasons,
            "manifest_digest": manifest_digest,
        }

    # Deterministic candidate ordering among eligible candidates:
    # 1. passes descending
    # 2. total tokens ascending (if known; unknown token sums treated as infinite advantage)
    # 3. freeze timestamp ascending
    # 4. candidate_id ascending
    def candidate_order_key(item: dict[str, Any]) -> tuple[Any, ...]:
        passes = -item["counted_passes"]
        t_val = item["total_tokens"] if item["total_tokens"] is not None else float("inf")
        return (passes, t_val, item["frozen_dt"], item["candidate_id"])

    eligible_cands.sort(key=candidate_order_key)
    best = eligible_cands[0]

    # Compare best candidate against Seed
    if best["counted_passes"] > seed_passes:
        status = "promoted"
        decision = "counted_improvement"
        winner = best["candidate_id"]
        promoted = True
        reason = (
            f"Candidate {best['candidate_id']} promoted: counted passes improved from "
            f"{seed_passes} to {best['counted_passes']}."
        )
    elif best["counted_passes"] < seed_passes:
        status = "seed_retained"
        decision = "seed_tie"
        winner = "seed"
        promoted = False
        reason = (
            f"Seed retained: candidate counted passes ({best['counted_passes']}) did not "
            f"exceed seed passes ({seed_passes})."
        )
    else:
        # Equal passes tiebreak on tokens
        if best["has_unknown_tokens"] or seed_has_unknown_tokens:
            status = "seed_retained"
            decision = "unknown_tokens"
            winner = "seed"
            promoted = False
            reason = (
                f"Seed retained: counted passes tied at {seed_passes}, but total token sum "
                "contains unknown observations and cannot establish a token improvement."
            )
        elif best["total_tokens"] < seed_total_tokens:
            status = "promoted"
            decision = "tokens_improvement"
            winner = best["candidate_id"]
            promoted = True
            reason = (
                f"Candidate {best['candidate_id']} promoted: counted passes tied at {seed_passes}, "
                f"total tokens improved from {seed_total_tokens} to {best['total_tokens']}."
            )
        elif best["total_tokens"] == seed_total_tokens:
            status = "seed_retained"
            decision = "seed_tie"
            winner = "seed"
            promoted = False
            reason = (
                f"Seed retained: exact tie on counted passes ({seed_passes}) and total tokens "
                f"({seed_total_tokens})."
            )
        else:
            status = "seed_retained"
            decision = "seed_tie"
            winner = "seed"
            promoted = False
            reason = (
                f"Seed retained: counted passes tied at {seed_passes}, but candidate total tokens "
                f"({best['total_tokens']}) exceeded seed ({seed_total_tokens})."
            )

    formatted_candidates = [
        {k: v for k, v in cs.items() if k != "frozen_dt"}
        for cs in cand_summaries
    ]

    return {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "status": status,
        "decision": decision,
        "winner": winner,
        "promoted": promoted,
        "seed": seed_summary,
        "candidates": formatted_candidates,
        "reasons": [reason],
        "manifest_digest": manifest_digest,
    }


# ---------------------------------------------------------------------------
# File Parsing & CLI Helpers
# ---------------------------------------------------------------------------


def load_rows_file(path: Path) -> list[dict[str, Any]]:
    """Load JSON or JSONL row records from file."""
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("["):
        data = json.loads(text)
        if isinstance(data, list):
            return data
    elif text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                if "rows" in data and isinstance(data["rows"], list):
                    return data["rows"]
                return [data]
        except json.JSONDecodeError:
            pass

    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def load_candidates_files(paths: list[Path]) -> list[dict[str, Any]]:
    """Load candidate JSON definitions from one or more paths."""
    candidates: list[dict[str, Any]] = []
    for p in paths:
        text = p.read_text(encoding="utf-8").strip()
        if text.startswith("["):
            data = json.loads(text)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        if "candidate_path" not in item:
                            item["candidate_path"] = str(p)
                        candidates.append(item)
        elif text.startswith("{"):
            data = json.loads(text)
            if isinstance(data, dict):
                if "candidates" in data and isinstance(data["candidates"], list):
                    for item in data["candidates"]:
                        if isinstance(item, dict):
                            if "candidate_path" not in item:
                                item["candidate_path"] = str(p)
                            candidates.append(item)
                else:
                    if "candidate_path" not in data:
                        data["candidate_path"] = str(p)
                    candidates.append(data)
    return candidates


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------


def cli_freeze(args: argparse.Namespace) -> int:
    """Run freeze subcommand."""
    proposal_path = Path(args.proposal)
    eval_tasks_path = Path(args.eval_tasks)
    out_path = Path(args.out)

    manifest = freeze_minibatch(proposal_path, eval_tasks_path)
    rendered = json.dumps(manifest, indent=2, sort_keys=True) + "\n"

    if out_path.exists():
        existing = out_path.read_text(encoding="utf-8")
        if existing == rendered:
            print(rendered, end="")
            return 0
        sys.stderr.write(
            f"ERROR: Refusing to overwrite existing manifest with different content: {out_path}\n"
        )
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


def cli_select(args: argparse.Namespace) -> int:
    """Run select subcommand."""
    manifest_path = Path(args.manifest)
    seed_path = Path(args.seed)
    cand_paths = [Path(p) for p in args.candidates]
    out_path = Path(args.out) if args.out else None

    if not manifest_path.is_file():
        sys.stderr.write(f"ERROR: Manifest file not found: {manifest_path}\n")
        return 2
    if not seed_path.is_file():
        sys.stderr.write(f"ERROR: Seed receipts file not found: {seed_path}\n")
        return 2

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    seed_rows = load_rows_file(seed_path)
    candidates = load_candidates_files(cand_paths)

    result = select_candidate(manifest, seed_rows, candidates, raise_on_reject=False)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")

    print(rendered, end="")
    return 2 if result.get("status") == "rejected" else 0


def main(argv: list[str] | None = None) -> int:
    """CLI dispatcher."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    # freeze
    freeze_parser = subparsers.add_parser("freeze", help="Freeze 10-task training minibatch manifest")
    freeze_parser.add_argument(
        "--proposal",
        default=str(ROOT / "research/experiments/python-task-ledger/har120_proposal.csv"),
        help="Path to har120_proposal.csv",
    )
    freeze_parser.add_argument(
        "--eval-tasks",
        default=str(ROOT / "research/experiments/ovn-sft-v0/eval_tasks.csv"),
        help="Path to eval_tasks.csv",
    )
    freeze_parser.add_argument("--out", required=True, help="Destination JSON path for frozen manifest")

    # select
    select_parser = subparsers.add_parser("select", help="Select candidate against seed repeat 1")
    select_parser.add_argument("--manifest", required=True, help="Path to frozen training manifest JSON")
    select_parser.add_argument("--seed", required=True, help="Path to seed receipts JSON / JSONL file")
    select_parser.add_argument(
        "--candidates",
        nargs="+",
        required=True,
        help="Path(s) to candidate JSON receipts files",
    )
    select_parser.add_argument("--out", required=False, help="Destination JSON path for selection result")

    args = parser.parse_args(argv)
    if args.command == "freeze":
        return cli_freeze(args)
    elif args.command == "select":
        return cli_select(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
