"""Conservative source-grounded failure atlas consumer (HAR-131).

Reads the transient trace-query views and emits
a small reproducible atlas: ``atlas.json`` + ``README.md``.

Numbers come from recorded query rows, frozen calibration receipts and explicit
expected cohort cells. ``counts_verdict`` is the sole counted authority; unknown
stays unknown. Import/connect errors fail visibly -- no fallback ingestor or
hardcoded measurements.

Usage::

    PYTHONPATH=src python research/explorations/trace-lab/failure-atlas/build.py \
        --repo-root . --out-dir research/explorations/trace-lab/failure-atlas

Optional explicit corpus pinning and GEPA gate::

    --job-dir PATH (repeatable) --results-home PATH --derived-root PATH
    --g2-bindings PATH  # authorized G2 training task+package bindings JSON
    --eval-tasks PATH --training-proposal PATH  # heldout gate + proposal provenance
    --g5-cohort PATH  # frozen G5 cohort.json; sibling specs bind expected cells

The G5 argument checks the admitted cohort SHA and all 60 frozen spec bytes.
Missing expected cells retain null outcomes and not_run/unknown states; reserved
G5 job names are never pooled into historical frequencies, even without a freeze.
Loop-calibration receipts load from har117-results-home and remain separate by
their manifest-bound label cohort. No historical accuracy is assigned to G5.

On a fully gated training selection the build also writes
``reflection-training.json`` (training-only safe payload) beside the outputs.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import json
import re
import shlex
import subprocess
import sys
from collections.abc import Collection
from pathlib import Path

MIMO_MODEL_RE = re.compile(r"(?:^|/)MiMo-V2\.6-Distill-Qwen-9B(?::har129)?$")
PYTHON_TASK_RE = re.compile(r"format-code-task-\d+$")
BUDGET_STOPS = {"TrialBudgetExhaustedError", "ceiling:input_tokens", "ceiling:requests"}
EVIDENCE_QUOTE_LIMIT = 200


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _optional_sha(path: Path) -> str | None:
    return _sha256_file(path) if path.is_file() else None


def verify_har119_freeze(repo_root: Path) -> dict:
    """Re-check the HAR-119 rater-label freeze; never assume it held."""
    labels_dir = repo_root / "research/explorations/trace-lab/har119/labels"
    manifest = labels_dir / "MANIFEST.sha256"
    result: dict = {
        "manifest_path": "research/explorations/trace-lab/har119/labels/MANIFEST.sha256",
        "manifest_sha256": _sha256_file(manifest) if manifest.is_file() else None,
        "files_ok": False,
        "checked": 0,
    }
    if not manifest.is_file():
        return result
    ok = True
    checked = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        digest, name = line.split(maxsplit=1)
        checked += 1
        target = labels_dir / name
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            ok = False
    result["files_ok"] = ok
    result["checked"] = checked
    frozen_at = labels_dir / "FROZEN_AT"
    result["frozen_at"] = frozen_at.read_text(encoding="utf-8").strip() if frozen_at.is_file() else None
    return result


def load_ledger(repo_root: Path) -> dict:
    """Authoritative Python-task ledger: task_id -> status/run/run_digest/split/project."""
    ledger_path = repo_root / "research/experiments/python-task-ledger/ledger.csv"
    by_task: dict = {}
    with ledger_path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            by_task[row["task_id"]] = {
                "status": row.get("status"),
                "run": row.get("run"),
                "run_digest": row.get("run_digest"),
                "split": row.get("split"),
                "project": row.get("project"),
            }
    return {"path": "research/experiments/python-task-ledger/ledger.csv",
            "sha256": _sha256_file(ledger_path), "n_tasks": len(by_task), "by_task": by_task}
DEFAULT_EVAL_TASKS = "research/experiments/ovn-sft-v0/eval_tasks.csv"
DEFAULT_TRAINING_PROPOSAL = "research/experiments/python-task-ledger/har120_proposal.csv"
PAYLOAD_FILENAME = "reflection-training.json"

DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

COUNTED_VERDICTS = {"counted_pass", "counted_fail"}


class PayloadSyncError(Exception):
    """The output directory cannot be left without a stale safe-export artifact."""


def validate_g2_bindings(g2_bindings: object) -> tuple[set, str | None]:
    """Whole-file validation: any malformed entry or duplicate fails the file.

    Returns (allowed pairs, error). Error is None only when every entry is a
    well-formed {task, package_digest} pair with canonical string identity.
    """
    if not isinstance(g2_bindings, list) or not g2_bindings:
        return set(), "empty or malformed bindings list"
    allowed: set = set()
    malformed: list = []
    duplicates: list = []
    for index, binding in enumerate(g2_bindings):
        task = binding.get("task") if isinstance(binding, dict) else None
        digest = binding.get("package_digest") if isinstance(binding, dict) else None
        if not (isinstance(task, str) and PYTHON_TASK_RE.fullmatch(task)
                and isinstance(digest, str) and DIGEST_RE.fullmatch(digest)):
            malformed.append(index)
            continue
        if (task, digest) in allowed:
            duplicates.append(index)
            continue
        allowed.add((task, digest))
    if malformed or duplicates:
        return set(), (f"rejecting bindings file: malformed entries at indices {malformed}; "
                       f"duplicate entries at indices {duplicates}")
    return allowed, None


def _first_present(row: dict, names: list) -> object:
    for name in names:
        if row.get(name) not in (None, ""):
            return row.get(name)
    return None


def load_eval_gate(repo_root: Path, eval_tasks: Path | None = None) -> dict | None:
    """Frozen G1 eval identities (task/digest/repo).

    Returns None (caller fails closed) when the file is missing, its header
    lacks the actual G1v2 fields, or it yields no task identities: empty sets
    must never pretend authority.
    """
    rel = str(eval_tasks) if eval_tasks else DEFAULT_EVAL_TASKS
    path = eval_tasks if eval_tasks and eval_tasks.is_absolute() else repo_root / rel
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or [])
        lowered = [c.lower() for c in columns]
        has_task = any(c in ("task", "task_id") for c in lowered)
        has_repo = any(c in ("repo", "project", "project_key") for c in lowered)
        has_digest = "digest" in lowered
        if not (has_task and has_repo and has_digest):
            return None
        task_ids, repos, digests = set(), set(), set()
        for row in reader:
            task = _first_present(row, ["task", "task_id"])
            repo = _first_present(row, ["repo", "project", "project_key"])
            digest = _first_present(row, ["digest"])
            if task:
                task_ids.add(str(task))
            if repo:
                repos.add(str(repo))
            if digest:
                digests.add(str(digest))
    if not task_ids:
        return None
    return {"path": rel, "sha256": _sha256_file(path), "n_tasks": len(task_ids),
            "columns": columns, "task_ids": task_ids, "repos": repos, "digests": digests}


def load_training_proposal(repo_root: Path, proposal: Path | None = None) -> dict | None:
    """Approved HAR120 training proposal, provenance only (never a selector)."""
    rel = str(proposal) if proposal else DEFAULT_TRAINING_PROPOSAL
    path = proposal if proposal and proposal.is_absolute() else repo_root / rel
    if not path.is_file():
        return None
    task_ids = set()
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            task = _first_present(row, ["task_id", "task"])
            if task:
                task_ids.add(str(task))
    return {"path": rel, "sha256": _sha256_file(path), "n_tasks": len(task_ids),
            "task_ids": task_ids}


def is_python_eligible(trial: dict, python_tasks: Collection[str]) -> tuple[bool, str]:
    """Require the admitted model and a positively identified canonical Python task."""
    model = trial.get("model_name") or ""
    task_name = trial.get("task_name") or ""
    family = task_name.split("/")[-1]
    if not MIMO_MODEL_RE.search(model):
        return False, f"non-eligible model: {model or 'null'}"
    if not PYTHON_TASK_RE.fullmatch(family):
        return False, f"outside canonical Python task family: {task_name}"
    if family not in python_tasks:
        return False, f"Python identity unverified: {family} is absent from the canonical Python ledger"
    return True, ""


def task_id_of(trial: dict) -> str:
    return (trial.get("task_name") or "").split("/")[-1]


def _parse_json_list(text: object) -> list:
    if not text:
        return []
    try:
        value = json.loads(text) if isinstance(text, str) else text
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _parse_json_dict(text: object) -> dict:
    if not text:
        return {}
    try:
        value = json.loads(text) if isinstance(text, str) else text
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _step_number(ref: object) -> int | None:
    if ref is None:
        return None
    match = re.search(r"#(\d+)", str(ref))
    return int(match.group(1)) if match else None


def _ordered_real_steps(steps: list) -> list:
    real = [s for s in steps if s.get("step_ref") != "no_steps"]
    return sorted(real, key=lambda s: (s.get("step_id") if isinstance(s.get("step_id"), int) else 10**9))


def pick_exemplar_step(trial: dict, steps: list, judgments: dict, taint: list,
                       prefer_last: bool = False, prefer_taint: bool = False) -> tuple[dict, str]:
    """Deterministic step pick; refs must resolve to a real view step (raw check is separate)."""
    ordered = _ordered_real_steps(steps)
    fresh = [s for s in ordered if not s.get("is_copied_context")]
    fresh_by_ref = {s.get("step_ref"): s for s in fresh}
    if prefer_last and fresh:
        return fresh[-1], "highest native step_id context; not a causal or chronological assertion"
    if prefer_taint:
        for flag in taint:
            if isinstance(flag, dict) and flag.get("kind") == "upstream_fetch":
                ref = flag.get("evidence")
                if ref in fresh_by_ref:
                    return fresh_by_ref[ref], f"upstream-fetch detector evidence {ref}"
    candidates: list[tuple[dict | None, str]] = []
    loop = judgments.get("loop_kind") or {}
    if isinstance(loop, dict) and loop.get("loop_onset_step") is not None:
        matches = [s for s in fresh if s.get("step_id") == loop["loop_onset_step"]]
        if len(matches) == 1:
            candidates.append((matches[0], f"loop-onset opinion {matches[0]['step_ref']}"))
    ff = judgments.get("first_failure")
    ff_ref = (ff or {}).get("step") if isinstance(ff, dict) else None
    if ff_ref is None and trial.get("first_failure_ref"):
        ff_ref = trial["first_failure_ref"]
    if _step_number(ff_ref) is not None:
        ref = str(ff_ref)
        candidates.append((fresh_by_ref.get(ref), f"first-failure opinion {ref}"))
    for entry in taint:
        if isinstance(entry, dict) and entry.get("evidence"):
            ref = str(entry["evidence"])
            candidates.append((fresh_by_ref.get(ref), f"taint evidence {ref}"))
            break
    for step, how in candidates:
        if step is not None and (step.get("command_text") or step.get("observation_excerpt")):
            return step, how
    if fresh:
        return fresh[len(fresh) // 2], "median real non-copied step (no recorded ref resolved)"
    return ordered[len(ordered) // 2], "median real step (only copied context available)"


FROZEN_LABEL_PROVENANCE = {
    "har109": "frozen_hand",
    "har119": "agent_rater",
    "har128-sft-pass": "frozen_adjudication",
    "har128-har116": "agent_rater",
    "har128-g2-a1": "agent_rater",
    "har128-g2-r2": "agent_rater",
    "har128-g2-tail": "agent_rater",
}
FROZEN_LABEL_COHORTS = set(FROZEN_LABEL_PROVENANCE)


def summarize_labels(trial: dict) -> tuple[list, int]:
    """Retain the manifest-verified trace-query annotations without merging taxonomies.

    The query surface verifies source bytes and manifests. Require its frozen
    cohort, provenance and source identity, not merely a familiar cohort name.
    Preserve complete entries, including adjudication batch and first-failure
    fields; these inspection labels never enter the separate proposer payload.
    """
    out, dropped = [], 0
    for entry in _parse_json_list(trial.get("labels_json")):
        if not isinstance(entry, dict):
            dropped += 1
            continue
        cohort = entry.get("cohort")
        source_sha = str(entry.get("source_sha256") or "").removeprefix("sha256:")
        if (not isinstance(cohort, str) or cohort not in FROZEN_LABEL_COHORTS
                or entry.get("provenance") != FROZEN_LABEL_PROVENANCE[cohort]
                or not entry.get("source_file")
                or not re.fullmatch(r"[0-9a-f]{64}", source_sha)):
            dropped += 1
            continue
        summary = dict(entry)
        out.append(summary)
    return out, dropped


def evidence_kind(step: dict) -> str:
    if step.get("command_provenance") == "recorded" and step.get("command_text"):
        return "recorded-command"
    if step.get("command_text"):
        return "reconstructed-command (not proof of execution)"
    if step.get("observation_excerpt"):
        return "observation-only"
    return "none"


def verify_step_raw(trial: dict, step: dict) -> dict:
    """Independently verify an exemplar ref against the raw trial directory.

    Resolves source_trial_dir + source_path on disk (no view trust): the file
    must exist, its sha256 must match the view, and some step in it must carry
    the exemplar native step_id.
    """
    result = {"file_exists": False, "sha_match": False, "step_present": False, "verified": False}
    trial_dir = trial.get("source_trial_dir")
    source_path = step.get("source_path")
    if not trial_dir or not source_path or step.get("step_ref") in (None, "no_steps"):
        return result
    doc = Path(trial_dir) / source_path
    if not doc.is_file():
        return result
    result["file_exists"] = True
    stored_sha = (step.get("source_sha256") or "").removeprefix("sha256:")
    if stored_sha and hashlib.sha256(doc.read_bytes()).hexdigest() == stored_sha:
        result["sha_match"] = True
    try:
        payload = json.loads(doc.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return result
    want = step.get("step_id")
    for raw in payload.get("steps", []) if isinstance(payload, dict) else []:
        if isinstance(raw, dict) and raw.get("step_id") == want:
            result["step_present"] = True
            break
    result["verified"] = result["file_exists"] and result["sha_match"] and result["step_present"]
    return result


def build_exemplar(trial: dict, steps: list, prefer_last: bool = False,
                   prefer_taint: bool = False) -> dict:
    judgments = _parse_json_dict(trial.get("decision_judgments_json"))
    taint = _parse_json_list(trial.get("taint_json"))
    step, how = pick_exemplar_step(
        trial, steps, judgments, taint, prefer_last=prefer_last, prefer_taint=prefer_taint
    )
    labels, _ = summarize_labels(trial)
    trial_dir = Path(trial["source_trial_dir"]) if trial.get("source_trial_dir") else None
    raw_path = trial_dir / step["source_path"] if trial_dir and step.get("source_path") else None
    step_link = (
        f"{raw_path.as_uri()}#step_id={step['step_id']}"
        if raw_path and raw_path.is_absolute() else None
    )
    return {
        "trial_name": trial.get("trial_name"),
        "job_name": trial.get("job_name"),
        "task_name": trial.get("task_name"),
        "split": trial.get("split"),
        "arm": trial.get("arm"),
        "counts_verdict": trial.get("counts_verdict"),
        "raw_reward": trial.get("raw_reward"),
        "scored": trial.get("scored"),
        "stop_reason": trial.get("stop_reason"),
        "loop_kind": trial.get("loop_kind"),
        "outcome_rule": trial.get("outcome_rule"),
        "step_ref": step.get("step_ref"),
        "step_link": step_link,
        "result_link": (trial_dir / "result.json").as_uri() if trial_dir and trial_dir.is_absolute() else None,
        "source_path": step.get("source_path"),
        "source_sha256": step.get("source_sha256"),
        "source_trial_dir": trial.get("source_trial_dir"),
        "command_provenance": step.get("command_provenance"),
        "evidence_kind": evidence_kind(step),
        "step_selection": how,
        "command_text": (step.get("command_text") or "")[:EVIDENCE_QUOTE_LIMIT],
        "observation_excerpt": (step.get("observation_excerpt") or "")[:EVIDENCE_QUOTE_LIMIT],
        "frozen_labels": labels,
        "raw_verification": verify_step_raw(trial, step),
    }


def select_exemplars(matching: list, steps_by_trial: dict, limit: int = 2,
                     prefer_last: bool = False, prefer_taint: bool = False) -> list:
    """First `limit` RAW-VERIFIED exemplars (by trial_name); unverified refs are skipped, never padded."""
    exemplars = []
    for trial in sorted(matching, key=lambda t: t.get("trial_name") or ""):
        if len(exemplars) == limit:
            break
        steps = steps_by_trial.get((trial.get("job_id"), trial.get("trial_id")), [])
        if not any(not step.get("is_copied_context") for step in _ordered_real_steps(steps)):
            continue
        candidate = build_exemplar(
            trial, steps, prefer_last=prefer_last, prefer_taint=prefer_taint
        )
        if candidate["raw_verification"]["verified"]:
            exemplars.append(candidate)
    return exemplars


CATEGORY_DEFS = [
    {
        "id": "recorded-budget-stop",
        "kind": "fact",
        "predicate": "stop_reason in {TrialBudgetExhaustedError, ceiling:input_tokens, ceiling:requests}",
        "denominator": "eligible trials (stop_reason is always recorded; 'unknown' stays separate)",
        "lever_hypothesis": "Harness budget/ceiling policy or agent time-to-first-edit efficiency; causal evidence absent.",
    },
    {
        "id": "completion-claim-loop",
        "kind": "opinion",
        "predicate": "decision loop_kind.kind == 'completion-claim' (producer rule HAR-119)",
        "denominator": "eligible trials with a recorded loop-kind prediction",
        "lever_hypothesis": "Prompt/harness completion discipline (e.g. confirm-then-stop); causal evidence absent.",
    },
    {
        "id": "repetition-loop",
        "kind": "opinion",
        "predicate": "decision loop_kind.kind == 'repetition' (producer rule HAR-119)",
        "denominator": "eligible trials with a recorded loop-kind prediction",
        "lever_hypothesis": "Harness loop-break / output-cap or agent stuckness recovery; causal evidence absent.",
    },
    {
        "id": "counts-excluded-copied-pass",
        "kind": "fact",
        "predicate": "counts_verdict == 'excluded' with reason copied_fix or pass_tainted (canonical counts)",
        "denominator": "eligible trials with counts_verdict non-null",
        "lever_hypothesis": "Data integrity: keep excluded from training; upstream-fetch guard is a hypothesis.",
    },
    {
        "id": "counts-excluded-infra",
        "kind": "fact",
        "predicate": "counts_verdict == 'excluded' with reason infra (canonical counts)",
        "denominator": "eligible trials with counts_verdict non-null",
        "lever_hypothesis": "Infra reliability (harness/sandbox), not model capability; kept out of pass-rate denominator.",
    },
    {
        "id": "recorded-upstream-fetch-signal",
        "kind": "fact-signal",
        "predicate": "processed taint contains an upstream_fetch detector entry; command provenance is separate, not proof of execution or copying",
        "denominator": "eligible trials with processed_available (taint lives in processed reports)",
        "lever_hypothesis": "Fetch command != fetched solution; treat as audit signal only.",
    },
]


def match_category(cat_id: str, trial: dict) -> bool:
    if cat_id == "recorded-budget-stop":
        return (trial.get("stop_reason") or "") in BUDGET_STOPS
    if cat_id == "completion-claim-loop":
        return trial.get("loop_kind") == "completion-claim"
    if cat_id == "repetition-loop":
        return trial.get("loop_kind") == "repetition"
    if cat_id == "counts-excluded-copied-pass":
        if trial.get("counts_verdict") != "excluded":
            return False
        reasons = _parse_json_list(trial.get("counts_reasons_json"))
        return "copied_fix" in reasons or "pass_tainted" in reasons
    if cat_id == "counts-excluded-infra":
        if trial.get("counts_verdict") != "excluded":
            return False
        return "infra" in _parse_json_list(trial.get("counts_reasons_json"))
    if cat_id == "recorded-upstream-fetch-signal":
        return any(isinstance(e, dict) and e.get("kind") == "upstream_fetch"
                   for e in _parse_json_list(trial.get("taint_json")))
    raise ValueError(f"unknown category {cat_id}")


def denominator_for(cat_id: str, eligible: list) -> list:
    if cat_id in ("completion-claim-loop", "repetition-loop"):
        return [t for t in eligible if t.get("loop_kind") is not None]
    if cat_id in ("counts-excluded-copied-pass", "counts-excluded-infra"):
        return [t for t in eligible if t.get("counts_verdict") is not None]
    if cat_id == "recorded-upstream-fetch-signal":
        return [t for t in eligible if t.get("processed_available")]
    return list(eligible)


def _agree_str(section: object) -> str | None:
    if not isinstance(section, dict):
        return None
    agree, n = section.get("agree"), section.get("n")
    if isinstance(agree, int) and isinstance(n, int):
        return f"{agree}/{n}"
    return None


LOOP_CALIBRATION_STUDIES = (
    ("har128-har116", "har116", "labels_har116"),
    ("har128-g2-a1", "g2-a1", "labels_g2_a1"),
    ("har128-g2-r2", "g2-r2", "labels_g2_r2"),
    ("har128-g2-tail", "g2-tail", "labels_g2_tail"),
)


def load_loop_calibrations(repo_root: Path) -> list:
    """Read each frozen study independently; bind receipt bytes to its label freeze."""
    studies = []
    for label_cohort, suffix, label_dir in LOOP_CALIBRATION_STUDIES:
        receipt = repo_root / (
            f"research/experiments/har117-results-home/har131-page-calibration-{suffix}.json")
        manifest = repo_root / f"research/explorations/trace-lab/har128/{label_dir}/MANIFEST.sha256"
        study = {
            "label_cohort": label_cohort,
            "source_file": str(receipt.relative_to(repo_root)),
            "source_sha256": None,
            "labels_manifest_sha256": None,
            "status": "unavailable",
            "loop_kind_vs_agreed": "unavailable",
            "page_abstentions": None,
            "scope_note": "This study only; not pooled or transferred to G5. Inspection/calibration only, never training reflection.",
        }
        studies.append(study)
        if not receipt.is_file():
            study["reason"] = "calibration receipt missing"
            continue
        try:
            raw = receipt.read_bytes()
            study["source_sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
            study["labels_manifest_sha256"] = _optional_sha(manifest)
            scores = json.loads(raw)
        except (OSError, ValueError, UnicodeDecodeError):
            study["reason"] = "calibration receipt or label manifest unreadable"
            continue
        if not isinstance(scores, dict) or scores.get("schema") != "har131.page_loop_calibration/v1":
            study["reason"] = "unrecognized calibration receipt"
            continue
        study.update({key: scores.get(key) for key in (
            "cohort", "predictor", "predictor_functions", "scorer_sha256",
            "in_sample", "labels_frozen_at", "heldout", "limit",
        )})
        claimed_manifest = str(scores.get("labels_manifest_sha256") or "").removeprefix("sha256:")
        actual_manifest = str(study["labels_manifest_sha256"] or "").removeprefix("sha256:")
        study["receipt_labels_manifest_sha256"] = scores.get("labels_manifest_sha256")
        if not actual_manifest or claimed_manifest != actual_manifest:
            study["reason"] = "calibration label manifest missing or mismatched"
            continue
        metric = scores.get("page_vs_agreed")
        agreement = scores.get("rater_agreement")
        abstentions = scores.get("page_abstentions")
        if (not isinstance(metric, dict) or not isinstance(agreement, dict)
                or any(type(metric.get(key)) is not int for key in ("agree", "n"))
                or any(type(agreement.get(key)) is not int for key in ("agree", "n"))
                or type(abstentions) is not int
                or not 0 <= metric["agree"] <= metric["n"] == agreement["agree"] <= agreement["n"]
                or not 0 <= abstentions <= metric["n"]):
            study["reason"] = "calibration metric or evidence denominator unavailable"
            continue
        study.update({
            "status": "available",
            "page_vs_agreed": dict(metric),
            "loop_kind_vs_agreed": _agree_str(metric),
            "page_abstentions": abstentions,
            "rater_agreement": dict(agreement),
            "disagreements": scores.get("disagreements"),
            "loop_kind_confusion": scores.get("loop_kind_confusion"),
        })
    return studies


def derive_opinion_limits(page_scores: object, loop_calibrations: list | None = None) -> dict:
    """Keep HAR-119 page scores and separately frozen studies scoped to their own cohorts."""
    scores = page_scores if isinstance(page_scores, dict) else {}
    limits: dict = {
        "provenance": "research/explorations/trace-lab/har119/page_scores.json",
        "predictor": scores.get("predictor") or "unavailable",
        "cohort": scores.get("cohort") or "unavailable",
        "in_sample": scores.get("in_sample"),
        "frozen_at": scores.get("frozen_at") or "unavailable",
        "scope_note": "measured on the page_scores cohort only; NOT transferred to other cohorts",
    }
    loop_kind = _agree_str(scores.get("page_vs_agreed_loop_kind"))
    limits["loop_kind_vs_agreed"] = loop_kind if loop_kind else "unavailable"
    loop_present = _agree_str(scores.get("page_vs_agreed_loop_present"))
    limits["loop_present_vs_agreed"] = loop_present if loop_present else "unavailable"
    first_failure = _agree_str(scores.get("page_vs_agreed_first_failure"))
    coverage = scores.get("first_failure_coverage") if isinstance(
        scores.get("first_failure_coverage"), dict) else {}
    expressed, of_total, abstentions = (coverage.get("expressed"), coverage.get("of"),
                                        coverage.get("abstentions"))
    if (first_failure and isinstance(expressed, int) and isinstance(of_total, int)
            and isinstance(abstentions, int)):
        limits["first_failure_vs_agreed"] = (
            f"{first_failure} with {expressed}/{of_total} coverage and "
            f"{abstentions} abstentions; page values are usually absent -- do not use as step truth")
    else:
        limits["first_failure_vs_agreed"] = "unavailable"
    blame = _agree_str(scores.get("page_vs_agreed_blame"))
    blame_abstentions = scores.get("blame_abstentions")
    if blame and isinstance(blame_abstentions, int):
        limits["blame_vs_agreed"] = (
            f"{blame} with {blame_abstentions} abstentions; near-constant prior, not skill")
    else:
        limits["blame_vs_agreed"] = "unavailable"
    for key, label in (("rater_agreement_loop_kind", "rater_agreement_loop_kind"),
                       ("rater_agreement_loop_present", "rater_agreement_loop_present"),
                       ("rater_agreement_first_failure", "rater_agreement_first_failure"),
                       ("rater_agreement_blame", "rater_agreement_blame")):
        value = _agree_str(scores.get(key))
        limits[label] = value if value else "unavailable"
    eligible_n = scores.get("eligible_n")
    limits["eligible_n"] = eligible_n if isinstance(eligible_n, int) else "unavailable"
    excluded = scores.get("excluded_rater_disagreement")
    limits["excluded_rater_disagreement"] = excluded if isinstance(excluded, int) else "unavailable"
    limits["loop_calibration_studies"] = loop_calibrations or []
    limits["g5_calibration"] = {
        "status": "unavailable",
        "reason": "No frozen G5 calibration receipt supplied; historical accuracy is not G5 accuracy.",
    }
    return limits


def _load_doc_step_ids(trial_dir: object, source_path: object) -> tuple | None:
    """Raw ATIF document -> (sha256 hex, set of native step ids). None when unreadable."""
    if not trial_dir or not source_path:
        return None
    doc = Path(trial_dir) / str(source_path)
    if not doc.is_file():
        return None
    try:
        raw = doc.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    steps = payload.get("steps") if isinstance(payload, dict) else None
    if not isinstance(steps, list):
        return None
    return (hashlib.sha256(raw).hexdigest(),
            {s.get("step_id") for s in steps if isinstance(s, dict)})


def verified_step_refs(trial: dict, steps: list) -> list:
    """Independently verified step refs for one trial: no contents, copied replays excluded."""
    refs = []
    cache: dict = {}
    for step in _ordered_real_steps(steps):
        if step.get("is_copied_context"):
            continue
        key = (trial.get("source_trial_dir"), step.get("source_path"))
        if key not in cache:
            cache[key] = _load_doc_step_ids(trial.get("source_trial_dir"), step.get("source_path"))
        loaded = cache[key]
        if not loaded:
            continue
        file_sha, step_ids = loaded
        stored = (step.get("source_sha256") or "").removeprefix("sha256:")
        if not stored or stored != file_sha:
            continue
        if step.get("step_id") not in step_ids:
            continue
        refs.append({"step_ref": step.get("step_ref"), "source_path": step.get("source_path"),
                     "source_sha256": step.get("source_sha256"),
                     "evidence_kind": evidence_kind(step),
                     "command_provenance": step.get("command_provenance")})
    return refs

G5_ARMS = ("stock", "tuned", "gepa")
G5_COHORT_SHA256 = "sha256:ecfb2613ecfe0fdc941b28a07fdf67fc0ee8110bf05234c758b244330bce80d3"
G5_SPEC_MANIFEST_SHA256 = "sha256:a8551c185172e7ad5b6c54acd50b4e41a8fd4a7d558cf6992e819b22c8838029"


def load_g5_cohort(path: Path) -> dict:
    """Bind the admitted G5 cohort and all 60 sibling specs, without reading runs."""
    path = path.resolve()
    cohort_sha = _sha256_file(path)
    if cohort_sha != G5_COHORT_SHA256:
        raise ValueError("G5 cohort bytes do not match the admitted freeze")
    cohort = _load_json(path)
    if not isinstance(cohort, dict) or cohort.get("arms") != list(G5_ARMS):
        raise ValueError("G5 cohort must declare stock/tuned/gepa separately")
    cells, spec_lines = [], {}
    for task in cohort["cohort"]:
        for arm in G5_ARMS:
            name = f"ovn-g5-{task['task_id'].removeprefix('format-code-task-')}-{arm}"
            spec_path = path.parent / f"{name}.json"
            spec_sha = _sha256_file(spec_path)
            spec = _load_json(spec_path)
            instruction = cohort["gepa_candidate"] if arm == "gepa" else {}
            expected = {
                "name": name, "question_ref": "ovn-g5", "task_id": task["task_id"],
                "task_package_digest": task["package_digest"],
                "verifier_digest": task["verifier_digest"], "model": cohort["models"][arm],
                "harness_tree_path": cohort["harness_tree"]["path"],
                "harness_tree_sha256": cohort["harness_tree"]["sha256"],
                "extra_instruction_path": instruction.get("path"),
                "extra_instruction_sha256": instruction.get("sha256"),
            }
            if not isinstance(spec, dict) or any(spec.get(key) != value for key, value in expected.items()):
                raise ValueError(f"G5 spec identity differs from the cohort: {spec_path.name}")
            spec_lines[spec_path.name] = f"{spec_sha.removeprefix('sha256:')}  {spec_path.name}\n"
            cells.append({
                "task_id": task["task_id"], "arm": arm, "job_name": name,
                "task_package_digest": task["package_digest"],
                "source_spec_file": str(spec_path), "source_spec_sha256": spec_sha,
                "spec": spec,
            })
    spec_manifest_sha = "sha256:" + hashlib.sha256(
        "".join(spec_lines[name] for name in sorted(spec_lines)).encode()
    ).hexdigest()
    if spec_manifest_sha != G5_SPEC_MANIFEST_SHA256:
        raise ValueError("G5 spec bytes do not match the admitted 60-spec freeze")
    return {
        "source_file": str(path), "source_sha256": cohort_sha,
        "spec_manifest_sha256": spec_manifest_sha, "experiment": cohort["experiment"],
        "eval_list": cohort["eval_list"], "harness_tree": cohort["harness_tree"],
        "gepa_candidate": cohort["gepa_candidate"], "models": cohort["models"], "cells": cells,
    }


def _recorded_job_name(trial: dict) -> str:
    """Use the native source identity, not the results-home card/directory label."""
    source = trial.get("source_job_dir")
    name = Path(source).name if isinstance(source, str) and source else trial.get("job_name") or ""
    return re.sub(r"^HAR-\d+-", "", name)


def _g5_binding(trial: dict, cell: dict, python_tasks: Collection[str]) -> tuple[dict | None, str]:
    """Check recorded identity/spec metadata only; never infer treatment from model."""
    if trial.get("arm") != cell["arm"]:
        return None, "recorded arm missing or mismatched"
    if (task_id_of(trial) != cell["task_id"]
            or trial.get("task_package_digest") != cell["task_package_digest"]):
        return None, "recorded task/package identity missing or mismatched"
    ok, reason = is_python_eligible(trial, python_tasks)
    if not ok:
        return None, reason
    if not trial.get("source_job_dir"):
        return None, "recorded job directory unavailable"
    spec_path = Path(trial.get("published_job_dir") or trial["source_job_dir"]) / "experiment-spec.json"
    try:
        spec = _load_json(spec_path)
        spec_sha = _sha256_file(spec_path)
    except (OSError, ValueError, UnicodeDecodeError):
        return None, "recorded experiment spec unavailable"
    # The executor serializes schema defaults as well as the supplied fields.
    # Compare every frozen field, not serialized-byte equality or just model.
    if not isinstance(spec, dict) or any(
            spec.get(key) != value for key, value in cell["spec"].items()):
        return {"source_file": str(spec_path), "source_sha256": spec_sha}, "recorded experiment spec differs from frozen G5 spec"
    return {"source_file": str(spec_path), "source_sha256": spec_sha}, ""


def build_g5_comparison(trials: list, steps_by_trial: dict, python_tasks: Collection[str],
                        cohort: dict | None) -> dict:
    """Expected-cell accounting over bound G5 jobs only; no historical arm pooling."""
    if cohort is None:
        return {
            "status": "unavailable", "reason": "No --g5-cohort supplied; G5 identity not asserted.",
            "arms": [], "cells": [],
        }
    by_job: dict = {}
    for trial in trials:
        job_name = _recorded_job_name(trial)
        by_job.setdefault(job_name, []).append(trial)
    cells, rows_by_arm, rejections = [], {arm: [] for arm in G5_ARMS}, []
    for expected in cohort["cells"]:
        cell = {key: value for key, value in expected.items() if key != "spec"}
        cell.update({"status": "not_run", "counts_verdict": None, "raw_reward": None,
                     "scored": None, "recorded_trials": []})
        candidates = by_job.get(expected["job_name"], [])
        bound = []
        for trial in candidates:
            binding, reason = _g5_binding(trial, expected, python_tasks)
            identity = {"job_id": trial.get("job_id"), "trial_id": trial.get("trial_id"),
                        "trial_name": trial.get("trial_name"), "recorded_arm": trial.get("arm"),
                        "task_id": task_id_of(trial), "task_package_digest": trial.get("task_package_digest"),
                        "counts_verdict": trial.get("counts_verdict"),
                        "raw_reward": trial.get("raw_reward"), "scored": trial.get("scored"),
                        "source_job_dir": trial.get("source_job_dir"),
                        "source_trial_dir": trial.get("source_trial_dir")}
            cell["recorded_trials"].append(identity)
            if reason:
                rejections.append({**identity, "expected_job_name": expected["job_name"],
                                   "recorded_spec": binding, "reason": reason})
            else:
                bound.append((trial, binding))
        if candidates:
            cell["status"] = "binding_unavailable"
        if len(bound) > 1 or (bound and len(candidates) > 1):
            cell["status"] = "ambiguous"
        elif len(bound) == 1:
            trial, binding = bound[0]
            verdict = trial.get("counts_verdict")
            cell.update({
                "status": "recorded" if verdict in ("counted_pass", "counted_fail", "excluded") else "unknown",
                "job_id": trial.get("job_id"), "trial_id": trial.get("trial_id"),
                "trial_name": trial.get("trial_name"), "recorded_arm": trial.get("arm"),
                "model_name": trial.get("model_name"), "recorded_split": trial.get("split"),
                "source_trial_dir": trial.get("source_trial_dir"), "recorded_spec": binding,
                "counts_verdict": verdict, "counts_reasons": _parse_json_list(trial.get("counts_reasons_json")),
                "raw_reward": trial.get("raw_reward"), "scored": trial.get("scored"),
            })
            rows_by_arm[expected["arm"]].append(trial)
        cells.append(cell)
    arms = []
    for arm in G5_ARMS:
        arm_cells = [cell for cell in cells if cell["arm"] == arm]
        rows = rows_by_arm[arm]
        counts = {
            "pass": sum(cell["counts_verdict"] == "counted_pass" for cell in arm_cells),
            "fail": sum(cell["counts_verdict"] == "counted_fail" for cell in arm_cells),
            "excluded": sum(cell["counts_verdict"] == "excluded" for cell in arm_cells),
            "missing": sum(cell["status"] != "recorded" for cell in arm_cells),
            "not_run": sum(cell["status"] == "not_run" for cell in arm_cells),
            "unknown": sum(cell["status"] == "unknown" for cell in arm_cells),
            "binding_unavailable": sum(cell["status"] in {"binding_unavailable", "ambiguous"}
                                       for cell in arm_cells),
        }
        categories = []
        for category in CATEGORY_DEFS:
            denom = denominator_for(category["id"], rows)
            matching = [row for row in denom if match_category(category["id"], row)]
            exemplars = select_exemplars(
                matching, steps_by_trial,
                prefer_last=category["id"] == "recorded-budget-stop",
                prefer_taint=category["id"] in {"counts-excluded-copied-pass", "recorded-upstream-fetch-signal"},
            )
            categories.append({
                "id": category["id"], "kind": category["kind"],
                "n": len(matching) if denom else None, "denominator_n": len(denom),
                "denominator": category["denominator"],
                "missing_from_expected_n": len(arm_cells) - len(denom),
                "trial_names": sorted(row["trial_name"] for row in matching),
                "exemplars": exemplars, "exemplar_coverage_met": len(exemplars) >= 2,
            })
        arms.append({
            "arm": arm, "status": "recorded" if rows else "unavailable",
            "expected_n": len(arm_cells), "recorded_n": len(rows), "counts": counts,
            "counted_denominator_n": counts["pass"] + counts["fail"],
            "evidence": {
                "processed_n": sum(bool(row.get("processed_available")) for row in rows),
                "counts_n": counts["pass"] + counts["fail"] + counts["excluded"],
                "atif_n": sum(bool(row.get("trajectory_available")) for row in rows),
                "loop_prediction_n": sum(row.get("loop_kind") is not None for row in rows),
                "scored_n": sum(bool(row.get("scored")) for row in rows),
            },
            "categories": categories,
        })
    if all(cell["status"] == "not_run" for cell in cells):
        status = "not_run"
    elif any(cell["status"] != "recorded" for cell in cells):
        status = "partial"
    else:
        status = "recorded_snapshot"
    return {
        "status": status,
        "scope_note": "Only exact frozen G5 task/arm/job/spec bindings; historical trials are not pooled. Counts missing includes not_run, unknown and unavailable/ambiguous bindings, not counted failures.",
        "source_file": cohort["source_file"], "source_sha256": cohort["source_sha256"],
        "spec_manifest_sha256": cohort["spec_manifest_sha256"],
        "experiment": cohort["experiment"], "eval_list": cohort["eval_list"],
        "harness_tree": cohort["harness_tree"], "gepa_candidate": cohort["gepa_candidate"],
        "models": cohort["models"], "arms": arms, "cells": cells,
        "binding_rejections": rejections,
        "calibration": {
            "status": "unavailable",
            "reason": "No frozen G5 calibration receipt supplied; historical accuracy is not G5 accuracy.",
        },
    }


def build_atlas(trials: list, steps_by_trial: dict, coverage: dict, repo_root: Path,
                ledger: dict, freeze: dict, page_scores: dict, g2_bindings: object,
                eval_gate: dict | None = None, proposal: dict | None = None,
                g5_cohort: dict | None = None) -> dict:
    eligible, excluded_rows = [], []
    for trial in trials:
        ok, reason = is_python_eligible(trial, ledger["by_task"])
        (eligible if ok else excluded_rows).append(trial if ok else {**trial, "exclusion_reason": reason})
    for trial in eligible:
        entry = ledger["by_task"].get(task_id_of(trial))
        trial["_ledger_status"] = entry["status"] if entry else "absent-from-ledger"
        trial["_ledger_run"] = entry["run"] if entry else None
        trial["_ledger_split"] = entry["split"] if entry else None
        trial["_ledger_project"] = entry["project"] if entry else None
        trial["_ledger_run_digest"] = entry["run_digest"] if entry else None

    opinion_limits = derive_opinion_limits(page_scores, load_loop_calibrations(repo_root))
    g5_comparison = build_g5_comparison(trials, steps_by_trial, ledger["by_task"], g5_cohort)
    # Reserve the G5 namespace even without a binding file: those rows remain
    # visible in the corpus, never quietly enter the historical frequencies.
    historical = [t for t in eligible if not _recorded_job_name(t).startswith("ovn-g5-")]
    label_summaries, retained_cohorts = {}, set()

    heuristic_labels_dropped = 0
    har128_labeled_trials = 0
    for trial in eligible:
        kept, dropped = summarize_labels(trial)
        heuristic_labels_dropped += dropped
        label_summaries[(trial["job_id"], trial["trial_id"])] = kept
        retained_cohorts.update(entry["cohort"] for entry in kept)
        if any(e.get("cohort") == "har128-sft-pass" for e in kept):
            har128_labeled_trials += 1

    categories = []
    for cat in CATEGORY_DEFS:
        denom = denominator_for(cat["id"], historical)
        matching = [t for t in denom if match_category(cat["id"], t)]
        exemplars = select_exemplars(
            matching, steps_by_trial,
            prefer_last=(cat["id"] == "recorded-budget-stop"),
            prefer_taint=(cat["id"] in {"counts-excluded-copied-pass", "recorded-upstream-fetch-signal"}),
        )
        categories.append({
            **cat,
            "n": len(matching),
            "denominator_n": len(denom),
            "excluded_from_denominator": len(historical) - len(denom),
            "trial_names": sorted(t.get("trial_name") for t in matching),
            "exemplars": exemplars,
            "exemplar_coverage_met": len(exemplars) >= 2,
            "opinion_limits": opinion_limits if cat["kind"] in ("opinion",) else None,
        })

    rare_cases = []
    discarded = [t for t in eligible if t.get("_ledger_status") == "discarded"]
    if discarded:
        rare_cases.append({
            "id": "ledger-discarded-task",
            "note": "Canonical ledger-discarded task identities are task-health exclusions, not pooled model failures.",
            "trial_names": sorted(t.get("trial_name") for t in discarded),
            "exemplars": select_exemplars(discarded, steps_by_trial, limit=2),
        })
    unscored = [t for t in eligible if not t.get("scored")]
    rare_cases.append({
        "id": "unscored-unknown",
        "note": "Eligible trials with no verifier reward (scored=False). Unknown is NOT infra or earned; kept visible in denominators, never in pass/fail rates. Overlaps counts-excluded-infra for the infra-excluded trials by design (overlapping accounting, not a causal partition).",
        "n": len(unscored),
        "trial_names": sorted(t.get("trial_name") for t in unscored),
    })

    task_health = []
    for task_id in sorted({task_id_of(t) for t in eligible}):
        rows = [t for t in eligible if task_id_of(t) == task_id]
        entry = ledger["by_task"].get(task_id)
        task_health.append({
            "task_id": task_id,
            "n_eligible_trials": len(rows),
            "ledger_status": entry["status"] if entry else "absent-from-ledger",
            "ledger_run": entry["run"] if entry else None,
            "ledger_split": entry["split"] if entry else None,
            "ledger_project": entry["project"] if entry else None,
        })

    reflection = reflection_status(g2_bindings, eligible, steps_by_trial, ledger, eval_gate, proposal)

    try:
        repo_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root,
                                   capture_output=True, text=True).stdout.strip()
    except OSError:
        repo_head = None
    try:
        import evallab.trace_query as trace_query_mod
        trace_query_path = Path(trace_query_mod.__file__)
        trace_query_sha = _sha256_file(trace_query_path)
    except (ImportError, OSError):
        trace_query_sha = None

    input_hasher = hashlib.sha256()
    for trial in sorted(trials, key=lambda t: (t.get("job_id") or "", t.get("trial_id") or "")):
        input_hasher.update(json.dumps({
            "job_id": trial.get("job_id"), "trial_id": trial.get("trial_id"),
            "job_name": trial.get("job_name"), "arm": trial.get("arm"),
            "task_name": trial.get("task_name"), "task_package_digest": trial.get("task_package_digest"),
            "raw_reward": trial.get("raw_reward"), "scored": trial.get("scored"),
            "counts_verdict": trial.get("counts_verdict"),
            "counts_reasons_json": trial.get("counts_reasons_json"),
            "stop_reason": trial.get("stop_reason"), "labels_json": trial.get("labels_json"),
            "loop_kind": trial.get("loop_kind"), "taint_json": trial.get("taint_json"),
            "processed_available": trial.get("processed_available"),
            "model_name": trial.get("model_name"),
        }, sort_keys=True).encode())
    input_hasher.update(json.dumps({
        "g5_cohort_sha256": (g5_cohort or {}).get("source_sha256"),
        "g5_spec_manifest_sha256": (g5_cohort or {}).get("spec_manifest_sha256"),
        "g5_recorded_bindings": [
            {key: cell.get(key) for key in ("job_name", "status", "recorded_spec", "recorded_trials")}
            for cell in g5_comparison["cells"]
        ],
        "g5_binding_rejections": g5_comparison.get("binding_rejections", []),
        "loop_calibrations": [
            {key: study.get(key) for key in (
                "label_cohort", "source_sha256", "labels_manifest_sha256", "status")}
            for study in opinion_limits["loop_calibration_studies"]
        ],
    }, sort_keys=True).encode())

    return {
        "atlas": "har131-failure-atlas",
        "status": "recorded_snapshot",
        "status_reasons": [
            f"Missing processed reports: {coverage.get('missing_processed', 0)}.",
            f"Missing canonical counts: {coverage.get('missing_counts', 0)}.",
            "Coverage and calibration limits apply; this is not a causal attribution study.",
        ],
        "generated_at_utc": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "repo_head": repo_head,
        "atlas_input_digest": f"sha256:{input_hasher.hexdigest()}",
        "source_hashes": {
            "trace_query_py": trace_query_sha,
            "trace_query_note": "Transient query surface over recorded evidence; implementation bytes are hashed here.",
            "ledger_csv": ledger["sha256"],
            "page_scores_json": _optional_sha(repo_root / "research/explorations/trace-lab/har119/page_scores.json"),
            "har119_manifest": freeze.get("manifest_sha256"),
            "har109_manifest": _optional_sha(repo_root / "research/explorations/trace-lab/har109/hand_labels.sha256"),
            "har128_labels_jsonl": _optional_sha(
                repo_root / "research/explorations/trace-lab/har128/sft_gate/labels.jsonl"),
            "label_manifests": coverage.get("label_manifest_hashes", {}),
            "loop_calibration_receipts": {
                study["label_cohort"]: study["source_sha256"]
                for study in opinion_limits["loop_calibration_studies"]
            },
            "g5_cohort": (g5_cohort or {}).get("source_sha256"),
            "g5_spec_manifest": (g5_cohort or {}).get("spec_manifest_sha256"),
        },
        "provenance": {
            "har119_freeze": freeze,
            "page_scores_match_manifest": (
                bool(freeze.get("manifest_sha256"))
                and str(page_scores.get("labels_manifest_sha256") or "").removeprefix("sha256:")
                == str(freeze["manifest_sha256"]).removeprefix("sha256:")
            ),
            "heuristic_labels_dropped": heuristic_labels_dropped,
            "label_cohorts_kept": sorted(retained_cohorts),
            "label_cohorts_supported": sorted(FROZEN_LABEL_COHORTS),
            "label_verification": coverage.get("label_verification", {}),
            "har128_sft_pass": {"label_scope": "sft_pass_cleanliness",
                                "labeled_eligible_trials": har128_labeled_trials,
                                "eligible_n": len(eligible)},
            "coverage": coverage,
        },
        "corpus": {
            "eligible_predicate": "recorded MiMo-V2.6-Distill-Qwen-9B (base or admitted :har129 adapter) AND task identity present in the canonical Python ledger; a format-code-task name alone is not a language label",
            "n_discovered": len(trials),
            "n_eligible": len(eligible),
            "historical_patterns_n": len(historical),
            "historical_patterns_scope": "Non-G5 eligible rows only; reserved ovn-g5 job identities are kept out, even when G5 binding is unavailable.",
            "trials": [
                {
                    "job_id": t["job_id"], "trial_id": t["trial_id"],
                    "job_name": t["job_name"], "trial_name": t["trial_name"],
                    "task_name": t["task_name"], "task_id": task_id_of(t),
                    "task_package_digest": t.get("task_package_digest"),
                    "card": t.get("card"), "arm": t.get("arm"),
                    "recorded_split": t.get("split"),
                    "ledger_split": (ledger["by_task"].get(task_id_of(t)) or {}).get("split"),
                    "ledger_status": t.get("_ledger_status"),
                    "ledger_project": (ledger["by_task"].get(task_id_of(t)) or {}).get("project"),
                    "counts_verdict": t.get("counts_verdict"),
                    "raw_reward": t.get("raw_reward"), "scored": t.get("scored"),
                    "frozen_labels": label_summaries[(t["job_id"], t["trial_id"])],
                    "category_membership": [
                        c["id"] for c in CATEGORY_DEFS if match_category(c["id"], t)
                    ],
                    "source_trial_dir": t.get("source_trial_dir"),
                    "report_path": t.get("report_path"),
                    "input_hashes": {
                        "native_result": _optional_sha(Path(t["source_trial_dir"]) / "result.json"),
                        "processed_report": _optional_sha(Path(t["report_path"])) if t.get("report_path") else None,
                        "atif": [
                            {"source_path": path, "sha256": digest}
                            for path, digest in sorted({
                                (step["source_path"], step["source_sha256"])
                                for step in steps_by_trial.get((t["job_id"], t["trial_id"]), [])
                            })
                        ],
                    },
                }
                for t in sorted(eligible, key=lambda item: (item["job_id"], item["trial_id"]))
            ],
            "excluded_rows": [
                {"job_id": t.get("job_id"), "trial_id": t.get("trial_id"),
                 "trial_name": t.get("trial_name"), "card": t.get("card"),
                 "task_name": t.get("task_name"), "model_name": t.get("model_name"),
                 "reason": t.get("exclusion_reason")}
                for t in sorted(excluded_rows, key=lambda t: t.get("trial_name") or "")
            ],
        },
        "categories": categories,
        "g5_comparison": g5_comparison,
        "rare_cases": rare_cases,
        "task_health": task_health,
        "opinion_limits": opinion_limits,
        "reflection": reflection,
        "refresh": {
            "command": "PYTHONPATH=src python research/explorations/trace-lab/failure-atlas/build.py --repo-root . --out-dir research/explorations/trace-lab/failure-atlas"
                       + (f" --g5-cohort {shlex.quote(g5_cohort['source_file'])}" if g5_cohort else ""),
            "notes": "Re-run after recorded reports or frozen labels change; frequencies, exemplars and input hashes are recomputed. New publishes appear on the next connection.",
        },
    }


def reflection_status(g2_bindings: object, eligible: list, steps_by_trial: dict,
                      ledger: dict | None = None, eval_gate: dict | None = None,
                      proposal: dict | None = None) -> dict:
    """Training-only GEPA reflection export; fail closed without full authorization.

    Selection requires every gate at once: a whole-file-validated explicit
    (task, package_digest) binding; canonical ledger split=train, status=usable
    and an exact run_digest match against the recorded trial digest (this alone
    excludes heldout-split and review-status tasks by their own columns);
    a counted verdict; and no frozen G1 eval task/digest/repo identity.
    The proposer payload carries ONLY selected trials, selected aggregates and
    source content hashes. Withheld counts and gate provenance live in the
    atlas-side audit object, never in the payload file.
    """
    note = ("Full atlas is inspection-only. Safe reflection covers ONLY explicitly authorized "
            "G2 training task+package bindings; it must never carry held-out examples, "
            "solutions, hidden-test contents or unselected refs to the proposer.")
    if not g2_bindings:
        return {"status": "unavailable", "scope": "training-only", "payload": None,
                "payload_file": None, "payload_sha256": None, "audit": None,
                "reason": "no authorized G2 training bindings supplied (--g2-bindings absent); "
                          "reflection unavailable.", "note": note}
    allowed, bindings_error = validate_g2_bindings(g2_bindings)
    if bindings_error:
        return {"status": "unavailable", "scope": "training-only", "payload": None,
                "payload_file": None, "payload_sha256": None, "audit": None,
                "reason": f"G2 bindings rejected: {bindings_error}; fail closed.",
                "note": note}
    if not isinstance(ledger, dict) or not isinstance(eval_gate, dict):
        return {"status": "unavailable", "scope": "training-only", "payload": None,
                "payload_file": None, "payload_sha256": None, "audit": None,
                "reason": "canonical ledger or frozen eval gate unavailable; fail closed "
                          "without the full authorization context.", "note": note}
    by_task = ledger.get("by_task") or {}
    eval_task_ids = eval_gate.get("task_ids") or set()
    eval_repos = eval_gate.get("repos") or set()
    eval_digests = eval_gate.get("digests") or set()
    proposal_ids = proposal.get("task_ids") if isinstance(proposal, dict) else None
    audit = {
        "bindings_well_formed": len(allowed),
        "ledger": {"path": ledger.get("path"), "sha256": ledger.get("sha256"),
                   "n_tasks": ledger.get("n_tasks")},
        "eval_list": {"path": eval_gate.get("path"), "sha256": eval_gate.get("sha256"),
                      "n_tasks": eval_gate.get("n_tasks"), "columns": eval_gate.get("columns")},
        "proposal": ({"path": proposal.get("path"), "sha256": proposal.get("sha256"),
                      "n_tasks": proposal.get("n_tasks")}
                     if isinstance(proposal, dict) else None),
        "policy": ("ledger split=train/status=usable/exact run_digest + counted verdicts + "
                   "eval task/digest/repo exclusion; heldout-split and review-status tasks "
                   "are excluded by those canonical columns, no literal ban list"),
    }
    withheld = {"not_authorized": 0, "eval_identity": 0,
                "ledger_mismatch": 0, "non_counted": 0}
    selected = []
    for trial in sorted(eligible, key=lambda t: t.get("trial_name") or ""):
        task_id = task_id_of(trial)
        trial_digest = trial.get("task_package_digest")
        if (task_id, trial_digest) not in allowed:
            withheld["not_authorized"] += 1
            continue
        entry = by_task.get(task_id) or {}
        project = entry.get("project")
        if (task_id in eval_task_ids or project in eval_repos
                or (trial_digest and trial_digest in eval_digests)):
            withheld["eval_identity"] += 1
            continue
        if not (entry.get("split") == "train" and entry.get("status") == "usable"
                and entry.get("run_digest") and entry.get("run_digest") == trial_digest):
            withheld["ledger_mismatch"] += 1
            continue
        if trial.get("counts_verdict") not in COUNTED_VERDICTS:
            withheld["non_counted"] += 1
            continue
        judgments = _parse_json_dict(trial.get("decision_judgments_json"))
        loop = judgments.get("loop_kind") if isinstance(judgments, dict) else None
        loop_opinion = None
        if isinstance(loop, dict) and loop.get("kind"):
            loop_opinion = {"kind": loop.get("kind"), "producer": "trial_decision.classify_loop_kind",
                            "scope": "training-selected trial opinion; calibration withheld"}
        steps = steps_by_trial.get((trial.get("job_id"), trial.get("trial_id")), [])
        selected.append({
            "task_id": task_id, "trial_id": trial.get("trial_id"),
            "trial_name": trial.get("trial_name"), "job_id": trial.get("job_id"),
            "job_name": trial.get("job_name"),
            "ledger": {"split": entry.get("split"), "status": entry.get("status"),
                       "run": entry.get("run"), "run_digest": entry.get("run_digest"),
                       "project": project},
            "in_approved_proposal": (task_id in proposal_ids) if proposal_ids is not None else None,
            "task_package_digest": trial_digest,
            "counts_verdict": trial.get("counts_verdict"),
            "raw_reward": trial.get("raw_reward"), "scored": trial.get("scored"),
            "stop_reason": trial.get("stop_reason"),
            "loop_kind_opinion": loop_opinion,
            "category_membership": [cat["id"] for cat in CATEGORY_DEFS
                                    if match_category(cat["id"], trial)],
            "report_path": trial.get("report_path"),
            "source_trial_dir": trial.get("source_trial_dir"),
            "verified_step_refs": verified_step_refs(trial, steps),
        })
    audit["withheld_counts"] = withheld
    if not selected:
        return {"status": "unavailable", "scope": "training-only", "payload": None,
                "payload_file": None, "payload_sha256": None, "audit": audit,
                "reason": "bindings supplied but zero trials satisfy every gate; "
                          "fail closed with no payload (never an available-empty export).",
                "note": note}
    by_verdict: dict = {}
    by_category: dict = {}
    by_stop: dict = {}
    for item in selected:
        by_verdict[item["counts_verdict"]] = by_verdict.get(item["counts_verdict"], 0) + 1
        by_stop[item["stop_reason"]] = by_stop.get(item["stop_reason"], 0) + 1
        for cat_id in item["category_membership"]:
            by_category[cat_id] = by_category.get(cat_id, 0) + 1
    payload = {
        "scope": "training-only",
        "selected_trials": selected,
        "aggregates": {"scope": "training-selected trials only; not evaluation feedback",
                       "n_selected": len(selected), "by_counts_verdict": by_verdict,
                       "by_category": by_category, "by_stop_reason": by_stop},
    }
    return {"status": "available", "scope": "training-only",
            "reason": f"{len(selected)} training trial(s) satisfy every gate.",
            "payload": payload, "payload_file": None, "payload_sha256": None,
            "audit": audit, "note": note}


def render_readme(atlas: dict) -> str:
    lines = [
        "# HAR-131 failure atlas (inspection-only)",
        "",
        f"Generated {atlas['generated_at_utc']} from current trace-query rows. "
        "Every number below is computed from recorded inputs; unknown stays unknown "
        "and `counts_verdict` is the sole counted authority. Overlapping patterns "
        "are not an exhaustive causal partition.",
        "",
        "## Corpus",
        "",
        f"- Discovered trials: {atlas['corpus']['n_discovered']}; "
        f"Python-eligible: {atlas['corpus']['n_eligible']} "
        f"({atlas['corpus']['eligible_predicate']})",
        f"- Excluded (reported, not dropped): {len(atlas['corpus']['excluded_rows'])} "
        "(model or canonical Python identity predicate failed; unknown identity is not asserted non-Python)",
        f"- Coverage: missing_processed={atlas['provenance']['coverage'].get('missing_processed')}, "
        f"missing_counts={atlas['provenance']['coverage'].get('missing_counts')}, "
        f"missing_atif={atlas['provenance']['coverage'].get('missing_atif')}",
        "- Missing evidence is retained explicitly. Rebuild after report or label refreshes; "
        "do not interpret missing counts or labels as clean outcomes.",
        f"- Frozen labels independently re-verified ({atlas['provenance']['har119_freeze'].get('checked')} files "
        f"ok={atlas['provenance']['har119_freeze'].get('files_ok')}); heuristic label rows dropped: "
        f"{atlas['provenance'].get('heuristic_labels_dropped')}.",
        "- Step links open retained local ATIF files and name the raw step_id. Verification "
        "checks file bytes and reference existence, not causal responsibility. Context-only "
        "anchors and page-opinion anchors remain explicitly labelled.",
        "",
    ]
    g5 = atlas["g5_comparison"]
    lines += ["## Recorded G5 stock / tuned / GEPA (separate frozen cohort)", ""]
    if g5["status"] == "unavailable":
        lines += [f"- Unavailable: {g5['reason']}", ""]
    else:
        lines += [
            f"- Cohort: `{g5['source_file']}` (`{g5['source_sha256']}`).",
            f"- Frozen 60-spec manifest: `{g5['spec_manifest_sha256']}`.",
            f"- Observed state: `{g5['status']}`. Submission or rejection bookkeeping is not a native outcome.",
            f"- {g5['scope_note']}",
            f"- GEPA: stock weights plus `{g5['gepa_candidate']['sha256']}`; arm identity comes from recorded metadata and exact spec binding, not the base-model name.",
            f"- Calibration: {g5['calibration']['status']}; {g5['calibration']['reason']}",
            "",
            "| Arm | Expected | Recorded/bound | Pass | Fail | Excluded | Missing | Not run | Unknown | Binding unavailable | Counted denominator |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for arm in g5["arms"]:
            counts = arm["counts"]
            lines.append(
                f"| {arm['arm']} | {arm['expected_n']} | {arm['recorded_n']} | "
                f"{counts['pass']} | {counts['fail']} | {counts['excluded']} | "
                f"{counts['missing']} | {counts['not_run']} | {counts['unknown']} | "
                f"{counts['binding_unavailable']} | {arm['counted_denominator_n']} |")
        lines.append("")
        for arm in g5["arms"]:
            evidence = arm["evidence"]
            lines.append(
                f"\n- {arm['arm']} evidence / {arm['expected_n']} expected: "
                f"processed={evidence['processed_n']}, counts={evidence['counts_n']}, "
                f"ATIF={evidence['atif_n']}, loop predictions={evidence['loop_prediction_n']}, "
                f"scored={evidence['scored_n']}.")
        lines += ["", "### G5 existing-pattern frequencies (available evidence denominators)", ""]
        for arm in g5["arms"]:
            for category in arm["categories"]:
                frequency = (f"{category['n']}/{category['denominator_n']}"
                             if category["n"] is not None else "unavailable (no eligible evidence)")
                lines.append(
                    f"- {arm['arm']} / {category['id']}: {frequency}; "
                    f"{category['missing_from_expected_n']} expected cells outside evidence denominator "
                    f"({category['denominator']}).")
                for ex in category["exemplars"]:
                    lines.append(
                        f"  - [`{ex['trial_name']}` / {ex['step_ref']}](<{ex.get('step_link') or ''}>), "
                        f"{ex['evidence_kind']}; raw-reference-verified={ex['raw_verification']['verified']}.")
        lines += ["", f"- Binding rejections: {len(g5['binding_rejections'])}; identities/reasons and every missing cell are retained in atlas.json.", ""]
    lines += [
        "## Frozen loop-calibration limits (studies remain separate)",
        "",
        f"- Historical HAR-119 page_scores cohort only: {atlas['opinion_limits']['loop_kind_vs_agreed']}; not transferred to G5.",
        "- These are frozen agent-rater calibration measurements, not human ground truth or G5 accuracy.",
    ]
    for study in atlas["opinion_limits"]["loop_calibration_studies"]:
        measurement = (
            f"{study['loop_kind_vs_agreed']} vs agreed raters; {study['page_abstentions']} abstentions"
            if study["status"] == "available" else f"unavailable: {study['reason']}")
        lines.append(
            f"- {study['label_cohort']}: {measurement}. "
            f"Receipt `{study['source_file']}` / `{study['source_sha256']}`; "
            f"label freeze `{study['labels_manifest_sha256']}`. {study['scope_note']}")
    lines += [
        "",
        "## Historical non-G5 patterns (frequency | eligible evidence denominator)",
        "",
        f"- Scope: {atlas['corpus']['historical_patterns_n']} eligible rows. {atlas['corpus']['historical_patterns_scope']}",
        "",
    ]
    for cat in atlas["categories"]:
        flag = "two verified exemplars" if cat["exemplar_coverage_met"] else "insufficient step-linked exemplars"
        lines.append(f"### {cat['id']} [{cat['kind']}; {flag}]")
        lines.append(f"- {cat['n']}/{cat['denominator_n']} ({cat['denominator']})")
        lines.append(f"- Predicate: `{cat['predicate']}`")
        if cat.get("opinion_limits"):
            limits = cat["opinion_limits"]
            lines.append(f"- Opinion limits (page_scores cohort only): loop "
                         f"{limits.get('loop_kind_vs_agreed')} vs rater-agreed; first_failure "
                         f"{limits.get('first_failure_vs_agreed')}; blame {limits.get('blame_vs_agreed')}.")
        for ex in cat["exemplars"]:
            verified = ex.get("raw_verification", {}).get("verified")
            lines.append(
                f"- [`{ex['trial_name']}` / {ex['step_ref']}](<{ex.get('step_link') or ''}>) "
                f"({ex.get('evidence_kind')}, reference-verified={verified}). "
                f"Selection: {ex.get('step_selection')}. "
                f"[Native result](<{ex.get('result_link') or ''}>)."
            )
            excerpt = (ex["command_text"] or ex["observation_excerpt"] or "")[:120]
            if excerpt:
                fence = "`" * max(3, 1 + max((len(s) for s in re.findall(r"`+", excerpt)), default=0))
                lines.extend([f"{fence}text", excerpt, fence])
            else:
                lines.append("  Setup-context anchor only; no recorded command or observation at this step.")
        lines.append(f"- Lever (hypothesis): {cat['lever_hypothesis']}")
        lines.append("")
    lines += [
        "## Rare cases",
        "",
    ]
    for rare in atlas["rare_cases"]:
        names = ", ".join(f"`{n}`" for n in rare.get("trial_names", [])[:8])
        lines.append(f"- {rare['id']}: {rare['note']} {names}")
    lines += [
        "",
        "## Task health (ledger)",
        "",
        "Ledger-discarded/review tasks are authoritative task signals, not model failures. "
        "Full per-task table in atlas.json.",
        "",
        "## GEPA reflection firewall",
        "",
        f"- Status: {atlas['reflection']['status']} -- {atlas['reflection'].get('reason')}",
        f"- Payload file: {atlas['reflection'].get('payload_file') or 'none (unavailable)'}",
        "",
        "## Refresh",
        "",
        f"`{atlas['refresh']['command']}`",
    ]
    return "\n".join(lines) + "\n"


def parse_args(argv: list | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the source-grounded HAR-131 failure atlas.")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--results-home", type=Path, default=None)
    parser.add_argument("--derived-root", type=Path, default=None)
    parser.add_argument("--job-dir", type=Path, action="append", default=None, dest="job_dirs")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--g2-bindings", type=Path, default=None)
    parser.add_argument("--g5-cohort", type=Path, default=None,
                        help="admitted G5 cohort.json with frozen sibling specs; expected missing cells stay not_run")
    parser.add_argument("--eval-tasks", type=Path, default=None,
                        help="frozen G1 eval list CSV (default research/experiments/ovn-sft-v0/eval_tasks.csv)")
    parser.add_argument("--training-proposal", type=Path, default=None,
                        help="approved HAR120 training proposal CSV (provenance only)")
    return parser.parse_args(argv)


def sync_reflection_payload(out_dir: Path, reflection: dict) -> tuple:
    """Write or remove the generated safe-export artifact; never leave it stale.

    Returns (payload_file, payload_sha256, fresh_written). A stale
    reflection-training.json is removed whenever no fresh payload is written,
    so an unavailable rerun can never sit beside a prior training export.
    Raises PayloadSyncError only when removal itself is impossible.
    """
    root = out_dir.resolve()
    path = root / PAYLOAD_FILENAME
    if path.resolve().parent != root:
        raise PayloadSyncError("payload path escapes the output directory")
    if reflection.get("status") == "available" and reflection.get("payload") is not None:
        try:
            path.write_text(json.dumps(reflection["payload"], indent=1) + "\n", encoding="utf-8")
        except OSError as exc:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc2:
                raise PayloadSyncError(
                    f"payload write failed and stale file not removable: {exc2}") from exc
            return None, None, False
        return path.name, _sha256_file(path), True
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise PayloadSyncError(f"stale payload not removable: {exc}") from exc
    return None, None, False


def main(argv: list | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo_root.resolve()
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        from evallab.trace_query import connect_trace_query
    except ImportError as exc:
        print(f"ATLAS ABORT: cannot import evallab.trace_query: {exc}", file=sys.stderr)
        return 2

    g2_bindings = None
    if args.g2_bindings is not None:
        if not args.g2_bindings.is_file():
            print(f"ATLAS ABORT: --g2-bindings {args.g2_bindings} not found (fail closed).", file=sys.stderr)
            return 2
        g2_bindings = _load_json(args.g2_bindings)
        _, bindings_error = validate_g2_bindings(g2_bindings)
        if bindings_error:
            print(f"ATLAS ABORT: {bindings_error}", file=sys.stderr)
            return 2

    g5_cohort = None
    if args.g5_cohort is not None:
        try:
            g5_cohort = load_g5_cohort(args.g5_cohort)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"ATLAS ABORT: --g5-cohort binding failed: {exc}", file=sys.stderr)
            return 2

    try:
        conn, coverage = connect_trace_query(
            repo_root=repo_root,
            results_home=args.results_home,
            derived_root=args.derived_root,
            job_dirs=args.job_dirs,
        )
    except Exception as exc:
        print(f"ATLAS ABORT: connect_trace_query failed: {exc}", file=sys.stderr)
        return 2

    def _rows(sql: str) -> list:
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    try:
        views = {r[0] for r in conn.execute(
            "SELECT view_name FROM duckdb_views()").fetchall()}
    except Exception:
        views = set()
    missing = {"v_trace_trials", "v_trace_steps"} - views
    if missing:
        conn.close()
        print(f"ATLAS ABORT: query surface missing views {sorted(missing)}; refusing silent partial build.",
              file=sys.stderr)
        return 2

    try:
        trials = _rows("SELECT * FROM v_trace_trials")
        steps_by_trial: dict = {}
        for row in _rows("SELECT * FROM v_trace_steps"):
            steps_by_trial.setdefault((row["job_id"], row["trial_id"]), []).append(row)
    finally:
        conn.close()

    if not trials:
        print("ATLAS ABORT: v_trace_trials returned zero rows; refusing to emit a fallback atlas.", file=sys.stderr)
        return 2

    ledger = load_ledger(repo_root)
    freeze = verify_har119_freeze(repo_root)
    page_scores = _load_json(repo_root / "research/explorations/trace-lab/har119/page_scores.json")
    assert isinstance(page_scores, dict)
    eval_gate = load_eval_gate(repo_root, args.eval_tasks)
    proposal = load_training_proposal(repo_root, args.training_proposal)

    atlas = build_atlas(trials, steps_by_trial, coverage, repo_root, ledger, freeze, page_scores,
                        g2_bindings, eval_gate=eval_gate, proposal=proposal, g5_cohort=g5_cohort)
    reflection = atlas["reflection"]
    try:
        payload_file, payload_sha256, fresh = sync_reflection_payload(out_dir, reflection)
    except PayloadSyncError as exc:
        print(f"ATLAS ABORT: {exc}", file=sys.stderr)
        return 2
    if fresh:
        reflection["payload_file"] = payload_file
        reflection["payload_sha256"] = payload_sha256
    elif reflection.get("status") == "available":
        atlas["reflection"] = {"status": "unavailable", "scope": "training-only",
                               "payload": None, "payload_file": None, "payload_sha256": None,
                               "audit": reflection.get("audit"),
                               "reason": "safe payload export failed; fail closed with no payload file.",
                               "note": reflection.get("note")}
    (out_dir / "atlas.json").write_text(json.dumps(atlas, indent=1) + "\n", encoding="utf-8")
    (out_dir / "README.md").write_text(render_readme(atlas), encoding="utf-8")

    print(f"trials={len(trials)} eligible={atlas['corpus']['n_eligible']} "
          f"excluded={len(atlas['corpus']['excluded_rows'])}")
    for cat in atlas["categories"]:
        print(f"{cat['id']}: {cat['n']}/{cat['denominator_n']} exemplars={len(cat['exemplars'])} "
              f"exemplar_coverage_met={cat['exemplar_coverage_met']}")
    print(f"g5={atlas['g5_comparison']['status']}")
    for arm in atlas["g5_comparison"]["arms"]:
        print(f"g5/{arm['arm']}: expected={arm['expected_n']} recorded={arm['recorded_n']} "
              f"counts={json.dumps(arm['counts'], sort_keys=True)}")
    print(f"reflection={atlas['reflection']['status']} "
          f"payload_file={atlas['reflection'].get('payload_file')}")
    print(f"wrote {out_dir / 'atlas.json'} and {out_dir / 'README.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
